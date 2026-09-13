#!/usr/bin/env python3
"""Test DHCP option 42 and the real NTP service in a disposable Alpine container."""

import argparse
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import threading
import time
import unittest


REPO = Path(__file__).resolve().parent.parent
CONF = Path("/etc/ntp.conf")
PIDFILE = Path("/run/ntpd.pid")
STATE = Path("/run/dhcpcd/hook-state")
RUN_HOOKS = "/usr/lib/dhcpcd/dhcpcd-run-hooks"


def run(*command, **kwargs):
    return subprocess.run(command, check=True, text=True, capture_output=True, timeout=20, **kwargs)


class NTPPeer:
    """Record real client queries without supplying any clock corrections."""

    def __init__(self, address):
        self.address = address
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((address, 123))
        self.socket.settimeout(0.1)
        self.queries = []
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.receive, daemon=True)
        self.thread.start()

    def receive(self):
        while not self.closed.is_set():
            try:
                packet, _ = self.socket.recvfrom(512)
            except TimeoutError:
                continue
            self.queries.append(packet)

    def close(self):
        self.closed.set()
        self.thread.join(timeout=2)
        self.socket.close()


class DHCPNTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.peers = [NTPPeer(address) for address in ("127.0.0.2", "127.0.0.3")]
        cls.hook_environment = {}
        for line in Path("/etc/dhcpcd.conf").read_text().splitlines():
            fields = shlex.split(line, comments=True)
            if fields and fields[0] == "env":
                name, _, value = " ".join(fields[1:]).partition("=")
                cls.hook_environment[name] = value

    @classmethod
    def tearDownClass(cls):
        for peer in cls.peers:
            peer.close()

    def setUp(self):
        run("rc-service", "--nodeps", "--ifstarted", "ntpd", "stop")
        shutil.rmtree(STATE, ignore_errors=True)
        shutil.copyfile(REPO / "overlay/etc/ntp.conf", CONF)
        for peer in self.peers:
            peer.queries.clear()
        self.addCleanup(run, "rc-service", "--nodeps", "--ifstarted", "ntpd", "stop")

    def lease(self, servers=None):
        packet = bytearray(240)
        packet[0:3] = bytes((2, 1, 6))  # BOOTREPLY, Ethernet, MAC address length.
        packet[16:20] = socket.inet_aton("192.0.2.10")
        packet[236:240] = bytes((99, 130, 83, 99))
        packet.extend(bytes((53, 1, 5, 54, 4, 192, 0, 2, 1)))  # DHCPACK, server identifier.
        if servers:
            addresses = b"".join(socket.inet_aton(server) for server in servers)
            packet.extend(bytes((42, len(addresses))) + addresses)
        packet.append(255)
        decoded = subprocess.run(
            ["dhcpcd", "-4", "-f", "/etc/dhcpcd.conf", "-U", "-"],
            input=packet, check=True, capture_output=True, timeout=10,
        )
        values = {}
        # dhcpcd -U emits literal key=value records, not shell source.
        for line in decoded.stdout.decode().splitlines():
            name, separator, value = line.partition("=")
            if separator:
                values[f"new_{name}"] = value
        self.assertEqual(values.get("new_ntp_servers", ""), " ".join(servers or []))
        return values

    def event(self, servers=None, reason="BOUND", interface="eth0"):
        up = reason in ("BOUND", "RENEW", "REBIND", "REBOOT")
        environment = {
            "PATH": os.environ["PATH"], "HOME": "/root", **self.hook_environment,
            "reason": reason, "interface": interface, "protocol": "dhcp",
            "if_configured": "true", "if_up": str(up).lower(),
            "if_down": str(not up).lower(),
            "skip_hooks": "resolv.conf hostname",
            **(self.lease(servers) if up else {}),
        }
        result = run("/bin/sh", RUN_HOOKS, env=environment)
        self.assertNotIn("failed", result.stdout.lower() + result.stderr.lower(), result)

    def configured_servers(self):
        return [line.split()[1] for line in CONF.read_text().splitlines()
                if line.startswith("server ")]

    def running_pid(self):
        run("rc-service", "--nodeps", "ntpd", "status")
        self.assertTrue(PIDFILE.is_file(), "ntpd has no PID file")
        pid = int(PIDFILE.read_text())
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        self.assertIn(b"-w", command, "test daemon must never set the clock")
        self.assertNotIn(b"-p", command, "command-line peers override DHCP's ntp.conf")
        self.assertNotEqual(Path(f"/proc/{pid}/stat").read_text().split()[2], "Z")
        return pid

    def assert_stopped(self):
        result = subprocess.run(["rc-service", "--nodeps", "ntpd", "status"],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(PIDFILE.exists(), "ntpd PID file remains after stop")

    def assert_queried(self, *peers):
        deadline = time.monotonic() + 10
        while not all(peer.queries for peer in peers) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.running_pid()
        for peer in peers:
            self.assertTrue(peer.queries, f"ntpd did not query DHCP server {peer.address}")
            self.assertTrue(all(len(packet) >= 48 and packet[0] & 7 == 3
                                for packet in peer.queries), "expected NTP client packets")

    def test_first_lease_starts_and_queries_all_option42_servers(self):
        requested = set()
        for line in Path("/etc/dhcpcd.conf").read_text().splitlines():
            fields = shlex.split(line, comments=True)
            if fields and fields[0] == "option":
                requested.update("".join(fields[1:]).split(","))
        self.assertIn("ntp_servers", requested, "DHCP must request option 42")
        self.assert_stopped()
        self.event([peer.address for peer in self.peers])
        self.assertEqual(self.configured_servers(), [peer.address for peer in self.peers])
        self.assert_queried(*self.peers)

    def test_renewal_replaces_server_and_duplicate_lease_keeps_process(self):
        first, second = self.peers
        self.event([first.address])
        self.assert_queried(first)
        original_pid = self.running_pid()
        self.event([second.address], reason="RENEW")
        self.assertEqual(self.configured_servers(), [second.address])
        self.assert_queried(second)
        renewed_pid = self.running_pid()
        self.assertNotEqual(original_pid, renewed_pid, "changed peers must reload ntpd")
        configuration = CONF.read_bytes()
        self.event([second.address], reason="RENEW")
        self.assertEqual(self.running_pid(), renewed_pid, "unchanged peers restarted ntpd")
        self.assertEqual(CONF.read_bytes(), configuration)

    def test_missing_option_does_not_start_and_withdrawal_stops(self):
        self.event()
        self.assertEqual(self.configured_servers(), [])
        self.assert_stopped()
        self.event([self.peers[0].address], reason="RENEW")
        self.assert_queried(self.peers[0])
        self.event(reason="RENEW")
        self.assertEqual(self.configured_servers(), [])
        self.assert_stopped()

    def test_interfaces_merge_deduplicate_remove_and_later_restart(self):
        first, second = self.peers
        self.event([first.address], interface="eth0")
        self.assert_queried(first)
        self.event([first.address, second.address, second.address], interface="eth1")
        self.assertEqual(self.configured_servers(), [first.address, second.address])
        self.assert_queried(first, second)
        self.event(reason="EXPIRE", interface="eth0")
        self.assertEqual(self.configured_servers(), [first.address, second.address])
        self.running_pid()
        self.event(reason="NOCARRIER", interface="eth1")
        self.assertEqual(self.configured_servers(), [])
        self.assert_stopped()
        second.queries.clear()
        self.event([second.address], interface="eth1")
        self.assert_queried(second)
        self.event(reason="STOP", interface="eth1")
        self.assertEqual(self.configured_servers(), [])
        self.assert_stopped()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in-container", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.in_container:
        if not shutil.which("docker"):
            parser.error("docker is required; tests only run in a disposable container")
        image = "netdesk-ntp-test:alpine-3.24.1"
        subprocess.run(["docker", "build", "-f", str(REPO / "scripts/Dockerfile.ntp-test"),
                        "-t", image, str(REPO)], check=True)
        subprocess.run(["docker", "run", "--rm", "--init", "--network", "none",
                        "--cap-drop", "SYS_TIME", "-v", f"{REPO}:/src:ro", image], check=True)
        return
    if not Path("/.dockerenv").exists() or not Path("/netdesk-ntp-test-container").is_file():
        parser.error("--in-container must run inside the dedicated NTP test image")
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("CapBnd:") and int(line.split()[1], 16) & (1 << 25):
            parser.error("the NTP test container must not have CAP_SYS_TIME")
    for relative in ("etc/dhcpcd.conf", "etc/ntp.conf", "etc/conf.d/ntpd",
                     "usr/local/sbin/netdesk-update-ntp"):
        destination = Path("/") / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / "overlay" / relative, destination)
    # Keep the shipped OpenRC service lifecycle and production NTPD_OPTS.
    # Query mode and removal of its capability request are container-only:
    # changing the host clock is neither necessary nor allowed in these tests.
    service = Path("/etc/init.d/ntpd")
    service.write_text(service.read_text().replace(
        'capabilities="^cap_sys_time,^cap_net_bind_service"', 'unset capabilities',
    ) + '\ncommand_args="$command_args -w"\n')
    Path("/run/openrc").mkdir(parents=True, exist_ok=True)
    Path("/run/openrc/softlevel").write_text("default\n")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DHCPNTPTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
