#!/usr/bin/env bash
set -e

OWNER="anthony-blynk"
REPO="blynk-agent-raspberry-pi"
BRANCH="master"
RAW_BASE="https://raw.githubusercontent.com/$OWNER/$REPO/$BRANCH"
STATE_DIR=/opt/blynk

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
  echo "Docker installed. Log out and back in (group membership needs a new session), then re-run:"
  echo "  curl -fsSL $RAW_BASE/install.sh | bash"
  exit 0
fi

sudo mkdir -p "$STATE_DIR/backups" "$STATE_DIR/mosquitto/conf.d"
sudo chown -R "$USER":"$USER" "$STATE_DIR"

if [ ! -f "$STATE_DIR/docker-compose.yml" ]; then
  curl -fsSL "$RAW_BASE/docker-compose.yml" -o "$STATE_DIR/docker-compose.yml"
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

echo "Pulling images..."
if ! docker compose -f "$STATE_DIR/docker-compose.yml" pull; then
  echo "Pull failed, cloning repo to build locally instead..."
  REPO_DIR="$HOME/$REPO"
  if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" pull
  else
    if ! command -v git >/dev/null 2>&1; then
      sudo apt-get update && sudo apt-get install -y git
    fi
    git clone "https://github.com/$OWNER/$REPO.git" "$REPO_DIR"
  fi
  (cd "$REPO_DIR" && docker compose build)
fi

echo "Starting stack..."
docker compose -f "$STATE_DIR/docker-compose.yml" up -d

echo "Done. Check status with: docker compose -f $STATE_DIR/docker-compose.yml ps"
echo
echo "This only ever runs once - from here, updates go through Blynk OTA."
echo "When a new version is available, fetch the latest docker-compose.yml:"
echo "  $RAW_BASE/docker-compose.yml"
echo "and upload it through your Blynk console's OTA feature for this device."
echo "(If you've added your own service(s) to $STATE_DIR/docker-compose.yml, merge"
echo "the version/image changes into your copy rather than overwriting it wholesale.)"
