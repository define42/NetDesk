#!/bin/sh
# Runs inside the Alpine builder, never directly on the host.
set -eu
# Alpine's BusyBox ash supports pipefail; a failed cpio must fail the build.
# shellcheck disable=SC3040
set -o pipefail
umask 022
export LC_ALL=C

if [ ! -f /etc/alpine-release ] || [ ! -d /out ] || [ ! -d /src/overlay ]; then
    echo 'Run make build to invoke this script in its container.' >&2
    exit 1
fi
: "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH must be set}"
work=$(mktemp -d /tmp/netdesk.XXXXXXXX)
trap 'rm -rf "$work"' EXIT HUP INT TERM
root=$work/rootfs
mkdir -p "$root/etc/apk/keys" "$root/dev" "$root/proc" "$root/sys" "$root/run"
cp /etc/apk/keys/* "$root/etc/apk/keys/"
cp /etc/apk/repositories "$root/etc/apk/repositories"
# apk executes package scripts in the new root. DNS is needed only while building.
cp /etc/resolv.conf "$root/etc/resolv.conf"
mknod -m 600 "$root/dev/console" c 5 1
mknod -m 666 "$root/dev/null" c 1 3
# NSS needs the random devices when initializing Chromium's database in chroot.
mknod -m 666 "$root/dev/random" c 1 8
mknod -m 666 "$root/dev/urandom" c 1 9

set --
while IFS= read -r line || [ -n "$line" ]; do
    package=${line%%#*}
    package=$(printf '%s' "$package" | tr -d '[:space:]')
    [ -n "$package" ] || continue
    case "$package" in
        -*|*[!a-zA-Z0-9._+-]*) echo "Invalid package: $package" >&2; exit 1 ;;
    esac
    set -- "$@" "$package"
done < /src/config/packages.txt
apk --root "$root" --initdb --no-cache add "$@"

chroot "$root" addgroup -g 1000 netdesk
chroot "$root" adduser -D -u 1000 -G netdesk -s /bin/sh netdesk
for group in audio video; do
    chroot "$root" addgroup netdesk "$group"
done
chroot "$root" passwd -l root
# adduser -D already creates a locked netdesk password.
cp -a --no-preserve=ownership /src/overlay/. "$root/"
# Package triggers ran before the overlay added its MIME types and launchers.
chroot "$root" update-mime-database /usr/share/mime
chroot "$root" update-desktop-database /usr/share/applications
chmod 0755 "$root/init" "$root"/etc/init.d/netdesk-* "$root"/usr/local/bin/netdesk-*
chmod 0755 "$root/usr/local/sbin/shutdown"
chmod 0755 "$root/usr/local/sbin/netdesk-install-ca"
chmod 0644 "$root/usr/lib/dhcpcd/dhcpcd-hooks/90-netdesk-ca"
chmod 0440 "$root/etc/sudoers.d/netdesk"
chmod 0644 "$root/etc/polkit-1/rules.d/49-netdesk-power.rules"
chroot "$root" visudo -c
# Verify passwordless elevation for the same locked account used by the desktop.
[ "$(chroot "$root" su-exec netdesk sudo -n /usr/bin/id -u)" = 0 ]
chroot "$root" chown -R netdesk:netdesk /home/netdesk
# Select Chromium's supported legacy NSS path before the browser can start.
# This also supports leases arriving after the desktop is already running.
chroot "$root" su-exec netdesk:netdesk mkdir -p /home/netdesk/.pki/nssdb
chroot "$root" su-exec netdesk:netdesk certutil -N --empty-password \
    -d sql:/home/netdesk/.pki/nssdb

# Explicit runlevels avoid Alpine disk mounts, swap and login prompts.
rm -rf "$root/etc/runlevels"
mkdir -p "$root/etc/runlevels/sysinit" "$root/etc/runlevels/boot" \
    "$root/etc/runlevels/default" "$root/etc/runlevels/shutdown"
for service in devfs dmesg udev udev-trigger udev-settle; do
    chroot "$root" rc-update add "$service" sysinit
done
for service in hostname modules sysctl bootmisc localmount loopback; do
    chroot "$root" rc-update add "$service" boot
done
for service in dbus dhcpcd netdesk-desktop; do
    chroot "$root" rc-update add "$service" default
done
chroot "$root" rc-update add killprocs shutdown

set -- "$root"/lib/modules/*
if [ "$#" -ne 1 ] || [ ! -d "$1" ]; then
    echo 'Expected one kernel module tree.' >&2
    exit 1
fi
kernel_version=${1##*/}
chroot "$root" depmod "$kernel_version"
cp "$root/boot/vmlinuz-lts" "$work/vmlinuz"
# The kernel is a separate PE section; do not embed a second copy or mkinitfs output.
rm -rf "${root:?}/boot" "$root/var/cache/apk" "$root/var/log"/* "$root/tmp"/*
rm -f "$root/lib/modules/$kernel_version/vmlinuz"
mkdir -p "$root/boot" "$root/var/cache/apk" "$root/tmp"
chmod 1777 "$root/tmp"
: > "$root/etc/resolv.conf"
rm -f "$root/etc/machine-id" "$root/var/lib/dbus/machine-id" "$root/etc/.pwd.lock"
chroot "$root" apk list --installed > "$root/etc/netdesk-packages.txt"

printf 'NetDesk root filesystem: '
du -sh "$root"
find "$root" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
(
    cd "$root"
    find . -print0 | sort -z | cpio --null --create --format=newc --reproducible --quiet | gzip -n -6
) > "$work/rootfs.cpio.gz"
ukify build --linux "$work/vmlinuz" --initrd "$work/rootfs.cpio.gz" \
    --cmdline @/src/config/cmdline --os-release "@$root/etc/os-release" \
    --uname "$kernel_version" --efi-arch x64 \
    --stub /usr/lib/systemd/boot/efi/linuxx64.efi.stub \
    --output "$work/netdesk.efi"
python3 /src/scripts/verify-image.py "$work/netdesk.efi"
install -m 0644 "$work/netdesk.efi" /out/.netdesk.efi.tmp
chown "${OUTPUT_UID:-0}:${OUTPUT_GID:-0}" /out/.netdesk.efi.tmp
mv /out/.netdesk.efi.tmp /out/netdesk.efi
printf 'Built '
ls -lh /out/netdesk.efi
