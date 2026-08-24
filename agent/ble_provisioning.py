"""
BLE provisioning (Blynk.Inject protocol) - runs at startup when there's no
stored auth token yet, instead of the normal MQTT-based operation. Also
re-entered in place, without exiting the process, by BlynkAgent's
connectivity watchdog after sustained connectivity loss on an
already-configured device (see the `reprovisioning` flag below).

Implements info/ifs/scan/set/connect/reset/reboot. Real WiFi devices are
reported (and can actually be scanned/connected) via NetworkManager's
D-Bus API - not yet verified against real hardware, since every prior
network interface reported here was a fake "eth" purely to skip the app's
WiFi picker (the doc suggests ifs/scan can be skipped for a device with
connectivity some other way, but the real app sends "ifs" unconditionally
and won't proceed past reading device details without a response to it -
confirmed via btmon against real hardware).

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
import struct
import zlib

from bluez_peripheral.gatt.service import Service
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as CharFlags
from bluez_peripheral.util import Adapter, get_message_bus
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.agent import NoIoAgent
from dbus_next import Variant
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
# (a deliberate check to catch app/device version mismatches).
KNOWN_SET_FIELDS = {"if", "ssid", "pass", "blynk", "host", "ip", "mask", "gw", "dns", "dns2", "save"}

# The host's NetworkManager, reached over the same D-Bus system socket
# already bind-mounted for BlueZ (see docker-compose.yml) - no separate
# mount needed. Real WiFi scan/connect goes through this rather than any
# in-container network stack, for the same reason BlueZ is targeted at the
# host adapter directly: this container has no visibility into the host's
# real wlan0/eth0 otherwise.
NM_BUS_NAME = "org.freedesktop.NetworkManager"
NM_ROOT_PATH = "/org/freedesktop/NetworkManager"
NM_IFACE = "org.freedesktop.NetworkManager"
NM_DEVICE_IFACE = "org.freedesktop.NetworkManager.Device"
NM_WIRELESS_IFACE = "org.freedesktop.NetworkManager.Device.Wireless"
NM_AP_IFACE = "org.freedesktop.NetworkManager.AccessPoint"
NM_SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
NM_SETTINGS_IFACE = "org.freedesktop.NetworkManager.Settings"
NM_SETTINGS_CONNECTION_IFACE = "org.freedesktop.NetworkManager.Settings.Connection"

# NMDeviceType (subset) - https://networkmanager.dev/docs/api/latest/nm-dbus-types.html
NM_DEVICE_TYPE_ETHERNET = 1
NM_DEVICE_TYPE_WIFI = 2
NM_DEVICE_TYPE_NAMES = {NM_DEVICE_TYPE_ETHERNET: "eth", NM_DEVICE_TYPE_WIFI: "wifi"}

# NMDeviceState (subset)
NM_DEVICE_STATE_ACTIVATED = 100
NM_DEVICE_STATE_FAILED = 120

# NMDeviceStateReason (subset relevant to a failed connection attempt) -
# confirmed against https://networkmanager.dev/docs/api/latest/nm-dbus-types.html
NM_DEVICE_STATE_REASON_NO_SECRETS = 7
NM_DEVICE_STATE_REASON_SUPPLICANT_DISCONNECT = 8
NM_DEVICE_STATE_REASON_SUPPLICANT_TIMEOUT = 11
NM_DEVICE_STATE_REASON_SSID_NOT_FOUND = 53

NM_802_11_AP_FLAGS_PRIVACY = 0x1
NM_802_11_AP_SEC_KEY_MGMT_SAE = 0x400  # WPA3-Personal

NM_CONNECT_TIMEOUT = 30  # seconds to wait for one WiFi association attempt to succeed or fail
NM_SCAN_GRACE_PERIOD = 4  # seconds to let RequestScan populate results before reading them


async def _nm_interface(bus, path: str, iface: str):
    introspection = await bus.introspect(NM_BUS_NAME, path)
    proxy = bus.get_proxy_object(NM_BUS_NAME, path, introspection)
    return proxy.get_interface(iface)


async def _get_wifi_device_path(bus):
    nm = await _nm_interface(bus, NM_ROOT_PATH, NM_IFACE)
    for path in await nm.call_get_devices():
        dev = await _nm_interface(bus, path, NM_DEVICE_IFACE)
        if await dev.get_device_type() == NM_DEVICE_TYPE_WIFI:
            return path
    return None


async def _delete_existing_wifi_connections(bus, ssid: str) -> None:
    """AddAndActivateConnection always creates a brand new saved profile,
    even for a network already known - confirmed on real hardware, where
    repeated (re)provisioning against the same SSID left 5 separate
    identically-named profiles behind. Deleting any existing profile for
    this SSID first means (re)connecting to a known network updates it in
    place instead of piling up duplicates indefinitely."""
    settings_iface = await _nm_interface(bus, NM_SETTINGS_PATH, NM_SETTINGS_IFACE)
    for path in await settings_iface.call_list_connections():
        try:
            conn = await _nm_interface(bus, path, NM_SETTINGS_CONNECTION_IFACE)
            wireless = (await conn.call_get_settings()).get("802-11-wireless")
            if not wireless:
                continue
            existing_ssid = bytes(wireless["ssid"].value).decode("utf-8", errors="replace")
            if existing_ssid == ssid:
                await conn.call_delete()
        except Exception as e:
            logger.warning(f"Could not inspect/delete existing NM connection {path}: {e}")


def _channel_from_frequency_mhz(freq: int) -> int:
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if freq >= 5000:
        return (freq - 5000) // 5
    return 0


def _ap_security_string(flags: int, wpa_flags: int, rsn_flags: int) -> str:
    if rsn_flags & NM_802_11_AP_SEC_KEY_MGMT_SAE:
        return "WPA3"
    if rsn_flags:
        return "WPA2"
    if wpa_flags:
        return "WPA"
    if flags & NM_802_11_AP_FLAGS_PRIVACY:
        return "WEP"
    return "OPEN"


def _map_nm_failure_reason(reason_code) -> str:
    # Matches the net_fail reason vocabulary already documented in
    # docs/blynk-ble-provisioning-protocol.md. NetworkManager gives us a
    # genuine per-attempt reason code here, unlike the ESP-IDF/Arduino
    # reference implementations which mostly collapse WiFi failures into a
    # generic timeout - "not_found" and "invalid_credentials" are real,
    # distinct outcomes here rather than aspirational protocol fields.
    return {
        NM_DEVICE_STATE_REASON_NO_SECRETS: "invalid_credentials",
        NM_DEVICE_STATE_REASON_SUPPLICANT_DISCONNECT: "invalid_credentials",
        NM_DEVICE_STATE_REASON_SUPPLICANT_TIMEOUT: "invalid_credentials",
        NM_DEVICE_STATE_REASON_SSID_NOT_FOUND: "not_found",
    }.get(reason_code, "timeout")


def _prefix_from_netmask(mask: str) -> int:
    try:
        return sum(bin(int(octet)).count("1") for octet in mask.split("."))
    except ValueError:
        return 24


def _ipv4_str_to_uint32(addr: str) -> int:
    # NetworkManager's ipv4.dns setting is a list of network-byte-order
    # uint32s, not dotted-decimal strings (unlike ipv4.address-data, which
    # is string-based) - confirmed against NetworkManager's own settings
    # reference, not yet against this project's real hardware.
    return struct.unpack("=I", socket.inet_aton(addr))[0]


def _ipv4_settings(pending: dict) -> dict:
    ip = pending.get("ip")
    if not ip:
        return {"method": Variant("s", "auto")}

    settings = {
        "method": Variant("s", "manual"),
        "address-data": Variant("aa{sv}", [{
            "address": Variant("s", ip),
            "prefix": Variant("u", _prefix_from_netmask(pending.get("mask", "255.255.255.0"))),
        }]),
    }
    gw = pending.get("gw")
    if gw:
        settings["gateway"] = Variant("s", gw)
    dns_servers = [d for d in (pending.get("dns"), pending.get("dns2")) if d]
    if dns_servers:
        settings["dns"] = Variant("au", [_ipv4_str_to_uint32(d) for d in dns_servers])
    return settings

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
        # options is a CharacteristicWriteOptions (bluez_peripheral 0.1.7),
        # not a raw dict - .device is the writing central's own D-Bus object
        # path, needed to watch it for a mid-session disconnect.
        self._on_rx(bytes(value), options.device)

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
    asyncio.run in provision()), so first-time provisioning always runs
    before normal MQTT operation starts. A reprovisioning=True session
    (BlynkAgent's connectivity watchdog) runs on its own thread instead,
    concurrently with the already-running MQTT loop against the local
    broker - see agent.py's _run_reprovisioning."""

    def __init__(self, config, compose_manager, bridge, reprovisioning: bool = False):
        self.config = config
        self.compose_manager = compose_manager
        self.bridge = bridge
        # True when called from BlynkAgent's connectivity watchdog on an
        # already-configured device (sustained connectivity loss), rather
        # than at first-boot with no stored identity. Keeps the existing
        # Blynk token intact - see _handle_reset and provision() below.
        # Mirrors the more capable of the two Blynk Edgent reference
        # implementations (ESP-IDF), which re-enters provisioning in place
        # without discarding what's already been proven to work.
        self.reprovisioning = reprovisioning
        self.service = ProvisioningService(self._on_rx)
        self.bus = None  # set once run() connects; used by NM/BlueZ D-Bus calls from handlers
        self._pending = {}
        if reprovisioning and config.auth_token:
            self._pending["blynk"] = config.auth_token
            self._pending["host"] = config.server
        self._watched_devices = set()  # central D-Bus device paths already being watched for disconnect
        self._background_tasks = set()  # in-flight ifs/scan/connect/watch tasks - see _spawn and run()'s cleanup
        self._done = asyncio.Event()
        self._result = False

    def _spawn(self, coro) -> "asyncio.Task":
        # A stuck task (e.g. a WiFi connect attempt against a nonexistent
        # SSID) left running when the overall session ends would otherwise
        # race run()'s bus.disconnect() during cleanup, surfacing as an
        # unretrieved BrokenPipeError instead of being cancelled cleanly -
        # confirmed on real hardware. Tracking every spawned task here lets
        # run() cancel whatever's still outstanding before disconnecting.
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _on_rx(self, raw: bytes, device_path=None) -> None:
        if device_path and device_path not in self._watched_devices:
            self._watched_devices.add(device_path)
            self._spawn(self._watch_central_disconnect(device_path))

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
            "scan": self._handle_scan,
            "set": self._handle_set,
            "connect": self._handle_connect,
            "reset": self._handle_reset,
            "reboot": self._handle_reboot,
        }.get(data.get("t"))

        if handler is None:
            self.service.send({"t": "error", "msg": "invalid command"})
            return
        handler(data)

    async def _watch_central_disconnect(self, device_path: str) -> None:
        """BlueZ tracks the connected phone as its own org.bluez.Device1
        object - watching its Connected property is how we learn it dropped
        the link mid-session, distinctly from the overall session inactivity
        timeout. Both Blynk Edgent reference implementations treat this as
        baseline provisioning behaviour, not an edge case. BlueZ itself
        keeps advertising registered and resumes it automatically once a
        central disconnects, so there's nothing to do here beyond clearing
        whatever this session had staged, so a reconnecting app starts clean
        (matches the Arduino reference's clearRuntimeConfig-on-disconnect)."""
        try:
            introspection = await self.bus.introspect("org.bluez", device_path)
            proxy = self.bus.get_proxy_object("org.bluez", device_path, introspection)
            props_iface = proxy.get_interface("org.freedesktop.DBus.Properties")
        except Exception as e:
            logger.warning(f"Could not watch central {device_path} for disconnect: {e}")
            return

        def on_properties_changed(interface_name, changed, invalidated):
            if interface_name != "org.bluez.Device1":
                return
            connected = changed.get("Connected")
            if connected is not None and connected.value is False:
                logger.info("BLE central disconnected mid-session, clearing staged config")
                self._pending.clear()
                if self.reprovisioning and self.config.auth_token:
                    self._pending["blynk"] = self.config.auth_token
                    self._pending["host"] = self.config.server

        props_iface.on_properties_changed(on_properties_changed)

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
        self._spawn(self._do_ifs())

    async def _do_ifs(self) -> None:
        # Real host interfaces via NetworkManager, reached over D-Bus (see
        # NM_* constants above) - this container has no network-namespace
        # visibility into the host's own wlan0/eth0 otherwise. A WiFi
        # device is now reported as "wifi" (previously always faked as
        # "eth" purely to skip the app's picker, since nothing here could
        # act on a real SSID/password yet) so the app shows its scan/picker
        # flow, which _handle_scan/_handle_connect below now actually serve.
        self.service.send({"t": "ifs_start"})
        try:
            nm = await _nm_interface(self.bus, NM_ROOT_PATH, NM_IFACE)
            sent_any = False
            for path in await nm.call_get_devices():
                dev = await _nm_interface(self.bus, path, NM_DEVICE_IFACE)
                name = NM_DEVICE_TYPE_NAMES.get(await dev.get_device_type())
                if name is None:
                    continue  # not something the app can pick (loopback, bridge, veth, ...)
                state = await dev.get_state()
                msg = {
                    "t": "if",
                    "name": name,
                    "mac": await dev.get_hw_address(),
                    "status": "ready" if state >= NM_DEVICE_STATE_ACTIVATED else "unavailable",
                    "static_ip": 1,
                }
                if name == "wifi":
                    msg["scan"] = 1
                self.service.send(msg)
                sent_any = True
            if not sent_any:
                self.service.send({"t": "if", "name": "eth", "status": "ready"})
        except Exception as e:
            logger.error(f"Failed to enumerate network interfaces via NetworkManager: {e}")
            self.service.send({"t": "if", "name": "eth", "status": "ready"})
        self.service.send({"t": "ifs_end"})

    def _handle_scan(self, data) -> None:
        self._spawn(self._do_scan())

    async def _do_scan(self) -> None:
        self.service.send({"t": "scan_start"})
        try:
            wifi_path = await _get_wifi_device_path(self.bus)
            if wifi_path is None:
                self.service.send({"t": "error", "msg": "no wifi"})
                self.service.send({"t": "scan_end"})
                return

            wireless = await _nm_interface(self.bus, wifi_path, NM_WIRELESS_IFACE)
            await wireless.call_request_scan({})
            await asyncio.sleep(NM_SCAN_GRACE_PERIOD)

            best_by_ssid = {}
            for ap_path in await wireless.call_get_all_access_points():
                ap = await _nm_interface(self.bus, ap_path, NM_AP_IFACE)
                ssid = bytes(await ap.get_ssid()).decode("utf-8", errors="replace")
                if not ssid:
                    continue  # hidden network - nothing for the user to pick from a list
                # NetworkManager reports a 0-100 signal quality, not raw
                # dBm - this is NetworkManager's own published formula for
                # approximating one back from the other.
                rssi = await ap.get_strength() // 2 - 100
                if rssi < -90:
                    continue
                if ssid in best_by_ssid and best_by_ssid[ssid]["rssi"] >= rssi:
                    continue
                best_by_ssid[ssid] = {
                    "ssid": ssid,
                    "bssid": await ap.get_hw_address(),
                    "rssi": rssi,
                    "sec": _ap_security_string(
                        await ap.get_flags(), await ap.get_wpa_flags(), await ap.get_rsn_flags()
                    ),
                    "ch": _channel_from_frequency_mhz(await ap.get_frequency()),
                }

            # Strongest first, capped - matches the protocol doc's "top 15-30 strongest" guidance.
            for network in sorted(best_by_ssid.values(), key=lambda n: n["rssi"], reverse=True)[:30]:
                self.service.send({"t": "scan", **network})
        except Exception as e:
            logger.error(f"WiFi scan failed: {e}")
        self.service.send({"t": "scan_end"})

    def _handle_set(self, data) -> None:
        unknown = set(data.keys()) - {"t"} - KNOWN_SET_FIELDS
        if unknown:
            self.service.send({"t": "set_fail"})
            return
        for field in ("blynk", "host", "ssid", "pass", "ip", "mask", "gw", "dns", "dns2"):
            if field in data:
                self._pending[field] = data[field]
        self.service.send({"t": "set_ok"})

    def _handle_connect(self, data) -> None:
        token = self._pending.get("blynk", "")
        if len(token) != 32:
            self.service.send({"t": "connect_fail", "msg": "configuration invalid"})
            return

        self.service.send({"t": "connecting"})
        self._spawn(self._do_connect(token, self._pending.get("host")))

    async def _do_connect(self, token: str, host) -> None:
        ssid = self._pending.get("ssid")
        if ssid:
            self.service.send({"t": "status", "s": "connecting_net"})
            try:
                ok, reason = await self._connect_wifi(ssid, self._pending.get("pass"))
            except Exception as e:
                logger.error(f"WiFi connect failed: {e}")
                ok, reason = False, "generic"
            if not ok:
                self.service.send({"t": "net_fail", "reason": reason})
                # Keep the BLE link and session open (matches both Blynk
                # Edgent reference implementations) - only forget the WiFi
                # fields just tried, so the app can resend set+connect with
                # corrected credentials without reconnecting BLE or losing
                # an already-staged token/host.
                for field in ("ssid", "pass", "ip", "mask", "gw", "dns", "dns2"):
                    self._pending.pop(field, None)
                return

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

    async def _connect_wifi(self, ssid: str, password) -> tuple:
        """Activates a WiFi connection via NetworkManager and waits on the
        device's own StateChanged D-Bus signal (event-driven, not polled)
        to learn success or a specific failure reason."""
        wifi_path = await _get_wifi_device_path(self.bus)
        if wifi_path is None:
            return False, "generic"

        await _delete_existing_wifi_connections(self.bus, ssid)

        settings = {
            "connection": {
                "id": Variant("s", ssid),
                "type": Variant("s", "802-11-wireless"),
            },
            "802-11-wireless": {
                "ssid": Variant("ay", ssid.encode("utf-8")),
                "mode": Variant("s", "infrastructure"),
            },
            "ipv4": _ipv4_settings(self._pending),
            "ipv6": {"method": Variant("s", "auto")},
        }
        if password:
            settings["802-11-wireless-security"] = {
                "key-mgmt": Variant("s", "wpa-psk"),
                "psk": Variant("s", password),
            }

        dev = await _nm_interface(self.bus, wifi_path, NM_DEVICE_IFACE)
        result = {}
        done = asyncio.Event()

        def on_state_changed(new_state, old_state, reason):
            if new_state == NM_DEVICE_STATE_ACTIVATED:
                result["ok"] = True
                done.set()
            elif new_state == NM_DEVICE_STATE_FAILED:
                result["ok"] = False
                result["reason"] = reason
                done.set()

        async def activate():
            nm = await _nm_interface(self.bus, NM_ROOT_PATH, NM_IFACE)
            # AddAndActivateConnection is supposed to return promptly and
            # report the actual outcome later via StateChanged, but a
            # nonexistent/unreachable SSID has been observed on real
            # hardware to leave this call (or NetworkManager's handling of
            # it) outstanding well past NM_CONNECT_TIMEOUT - wrapping only
            # done.wait() below let that hang the entire session until the
            # outer INACTIVITY_TIMEOUT gave up. Wrapping this call too
            # means asyncio.wait_for's timeout bounds the whole attempt
            # regardless of where it's actually stuck.
            await nm.call_add_and_activate_connection(settings, wifi_path, "/")
            await done.wait()

        dev.on_state_changed(on_state_changed)
        try:
            try:
                await asyncio.wait_for(activate(), timeout=NM_CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                return False, "timeout"
        finally:
            dev.off_state_changed(on_state_changed)

        if result.get("ok"):
            return True, None
        return False, _map_nm_failure_reason(result.get("reason"))

    def _handle_reset(self, data) -> None:
        # In reprovisioning mode (an already-configured device recovering
        # from sustained connectivity loss - see BlynkAgent's connectivity
        # watchdog) this only forgets the just-staged WiFi fields, not the
        # existing Blynk identity: "reset" here means "let me pick a
        # different network," not "forget this device," mirroring the
        # Arduino reference's own reset command (network-only, never the
        # cloud token).
        if not self.reprovisioning:
            self.config.auth_token = None
            self.config.save()
        for field in ("ssid", "pass", "ip", "mask", "gw", "dns", "dns2"):
            self._pending.pop(field, None)
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
        self.bus = bus
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

            # A still-running task here (e.g. a WiFi connect attempt that
            # hasn't resolved yet) would otherwise race bus.disconnect()
            # below and surface as an unretrieved BrokenPipeError instead
            # of stopping cleanly - confirmed on real hardware.
            for task in list(self._background_tasks):
                task.cancel()
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)

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


def provision(config, compose_manager, bridge, reprovisioning: bool = False) -> bool:
    """Blocks until provisioning succeeds, is abandoned, or times out.
    Returns True only if a valid token was received and the bridge was
    brought up with it. reprovisioning=True is for an already-configured
    device recovering from sustained connectivity loss (see BlynkAgent's
    connectivity watchdog) - it keeps the existing Blynk token/server
    instead of requiring the app to resupply them."""
    try:
        return asyncio.run(
            ProvisioningSession(config, compose_manager, bridge, reprovisioning).run()
        )
    except Exception as e:
        logger.error(f"BLE provisioning failed: {e}")
        return False
