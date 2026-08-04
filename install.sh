#!/usr/bin/env bash
set -e

REPO_URL="https://github.com/anthony-blynk/blynk-agent-raspberry-pi.git"
REPO_DIR="$HOME/blynk-agent-raspberry-pi"
STATE_DIR=/opt/blynk

if ! command -v git >/dev/null 2>&1; then
  echo "Installing git..."
  sudo apt-get update && sudo apt-get install -y git
fi

if [ -d "$REPO_DIR/.git" ]; then
  echo "Updating existing checkout at $REPO_DIR"
  git -C "$REPO_DIR" pull
else
  echo "Cloning $REPO_URL to $REPO_DIR"
  git clone "$REPO_URL" "$REPO_DIR"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
  echo "Docker installed. Log out and back in (group membership needs a new session), then re-run:"
  echo "  curl -fsSL https://raw.githubusercontent.com/anthony-blynk/blynk-agent-raspberry-pi/master/install.sh | bash"
  exit 0
fi

sudo mkdir -p "$STATE_DIR/backups" "$STATE_DIR/mosquitto/conf.d"
sudo chown -R "$USER":"$USER" "$STATE_DIR"

if [ ! -f "$STATE_DIR/docker-compose.yml" ]; then
  cp "$REPO_DIR/docker-compose.yml" "$STATE_DIR/docker-compose.yml"
  echo "Installed docker-compose.yml to $STATE_DIR"
else
  echo "$STATE_DIR/docker-compose.yml already exists, leaving it alone"
fi

if [ ! -f "$STATE_DIR/blynk.env" ]; then
  # Piping this script through `curl | bash` connects stdin to the pipe, not
  # the keyboard - reading from /dev/tty explicitly is what makes these
  # prompts actually work rather than silently fail or hang.
  echo "Enter this device's Blynk credentials:"
  read -r -p "BLYNK_SERVER (e.g. lon1.blynk.cloud): " BLYNK_SERVER </dev/tty
  read -r -p "BLYNK_TEMPLATE_ID: " BLYNK_TEMPLATE_ID </dev/tty
  read -r -s -p "BLYNK_AUTH_TOKEN: " BLYNK_AUTH_TOKEN </dev/tty
  echo
  cat > "$STATE_DIR/blynk.env" <<EOF
BLYNK_SERVER=$BLYNK_SERVER
BLYNK_TEMPLATE_ID=$BLYNK_TEMPLATE_ID
BLYNK_AUTH_TOKEN=$BLYNK_AUTH_TOKEN
EOF
  echo "Wrote $STATE_DIR/blynk.env"
else
  echo "$STATE_DIR/blynk.env already exists, leaving it alone"
fi

echo "Building images..."
(cd "$REPO_DIR" && docker compose build)

echo "Starting stack..."
docker compose -f "$STATE_DIR/docker-compose.yml" up -d

echo "Done. Check status with: docker compose -f $STATE_DIR/docker-compose.yml ps"
