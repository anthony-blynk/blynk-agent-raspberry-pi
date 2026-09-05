# pi-image-builder

Builds a pre-baked image (Raspberry Pi 5 or Compute Module 4) with Docker, this project's stack, and a `blynk.env` already filled in (server + template ID + optional vendor prefix - **not** the auth token, which is the one genuinely per-device secret and still comes from the Blynk app during BLE provisioning). Flash it, boot it with no network at all, and it starts BLE-advertising immediately - no `install.sh`/SSH step needed.

Pi 5 and CM4 (wireless SKU, on the official CM4 IO Board, booting from onboard eMMC) are supported. [rpi-image-gen](https://github.com/raspberrypi/rpi-image-gen) (the tool this uses) has a separate "device layer" per Pi model, so adding others (e.g. Pi Zero 2 W) later is a config addition, not a redesign.

## Host requirements

Build this on **real arm64 Debian Bookworm/Trixie, or Raspberry Pi OS itself** - a spare Pi, or an arm64 Debian VM. That's `rpi-image-gen`'s own natively-supported host; x86_64 needs containers/QEMU and is the slower, less-supported path, not a faster one - don't assume a beefy x86 dev machine is the better choice here.

## Usage

1. Edit `config/template.env` directly and fill in `BLYNK_SERVER` and `BLYNK_TEMPLATE_ID` (and `BLYNK_VENDOR_PREFIX` if white-labeling) - `build.sh` reads this file in place, there's nothing to copy it to. These aren't secrets (no token here), so it's fine for this file to carry real values.
2. Run `./build.sh [device]` from this directory, where `device` is `rpi5` (default) or `cm4`, matching a `config/<device>.yaml` file. First run clones and pins `rpi-image-gen` into a gitignored directory (not vendored into this repo) and installs its host dependencies (needs `sudo` once) - subsequent runs skip both.
3. Flash the resulting image (`rpi-image-gen/work/.../*.img`) - for a Pi 5, `rpi-imager --cli` or the Raspberry Pi Imager GUI straight to an SD card; for a CM4, see "Flashing a CM4" below first.
4. Boot the device - it should start advertising as `Blynk Device-XXXX` over BLE with no network connection at all. Provision it via the Blynk app as normal.

## Flashing a CM4

The CM4 module boots from onboard eMMC, not a removable SD card, so it needs an extra step before you can flash it the same way:

1. `rpiboot` isn't reliably available as a distro package - clone and build it from source instead: `git clone https://github.com/raspberrypi/usbboot && cd usbboot && make` (needs `libusb-1.0-0-dev` and normal build tools).
2. Fit the CM4 IO Board's boot-mode jumper (labelled `nRPIBOOT` / `J2` / `JP2` depending on the board revision) to enable USB boot mode, connect a USB cable from the IO board's **USB slave/OTG port specifically** (not one of the 4 USB-A host ports) to your build/flash host, then power the board on.
3. From the `usbboot` checkout, run `sudo ./rpiboot -d mass-storage-gadget64` - the CM4's eMMC then enumerates as a normal block device (e.g. `/dev/sda`), same as an SD card. 
4. Flash it exactly like the Pi 5 image: `sudo rpi-imager --cli <path-to-.img> /dev/sda` (confirm the device name with `lsblk` first), or `sudo dd if=<path-to-.img> of=/dev/sda bs=4M status=progress conv=fsync && sync`.
5. Power off, remove the boot-mode jumper, power back on - it should boot normally and start BLE-advertising.

Confirmed build/boot/BLE-provisioning working on real hardware - see the Status section - though only on a **Lite (non-wireless) SKU with a USB Bluetooth dongle**, which needs the extra config in "CM4 Lite SKU + USB Bluetooth dongle" below. The wireless SKU's onboard chip path this config was originally written for is still unverified.

## CM4 Lite SKU + USB Bluetooth/WiFi dongles

A Lite (non-wireless) CM4 has no onboard BT/WiFi chip at all, so both need to come from USB adapters instead - confirmed working end-to-end (real BLE provisioning over a USB BT dongle, then real WiFi scan/connect over a *separate* USB WiFi dongle, both through the same single working USB port simultaneously via a plain hub) with a generic BCM20702A0 Bluetooth dongle and a Realtek RTL8188CUS-based WiFi dongle (Edimax EW-7811Un). Two `config.txt` changes are needed beyond the stock CM4 defaults, both confirmed necessary on real hardware:

```
[cm4]
#otg_mode=1
dtoverlay=dwc2,dr_mode=host
dtoverlay=disable-bt
```

- `otg_mode=1` switches the CM4's internal XHCI controller into host mode for the IO Board's 4 USB-A ports, which are wired through the module's PCIe lane to a separate on-board USB3 chip - if that PCIe link won't train (`brcm-pcie ...: link down`, `xhci-hcd ...: USB3 root hub has no ports` in `dmesg`, confirmed not fixable by reseating the module or forcing `PCIE_PROBE=1` via the EEPROM - see "Recovering CM4 EEPROM access" below), commenting it out and using `dtoverlay=dwc2,dr_mode=host` instead switches the CM4's *other*, PCIe-independent USB2 OTG controller into host mode. That's the same physical port used for `rpiboot` flashing above - plug the dongle in there, not into the (possibly non-functional) USB-A ports. USB2 is plenty for a BLE dongle.
- `dtoverlay=disable-bt` turns off the onboard-wireless-SKU device tree's phantom UART-attached Bluetooth interface, which this device layer includes unconditionally (it doesn't know your specific module lacks it) and which otherwise fails at boot with `Bluetooth: hci0: BCM: failed to write update baudrate (-110)` - harmless noise, but without disabling it, it claims the `hci0` slot and pushes the working USB dongle to `hci1`, and `ble_provisioning.py` hardcodes `ADAPTER_PATH = "/org/bluez/hci0"`.

No specific Broadcom firmware/patchram file was needed for the BCM20702A0 to work - `dmesg` reports `BCM: firmware Patch file not found` but the adapter still comes up `UP RUNNING` and fully functional regardless (its ROM firmware is apparently sufficient on its own for basic HCI operation).

The WiFi dongle needed one more thing the BT one didn't: an actual firmware package, `firmware-realtek` (`sudo apt install firmware-realtek`) - without it, `dmesg` shows `rtlwifi: Selected firmware is not available` / `Firmware is not ready to run!` and the device sits stuck in NetworkManager's `unavailable` state (not `disconnected`, which is what a genuinely working-but-unconnected radio shows). After installing it and reloading the driver (`sudo modprobe -r rtl8192cu && sudo modprobe rtl8192cu`) plus a `sudo systemctl restart NetworkManager` to clear its stale state, `nmcli device wifi list` returned a real scan (genuine signal strengths/channels, confirmed against `iw dev <iface> scan` directly) rather than the suspicious all-identical placeholder-looking entries NetworkManager showed beforehand. `ble_provisioning.py` needed no changes for this at all - unlike Bluetooth's hardcoded `hci0`, its WiFi code already looks up the NetworkManager device by *type*, not a specific interface name, so whatever the dongle enumerates as (`wlx...`, in this case) just works.

### Recovering CM4 EEPROM access

If you need to change the CM4's own bootloader/EEPROM config (e.g. to test `PCIE_PROBE=1`), `rpi-eeprom-config --apply` from within the running OS refuses on CM4(s) by design - it has to go through `rpiboot`'s recovery mode instead, same USB setup as flashing the eMMC:

```
cd usbboot/recovery
echo "PCIE_PROBE=1" >> boot.conf   # or whatever setting you're testing
./update-pieeprom.sh               # regenerates pieeprom.bin from boot.conf
# jumper + USB cable + power on, same as the flashing steps above
sudo ../rpiboot -d .
```

Confirm it actually applied after rebooting normally with `sudo rpi-eeprom-config` (should show your new setting) and `vcgencmd bootloader_version` (timestamp/version should have changed).

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
sudo chroot /mnt passwd pi
sudo chroot /mnt usermod -aG sudo pi   # confirmed on real hardware: pi has no sudo access by default
sudo umount /mnt
```

Put the card back in the Pi and boot normally - the password now works over the console or SSH. Confirmed on real hardware: `pi` isn't in the `sudo` group by default (only the `docker` group omission was previously documented here) - without the `usermod` line above, you can SSH in but can't run anything as root at all, which blocks most real debugging (`systemctl`, `dmesg` needing sudo on some setups, editing `/boot/firmware/config.txt`, etc.).

## Status: working end-to-end, confirmed on real hardware

Built, flashed, and boot-tested on a real Pi 5 with no network connection at all: it started BLE-advertising on its own, was provisioned via the Blynk app, connected to WiFi, and came up reachable over SSH - the `blynk-first-boot.service` loaded both pre-cached images and started the stack cleanly (`status=0/SUCCESS`), and both images show up correctly tagged in `docker images` (not `<none>`).

Every fix below came from testing against the actual tool and real hardware, not web research (which got several details wrong: the device layer name, the layer metadata field names, and the hook file structure).

Fixed along the way:
- Device layer is `rpi5`, not the `pi5` directory name it's defined in.
- Custom layers must live under a `layer/` subdirectory of the `-S` directory (mirroring `rpi-image-gen`'s own internal `device`/`image`/`layer` structure) - confirmed by testing, so `blynk-agent` lives at `layers/layer/blynk-agent/`, not `layers/blynk-agent/`.
- Layer metadata fields are `X-Env-Layer-Desc` (not `Description`) and require an `X-Env-Layer-Version`, with a `---` document separator before the `mmdebstrap:` section.
- Hooks are a flat `customize-hooks:` list (not nested under `hooks: customize:`), run **outside** the chroot with the target rootfs passed as `"$1"` (every path in a hook needs that prefix), and their working directory is `SRCROOT` (the `-S` value), not the individual layer's own subdirectory - hooks reference `"$SRCROOT/layer/blynk-agent/files/..."` accordingly.
- Docker comes from the `docker-debian-trixie` layer instead of guessed package names.
- `network-manager-iwd` is required, not optional - this project's whole BLE WiFi provisioning feature depends on NetworkManager's D-Bus API being present and running. Can't use the `trixie-minbase` suite layer directly for this: confirmed via its own source that it hard-requires `systemd-net-min`, which conflicts with NetworkManager (both declare themselves the system's `network-activator`) - each `config/<device>.yaml` reproduces `trixie-minbase`'s own layer list manually, swapping `systemd-net-min` for `network-manager-iwd`.
- `bluez` is required too - the `rpi5` device layer only *declares* Bluetooth hardware capability, it doesn't install/enable the actual BlueZ stack. Without it there's no `bluetoothd`/`org.bluez` D-Bus service, so nothing ever advertises over BLE.
- `skopeo copy`'s `docker-archive:` destination needs an explicit `:<image-ref>` suffix - without it, `docker load` imports the tarball untagged and `docker compose` can't find the image by name, falling back to a network pull (fails, no network) or `build: ./agent` (fails, no source shipped).
- This repo is checked out on Windows, so `docker-compose.yml` has CRLF line endings - stripping them (`tr -d '\r'`) is needed when extracting image references with `awk`, or the trailing `\r` corrupts the reference skopeo sees.
- `image-rpios`'s default `100%` auto-sizing root partition doesn't account for the pre-cached image tarballs (~300-400MB) - fixed with an explicit `root_part_size: 4G`.
- No console/SSH login is baked in on purpose - a shared password/key across every device built from the same image is exactly the kind of fleet-wide credential this should avoid. Instead, use the [Blynk Terminal widget](../README.md) once a device is provisioned: it runs commands `nsenter`'d into the host's own namespaces as root (see `agent/agent.py`'s `_run_terminal_command`), so you can set up SSH access per-device on demand, e.g. `echo 'pi:yourpassword' | chpasswd` or appending a key to `~pi/.ssh/authorized_keys` - only for the specific devices you actually need to reach.

Known benign cosmetic wrinkle: `dockerd` logs a `failed to validate image signature` / `expected image index descriptor, got application/vnd.docker.distribution.manifest.v2+json` error once per pre-cached image on first boot. This is the containerd image store's signature/provenance check expecting a multi-platform OCI image index, which a single-platform `skopeo`-produced tarball doesn't have - it doesn't block the load (both images load, tag, and run correctly) and isn't a real content-trust failure.

Also note: the interactive `pi` user is **not** added to the `docker` group by default (`device: user1... ` doesn't touch this; it's the `docker-debian-trixie` layer's own `docker_trust_user1` variable, left at its default `n`). Ad-hoc `docker` commands as `pi` need `sudo` - this doesn't affect the actual stack, since `blynk-first-boot.service` runs as root via systemd regardless. `pi` isn't in the `sudo` group either, by the way, so even that `sudo docker ...` won't work unless you've fixed it via the "Recovery" section above first - this is a device-layer default, not something specific to Docker.

CM4 support (`config/cm4.yaml`, device layer `rpi-cm4`) was added by porting the rpi5 config unchanged (same network/docker/bluetooth layers), and is now confirmed **build/boot/BLE-provisioning working end-to-end on real hardware** - but only on a Lite (non-wireless) SKU module with a USB Bluetooth dongle (see "CM4 Lite SKU + USB Bluetooth dongle" above for the extra config that needs adding); `bluez`/`network-manager-iwd`/`root_part_size: 4G` all carried over from rpi5 unchanged, no further fixes needed there. The wireless SKU's onboard chip path this config was originally written for is still unverified - no wireless-SKU module was available to test against. Also unresolved: PCIe never linked up on the test unit's official IO Board (`brcm-pcie ...: link down`, ruled out as a config issue - confirmed correct `otg_mode=1`, reseating the module, and forcing `PCIE_PROBE=1` via a real EEPROM reflash all made no difference), so its 4 USB-A ports never worked; whether that's a fault in that specific module or that specific board couldn't be determined without a swap test, and may not reproduce on other units.

Not yet done:
- No CI/automation for the build - it's a manual `./build.sh` on a real arm64 host for now.
- Other device layers (e.g. `zero2w`) beyond `rpi5` and `cm4` are a config addition, not a redesign, when needed.
