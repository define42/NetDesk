# NetDesk

NetDesk is a stateless Linux desktop that boots from the network into Weston,
with Chromium, FreeRDP, and a terminal. It uses Alpine Linux and runs entirely
from RAM. No installation, local disk, or persistent storage is required.

The build produces one deployable file: **`dist/netdesk.efi`**. This x86-64 UEFI
executable contains the Linux kernel, initramfs, complete root filesystem,
applications, drivers, and configuration. It can be loaded by UEFI iPXE or
UEFI HTTP Boot; it does not fetch a separate root filesystem at startup.

Version one opens the desktop automatically as the unprivileged `netdesk` user.
Chromium retains its sandbox. There is no login screen, local password, or remote
management service. The root account is locked. All settings, browser data,
downloads, and remote desktop credentials disappear when the machine reboots.
Authentication, centralized configuration, and user-specific access are future
work.

## Build

Use an x86-64 Linux host with Docker, Git, and Make:

```sh
make build
```

The build runs in an Alpine 3.24.1 container and writes `dist/netdesk.efi`, owned
by the invoking user. No privileged container or host package installation is
needed for the build. Allow several gigabytes of free RAM and disk space.
The base container is pinned by digest; packages receive updates from Alpine's
3.24 `main` and `community` repositories, so rebuilds can contain newer packages.

[GitHub Actions](.github/workflows/build.yml) runs on pushes, pull requests, and
manual dispatch. It checks the scripts, builds and verifies the image, boots it
over HTTP in a diskless UEFI VM, and uploads only `netdesk.efi` after success.
Download that file from the workflow's artifacts and place it on your boot server.

## Boot

Use an x86-64 UEFI machine with Secure Boot disabled, a supported wired network
adapter and graphics device, and sufficient RAM for the whole desktop plus its
applications. The VM smoke test uses 6 GiB; physical firmware memory requirements
vary. Legacy BIOS is not supported.

Serve the image at a direct HTTP URL with `Content-Type: application/efi`.
From **UEFI iPXE**:

```ipxe
dhcp
chain http://192.0.2.10/netdesk.efi
```

For **UEFI HTTP Boot**, configure the firmware boot URL or your DHCP HTTP Boot
policy to use that same URL. Replace the example address with your server.
See [boot design and server configuration](docs/boot-design.md) for the nginx
example, DHCP options, and firmware limitations.

After startup, use Weston's top panel to open Chromium, Remote Desktop, or the
terminal. Remote Desktop prompts for the server and username in a terminal;
FreeRDP handles the password and server certificate prompt. Reboot to reset the
session and receive an updated image.

## Customize

- [`config/packages.txt`](config/packages.txt): desktop applications, kernel,
  drivers, and selected firmware. Add packages from Alpine's enabled repositories.
- [`overlay/`](overlay/): configuration and scripts copied into the root filesystem.
- [`overlay/etc/xdg/weston/weston.ini`](overlay/etc/xdg/weston/weston.ini): panel,
  launchers, display settings, and US keyboard layout.
- [`config/cmdline`](config/cmdline): embedded kernel arguments, including serial
  console output for diagnostics.

Wired networking uses DHCP on `eth*` and `en*` interfaces. The default firmware
selection covers i915, AMDGPU, and Realtek Ethernet; support still depends on the
hardware generation and kernel. Add other firmware for your devices. Wi-Fi
provisioning and proprietary NVIDIA drivers are not configured in this version.

## Validate and diagnose

On Ubuntu, install `shellcheck`, `python3`, `qemu-system-x86`, and `ovmf`, then run:

```sh
make check
make verify
make smoke
```

`verify` inspects the EFI sections and embedded filesystem. `smoke` serves the
actual image over local HTTP and boots it with QEMU and UEFI firmware, without
attaching a disk. It requires Weston to answer a Wayland output query before
passing. KVM is used when available; otherwise QEMU uses software emulation.
Local logs and a screenshot are kept under `build/`, outside the deployable
output directory. `OVMF_CODE` and `OVMF_VARS` override firmware paths.

On a running desktop, compositor diagnostics are in
`/run/user/1000/weston.log` and session output is in
`/var/log/netdesk-session.log`. Those logs also disappear on reboot.
