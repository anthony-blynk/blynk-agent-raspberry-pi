# pi-image-builder

Builds a pre-baked Raspberry Pi 5 image with Docker, this project's stack, and a `blynk.env` already filled in (server + template ID + optional vendor prefix - **not** the auth token, which is the one genuinely per-device secret and still comes from the Blynk app during BLE provisioning). Flash it, boot it with no network at all, and it starts BLE-advertising immediately - no `install.sh`/SSH step needed.

Only Pi 5 is supported right now. [rpi-image-gen](https://github.com/raspberrypi/rpi-image-gen) (the tool this uses) has a separate "device layer" per Pi model, so adding others (e.g. Pi Zero 2 W) later is a config addition, not a redesign.

## Host requirements

Build this on **real arm64 Debian Bookworm/Trixie, or Raspberry Pi OS itself** - a spare Pi, or an arm64 Debian VM. That's `rpi-image-gen`'s own natively-supported host; x86_64 needs containers/QEMU and is the slower, less-supported path, not a faster one - don't assume a beefy x86 dev machine is the better choice here.

## Usage

1. Edit `config/template.env` directly and fill in `BLYNK_SERVER` and `BLYNK_TEMPLATE_ID` (and `BLYNK_VENDOR_PREFIX` if white-labeling) - `build.sh` reads this file in place, there's nothing to copy it to. These aren't secrets (no token here), so it's fine for this file to carry real values.
2. Run `./build.sh` from this directory. First run clones and pins `rpi-image-gen` into a gitignored directory (not vendored into this repo) and installs its host dependencies (needs `sudo` once) - subsequent runs skip both.
3. Flash the resulting image (`rpi-image-gen/work/.../*.img`) with `rpi-imager --cli` or the Raspberry Pi Imager GUI.
4. Boot the Pi 5 - it should start advertising as `Blynk Device-XXXX` over BLE with no network connection at all. Provision it via the Blynk app as normal.

## Creating a login via the Blynk Terminal

Once a device is provisioned and online, flip the `AgentTerminalEnabled` switch in the app and paste this into the Terminal widget - it creates a new sudo+docker-capable user with a password, in one command, no separate `passwd` prompt needed:

```
useradd -m -s /bin/bash -G sudo,docker -p "$(openssl passwd -6 'ReplaceWithAStrongPassword')" newuser
```

`openssl passwd -6` generates a proper SHA-512 crypt hash inline, so `useradd -p` can set the password directly - no interactive prompt, which the terminal can't handle anyway (see the "Recovery" note below on why `passwd` alone doesn't work here). Swap `newuser` and the password for whatever you want; add `docker` to `-G` only if you actually want passwordless `docker` CLI access from that login.

## Recovery: no password, no network, no terminal access

Since no console/SSH login is baked in (see below), the normal way to get a shell on a device is the Blynk Terminal widget - but that needs the device provisioned and online first. If you need to get in before that (e.g. debugging a device that never made it to BLE-advertising), pull the SD card and set a password directly from another Linux machine:

```
lsblk                          # find the card's root partition, e.g. /dev/sda2
sudo mount /dev/sda2 /mnt
sudo chroot /mnt passwd pi     # also handy here: usermod -aG docker pi, etc.
sudo umount /mnt
```

Put the card back in the Pi and boot normally - the password now works over the console or SSH.

## Status: working end-to-end, confirmed on real hardware

Built, flashed, and boot-tested on a real Pi 5 with no network connection at all: it started BLE-advertising on its own, was provisioned via the Blynk app, connected to WiFi, and came up reachable over SSH - the `blynk-first-boot.service` loaded both pre-cached images and started the stack cleanly (`status=0/SUCCESS`), and both images show up correctly tagged in `docker images` (not `<none>`).

Every fix below came from testing against the actual tool and real hardware, not web research (which got several details wrong: the device layer name, the layer metadata field names, and the hook file structure).

Fixed along the way:
- Device layer is `rpi5`, not the `pi5` directory name it's defined in.
- Custom layers must live under a `layer/` subdirectory of the `-S` directory (mirroring `rpi-image-gen`'s own internal `device`/`image`/`layer` structure) - confirmed by testing, so `blynk-agent` lives at `layers/layer/blynk-agent/`, not `layers/blynk-agent/`.
- Layer metadata fields are `X-Env-Layer-Desc` (not `Description`) and require an `X-Env-Layer-Version`, with a `---` document separator before the `mmdebstrap:` section.
- Hooks are a flat `customize-hooks:` list (not nested under `hooks: customize:`), run **outside** the chroot with the target rootfs passed as `"$1"` (every path in a hook needs that prefix), and their working directory is `SRCROOT` (the `-S` value), not the individual layer's own subdirectory - hooks reference `"$SRCROOT/layer/blynk-agent/files/..."` accordingly.
- Docker comes from the `docker-debian-trixie` layer instead of guessed package names.
- `network-manager-iwd` is required, not optional - this project's whole BLE WiFi provisioning feature depends on NetworkManager's D-Bus API being present and running. Can't use the `trixie-minbase` suite layer directly for this: confirmed via its own source that it hard-requires `systemd-net-min`, which conflicts with NetworkManager (both declare themselves the system's `network-activator`) - `config/blynk.yaml` reproduces `trixie-minbase`'s own layer list manually, swapping `systemd-net-min` for `network-manager-iwd`.
- `bluez` is required too - the `rpi5` device layer only *declares* Bluetooth hardware capability, it doesn't install/enable the actual BlueZ stack. Without it there's no `bluetoothd`/`org.bluez` D-Bus service, so nothing ever advertises over BLE.
- `skopeo copy`'s `docker-archive:` destination needs an explicit `:<image-ref>` suffix - without it, `docker load` imports the tarball untagged and `docker compose` can't find the image by name, falling back to a network pull (fails, no network) or `build: ./agent` (fails, no source shipped).
- This repo is checked out on Windows, so `docker-compose.yml` has CRLF line endings - stripping them (`tr -d '\r'`) is needed when extracting image references with `awk`, or the trailing `\r` corrupts the reference skopeo sees.
- `image-rpios`'s default `100%` auto-sizing root partition doesn't account for the pre-cached image tarballs (~300-400MB) - fixed with an explicit `root_part_size: 4G`.
- No console/SSH login is baked in on purpose - a shared password/key across every device built from the same image is exactly the kind of fleet-wide credential this should avoid. Instead, use the [Blynk Terminal widget](../README.md) once a device is provisioned: it runs commands `nsenter`'d into the host's own namespaces as root (see `agent/agent.py`'s `_run_terminal_command`), so you can set up SSH access per-device on demand, e.g. `echo 'pi:yourpassword' | chpasswd` or appending a key to `~pi/.ssh/authorized_keys` - only for the specific devices you actually need to reach.

Known benign cosmetic wrinkle: `dockerd` logs a `failed to validate image signature` / `expected image index descriptor, got application/vnd.docker.distribution.manifest.v2+json` error once per pre-cached image on first boot. This is the containerd image store's signature/provenance check expecting a multi-platform OCI image index, which a single-platform `skopeo`-produced tarball doesn't have - it doesn't block the load (both images load, tag, and run correctly) and isn't a real content-trust failure.

Also note: the interactive `pi` user is **not** added to the `docker` group by default (`device: user1... ` doesn't touch this; it's the `docker-debian-trixie` layer's own `docker_trust_user1` variable, left at its default `n`). Ad-hoc `docker` commands as `pi` need `sudo` - this doesn't affect the actual stack, since `blynk-first-boot.service` runs as root via systemd regardless.

Not yet done:
- No CI/automation for the build - it's a manual `./build.sh` on a real arm64 host for now.
- Only `rpi5` is supported; other device layers (e.g. `zero2w`) are a config addition, not a redesign, when needed.
