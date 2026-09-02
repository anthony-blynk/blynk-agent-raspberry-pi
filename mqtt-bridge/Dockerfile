FROM eclipse-mosquitto:2

# Baked in rather than bind-mounted from the host, so the bridge's CA bundle
# doesn't depend on a specific host OS layout (e.g. Raspberry Pi OS vs.
# Docker Desktop's own VM) - same path (/etc/ssl/certs/ca-certificates.crt)
# either way, since eclipse-mosquitto is alpine-based too.
RUN apk add --no-cache ca-certificates

COPY mosquitto.conf /mosquitto/config/mosquitto.conf
