# TODO

Ideas worth revisiting, not yet planned or scheduled.

## Cellular modem support (BLE provisioning)

Extend BLE provisioning to support a cellular interface, alongside the WiFi/Ethernet support added in v1.7.x. The protocol already has fields for this (`docs/blynk-ble-provisioning-protocol.md`'s `cell` interface: `imei`/`imsi`/`iccid`/`pin`/`apn`, plus `sim_missing`/`sim_locked`/`sim_wrong_pin` failure reasons) - nothing in `agent/ble_provisioning.py` implements it yet.

Real use case: domestic heat pump installers want a device to come online over cellular immediately at install time in a new-build property (no WiFi configured yet), then get switched to WiFi later once occupants move in and set up their own network. The existing multi-interface picker already supports this handover naturally - it's just a second provisioning pass choosing `wifi` instead of `cell`, no special-case design needed.

Implementation-wise this is a separate integration, not a tweak to the NetworkManager WiFi code: NetworkManager only activates a cellular connection, but the IMEI/IMSI/ICCID/SIM-lock details come from a different service, ModemManager, over its own D-Bus API (`org.freedesktop.ModemManager1`). Should be genuinely portable across devices in principle (ModemManager standardizes across modem chipsets via a plugin architecture, the same way NetworkManager did for WiFi), but that needs confirming on real hardware rather than assumed - check whether ModemManager is installed/enabled and whether the specific modem is supported, the same way NetworkManager-only scope was verified across three device families before being relied on for WiFi. Test hardware: the CompuLab IOT-GATE-iMX8 has a cellular modem; needs a real SIM with a data plan to test properly.

## Blynk HTTP file-upload proxy in agent.py

Give `agent.py` a generic, app-agnostic local topic (e.g. `local/blynk/upload`) that proxies to Blynk's HTTP-only Device API - starting with [file upload](https://docs.blynk.io/en/blynk.cloud/device-https-api/upload-a-file) (`POST /external/api/upload?token=...`, multipart field `upfile`, 5MB/file, 10 files held per device). A local app publishes raw bytes to that topic; the agent (which already holds the token, to render the mqtt-bridge config) makes the authenticated HTTP call and publishes the resulting URL back on a reply topic (e.g. `local/blynk/upload/result`).

Why: MQTT's device API has no file-upload capability, only the HTTPS API does - which needs the raw token. The project's whole security model is that only mqtt-bridge (and now, by extension, the agent) ever holds the token; local apps never do. Came up while discussing a camera-detection demo that wanted to push captured frames to Blynk (see `examples/camera-detector/`).

Keep the proxy itself ignorant of what's being uploaded or why - same generic-infrastructure principle as the existing OTA/ping/reboot/redirect handling, or how mqtt-bridge bridges `ds/#` traffic without caring what a datastream means. Don't bake in app-specific assumptions (e.g. camera-frame-specific topics/logic) the way an earlier version of this idea wrongly did.

## OTA rollback: post-start health check, not just apply-failure

`ComposeManager`/`run_apply_only` in `agent.py` already roll back to the last backed-up `docker-compose.yml` when `docker compose up -d` itself fails (non-zero exit - bad image ref, compose syntax error, etc.) - this part is done, not a gap. What's missing is the case where `up -d` succeeds but the new container then crash-loops or never becomes healthy afterward - nothing currently watches for that, so a device can end up "successfully applied" an OTA that's actually broken. Every comparable fleet-management system that came up while researching this (balena's post-boot health-check rollback on top of its unbootable-detection; Home Assistant OS's RAUC boot-attempt-counted fallback; AWS Greengrass's `FailureHandlingPolicy: ROLLBACK`, which explicitly covers a component that fails to report healthy, not just one that fails to start) treats this as a baseline expectation.

Feasible pattern: after `_run_docker_compose()` reports success, watch the affected container(s) for N seconds (e.g. poll `docker inspect`'s restart count / running state) - if it crash-loops or exits repeatedly in that window, treat it the same as an apply failure and roll back via the existing backup-restore path in `run_apply_only`. Extends already-existing OTA-handling logic in `agent.py`, doesn't need new infrastructure.

## Diagnostic/support-bundle upload command

A single MQTT-triggered command (e.g. a new downlink topic) that gathers `docker logs`, `docker ps`, and `systemctl status` for the stack's units, then uploads the result somewhere reachable for support purposes - instead of walking a user through the terminal manually every time. Directly depends on the **Blynk HTTP file-upload proxy** idea above (same upload path, generic infrastructure). Precedent: Azure IoT Edge's `UploadSupportBundle` direct method and Memfault's auto-collected coredumps/custom data recordings both solve the same "get diagnostics off a misbehaving device without an interactive session" problem.
