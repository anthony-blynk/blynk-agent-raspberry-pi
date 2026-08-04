FROM alpine:3.20

# docker-cli + docker-cli-compose let the agent drive the HOST's Docker
# daemon (via the mounted socket) to apply OTA updates and restart mosquitto.
RUN apk add --no-cache \
    python3 \
    py3-pip \
    docker-cli \
    docker-cli-compose

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

COPY agent.py .

CMD ["python3", "agent.py"]
