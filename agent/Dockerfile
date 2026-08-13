FROM alpine:3.20

# docker-cli + docker-cli-compose let the agent drive the HOST's Docker
# daemon (via the mounted socket) to apply OTA updates and restart mosquitto.
# util-linux provides nsenter - the terminal feature (see
# TERMINAL_CAPABILITY_ENABLED in agent.py) uses it to join the host's PID
# 1 namespaces for real host-level command execution, not just this
# container's own isolated filesystem/network view.
RUN apk add --no-cache \
    python3 \
    py3-pip \
    docker-cli \
    docker-cli-compose \
    util-linux

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

COPY agent.py ble_provisioning.py ./

CMD ["python3", "agent.py"]
