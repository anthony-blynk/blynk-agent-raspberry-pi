# Blynk Agent for Raspberry Pi

Turns a Raspberry Pi into a Blynk device. A local MQTT broker acts as a bridge to Blynk Cloud and handles the connection complexity — auth, certificates, all of it — so every other service on the Pi just speaks plain local MQTT, while the whole stack stays updatable over the air through Blynk OTA.

## Install

Get up and running on a fresh Pi with a single command:

```
curl -fsSL https://raw.githubusercontent.com/anthony-blynk/blynk-agent-raspberry-pi/master/install.sh | bash
```

Installs Docker if needed, prompts for this device's Blynk server/template/auth token, and starts the stack. This only needs to run once per Pi — see [Updating](#updating).

## How it works

```mermaid
flowchart LR
    console(["Blynk Console / App"])
    cloud(["Blynk Cloud"])

    subgraph pi["Raspberry Pi"]
        subgraph compose["docker compose"]
            mosquitto["mosquitto<br/>local broker :1883"]
            agent["agent<br/>OTA / ping / reboot / redirect"]
            subgraph apps["your app(s) - optional"]
                app1["app 1"]
                app2["app 2"]
                appdots["..."]
            end
        end
        local(["local scripts<br/>e.g. test/*.py"])
    end

    console <--> cloud
    cloud <-->|bridge: TLS, mqttv5| mosquitto
    agent <--> mosquitto
    app1 -.-> mosquitto
    app2 -.-> mosquitto
    local <--> mosquitto

    style compose fill:#dbe9ff,stroke:#5b8def,color:#1a1a1a
```

- **mosquitto** and **agent** both run as Docker containers, managed by the same `docker-compose.yml` the agent OTA-updates.
- **mosquitto** bridges the local broker to Blynk Cloud. Only mosquitto holds the Blynk auth token (for that cloud bridge connection) - anything else on the Pi just connects to the local broker on plain, unauthenticated MQTT. Your own apps never need to know about Blynk credentials at all.
- That local broker is only reachable on the Pi itself (`127.0.0.1:1883`) - nothing outside the Pi can connect to it.
- **agent** subscribes to Blynk's downlink control topics: `downlink/ota/json` (downloads, validates, and applies a new `docker-compose.yml`, with automatic rollback on failure), `downlink/ping`, `downlink/reboot`, and `downlink/redirect`.
- You can add your own service(s) to `docker-compose.yml` alongside mosquitto and agent, and/or just run your own programs directly on the Pi (outside Docker) - either way, they talk to the local broker, which is already bridged to Blynk. See `test/` for minimal pub/sub examples.
- Blynk's own topics (`ds/#`, `downlink/#`, etc. - see [the MQTT API docs](https://docs.blynk.io/en/blynk.cloud-mqtt-api/device-mqtt-api/topic-structure)) are what actually reach Blynk Cloud through the bridge. Your apps are free to use any other topics on the local broker too - those just stay local and never interact with Blynk at all.

## Updating

Updates go through Blynk OTA, not by re-running `install.sh`. Grab the latest [`docker-compose.yml`](docker-compose.yml) (merging in your own additions if you've customized it) and upload it through your Blynk console's OTA feature for that device.

## Troubleshooting

### BLE provisioning: device won't advertise / registering the advertisement fails

Raspberry Pi kernels around `6.18.34` have a Bluetooth regression that breaks BLE advertising entirely - the agent logs `Failed to register advertisement`, and `sudo btmon` or `sudo journalctl -u bluetooth` shows `Invalid Parameters (0x0d)` on `Add Extended Advertising Data`. Confirmed and fixed upstream: [raspberrypi/linux#7473](https://github.com/raspberrypi/linux/issues/7473), fix commit `58d810354de1b`, first shipped in Linux **6.18.36**.

- Check `apt-cache policy raspberrypi-kernel` (or `linux-image-*`) - if a version ≥6.18.36 is offered, a normal `sudo apt upgrade` fixes this and nothing else below is needed.
- If a fixed version isn't in apt yet, pull it directly:
  ```
  sudo rpi-update
  sudo reboot
  ```
- To instead roll back to the exact kernel this project was tested against while waiting for a fixed release (`6.12.47+rpt`, confirmed working):
  ```
  sudo rpi-update 6d1da66a7b1358c9cd324286239f37203b7ce25c
  sudo reboot
  ```
  That commit is from [raspberrypi/rpi-firmware](https://github.com/raspberrypi/rpi-firmware), not `raspberrypi/firmware` - the two repos look similar but only `rpi-firmware` commits work with `rpi-update`. After rolling back, hold the broken kernel packages so a routine `apt upgrade` doesn't reintroduce the bug (check `apt list --installed | grep linux-image` for your exact package names first):
  ```
  sudo apt-mark hold linux-image-6.18.34+rpt-rpi-2712 linux-image-6.18.34+rpt-rpi-v8 linux-image-rpi-2712 linux-image-rpi-v8
  ```
