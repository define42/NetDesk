# NetDesk

NetDesk is a stateless Linux desktop that boots from the network into XFCE,
with Chromium, FreeRDP, LibreOffice, a file manager, and a terminal. It uses Alpine
Linux and runs entirely from RAM. No installation, local disk, or persistent
storage is required.

The build produces one deployable file: **`dist/netdesk.efi`**. This x86-64 UEFI
executable contains the Linux kernel, initramfs, complete root filesystem,
applications, drivers, and configuration. It can be loaded by UEFI iPXE or
UEFI HTTP Boot; it does not fetch a separate root filesystem at startup.

Version one opens the desktop automatically as the `netdesk` user.
Chromium retains its sandbox. There is no login screen, local password, or remote
management service. The root account is locked. All settings, browser data,
downloads, and remote desktop credentials disappear when the machine reboots.
Authentication, centralized configuration, and user-specific access are future
work.

The shared `netdesk` account can run administrative commands with passwordless
`sudo`. Applications normally run without root privileges; use `sudo` when an
administrative action is needed.

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

After startup, use XFCE's top panel to open Chromium, Remote Desktop, the
terminal, or Keyboard settings. The Applications menu provides the other desktop
applications and settings. Remote Desktop prompts for the server and username
in a terminal; FreeRDP handles the password and server certificate prompt. Reboot
to reset the session and receive an updated image.

LibreOffice is available under **Applications → Office** for documents,
spreadsheets, and presentations. You can also run `libreoffice` from Terminal.
Save documents to external storage or a remote service to keep them after reboot.

## Automatic root CA trust

Announce your root CA certificate's full URL in DHCP option 43 as plain ASCII
text, for example `http://192.0.2.10/certs/company-root.pem`. NetDesk downloads
the certificate from that URL. HTTP and HTTPS are supported, with normal
certificate verification for HTTPS. Serve a single self-signed CA certificate
in PEM or DER format; NetDesk installs it for the system (including FreeRDP) and
Chromium.

Supply option 43 to NetDesk's Linux DHCP client; its lease is separate from
firmware or iPXE. See
[CA installation](docs/boot-design.md#automatic-root-ca-installation) for details.
An absent or invalid URL, failed download, or invalid certificate leaves trust
unchanged and the desktop working. Later DHCP renewals retry the provided URL
until a root is installed. Trust lasts for the current boot and assumes a trusted
DHCP/boot network.

To disable this feature in a custom image, add `nohook netdesk-ca` to
[`overlay/etc/dhcpcd.conf`](overlay/etc/dhcpcd.conf).

## Choose a keyboard layout

Open **Keyboard** from the top panel, then select the **Layout** tab. Leave
**Use system defaults** disabled, select the current layout, and choose **Edit**
to select your keyboard layout and optional variant. Changes apply immediately
to the desktop. The default is English (US).

XFCE also provides controls for key repeat, shortcuts, keyboard model, and
additional layouts in this window. See the
[XFCE keyboard settings documentation](https://docs.xfce.org/xfce/xfce4-settings/4.20/keyboard).
Your selection lasts for the current boot; restarting NetDesk restores the
defaults embedded in the image.

## Audio volume

Click the speaker icon next to the clock to adjust the volume or mute audio.
You can also scroll over the icon or use your keyboard's volume and mute keys.
Choose **Audio mixer** from the speaker menu to open Volume Control, where you
can select output devices, adjust microphone levels, and change application
volumes. You can also launch it with `pavucontrol` from Terminal.

Audio uses PulseAudio in the desktop session and detects supported ALSA devices.
Volume and device choices reset when NetDesk reboots.

## Administration and power

Use **Power → Shut Down** or **Power → Restart** at the right end of the panel.
XFCE shows its standard confirmation dialog before ending the session. These
actions are also available from the Applications menu's logout dialog.

The `netdesk` account has passwordless sudo. For example, in Terminal:

```sh
sudo id
sudo shutdown -h now
sudo reboot
```

Vim and Nano are included. Open a file with `vim filename` or `nano filename`;
use `sudo vim` or `sudo nano` when editing system configuration.

`sudo poweroff` also shuts down, and `sudo shutdown -r now` restarts. NetDesk's
`shutdown` command supports immediate actions with `now` or `+0`; scheduled
shutdown is not implemented. Shutdown and reboot discard all session data.

## Customize

- [`config/packages.txt`](config/packages.txt): desktop applications, kernel,
  drivers, and selected firmware. Add packages from Alpine's enabled repositories.
- [`overlay/`](overlay/): configuration and scripts copied into the root filesystem.
- [`overlay/etc/xdg/xfce4/`](overlay/etc/xdg/xfce4/): XFCE panel, desktop, and
  keyboard defaults.
- [`config/cmdline`](config/cmdline): embedded kernel arguments, including serial
  console output for diagnostics.

Wired networking uses DHCP on `eth*` and `en*` interfaces and adopts the hostname
supplied by the DHCP server, falling back to `netdesk` when none is supplied.
The default firmware selection covers i915, AMDGPU, and Realtek Ethernet; support
still depends on the hardware generation and kernel. Add other firmware for your
devices. Wi-Fi provisioning and proprietary NVIDIA drivers are not configured in
this version.

## Validate and diagnose

On Ubuntu, install `shellcheck`, `python3`, `qemu-system-x86`, and `ovmf`, then run:

```sh
make check
make test-ca
make verify
make smoke
```

`test-ca` uses an isolated Docker container to check option 43 URL handling and
CA installation with HTTP, TLS, and Chromium's NSS trust database. It does not
change the host's trust stores.

`verify` inspects the EFI sections and embedded filesystem. `smoke` serves the
actual image over local HTTP and boots it with QEMU and UEFI firmware, without
attaching a disk. It requires XFCE's window manager and panel to be running and
Xorg to report an active display output before passing. KVM is used when
available; otherwise QEMU uses software emulation.
Local logs and a screenshot are kept under `build/`, outside the deployable
output directory. `OVMF_CODE` and `OVMF_VARS` override firmware paths.

On a running desktop, Xorg diagnostics are in
`/var/log/Xorg.0.log` and session output is in
`/var/log/netdesk-session.log`. After a successful CA installation,
`/run/netdesk-ca/installed` records the certificate's source URL. These files also
disappear on reboot.
