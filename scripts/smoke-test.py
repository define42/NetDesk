#!/usr/bin/env python3
"""Boot the actual EFI artifact over HTTP in a diskless UEFI virtual machine."""

import argparse
import functools
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time


class ImageServer(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".efi": "application/efi"}
    downloaded = threading.Event()

    def do_GET(self):
        if self.path == "/netdesk.efi":
            self.downloaded.set()
        super().do_GET()

    def log_message(self, fmt, *args):
        print("HTTP: " + fmt % args, flush=True)


def screenshot(qmp_path, destination):
    """Keep a screenshot for local inspection without producing another release artifact."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(qmp_path))
        stream = connection.makefile("rwb")
        json.loads(stream.readline())
        for command in (
            {"execute": "qmp_capabilities"},
            {"execute": "screendump", "arguments": {"filename": str(destination)}},
        ):
            stream.write(json.dumps(command).encode() + b"\n")
            stream.flush()
            while True:
                response = json.loads(stream.readline())
                if "error" in response:
                    raise RuntimeError(response["error"])
                if "return" in response:
                    break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--memory", type=int, default=6144, help="Guest RAM in MiB")
    args = parser.parse_args()
    image = args.image.resolve(strict=True)
    if image.name != "netdesk.efi":
        parser.error("image must be named netdesk.efi")
    qemu = shutil.which("qemu-system-x86_64")
    code = Path(os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd"))
    variables = Path(os.environ.get("OVMF_VARS", "/usr/share/OVMF/OVMF_VARS_4M.fd"))
    if not qemu or not code.is_file() or not variables.is_file():
        parser.error("install qemu-system-x86 and ovmf (or set OVMF_CODE and OVMF_VARS)")
    logs = Path("build").resolve()
    logs.mkdir(exist_ok=True)
    serial_path = logs / "smoke-serial.log"
    qemu_path = logs / "smoke-qemu.log"
    screenshot_path = logs / "smoke-desktop.ppm"
    screenshot_path.unlink(missing_ok=True)
    handler = functools.partial(ImageServer, directory=str(image.parent))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="netdesk-smoke-") as temporary:
            work = Path(temporary)
            shutil.copyfile(variables, work / "vars.fd")
            qmp = work / "qmp.sock"
            acceleration = "kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg"
            url = f"http://10.0.2.2:{server.server_port}/netdesk.efi"
            command = [
                qemu, "-machine", f"q35,accel={acceleration}", "-cpu", "max",
                "-m", str(args.memory), "-smp", "2", "-no-reboot",
                # Keep OVMF's HTTP Boot driver and skip unrelated TFTP timeouts.
                "-fw_cfg", "name=opt/org.tianocore/IPv4PXESupport,string=no",
                "-fw_cfg", "name=opt/org.tianocore/IPv6PXESupport,string=no",
                "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                "-drive", f"if=pflash,format=raw,file={work / 'vars.fd'}",
                "-device", "virtio-vga", "-vga", "none",
                "-device", "virtio-net-pci,netdev=net0,bootindex=1",
                "-netdev", f"user,id=net0,bootfile={url}",
                "-device", "qemu-xhci", "-device", "usb-tablet",
                "-device", "virtio-rng-pci", "-display", "none",
                "-serial", f"file:{serial_path}", "-monitor", "none",
                "-qmp", f"unix:{qmp},server=on,wait=off",
            ]
            print(f"Booting {image.name} over HTTP, {args.memory} MiB RAM, {acceleration}.", flush=True)
            with qemu_path.open("wb") as output:
                process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + args.timeout
                    while time.monotonic() < deadline:
                        serial = serial_path.read_text(errors="replace") if serial_path.exists() else ""
                        if "Kernel panic" in serial:
                            raise RuntimeError("guest kernel panicked")
                        if "NetDesk desktop ready" in serial and ImageServer.downloaded.is_set():
                            screenshot(qmp, screenshot_path)
                            print(f"PASS: HTTP-loaded EFI reached the XFCE desktop; logs and screenshot in {logs}.")
                            return
                        if process.poll() is not None:
                            raise RuntimeError(f"QEMU exited with status {process.returncode}")
                        time.sleep(1)
                    screenshot(qmp, screenshot_path)
                    raise RuntimeError(f"desktop did not become ready within {args.timeout} seconds")
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    except (RuntimeError, OSError) as error:
        print(f"FAIL: {error}")
        for path in (qemu_path, serial_path):
            if path.exists():
                print(f"--- {path} ---\n{path.read_text(errors='replace')[-16000:]}")
        raise SystemExit(1) from error
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
