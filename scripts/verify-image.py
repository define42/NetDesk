#!/usr/bin/env python3
"""Check the EFI container and stream its embedded desktop filesystem."""

import argparse
from dataclasses import dataclass
import gzip
import io
from pathlib import Path
import posixpath
import re
import shlex
import stat
import struct
import sys


CHUNK_SIZE = 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_exact(stream, size):
    data = stream.read(size)
    require(len(data) == size, "truncated image or initramfs")
    return data


@dataclass
class Section:
    offset: int
    size: int


@dataclass
class Entry:
    mode: int
    uid: int
    gid: int
    size: int
    device_major: int
    device_minor: int
    target: str = ""


class SectionReader(io.RawIOBase):
    """Expose only one PE section to gzip without copying it into memory."""

    def __init__(self, source, section):
        self.source = source
        self.remaining = section.size
        source.seek(section.offset)

    def readable(self):
        return True

    def read(self, size=-1):
        if size < 0:
            size = self.remaining
        data = self.source.read(min(size, self.remaining))
        self.remaining -= len(data)
        return data


def pe_sections(image, image_size):
    header = read_exact(image, 64)
    require(header[:2] == b"MZ", "image has no DOS/EFI header")
    pe_offset = struct.unpack_from("<I", header, 60)[0]
    require(pe_offset + 24 <= image_size, "PE header lies outside image")
    image.seek(pe_offset)
    header = read_exact(image, 24)
    require(header[:4] == b"PE\0\0", "image has no PE signature")
    machine, count, _, _, _, optional_size, _ = struct.unpack("<HHIIIHH", header[4:])
    require(machine == 0x8664, "image is not an AMD64 EFI executable")
    require(0 < count <= 96 and optional_size >= 70, "invalid PE header dimensions")
    optional = read_exact(image, optional_size)
    require(struct.unpack_from("<H", optional)[0] == 0x20B, "image is not PE32+")
    require(struct.unpack_from("<H", optional, 68)[0] == 10, "image is not an EFI application")

    sections = {}
    for _ in range(count):
        header = read_exact(image, 40)
        name = header[:8].rstrip(b"\0").decode("ascii")
        virtual_size, _, raw_size, offset = struct.unpack_from("<IIII", header, 8)
        require(offset + raw_size <= image_size, f"{name} section lies outside image")
        require(name not in sections, f"duplicate PE section {name}")
        sections[name] = Section(offset, min(virtual_size, raw_size))
        if name in {".linux", ".initrd", ".osrel", ".cmdline", ".uname"}:
            require(0 < virtual_size <= raw_size, f"invalid {name} section size")

    for name in (".linux", ".initrd", ".osrel", ".cmdline", ".uname"):
        require(name in sections and sections[name].size > 0, f"missing {name} section")
    return sections


def section_text(image, section):
    require(section.size <= 65536, "unexpectedly large metadata section")
    image.seek(section.offset)
    return read_exact(image, section.size).rstrip(b"\0\n").decode("utf-8")


def read_archive(stream):
    entries = {}
    contents = {}
    capture = {"init", "etc/passwd", "etc/inittab"}
    while True:
        header = read_exact(stream, 110)
        require(header[:6] == b"070701", "initramfs must use the newc cpio format")
        fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
        _, mode, uid, gid, _, _, size, _, _, major, minor, name_size, _ = fields
        require(0 < name_size <= 4096, "invalid cpio path length")
        raw_name = read_exact(stream, name_size)
        require(raw_name.endswith(b"\0"), "unterminated cpio path")
        name = raw_name[:-1].decode("utf-8")
        read_exact(stream, -(110 + name_size) % 4)
        if name == "TRAILER!!!":
            require(size == 0, "invalid cpio trailer")
            break

        name = posixpath.normpath(name)
        require(not name.startswith(("/", "../")) and name != "..", "invalid cpio path")
        require(name not in entries, f"duplicate cpio entry {name}")
        entry = Entry(mode, uid, gid, size, major, minor)
        entries[name] = entry
        if name in capture or stat.S_ISLNK(mode):
            require(size <= CHUNK_SIZE, f"unexpectedly large config or symlink: {name}")
            content = read_exact(stream, size).decode("utf-8")
            if stat.S_ISLNK(mode):
                entry.target = content
            else:
                contents[name] = content
        else:
            remaining = size
            while remaining:
                remaining -= len(read_exact(stream, min(remaining, CHUNK_SIZE)))
        read_exact(stream, -size % 4)

    # Reading to EOF validates gzip's CRC and length trailer, including padding
    # after TRAILER!!!. Stopping at the cpio trailer would miss corrupt gzip data.
    while data := stream.read(CHUNK_SIZE):
        require(not data.strip(b"\0"), "unexpected data after cpio trailer")
    return entries, contents


def resolve(entries, name):
    for _ in range(16):
        require(name in entries, f"missing runtime path /{name}")
        entry = entries[name]
        if not stat.S_ISLNK(entry.mode):
            return entry
        name = posixpath.normpath(posixpath.join(posixpath.dirname(name), entry.target)).lstrip("/")
    raise ValueError("runtime symlink loop")


def verify_filesystem(entries, contents, kernel_version):
    executable_paths = (
        "init", "sbin/init", "sbin/openrc", "usr/bin/weston", "usr/bin/chromium",
        "usr/lib/chromium/chromium", "usr/bin/xfreerdp3", "usr/bin/Xwayland",
        "etc/init.d/netdesk-desktop", "usr/local/bin/netdesk-session",
        "usr/local/bin/netdesk-browser", "usr/local/bin/netdesk-remote-desktop",
        "usr/local/bin/netdesk-rdp",
    )
    for name in executable_paths:
        entry = resolve(entries, name)
        require(stat.S_ISREG(entry.mode) and entry.mode & 0o111, f"/{name} is not executable")
    require("etc/xdg/weston/weston.ini" in entries, "missing Weston desktop configuration")

    console = resolve(entries, "dev/console")
    require(stat.S_ISCHR(console.mode) and (console.device_major, console.device_minor) == (5, 1),
            "/dev/console must be character device 5:1")
    users = [line.split(":") for line in contents.get("etc/passwd", "").splitlines()
             if line.startswith("netdesk:")]
    require(len(users) == 1 and len(users[0]) == 7, "missing or invalid netdesk account")
    require(users[0][2:4] == ["1000", "1000"] and users[0][5] == "/home/netdesk",
            "desktop account must have UID/GID 1000 and /home/netdesk")
    home = resolve(entries, "home/netdesk")
    require(stat.S_ISDIR(home.mode) and home.uid == home.gid == 1000,
            "/home/netdesk must be owned by the desktop user")
    sandbox = resolve(entries, "usr/lib/chromium/chrome-sandbox")
    require(stat.S_ISREG(sandbox.mode) and sandbox.uid == 0
            and sandbox.mode & stat.S_ISUID and sandbox.mode & 0o111,
            "Chromium sandbox helper must be executable and setuid root")

    modules = [name for name, entry in entries.items()
               if name.startswith(f"lib/modules/{kernel_version}/")
               and re.search(r"\.ko(?:\.(?:gz|xz|zst))?$", name) and entry.size > 0]
    require(modules, f"no kernel modules for {kernel_version}")
    module_versions = {name.split("/")[2] for name in entries
                       if name.startswith("lib/modules/")
                       and re.search(r"\.ko(?:\.(?:gz|xz|zst))?$", name)}
    require(module_versions == {kernel_version}, "kernel module versions do not match .uname")
    require(not any(name.startswith("boot/vmlinuz") for name in entries),
            "initramfs contains a redundant kernel")

    init = "\n".join(line for line in contents.get("init", "").splitlines()
                     if not line.lstrip().startswith("#"))
    inittab = "\n".join(line for line in contents.get("etc/inittab", "").splitlines()
                        if not line.lstrip().startswith("#"))
    require("exec /sbin/init" in init, "/init must start the embedded init system")
    require("/sbin/openrc sysinit" in inittab and "/sbin/openrc default" in inittab,
            "inittab must start the OpenRC desktop runlevels")
    require(not re.search(r"\b(?:switch_root|pivot_root)\b", init + inittab),
            "boot configuration must keep the embedded root filesystem")
    require(not re.search(r"(?:^|[:;\s/])(?:mount|swapon)(?:\s|$)", inittab),
            "inittab must not mount external storage")
    for line in init.splitlines():
        words = shlex.split(line, comments=True)
        if words and words[0] == "mount":
            require("-t" in words and words.index("-t") + 1 < len(words),
                    "/init has an untyped filesystem mount")
            require(words[words.index("-t") + 1] in {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs"},
                    "/init must only mount in-memory and kernel filesystems")
    return len(modules)


def verify(path):
    image_size = path.stat().st_size
    with path.open("rb") as image:
        sections = pe_sections(image, image_size)
        kernel_version = section_text(image, sections[".uname"])
        require(re.fullmatch(r"[A-Za-z0-9_.+-]+", kernel_version), "invalid kernel version")
        cmdline = shlex.split(section_text(image, sections[".cmdline"]))
        require("rdinit=/init" in cmdline, "kernel must run embedded /init")
        require(not any(arg.startswith(("root=", "nfsroot=")) for arg in cmdline),
                "kernel command line must not require an external root filesystem")
        require(re.search(r"^ID=", section_text(image, sections[".osrel"]), re.MULTILINE),
                "missing OS identity in .osrel")
        require(sections[".linux"].size >= 0x206, "embedded Linux kernel is too small")
        image.seek(sections[".linux"].offset + 0x202)
        require(read_exact(image, 4) == b"HdrS", "embedded kernel has no Linux boot header")
        with gzip.GzipFile(fileobj=SectionReader(image, sections[".initrd"]), mode="rb") as archive:
            entries, contents = read_archive(archive)
        module_count = verify_filesystem(entries, contents, kernel_version)
    print(f"Verified {path}: {image_size / (1024 * 1024):.1f} MiB EFI, "
          f"kernel {kernel_version}, {len(entries)} filesystem entries, {module_count} modules")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", type=Path, default=Path("dist/netdesk.efi"))
    args = parser.parse_args()
    try:
        verify(args.image)
    except (OSError, ValueError, EOFError, struct.error) as error:
        print(f"Image verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
