# Boot image design

NetDesk builds `dist/netdesk.efi`, an x86_64 Unified Kernel Image (UKI): a PE/COFF
EFI executable containing the EFI stub, Linux kernel, boot arguments, and a
compressed initramfs holding the complete Alpine root filesystem. `ukify`
assembles the image and handles PE alignment. The stub runs before Linux and
does not require systemd userspace; NetDesk uses BusyBox init and OpenRC. See the
[UKI specification](https://uapi-group.org/specifications/specs/unified_kernel_image/).

The `.linux` section holds Alpine's `linux-lts` kernel; `.initrd` holds the
compressed `newc` cpio filesystem; `.cmdline`, `.osrel`, and `.uname` hold the boot
arguments, release identity, and kernel version matching the packaged modules.

## Startup and state

1. Firmware or iPXE downloads the EFI image into memory and executes it.
2. The EFI stub passes the embedded kernel and initramfs to Linux.
3. Linux unpacks the filesystem into RAM and runs `/init` as PID 1.
4. `/init` gives the same RAM filesystem a parent mount, mounts the kernel
   interfaces, and hands control to `/sbin/init`.
5. OpenRC starts devices, networking, Xorg, and the XFCE desktop automatically.

The initial root filesystem is the running system. There is no second root image
to download and no root partition to discover. A bind mount, move mount, and
`chroot` align the process root with the mounted root before normal startup.
This lets sandboxed GTK image loaders switch roots inside their private mount
namespaces. It does not copy the filesystem or introduce disk storage. The Linux
[initramfs documentation](https://www.kernel.org/doc/html/latest/filesystems/ramfs-rootfs-initramfs.html)
describes this complete-root-filesystem model.

The desktop runs XFCE on Xorg. OpenRC supervises `netdesk-xserver`, which starts
Xorg through `xinit` with a fresh Xauthority cookie and TCP access disabled. Xorg
owns the display devices as root; `netdesk-session` drops to UID 1000 before
starting the session D-Bus and `startxfce4`. There is no display manager or login
screen. Chromium and desktop applications run as the unprivileged `netdesk` user.

The `netdesk` account has passwordless sudo for administrative commands. The
build validates the root-owned sudoers policy and checks elevation as that
account. XFCE's native power actions use its packaged shutdown helper through
polkit, with authorization limited to the `netdesk` user and that helper's
action. Alpine's helper invokes BusyBox `poweroff` or `reboot`, which ask PID 1
to run the OpenRC shutdown sequence. No alternate init system is needed.

Changes live in memory and disappear on reboot. Applications still need network
connectivity to reach websites and remote desktops; the desktop software itself
is already in the EFI image. XFCE's native Keyboard settings change layouts and
variants during the session; reboot restores the image's default English (US)
layout. Version one starts without authentication.

## Serve the image

Copy `netdesk.efi` from the build artifact to an HTTP server. Use a direct
URL without an interactive login page. For UEFI HTTP Boot, configure the server
to respond with `Content-Type: application/efi`. TianoCore identifies executable
images by this media type; relying on an arbitrary default content type reduces
firmware compatibility. See
[TianoCore HTTP Boot documentation](https://www.tianocore.org/tianocore-wiki.github.io/platforms-packages/component-guides/http_boot.html).

For example, inside an existing nginx server block:
```nginx
location = /netdesk.efi {
    root /srv/netboot;
    types { }
    default_type application/efi;
}
```

Replace the image atomically to prevent partial downloads. Updates apply on reboot.

### iPXE

Run x86_64 UEFI iPXE. At its command prompt:

```ipxe
dhcp
chain http://192.0.2.10/netdesk.efi
```

These commands also work in a script prefixed with `#!ipxe`. That script is
optional server configuration. The [iPXE `chain` command](https://ipxe.org/cmd/chain)
downloads and executes the image. Legacy BIOS iPXE cannot execute a UKI.

### UEFI HTTP Boot

On firmware that offers an HTTP Boot URL field, point it directly at
`http://192.0.2.10/netdesk.efi` and select that boot entry. For managed
networks, scope the DHCP response to HTTP Boot clients: use the `HTTPClient`
vendor class and supply the full boot URI in DHCPv4 option 67 (or DHCPv6 bootfile
URL option 59). Keep PXE and HTTP Boot client policies separate. The fields are
documented in
[TianoCore's URI configuration guide](https://www.tianocore.org/tianocore-wiki.github.io/platforms-packages/component-guides/http_boot.html).

HTTPS depends on the firmware or iPXE build and its trust configuration. Some
firmware disables plain HTTP by default. Validate the intended transport and
certificate configuration on the target fleet.

## Compatibility and limitations

- **RAM capacity:** the complete unpacked system remains in RAM. Boot also needs
  space for the downloaded EFI image, temporary copies, decompression, and the
  kernel. Installed RAM requirements therefore exceed the compressed image
  size; browser workloads need additional memory.
- **Firmware limits:** large network-loaded executables can expose firmware
  allocation limits or bugs. A successful virtual-machine test does not prove
  every device's HTTP Boot implementation can load the same image.
- **Device support:** include the kernel modules, firmware, and graphics
  userspace required by the target hardware in the package configuration.
- **Secure Boot:** the initial build is unsigned and requires Secure Boot to be
  disabled. Future signing must cover the final EFI image, with its signing key
  trusted by the boot chain. Embedding the components in a UKI makes that possible;
  it does not by itself make the image trusted. See the upstream
  [EFI stub documentation](https://raw.githubusercontent.com/systemd/systemd/v260/man/systemd-stub.xml).
- **Access:** physical access permits entering the shared desktop. Authentication,
  centrally managed configuration, and user-specific access are future additions.

The Alpine 3.24.1 builder image is pinned by digest; package updates float within
the 3.24 branch. Rebuild to incorporate fixes. Builds are not guaranteed to be
byte-for-byte reproducible. Upgrade branches while the community repository is
supported; see the [Alpine release policy](https://www.alpinelinux.org/releases/).
