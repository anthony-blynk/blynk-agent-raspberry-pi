"""
Blynk Agent - runs as a plain local MQTT client against the mosquitto
container (no TLS, no cloud auth - that's handled entirely by mosquitto's
bridge connection to Blynk). Manages OTA updates to the stack's own
docker-compose.yml and reacts to a few other downlink control topics.
"""

import json
import logging
import time
import subprocess
import shutil
import signal
import sys
import os
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
from dotenv import dotenv_values
import yaml
import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

CONFIG_BASE = Path(os.getenv('BLYNK_CONFIG_DIR', '/opt/blynk'))
ENV_FILE = CONFIG_BASE / "blynk.env"
COMPOSE_FILE = CONFIG_BASE / "docker-compose.yml"
BACKUP_DIR = CONFIG_BASE / "backups"
BRIDGE_CONF_DIR = CONFIG_BASE / "mosquitto" / "conf.d"
BRIDGE_CONF_FILE = BRIDGE_CONF_DIR / "blynk-bridge.conf"
BRIDGE_HOST_OVERRIDE_FILE = CONFIG_BASE / "bridge_host_override"

MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

TOPIC_DOWNLINK = "downlink/#"
TOPIC_OTA = "downlink/ota/json"
TOPIC_PING = "downlink/ping"
TOPIC_REBOOT = "downlink/reboot"
TOPIC_REDIRECT = "downlink/redirect"
TOPIC_INFO = "info/mcu"

RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60
KEEPALIVE = 60

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
notifications false
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
    """Cloud identity (server/token/template) - used to render the mosquitto
    bridge config and to report device info, not for any MQTT connection
    the agent itself makes."""

    def __init__(self):
        self.server: Optional[str] = None
        self.auth_token: Optional[str] = None
        self.template_id: Optional[str] = None

    def load(self, env_path: Path = ENV_FILE) -> bool:
        values = dotenv_values(env_path) if env_path.exists() else {}
        self.server = values.get("BLYNK_SERVER") or os.getenv("BLYNK_SERVER")
        self.auth_token = values.get("BLYNK_AUTH_TOKEN") or os.getenv("BLYNK_AUTH_TOKEN")
        self.template_id = values.get("BLYNK_TEMPLATE_ID") or os.getenv("BLYNK_TEMPLATE_ID")

        if not all([self.server, self.auth_token, self.template_id]):
            logger.error("Missing BLYNK_SERVER / BLYNK_AUTH_TOKEN / BLYNK_TEMPLATE_ID")
            return False

        logger.info(f"Loaded configuration for server: {self.server}")
        return True

    def effective_server(self) -> str:
        """The bridge host in effect right now - a downlink/redirect
        overrides this until the agent is redeployed with a new blynk.env."""
        if BRIDGE_HOST_OVERRIDE_FILE.exists():
            override = BRIDGE_HOST_OVERRIDE_FILE.read_text().strip()
            if override:
                return override
        return self.server


class MosquittoBridge:
    """Renders the mosquitto bridge connection config and restarts the
    mosquitto service (via the docker-compose project) when it changes."""

    def __init__(self, config: BlynkConfig, compose_path: Path = COMPOSE_FILE):
        self.config = config
        self.compose_path = compose_path

    def ensure_current(self, server_override: Optional[str] = None) -> None:
        server = server_override or self.config.effective_server()
        rendered = BRIDGE_TEMPLATE.format(
            server=server,
            token=self.config.auth_token,
            template_id=self.config.template_id,
        )

        BRIDGE_CONF_DIR.mkdir(parents=True, exist_ok=True)
        if BRIDGE_CONF_FILE.exists() and BRIDGE_CONF_FILE.read_text() == rendered:
            return

        BRIDGE_CONF_FILE.write_text(rendered)
        logger.info(f"Bridge config updated for {server}, restarting mosquitto")
        self._restart_mosquitto()

    def apply_redirect(self, new_server: str) -> None:
        BRIDGE_HOST_OVERRIDE_FILE.write_text(new_server)
        self.ensure_current(server_override=new_server)

    def _restart_mosquitto(self) -> None:
        try:
            process = subprocess.run(
                ["docker", "compose", "-f", str(self.compose_path), "restart", "mosquitto"],
                capture_output=True, text=True, timeout=60,
            )
            if process.returncode != 0:
                logger.error(f"Failed to restart mosquitto: {process.stderr}")
        except Exception as e:
            logger.error(f"Failed to restart mosquitto: {e}")


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

            if self._run_docker_compose():
                return True

            logger.error("docker compose up failed, rolling back to previous version")
            if backup_path:
                shutil.copy2(backup_path, self.compose_path)
                self._run_docker_compose()
            return False

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


class BlynkAgent:
    """MQTT client against the local mosquitto broker only - no TLS, no
    cloud credentials here, those live entirely in the mosquitto bridge."""

    def __init__(self, config: BlynkConfig, compose_manager: ComposeManager, bridge: MosquittoBridge):
        self.config = config
        self.compose_manager = compose_manager
        self.bridge = bridge
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._connected = False
        self._reconnect_count = 0
        self._shutting_down = False
        self.last_cloud_contact: Optional[float] = None
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
            self._publish_device_info()
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
            logger.info("OTA update completed successfully")
            time.sleep(2)
            self._publish_device_info()
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

    def run(self) -> None:
        def signal_handler(signum, frame):
            self._shutting_down = True
            self.client.disconnect()
            self.client.loop_stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

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
    config = BlynkConfig()
    if not config.load():
        logger.error("Failed to load configuration. Exiting.")
        return

    compose_manager = ComposeManager()
    bridge = MosquittoBridge(config)
    bridge.ensure_current()

    agent = BlynkAgent(config, compose_manager, bridge)
    agent.run()


if __name__ == "__main__":
    main()
