"""
BLE provisioning (Blynk.Inject protocol) - runs at startup when there's no
stored auth token yet, instead of the normal MQTT-based operation.

Implements info/set/connect/reset/reboot plus a minimal ifs (interface
list) response. The doc suggests ifs/scan can be skipped for a device
that already has network connectivity some other way, but the real app
sends "ifs" unconditionally and won't proceed past reading device details
without a response to it - confirmed via btmon against real hardware.
"scan" (WiFi network scan) is still skipped: the reported interface is
never "wifi", so the app has no reason to ask for one.

Note: bluez-peripheral's PyPI release (0.1.7) predates the API shown on
its own docs site (which tracks an unreleased rewrite on GitHub master) -
imports and a few call signatures below are deliberately the older,
actually-installed shapes, confirmed against the installed package itself
rather than the docs site.
"""

import asyncio
import json
import logging
import socket
import zlib

from bluez_peripheral.gatt.service import Service
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as CharFlags
from bluez_peripheral.util import Adapter, get_message_bus
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.agent import NoIoAgent
from dbus_next.service import dbus_property
from dbus_next.constants import PropertyAccess

logger = logging.getLogger(__name__)

ADAPTER_PATH = "/org/bluez/hci0"


async def _get_adapter(bus, path: str = ADAPTER_PATH) -> Adapter:
    """bluez-peripheral 0.1.7's Adapter.get_first()/get_all() walk every
    child node under /org/bluez and assume each one implements
    org.bluez.Adapter1 - that's not actually guaranteed (BlueZ can expose
    other, non-adapter objects there too - confirmed on real hardware,
    where a stray "/org/bluez/test" node made get_all() crash before it
    ever reached the real adapter). Targeting the known adapter path
    directly sidesteps that broken enumeration entirely."""
    introspection = await bus.introspect("org.bluez", path)
    proxy = bus.get_proxy_object("org.bluez", path, introspection)
    return Adapter(proxy)


SERVICE_UUID = "95e30001-5737-45a9-a092-a88e2e5dd659"
RX_UUID = "95e30002-5737-45a9-a092-a88e2e5dd659"
TX_UUID = "95e30003-5737-45a9-a092-a88e2e5dd659"

# Fields the Blynk app's "set" message may send - anything outside this
# set triggers set_fail, matching the reference implementation's behaviour
# (a deliberate check to catch app/device version mismatches). We only
# actually use "blynk" and "host"; the rest (WiFi-specific) are accepted
# and ignored since this device doesn't provision its network over BLE.
KNOWN_SET_FIELDS = {"if", "ssid", "pass", "blynk", "host", "ip", "mask", "gw", "dns", "dns2", "save"}

# Excludes visually-confusable characters (0/O, 1/I/L) per the doc's naming guidance.
NAME_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

INACTIVITY_TIMEOUT = 300  # give up if no "connect" arrives within 5 minutes


def _device_serial() -> str:
    # /proc/cpuinfo reflects the real host kernel regardless of container
    # PID namespace, so this is stable across container recreates - unlike
    # the container's own hostname/machine-id, which change every time.
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return socket.gethostname()


def _short_suffix(unique_id: str, length: int = 4) -> str:
    value = zlib.crc32(unique_id.encode())
    chars = []
    for _ in range(length):
        value, rem = divmod(value, len(NAME_ALPHABET))
        chars.append(NAME_ALPHABET[rem])
    return "".join(chars)


def device_name() -> str:
    return f"Blynk Device-{_short_suffix(_device_serial())}"


class FastAdvertisement(Advertisement):
    """bluez-peripheral 0.1.7's Advertisement doesn't expose LEAdvertisement1's
    optional MinInterval/MaxInterval properties, so BlueZ falls back to its
    own default (~1.28s, confirmed via btmon) - slow enough to make the
    device noticeably sluggish to show up in a phone's scan results.
    Provisioning only ever runs for a short window with a user actively
    watching, so there's no power-saving reason to hold back here."""

    ADVERTISING_INTERVAL_MS = 100

    @dbus_property(PropertyAccess.READ)
    def MinInterval(self) -> "u":  # type: ignore
        return self.ADVERTISING_INTERVAL_MS

    @dbus_property(PropertyAccess.READ)
    def MaxInterval(self) -> "u":  # type: ignore
        return self.ADVERTISING_INTERVAL_MS

    _tx_power = 0  # class-level default; overwritten per-instance by BlueZ's own setter call below

    @dbus_property(PropertyAccess.READWRITE)
    def TxPower(self) -> "n":  # type: ignore
        # Also missing from bluez-peripheral 0.1.7's Advertisement - a
        # newer BlueZ (confirmed on a CompuLab IOT-GATE-iMX8 after an OS
        # update) queries this during registration and logs a "does not
        # have property" DBusError when absent, then writes to it
        # (presumably reporting the negotiated/actual transmit power back)
        # so it needs to be genuinely settable, not just readable.
        return self._tx_power

    @TxPower.setter
    def TxPower(self, value: "n") -> None:  # type: ignore
        self._tx_power = value


class ProvisioningService(Service):
    def __init__(self, on_rx):
        self._on_rx = on_rx
        super().__init__(SERVICE_UUID, True)

    @characteristic(RX_UUID, CharFlags.WRITE | CharFlags.WRITE_WITHOUT_RESPONSE)
    def rx(self, options):
        pass

    @rx.setter
    def rx(self, value, options):
        self._on_rx(bytes(value))

    # NOTIFY-only - the getter is never meaningfully called, but the
    # characteristic object needs one to exist at all (see the library's
    # own heart-rate example, which does the same for a notify-only char).
    @characteristic(TX_UUID, CharFlags.NOTIFY)
    def tx(self, options):
        pass

    def send(self, message: dict) -> None:
        logger.debug(f"BLE TX: {message}")
        self.tx.changed(json.dumps(message).encode("utf-8"))


class ProvisioningSession:
    """Runs the state machine for one provisioning attempt. Blocking (via
    asyncio.run in provision()) since this only ever happens before normal
    MQTT operation starts, never concurrently with it."""

    def __init__(self, config, compose_manager, bridge):
        self.config = config
        self.compose_manager = compose_manager
        self.bridge = bridge
        self.service = ProvisioningService(self._on_rx)
        self._pending = {}
        self._done = asyncio.Event()
        self._result = False

    def _on_rx(self, raw: bytes) -> None:
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (ValueError, UnicodeDecodeError):
            self.service.send({"t": "error", "msg": "wrong format"})
            return

        logger.debug(f"BLE RX: {data}")
        handler = {
            "info": self._handle_info,
            "ifs": self._handle_ifs,
            "set": self._handle_set,
            "connect": self._handle_connect,
            "reset": self._handle_reset,
            "reboot": self._handle_reboot,
        }.get(data.get("t"))

        if handler is None:
            self.service.send({"t": "error", "msg": "invalid command"})
            return
        handler(data)

    def _handle_info(self, data) -> None:
        self.service.send({
            "t": "info",
            "vendor": "Blynk",
            "tmpl_id": self.config.template_id,
            "fw_type": "0",
            "fw_ver": self.compose_manager.get_version() or "unknown",
            "name": device_name(),
        })

    def _handle_ifs(self, data) -> None:
        # This container sits on its own bridge network, not the host's -
        # it has no visibility into the Pi's real wlan0/eth0, so reporting
        # a MAC/IP here would mean guessing rather than reading real state.
        # Deliberately "eth", not "wifi", even on boards with no Ethernet
        # port: confirmed on real hardware that reporting "wifi" makes the
        # app prompt for an SSID/password we'd just discard anyway, while
        # any non-wifi type is shown as already connected and skips that
        # prompt. The label is cosmetic either way - we aren't offering to
        # change the interface over BLE - so avoiding the pointless prompt
        # wins over medium accuracy.
        self.service.send({"t": "ifs_start"})
        self.service.send({"t": "if", "name": "eth", "status": "ready"})
        self.service.send({"t": "ifs_end"})

    def _handle_set(self, data) -> None:
        unknown = set(data.keys()) - {"t"} - KNOWN_SET_FIELDS
        if unknown:
            self.service.send({"t": "set_fail"})
            return
        if "blynk" in data:
            self._pending["blynk"] = data["blynk"]
        if "host" in data:
            self._pending["host"] = data["host"]
        self.service.send({"t": "set_ok"})

    def _handle_connect(self, data) -> None:
        token = self._pending.get("blynk", "")
        if len(token) != 32:
            self.service.send({"t": "connect_fail", "msg": "configuration invalid"})
            return

        self.service.send({"t": "connecting"})
        asyncio.ensure_future(self._do_connect(token, self._pending.get("host")))

    async def _do_connect(self, token: str, host) -> None:
        self.service.send({"t": "status", "s": "connecting_cloud"})

        self.config.auth_token = token
        if host:
            self.config.server = host
        self.config.save()
        # ensure_current() shells out to `docker compose restart mosquitto`
        # and blocks for however long that takes (confirmed on real
        # hardware: ~12s) - running it inline would freeze this whole
        # asyncio loop, so no BLE notification (including the ones just
        # below) would go out until it finished.
        await asyncio.get_event_loop().run_in_executor(None, self.bridge.ensure_current)

        self.service.send({"t": "status", "s": "connected"})
        await asyncio.sleep(0.3)  # let the notification flush before the link drops
        self._result = True
        self._done.set()

    def _handle_reset(self, data) -> None:
        self.config.auth_token = None
        self.config.save()
        self.service.send({"t": "reset_ok"})

    def _handle_reboot(self, data) -> None:
        # No response expected - the BLE link just drops. See agent.py's
        # own reboot handler for why /proc/sysrq-trigger is what actually
        # reboots the host rather than just this container.
        try:
            with open("/proc/sysrq-trigger", "w") as f:
                f.write("b")
        except Exception as e:
            logger.error(f"Failed to trigger reboot: {e}")

    async def run(self) -> bool:
        bus = await get_message_bus()
        try:
            adapter = await _get_adapter(bus)
            await adapter.set_powered(True)

            await self.service.register(bus, adapter=adapter)

            agent = NoIoAgent()
            await agent.register(bus)

            name = device_name()
            # A 128-bit custom service UUID plus this name doesn't fit in
            # the primary 31-byte advertising packet, but BlueZ packs what
            # it can into the primary packet and automatically overflows
            # the rest into the scan response (its own separate 31-byte
            # budget) - confirmed via btmon against real hardware that the
            # primary packet alone (UUID+appearance+flags) only uses 25 of
            # 31 bytes, so there's no need to sacrifice the name here.
            advert = FastAdvertisement(name, [SERVICE_UUID], appearance=0, timeout=0)
            await advert.register(bus, adapter=adapter)

            logger.info(f"BLE provisioning: advertising as '{name}'")
            try:
                await asyncio.wait_for(self._done.wait(), timeout=INACTIVITY_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error("BLE provisioning timed out waiting for the app")

            try:
                await self.service.unregister()
                await agent.unregister(bus)
                # Advertisement has no public unregister in this library
                # version - disconnecting the bus makes bluez notice the
                # peer is gone and clean up the LEAdvertisement1
                # registration itself.
            except Exception as e:
                # Confirmed on real hardware: bluetoothd can drop off the
                # D-Bus system bus right around here (device/BlueZ-version
                # specific) and this raises even though provisioning itself
                # already succeeded - self._result already reflects the
                # real outcome, so a cleanup hiccup shouldn't overturn it.
                logger.warning(f"BLE cleanup after provisioning: {e}")
        finally:
            bus.disconnect()

        return self._result


def provision(config, compose_manager, bridge) -> bool:
    """Blocks until provisioning succeeds, is abandoned, or times out.
    Returns True only if a valid token was received and the bridge was
    brought up with it."""
    try:
        return asyncio.run(ProvisioningSession(config, compose_manager, bridge).run())
    except Exception as e:
        logger.error(f"BLE provisioning failed: {e}")
        return False
