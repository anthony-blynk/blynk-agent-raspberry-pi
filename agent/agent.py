"""
Blynk Agent - runs as a plain local MQTT client against the mqtt-bridge
container (no TLS, no cloud auth - that's handled entirely by mqtt-bridge's
bridge connection to Blynk). Manages OTA updates to the stack's own
docker-compose.yml and reacts to a few other downlink control topics.
"""

import json
import logging
import platform
import time
import subprocess
import shutil
import signal
import socket
import sys
import os
import threading
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
from dotenv import dotenv_values
import yaml
import requests

import ble_provisioning

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

CONFIG_BASE = Path(os.getenv('BLYNK_CONFIG_DIR', '/opt/blynk'))
ENV_FILE = CONFIG_BASE / "blynk.env"
COMPOSE_FILE = CONFIG_BASE / "docker-compose.yml"
BACKUP_DIR = CONFIG_BASE / "backups"
BRIDGE_CONF_DIR = CONFIG_BASE / "mqtt-bridge" / "conf.d"
BRIDGE_CONF_FILE = BRIDGE_CONF_DIR / "blynk-bridge.conf"
BRIDGE_HOST_OVERRIDE_FILE = CONFIG_BASE / "bridge_host_override"

MQTT_HOST = os.getenv("MQTT_HOST", "mqtt-bridge")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

# Capability gate, not a runtime toggle - deliberately only settable via
# docker-compose.yml (i.e. an OTA push), not from Blynk. Compromising the
# Blynk account alone should never be enough to get a shell on a device
# that never had this turned on; that requires a second, harder action
# (pushing a valid OTA update). Absent from the tracked docker-compose.yml
# on purpose - this is a per-device opt-in (see the camera-detector
# example for the same pattern), not something the whole fleet gets by
# default from a normal install/update.
TERMINAL_CAPABILITY_ENABLED = os.getenv("AGENT_TERMINAL_ENABLED", "false").lower() == "true"
TERMINAL_COMMAND_TIMEOUT = 60  # seconds - long enough for things like `apt update`, not unbounded

TOPIC_DOWNLINK = "downlink/#"
TOPIC_OTA = "downlink/ota/json"
TOPIC_PING = "downlink/ping"
TOPIC_REBOOT = "downlink/reboot"
TOPIC_REDIRECT = "downlink/redirect"
TOPIC_RECONFIGURE = "downlink/reconfigure"
TOPIC_DIAGNOSTICS_ENABLED = "downlink/ds/AgentDiagnosticsEnabled"
TOPIC_TERMINAL_ENABLED = "downlink/ds/AgentTerminalEnabled"
TOPIC_TERMINAL = "downlink/ds/AgentTerminal"
TOPIC_INFO = "info/mcu"

RECONNECT_DELAY = 1
MAX_RECONNECT_DELAY = 60
KEEPALIVE = 60
DIAGNOSTICS_INTERVAL = 60  # seconds between CPU/mem/disk/temp reports while enabled

# How long the Blynk cloud bridge can stay down before this device falls
# back into BLE provisioning in place (see BlynkAgent._check_connectivity_watchdog)
# to let the app supply corrected network info. Long enough to ride out a
# brief WiFi blip or router reboot, short enough to actually recover from a
# real outage - unlike the Arduino Edgent reference's retry-forever-for-
# hours loop, which never re-enters provisioning automatically at all.
BRIDGE_DISCONNECT_GRACE_PERIOD = 300

BRIDGE_TEMPLATE = """\
connection blynk-cloud
address {server}:8883
# mqttv311 gets a clean CONNACK/SUBACK from Blynk's broker but then dies
# with "malformed packet" / "protocol error: RESERVED packet" within
# seconds - confirmed against the real cloud broker. mqttv50 is stable.
bridge_protocol_version mqttv50
remote_username device
remote_password {token}
remote_clientid blynk-bridge-{template_id}
bridge_cafile /etc/ssl/certs/ca-certificates.crt
cleansession true
# Published locally (retained) to $SYS/broker/connection/<remote_clientid>/state
# on state change - the connectivity watchdog subscribes to this to learn
# the cloud bridge is down, distinctly from this container's own local
# broker connection (which stays up regardless of WiFi/cloud state).
notifications true
try_private false

topic downlink/# in 1
topic ds/# out 1
topic batch_ds out 1
topic info/mcu out 1
topic event/# out 1
topic get/# out 1
topic meta/# out 1
"""


class BlynkConfig:
    """Cloud identity (server/token/template) - used to render the mqtt-bridge
    config and to report device info, not for any MQTT connection
    the agent itself makes."""

    def __init__(self):
        self.server: Optional[str] = None
        self.auth_token: Optional[str] = None
        self.template_id: Optional[str] = None
        # White-labeling for Blynk Enterprise clients running their own
        # server/app - matches Edgent-ESP-IDF/Edgent-Arduino's own
        # BLYNK_VENDOR_PREFIX. Used for the BLE-advertised device name and
        # the "vendor" field in the info response (Arduino's Edgent uses it
        # for both; ESP-IDF's hardcodes "vendor" to literal "Blynk", which
        # looks like an oversight given the whole point is hiding "Blynk"
        # branding from a white-labeled app - matching Arduino's fuller
        # behavior here instead).
        self.vendor_prefix: str = "Blynk"

    def load(self, env_path: Path = ENV_FILE) -> bool:
        values = dotenv_values(env_path) if env_path.exists() else {}
        self.server = values.get("BLYNK_SERVER") or os.getenv("BLYNK_SERVER") or None
        self.auth_token = values.get("BLYNK_AUTH_TOKEN") or os.getenv("BLYNK_AUTH_TOKEN") or None
        self.template_id = values.get("BLYNK_TEMPLATE_ID") or os.getenv("BLYNK_TEMPLATE_ID") or None
        self.vendor_prefix = values.get("BLYNK_VENDOR_PREFIX") or os.getenv("BLYNK_VENDOR_PREFIX") or "Blynk"

        # Server/token are optional here - a device shipped with no auth
        # token is expected to get them later via BLE provisioning.
        # Template ID identifies the product itself, not something the
        # Blynk app hands over during provisioning, so it's still required
        # up front.
        if not self.template_id:
            logger.error("Missing BLYNK_TEMPLATE_ID")
            return False

        if self.is_provisioned():
            logger.info(f"Loaded configuration for server: {self.server}")
        else:
            logger.info("No stored server/auth token - BLE provisioning required")
        return True

    def is_provisioned(self) -> bool:
        return bool(self.server and self.auth_token)

    def save(self, env_path: Path = ENV_FILE) -> None:
        """Persist server/token/template_id to blynk.env - used once BLE
        provisioning hands over a token (and possibly a new server), or
        when reconfigure/reset clears one back out. `or ''` matters here:
        an unformatted None would write the literal text "None", which
        reloads as a truthy non-empty string and defeats is_provisioned()."""
        env_path.write_text(
            f"BLYNK_SERVER={self.server or ''}\n"
            f"BLYNK_TEMPLATE_ID={self.template_id or ''}\n"
            f"BLYNK_AUTH_TOKEN={self.auth_token or ''}\n"
            f"BLYNK_VENDOR_PREFIX={self.vendor_prefix}\n"
        )

    def effective_server(self) -> str:
        """The bridge host in effect right now - a downlink/redirect
        overrides this until the agent is redeployed with a new blynk.env."""
        if BRIDGE_HOST_OVERRIDE_FILE.exists():
            override = BRIDGE_HOST_OVERRIDE_FILE.read_text().strip()
            if override:
                return override
        return self.server


MQTT_BRIDGE_CONTAINER = "blynk-mqtt-bridge-1"  # docker-compose.yml's fixed `name: blynk` + service `mqtt-bridge`, single instance


class MqttBridge:
    """Renders the mqtt-bridge connection config and restarts the
    mqtt-bridge container (by name, not via the docker-compose project -
    see _restart_mqtt_bridge) when it changes."""

    def __init__(self, config: BlynkConfig, compose_path: Path = COMPOSE_FILE):
        self.config = config
        self.compose_path = compose_path

    def ensure_current(self, server_override: Optional[str] = None, force_restart: bool = False) -> None:
        server = server_override or self.config.effective_server()
        rendered = BRIDGE_TEMPLATE.format(
            server=server,
            token=self.config.auth_token,
            template_id=self.config.template_id,
        )

        BRIDGE_CONF_DIR.mkdir(parents=True, exist_ok=True)
        unchanged = BRIDGE_CONF_FILE.exists() and BRIDGE_CONF_FILE.read_text() == rendered
        if unchanged and not force_restart:
            return

        if unchanged:
            # force_restart=True after a real WiFi (re)connect, even though
            # the bridge conf content itself didn't change - confirmed on
            # real hardware that the mqtt-bridge container keeps whatever DNS
            # server Docker generated its resolv.conf from at container
            # start, and never re-reads the host's current one. Switching
            # WiFi networks with a different DNS server (e.g. moving a
            # device between locations) left the bridge permanently unable
            # to resolve the Blynk host, even though the host itself, and
            # WiFi, were both fine - only a container restart picks up the
            # host's current resolver.
            logger.info(f"Bridge config unchanged for {server}, restarting mqtt-bridge anyway after a WiFi (re)connect")
        else:
            BRIDGE_CONF_FILE.write_text(rendered)
            logger.info(f"Bridge config updated for {server}, restarting mqtt-bridge")
        self._restart_mqtt_bridge()

    def apply_redirect(self, new_server: str) -> None:
        BRIDGE_HOST_OVERRIDE_FILE.write_text(new_server)
        self.ensure_current(server_override=new_server)

    def _restart_mqtt_bridge(self) -> None:
        try:
            process = subprocess.run(
                # Plain `docker restart`, not `docker compose restart`:
                # confirmed on real hardware that the agent container's
                # Alpine-packaged docker-cli-compose (v2.27.0) takes ~11s
                # for this exact restart regardless of -t, vs. ~3.5s for
                # the same command via the host's own compose plugin
                # (v5.4.0) - a version-specific slowdown in compose's own
                # restart path. The low-level engine call sidesteps it.
                # -t 0 skips the graceful-shutdown wait too: mqtt-bridge's
                # persistence log tolerates abrupt termination fine, and
                # there's nothing worth flushing at the point this fires
                # (first provisioning, or an occasional bridge redirect).
                ["docker", "restart", "-t", "0", MQTT_BRIDGE_CONTAINER],
                capture_output=True, text=True, timeout=30,
            )
            if process.returncode != 0:
                logger.error(f"Failed to restart mqtt-bridge: {process.stderr}")
        except Exception as e:
            logger.error(f"Failed to restart mqtt-bridge: {e}")


class ComposeManager:
    """Applies OTA updates to the stack's own docker-compose.yml: skips
    no-op re-applies of the same version, validates before touching the
    live file, and rolls back if the new file fails to come up."""

    def __init__(self, compose_path: Path = COMPOSE_FILE):
        self.compose_path = compose_path
        self.compose_dir = compose_path.parent

    def get_version(self, path: Optional[Path] = None) -> Optional[str]:
        path = path or self.compose_path
        try:
            if not path.exists():
                return None
            with path.open('r') as f:
                compose_data = yaml.safe_load(f)
            for key, value in compose_data.items():
                if key.startswith('x-') and isinstance(value, dict):
                    if version := value.get('version'):
                        return version
            return None
        except (yaml.YAMLError, OSError) as e:
            logger.error(f"Failed to read compose file {path}: {e}")
            return None

    def update_from_url(self, url: str) -> bool:
        try:
            logger.info(f"Downloading compose file from: {url}")
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            try:
                new_data = yaml.safe_load(response.text)
                if not isinstance(new_data, dict):
                    raise ValueError("YAML root is not a dictionary")
                if "x-stack" not in new_data:
                    raise ValueError("Missing required 'x-stack' field")
            except (yaml.YAMLError, ValueError) as e:
                logger.error(f"Invalid compose file format: {e}")
                return False

            new_version = self.get_version_from_data(new_data)
            current_version = self.get_version()
            if new_version and new_version == current_version:
                logger.info(f"Already at version {current_version}, skipping re-apply")
                return True

            new_file = self.compose_path.with_suffix(".new")
            new_file.write_text(response.text)
            if not self._validate(new_file):
                logger.error("New compose file failed validation, leaving current stack untouched")
                new_file.unlink(missing_ok=True)
                return False

            backup_path = self._backup_existing_file()
            new_file.replace(self.compose_path)
            logger.info(f"Updated compose file: {self.compose_path} ({current_version} -> {new_version})")

            return self._apply_via_helper(backup_path)

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to download compose file: {e}")
            return False
        except OSError as e:
            logger.error(f"Failed to write compose file: {e}")
            return False

    @staticmethod
    def get_version_from_data(compose_data: dict) -> Optional[str]:
        for key, value in compose_data.items():
            if key.startswith('x-') and isinstance(value, dict):
                if version := value.get('version'):
                    return version
        return None

    def _validate(self, path: Path) -> bool:
        try:
            process = subprocess.run(
                ["docker", "compose", "-f", str(path), "config", "--quiet"],
                capture_output=True, text=True, timeout=30,
            )
            if process.returncode != 0:
                logger.error(f"Compose validation failed: {process.stderr}")
                return False
            return True
        except Exception as e:
            logger.error(f"Compose validation failed: {e}")
            return False

    def _backup_existing_file(self) -> Optional[Path]:
        if not self.compose_path.exists():
            return None
        try:
            BACKUP_DIR.mkdir(exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = BACKUP_DIR / f"docker-compose_backup_{timestamp}.yml"
            shutil.copy2(self.compose_path, backup_path)
            logger.info(f"Backed up existing file to: {backup_path}")
            return backup_path
        except Exception as e:
            logger.error(f"Failed to backup existing file: {e}")
            return None

    def _run_docker_compose(self) -> bool:
        try:
            # A device set up via install.sh has no source checkout, only
            # pulled images - `up -d` alone would try to *build* an image
            # tag it doesn't recognize (since `build:` is still in the
            # file for local dev) rather than pull it. Pulling explicitly
            # first sidesteps that entirely.
            pull = subprocess.run(
                ["docker", "compose", "-f", str(self.compose_path), "pull"],
                capture_output=True, text=True, timeout=300,
            )
            if pull.returncode != 0:
                logger.warning(f"Pull failed, falling back to build: {pull.stderr}")

            logger.info(f"Running docker compose for {self.compose_path}")
            process = subprocess.run(
                ["docker", "compose", "-f", str(self.compose_path), "up", "-d", "--remove-orphans"],
                capture_output=True, text=True, timeout=300,
            )
            if process.returncode == 0:
                logger.info("Docker compose applied successfully")
                return True
            logger.error(f"Docker compose failed: {process.stderr}")
            return False
        except Exception as e:
            logger.error(f"Failed to run docker compose: {e}")
            return False

    def _apply_via_helper(self, backup_path: Optional[Path]) -> bool:
        """`docker compose up -d` run directly from here would be a child
        process of THIS container - if the update recreates the agent
        service (i.e. updates the agent itself), Docker tears this
        container down mid-recreate and kills that child with it, leaving
        the operation half-done. A detached helper container is not part
        of what's being recreated, so it survives regardless and can
        finish the job (and roll back) on its own."""
        try:
            my_image = subprocess.run(
                ["docker", "inspect", "--format", "{{.Config.Image}}", socket.gethostname()],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            if not my_image:
                logger.error("Could not determine own image, applying directly instead")
                return self._run_docker_compose()

            # This container gets attached to the project's network by
            # Compose automatically - a standalone `docker run` for the
            # helper doesn't join it on its own, so "mqtt-bridge" wouldn't
            # resolve inside the helper unless we explicitly reuse it.
            my_network = subprocess.run(
                ["docker", "inspect", "--format",
                 "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}", socket.gethostname()],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()

            cmd = ["docker", "run", "-d", "--rm", "--name", "blynk-apply-helper"]
            if my_network:
                cmd += ["--network", my_network]
            cmd += [
                "-v", f"{CONFIG_BASE}:{CONFIG_BASE}",
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-e", f"BLYNK_CONFIG_DIR={CONFIG_BASE}",
                my_image,
                "python3", "agent.py", "--apply-only", str(backup_path or ""),
            ]
            process = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if process.returncode != 0:
                logger.error(f"Failed to launch apply helper: {process.stderr}")
                return False
            logger.info("Handed off compose apply to a detached helper container")
            return True
        except Exception as e:
            logger.error(f"Failed to launch apply helper: {e}")
            return False


def publish_device_info_once(config: 'BlynkConfig', compose_manager: 'ComposeManager') -> None:
    """One-shot connect/publish/disconnect, used by the detached apply
    helper to report the actual outcome once it's known - the process that
    hands off the update can't know yet whether it'll succeed or roll back."""
    payload = {
        "tmpl": config.template_id,
        "ver": compose_manager.get_version() or "unknown",
        "build": time.strftime("%b %d %Y %H:%M:%S"),
        "type": config.template_id,
        "rxbuff": 1024
    }
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=KEEPALIVE)
        client.loop_start()
        info = client.publish(TOPIC_INFO, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=5)
        logger.info(f"Published device info (post-apply): {payload}")
    except Exception as e:
        logger.error(f"Failed to publish post-apply device info: {e}")
    finally:
        client.loop_stop()
        client.disconnect()


def run_apply_only(backup_arg: str) -> None:
    compose_manager = ComposeManager()
    config = BlynkConfig()
    config.load()

    if compose_manager._run_docker_compose():
        logger.info("Detached apply succeeded")
        publish_device_info_once(config, compose_manager)
        return

    logger.error("Detached apply failed, attempting rollback")
    backup_path = Path(backup_arg) if backup_arg else None
    if not backup_path or not backup_path.exists():
        candidates = sorted(BACKUP_DIR.glob("docker-compose_backup_*.yml"))
        backup_path = candidates[-1] if candidates else None

    if backup_path and backup_path.exists():
        shutil.copy2(backup_path, COMPOSE_FILE)
        compose_manager._run_docker_compose()
    else:
        logger.error("No backup available to roll back to")
    publish_device_info_once(config, compose_manager)


# Metadata (static facts, published once at startup) and diagnostics
# (live metrics, published periodically) - all read directly from
# /proc, /sys, and stdlib rather than a dependency like psutil, matching
# how the rest of this codebase already reads /proc/cpuinfo etc directly.

def _read_device_model() -> str:
    try:
        with open("/proc/device-tree/model") as f:
            return f.read().strip("\x00\n")
    except OSError:
        return "unknown"


def _read_os_pretty_name() -> str:
    # /host/etc/os-release (see docker-compose.yml) is the real host OS -
    # pid: host makes /proc reflect the host, but /etc/ is still this
    # container's own Alpine image, so plain /etc/os-release would report
    # "Alpine Linux" instead. Falls back if that mount isn't present.
    for path in ("/host/etc/os-release", "/etc/os-release"):
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            continue
    return "unknown"


def _read_mem_total_mb() -> Optional[float]:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def _read_disk_total_gb() -> Optional[float]:
    try:
        stats = os.statvfs("/")
        return stats.f_frsize * stats.f_blocks / (1024 ** 3)
    except OSError:
        return None


def _read_cpu_usage_percent() -> Optional[float]:
    # 1-minute load average / core count - an approximation (load average
    # also counts processes waiting on I/O, not just CPU-bound ones), not
    # an instantaneous reading, but sufficient for health/trend monitoring
    # without needing a two-sample /proc/stat delta.
    try:
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
        cores = os.cpu_count() or 1
        return min(load1 / cores * 100, 100.0)
    except OSError:
        return None


def _read_mem_usage_percent() -> Optional[float]:
    # MemAvailable, not MemFree - Linux uses "free" RAM aggressively for
    # disk caching, so MemFree alone reads artificially low.
    try:
        values = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    values[key] = int(rest.split()[0])
        total, available = values.get("MemTotal"), values.get("MemAvailable")
        if not total or available is None:
            return None
        return (total - available) / total * 100
    except OSError:
        return None


def _read_disk_usage_percent() -> Optional[float]:
    try:
        stats = os.statvfs("/")
        return (stats.f_blocks - stats.f_bavail) / stats.f_blocks * 100
    except OSError:
        return None


def _read_temperature_c() -> Optional[float]:
    # thermal_zone0 is the common convention for "the main SoC sensor" on
    # single-SoC boards (confirmed present on both Pi and Jetson), though
    # it isn't universally guaranteed to be the CPU on every board.
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000
    except OSError:
        return None


class BlynkAgent:
    """MQTT client against the local mqtt-bridge broker only - no TLS, no
    cloud credentials here, those live entirely in the mqtt-bridge."""

    def __init__(self, config: BlynkConfig, compose_manager: ComposeManager, bridge: MqttBridge):
        self.config = config
        self.compose_manager = compose_manager
        self.bridge = bridge
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._connected = False
        self._reconnect_count = 0
        self._shutting_down = False
        self.last_cloud_contact: Optional[float] = None
        self.diagnostics_enabled = False  # until told otherwise via get/ds on connect
        # Mirrors BRIDGE_TEMPLATE's remote_clientid - see the notifications
        # comment there for why this topic tells us the cloud bridge's own
        # connection state, not just this container's local-broker link.
        self._bridge_state_topic = f"$SYS/broker/connection/blynk-bridge-{config.template_id}/state"
        self._bridge_disconnected_since: Optional[float] = None
        self._reprovisioning = False
        self.terminal_session_enabled = False  # the fast on/off switch, on top of TERMINAL_CAPABILITY_ENABLED
        self._terminal_lock = threading.Lock()  # one command's output at a time - see _run_terminal_command
        self._setup_mqtt_client()

    def _setup_mqtt_client(self) -> None:
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code == 0:
            self._connected = True
            self._reconnect_count = 0
            logger.info(f"Connected to local broker at {MQTT_HOST}:{MQTT_PORT}")
            client.subscribe(TOPIC_DOWNLINK, qos=1)
            client.subscribe(self._bridge_state_topic, qos=1)
            self._publish_device_info()
            self._publish_system_info()
            # Current on/off state lives in Blynk (a console Switch widget),
            # not locally - fetch it rather than assume; the response
            # arrives on TOPIC_DIAGNOSTICS_ENABLED, same as a live toggle.
            client.publish("get/ds", "AgentDiagnosticsEnabled", qos=1)
            if TERMINAL_CAPABILITY_ENABLED:
                client.publish("get/ds", "AgentTerminalEnabled", qos=1)
        else:
            self._connected = False
            logger.error(f"Failed to connect to local broker: {reason_code}")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self._connected = False
        if reason_code != 0 and not self._shutting_down:
            logger.warning(f"Unexpected disconnection from local broker: {reason_code}")

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = message.payload.decode('utf-8')
        except UnicodeDecodeError as e:
            logger.error(f"Failed to decode message payload: {e}")
            return

        if message.topic == TOPIC_OTA:
            self._handle_ota_update(payload)
        elif message.topic == TOPIC_PING:
            self.last_cloud_contact = time.time()
            logger.info("Received downlink/ping")
        elif message.topic == TOPIC_REBOOT:
            self._handle_reboot(payload)
        elif message.topic == TOPIC_REDIRECT:
            self._handle_redirect(payload)
        elif message.topic == TOPIC_RECONFIGURE:
            self._handle_reconfigure(payload)
        elif message.topic == TOPIC_DIAGNOSTICS_ENABLED:
            self._handle_diagnostics_enabled(payload)
        elif message.topic == self._bridge_state_topic:
            self._handle_bridge_state(payload)
        elif message.topic == TOPIC_TERMINAL_ENABLED and TERMINAL_CAPABILITY_ENABLED:
            self._handle_terminal_enabled(payload)
        elif message.topic == TOPIC_TERMINAL:
            if TERMINAL_CAPABILITY_ENABLED:
                self._handle_terminal_command(payload)
            else:
                # Distinct from _handle_terminal_command's own "[terminal
                # disabled]" (that one means the session switch is off;
                # this means the capability itself isn't - typing a
                # command previously did nothing visible here at all).
                self.client.publish(
                    "ds/AgentTerminal",
                    "[terminal disabled: set AGENT_TERMINAL_ENABLED=true in docker-compose.yml]",
                    qos=1,
                )
        else:
            logger.debug(f"Unhandled message on {message.topic}: {payload}")

    def _handle_ota_update(self, json_payload: str) -> None:
        try:
            data = json.loads(json_payload)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid OTA JSON: {e}")
            return

        url = data.get('url')
        if not url:
            logger.error("OTA message missing 'url' field")
            return

        logger.info(f"Starting OTA update from: {url}")
        if self.compose_manager.update_from_url(url):
            # Not confirmed successful yet - the detached helper (see
            # run_apply_only) is the one that knows the real outcome, and
            # publishes device info itself once it actually finishes.
            # Publishing here would report the file's new version before
            # it's actually running (or possibly about to be rolled back).
            logger.info("OTA update handed off for apply")
        else:
            logger.error("OTA update failed")

    def _handle_reboot(self, payload: str) -> None:
        logger.warning(f"Reboot requested via downlink/reboot: {payload!r}")
        try:
            # A container's own PID namespace intercepts the reboot() syscall
            # rather than rebooting the real machine, and recent docker/runc
            # refuse to let you bind-mount a path inside /proc directly. The
            # working combination is pid: "host" + privileged: true (see
            # docker-compose.yml) so this container's own /proc genuinely is
            # the host's - this is an immediate, unclean reboot, not the
            # equivalent of a graceful `reboot`.
            with open("/proc/sysrq-trigger", "w") as f:
                f.write("b")
        except Exception as e:
            logger.error(f"Failed to trigger reboot: {e}")

    def _handle_reconfigure(self, payload: str) -> None:
        # Clear the token and exit - there's no live "drop back into BLE
        # provisioning mid-run" path, so this relies on docker-compose's
        # restart: unless-stopped to bring the container back up, at which
        # point main() sees no stored token and starts BLE provisioning
        # fresh, same as a brand new device.
        logger.warning(f"Reconfigure requested via downlink/reconfigure: {payload!r}")
        self.config.auth_token = None
        self.config.save()
        self._shutting_down = True
        self.client.disconnect()
        self.client.loop_stop()
        sys.exit(0)

    def _handle_bridge_state(self, payload: str) -> None:
        connected = payload.strip() == "1"
        if connected:
            if self._bridge_disconnected_since is not None:
                logger.info("Blynk cloud bridge reconnected")
            self._bridge_disconnected_since = None
        elif self._bridge_disconnected_since is None:
            self._bridge_disconnected_since = time.time()
            logger.warning("Blynk cloud bridge disconnected")

    def _check_connectivity_watchdog(self) -> None:
        # Ticked from _diagnostics_loop's existing 60s cadence rather than a
        # dedicated thread - the bridge-state notification above is only
        # republished on a real state change (mqtt-bridge's notifications
        # feature is retained-on-change, not periodic), so something has to
        # poll elapsed time against BRIDGE_DISCONNECT_GRACE_PERIOD.
        if self._bridge_disconnected_since is None or self._reprovisioning:
            return
        outage = time.time() - self._bridge_disconnected_since
        if outage < BRIDGE_DISCONNECT_GRACE_PERIOD:
            return
        logger.warning(
            f"Blynk cloud bridge has been down for {int(outage)}s - re-entering BLE "
            "provisioning in place so the app can supply corrected network info, "
            "without discarding the stored Blynk token"
        )
        self._start_reprovisioning()

    def _start_reprovisioning(self) -> None:
        # Unlike _handle_reconfigure, this keeps the Blynk token and
        # existing network config intact - it only reopens BLE provisioning
        # in place so the app can supply corrected WiFi info. Mirrors the
        # more capable of the two Blynk Edgent reference implementations
        # (ESP-IDF), which re-enters provisioning without discarding what's
        # already proven to work. ble_provisioning.provision() is a
        # blocking asyncio.run() call, so it needs its own thread - the
        # MQTT loop_forever() driving this agent keeps running unaffected
        # meanwhile, since it only ever talks to the local broker, not the
        # cloud directly.
        self._reprovisioning = True
        threading.Thread(target=self._run_reprovisioning, daemon=True).start()

    def _run_reprovisioning(self) -> None:
        try:
            succeeded = ble_provisioning.provision(
                self.config, self.compose_manager, self.bridge, reprovisioning=True
            )
        except Exception as e:
            logger.error(f"In-place reprovisioning attempt failed: {e}")
            succeeded = False
        finally:
            self._reprovisioning = False
            if succeeded:
                self._bridge_disconnected_since = None
            else:
                # Still down and no one supplied corrected config in that
                # session (e.g. it timed out unattended) - or so we assume.
                # The outage could just as easily have resolved itself
                # while this unattended session was running (WiFi/ISP came
                # back on its own with no one around to reconfigure
                # anything) - mqtt-bridge's bridge-state notification only
                # republishes on an actual change, so if it already
                # flipped back to connected while we were busy advertising,
                # nothing would otherwise tell us. Re-subscribing forces
                # mqtt-bridge to redeliver the *current* retained value, so
                # we check reality again instead of blindly re-arming the
                # clock and looping into another pointless advertising
                # window forever even after the device is actually fine.
                self.client.subscribe(self._bridge_state_topic, qos=1)
                time.sleep(2)
                if self._bridge_disconnected_since is not None:
                    self._bridge_disconnected_since = time.time()

    def _handle_redirect(self, json_payload: str) -> None:
        try:
            data = json.loads(json_payload)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid redirect JSON: {e}")
            return

        new_server = data.get('host') or data.get('server') or data.get('address')
        if not new_server:
            logger.error(f"Redirect message missing host/server/address field: {data}")
            return

        logger.info(f"Redirect received, moving bridge to {new_server}")
        self.bridge.apply_redirect(new_server)

    def _publish_device_info(self) -> None:
        if not self._connected:
            return
        compose_version = self.compose_manager.get_version()
        payload = {
            "tmpl": self.config.template_id,
            "ver": compose_version or "unknown",
            "build": time.strftime("%b %d %Y %H:%M:%S"),
            "type": self.config.template_id,
            "rxbuff": 1024
        }
        self.client.publish(TOPIC_INFO, json.dumps(payload), qos=1)
        logger.info(f"Published device info: {payload}")

    def _publish_system_info(self) -> None:
        """Static facts about the device, published once per connect (same
        as _publish_device_info). Originally sent via meta/FIELD, since
        semantically these describe what the device is rather than a
        live-varying value - switched to plain datastreams (ds/FIELD)
        after confirming with Blynk that dashboard widgets currently can't
        display metadata fields at all, only datastreams."""
        fields = {
            "AgentDeviceModel": _read_device_model(),
            "AgentOS": _read_os_pretty_name(),
            "AgentKernel": platform.release(),
            "AgentArchitecture": platform.machine(),
        }
        mem_total = _read_mem_total_mb()
        if mem_total is not None:
            fields["AgentTotalMemory"] = f"{mem_total:.0f} MB"
        disk_total = _read_disk_total_gb()
        if disk_total is not None:
            fields["AgentTotalDisk"] = f"{disk_total:.1f} GB"

        for name, value in fields.items():
            self.client.publish(f"ds/{name}", value, qos=1)
        logger.info(f"Published system info: {fields}")

    def _handle_diagnostics_enabled(self, payload: str) -> None:
        self.diagnostics_enabled = payload.strip() == "1"
        logger.info(f"Diagnostics reporting {'enabled' if self.diagnostics_enabled else 'disabled'}")

    def _publish_diagnostics(self) -> None:
        metrics = {
            "ds/AgentCPUUsage": _read_cpu_usage_percent(),
            "ds/AgentMemUsage": _read_mem_usage_percent(),
            "ds/AgentDiskUsage": _read_disk_usage_percent(),
            "ds/AgentTemperature": _read_temperature_c(),
        }
        for topic, value in metrics.items():
            if value is not None:
                self.client.publish(topic, f"{value:.1f}", qos=1)
        logger.debug(f"Published diagnostics: {metrics}")

    def _diagnostics_loop(self) -> None:
        while not self._shutting_down:
            time.sleep(DIAGNOSTICS_INTERVAL)
            if self.diagnostics_enabled and self._connected:
                self._publish_diagnostics()
            self._check_connectivity_watchdog()

    def _handle_terminal_enabled(self, payload: str) -> None:
        self.terminal_session_enabled = payload.strip() == "1"
        logger.warning(f"Terminal session {'enabled' if self.terminal_session_enabled else 'disabled'}")

    def _handle_terminal_command(self, command: str) -> None:
        if not self.terminal_session_enabled:
            self.client.publish("ds/AgentTerminal", "[terminal disabled]", qos=1)
            return
        # Runs in its own thread, not inline on the MQTT network thread -
        # a slow/hanging command shouldn't stall ping/reboot/OTA handling
        # for everyone else while it runs.
        threading.Thread(target=self._run_terminal_command, args=(command,), daemon=True).start()

    def _run_terminal_command(self, command: str) -> None:
        # Each command runs in its own thread (see _handle_terminal_command)
        # so a second command typed before the first finishes doesn't stall
        # behind it - but both would otherwise publish chunked output to
        # the same topic concurrently and interleave/garble on screen. The
        # lock only serializes execution+publishing, not the incoming
        # subscription, so a second command just waits its turn instead of
        # racing the first one's output.
        with self._terminal_lock:
            logger.warning(f"Terminal command executed via Blynk: {command!r}")
            try:
                result = subprocess.run(
                    # nsenter into PID 1's namespaces - pid: host already
                    # makes PID 1 here genuinely the host's real init, but
                    # this container's own mount/network namespaces are
                    # still its own. Without this, commands only ever see
                    # the agent's own isolated filesystem (e.g. `ls`
                    # showing /app's contents instead of the real host's).
                    ["nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid",
                     "--", "sh", "-c", command],
                    capture_output=True, text=True, timeout=TERMINAL_COMMAND_TIMEOUT,
                )
                output = (result.stdout + result.stderr).strip() or "(no output)"
            except subprocess.TimeoutExpired as e:
                # capture_output still populates these on the exception with
                # whatever the process had produced before being killed -
                # show it rather than discarding it, the command may have
                # gotten most of the way there. Despite text=True, CPython
                # leaves these as raw bytes on the TimeoutExpired path
                # specifically (only the normal-return path decodes them) -
                # confirmed on real hardware via `docker logs -f`, which
                # always hits this timeout since -f never terminates.
                def _decode(value):
                    return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
                partial = (_decode(e.stdout) + _decode(e.stderr)).strip()
                output = f"(timed out after {TERMINAL_COMMAND_TIMEOUT}s)"
                if partial:
                    output += f"\n{partial}"
            except Exception as e:
                output = f"(error: {e})"

            # Terminal widget messages are capped at 255 chars - chunk longer output.
            for i in range(0, len(output), 255):
                self.client.publish("ds/AgentTerminal", output[i:i + 255], qos=1)

    def run(self) -> None:
        def signal_handler(signum, frame):
            self._shutting_down = True
            self.client.disconnect()
            self.client.loop_stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        threading.Thread(target=self._diagnostics_loop, daemon=True).start()

        while not self._shutting_down:
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=KEEPALIVE)
                self.client.loop_forever()
            except Exception as e:
                if not self._shutting_down:
                    self._reconnect_count += 1
                    delay = min(RECONNECT_DELAY * (2 ** (self._reconnect_count - 1)), MAX_RECONNECT_DELAY)
                    logger.error(f"Connection error: {e}, retrying in {delay}s")
                    time.sleep(delay)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--apply-only":
        run_apply_only(sys.argv[2] if len(sys.argv) > 2 else "")
        return

    config = BlynkConfig()
    if not config.load():
        logger.error("Failed to load configuration. Exiting.")
        return

    compose_manager = ComposeManager()
    bridge = MqttBridge(config)

    if config.is_provisioned():
        bridge.ensure_current()
    else:
        logger.info("No auth token stored, starting BLE provisioning")
        if not ble_provisioning.provision(config, compose_manager, bridge):
            logger.error("BLE provisioning did not complete. Exiting.")
            return

    agent = BlynkAgent(config, compose_manager, bridge)
    agent.run()


if __name__ == "__main__":
    main()
