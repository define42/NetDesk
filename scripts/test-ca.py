#!/usr/bin/env python3
"""Test DHCP option 43 CA installation and OS/browser trust inside a disposable Alpine container."""

import argparse
import http.server
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest


REPO = Path(__file__).resolve().parent.parent
INSTALLER = Path("/usr/local/sbin/netdesk-install-ca")
HOOK = Path("/usr/lib/dhcpcd/dhcpcd-hooks/90-netdesk-ca")
TRUST = Path("/usr/local/share/ca-certificates/netdesk-boot-ca.crt")
STATE = Path("/run/netdesk-ca")
NSS = Path("/home/netdesk/.pki/nssdb")
LEASE = Path("/var/lib/dhcpcd/netdesk-test0.lease")
HTTPS_TRUST = Path("/usr/local/share/ca-certificates/netdesk-test-https-ca.crt")


def run(*command, **kwargs):
    return subprocess.run(command, check=True, text=True, capture_output=True, **kwargs)


class CertificateServer(http.server.BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Clients intentionally abort TLS with an untrusted root.

    def do_GET(self):
        authority = self.headers["Host"]
        self.server.requests.append((authority, self.path))
        time.sleep(self.server.delays.get((authority, self.path), 0))
        response = self.server.routes.get((authority, self.path), (404, b"not found", {}))
        status, body, headers = response
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # A bounded download is allowed to close the connection early.

    def log_message(self, fmt, *args):
        pass


class CAInstallationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="netdesk-ca-tests-")
        cls.certificates = Path(cls.temporary.name)
        cls.certificates.chmod(0o755)
        cls.root = cls.make_root("root")
        cls.other_root = cls.make_root("other-root")
        cls.root_der = cls.certificates / "root.der"
        run("openssl", "x509", "-in", str(cls.root), "-outform", "DER", "-out", str(cls.root_der))
        cls.leaf = cls.make_signed("leaf", ca=False)
        cls.intermediate = cls.make_signed("intermediate", ca=True)
        cls.forged_root = cls.make_signed("forged-root", ca=True, subject="NetDesk Test root")

        cls.http = cls.start_server("0.0.0.0", 80)
        cls.alternate_http = cls.start_server("127.0.0.1", 0)
        cls.https = cls.start_server("127.0.0.1", 0, tls=True)
        cls.tls_authority = f"127.0.0.1:{cls.https.server_port}"
        cls.https.routes[(cls.tls_authority, "/")] = (200, b"trusted TLS", {})

    @classmethod
    def make_root(cls, name):
        certificate = cls.certificates / f"{name}.crt"
        run(
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "2", "-subj", f"/CN=NetDesk Test {name}",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout", str(cls.certificates / f"{name}.key"), "-out", str(certificate),
        )
        return certificate

    @classmethod
    def make_signed(cls, name, ca, subject=None):
        key = cls.certificates / f"{name}.key"
        csr = cls.certificates / f"{name}.csr"
        certificate = cls.certificates / f"{name}.crt"
        extensions = cls.certificates / f"{name}.ext"
        extensions.write_text(
            "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
            if ca else "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
        )
        run("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
            "-subj", f"/CN={subject or 'NetDesk Test ' + name}", "-keyout", str(key), "-out", str(csr))
        run("openssl", "x509", "-req", "-in", str(csr), "-CA", str(cls.root),
            "-CAkey", str(cls.certificates / "root.key"), "-CAcreateserial",
            "-days", "2", "-extfile", str(extensions), "-out", str(certificate))
        return certificate

    @classmethod
    def start_server(cls, address, port, tls=False):
        server = http.server.ThreadingHTTPServer((address, port), CertificateServer)
        server.routes = {}
        server.requests = []
        server.delays = {}
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cls.leaf, cls.certificates / "leaf.key")
            server.socket = context.wrap_socket(server.socket, server_side=True)
        server.thread = threading.Thread(target=server.serve_forever, daemon=True)
        server.thread.start()
        return server

    @classmethod
    def tearDownClass(cls):
        for server in (cls.http, cls.alternate_http, cls.https):
            server.shutdown()
            server.server_close()
            server.thread.join(timeout=5)
        cls.temporary.cleanup()

    def setUp(self):
        shutil.rmtree(STATE, ignore_errors=True)
        TRUST.unlink(missing_ok=True)
        HTTPS_TRUST.unlink(missing_ok=True)
        LEASE.unlink(missing_ok=True)
        shutil.rmtree(NSS.parent, ignore_errors=True)
        NSS.mkdir(parents=True)
        run("certutil", "-N", "--empty-password", "-d", f"sql:{NSS}")
        run("chown", "-R", "netdesk:netdesk", str(NSS.parent))
        run("update-ca-certificates", "--fresh")
        for server in (self.http, self.alternate_http):
            server.routes.clear()
            server.requests.clear()
            server.delays.clear()
        self.https.requests.clear()
        self.environment = {
            "PATH": os.environ["PATH"], "HOME": "/root", "interface": "netdesk-test0",
        }

    def offer(self, certificate=None, authority="127.0.0.1", path="/ca.crt", server=None):
        server = server or self.http
        certificate = certificate or self.root
        server.routes[(authority, path)] = (200, certificate.read_bytes(), {})

    def install(self, **environment):
        return subprocess.run(
            [str(INSTALLER)], env={**self.environment, **environment},
            text=True, capture_output=True, timeout=40,
        )

    def assert_installed(self, certificate=None):
        certificate = certificate or self.root
        self.assertTrue((STATE / "installed").is_file(), "CA success marker is missing")
        self.assertTrue(TRUST.is_file(), "OS CA certificate is missing")
        expected = run("openssl", "x509", "-in", str(certificate), "-noout", "-fingerprint", "-sha256").stdout
        actual = run("openssl", "x509", "-in", str(TRUST), "-noout", "-fingerprint", "-sha256").stdout
        self.assertEqual(actual, expected)
        browser = run("su-exec", "netdesk", "certutil", "-L", "-d", f"sql:{NSS}").stdout
        self.assertRegex(browser, r"NetDesk Boot CA\s+C,,")
        self.assertEqual(NSS.stat().st_uid, 1000)

    def assert_not_installed(self):
        self.assertFalse((STATE / "installed").exists())
        self.assertFalse(TRUST.exists())
        browser = run("certutil", "-L", "-d", f"sql:{NSS}").stdout
        self.assertNotIn("NetDesk Boot CA", browser)

    def test_missing_option43_ignores_boot_dhcp_and_raw_lease_servers(self):
        self.write_lease(server="127.0.0.3")
        for authority in ("127.0.0.1", "127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5", "127.0.0.6"):
            self.offer(authority=authority)
            self.offer(authority=authority, path="/ca.pem")
        environment = {
            "new_bootfile_name": "http://127.0.0.1/netdesk.efi",
            "new_filename": "http://127.0.0.2/netdesk.efi", "new_ip_address": "192.0.2.10",
            "new_tftp_server_name": "127.0.0.4", "new_server_name": "127.0.0.5",
            "new_dhcp_server_identifier": "127.0.0.6",
        }
        for option43 in ({}, {"new_netdesk_ca_url": ""}):
            with self.subTest(option43=option43):
                result = self.install(**environment, **option43)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_not_installed()
                self.assertEqual(self.http.requests, [])
                self.assertEqual(self.alternate_http.requests, [])
                self.assertEqual(self.https.requests, [])

    def test_pem_installs_os_and_browser_trust(self):
        untrusted = subprocess.run(
            ["curl", "--noproxy", "*", "--fail", "--silent", "--show-error", f"https://{self.tls_authority}/"],
            capture_output=True,
        )
        self.assertEqual(untrusted.returncode, 60, "test TLS root must initially be untrusted")
        self.offer()
        result = self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_installed()
        self.assertEqual(self.http.requests, [("127.0.0.1", "/ca.crt")])
        response = run("su-exec", "netdesk", "curl", "--noproxy", "*", "--fail", "--silent", "--show-error",
                       f"https://{self.tls_authority}/").stdout
        self.assertEqual(response, "trusted TLS")
        run("su-exec", "netdesk", "certutil", "-A", "-d", f"sql:{NSS}",
            "-n", "Test TLS Leaf", "-t", ",,", "-i", str(self.leaf))
        run("su-exec", "netdesk", "certutil", "-V", "-d", f"sql:{NSS}",
            "-n", "Test TLS Leaf", "-u", "V")

    def test_der_crt_is_normalized_to_pem(self):
        self.offer(self.root_der)
        self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
        self.assert_installed()
        self.assertTrue(TRUST.read_text().startswith("-----BEGIN CERTIFICATE-----"))

    def test_explicit_pem_url_is_used_without_crt_probe(self):
        self.offer(path="/ca.pem")
        self.install(new_netdesk_ca_url="http://127.0.0.1/ca.pem")
        self.assert_installed()
        self.assertEqual(self.http.requests, [("127.0.0.1", "/ca.pem")])

    def test_option43_full_url_preserves_path_query_and_port(self):
        authority = f"127.0.0.1:{self.alternate_http.server_port}"
        path = "/company/certificates/trust%20anchor?format=pem&site=office%2Fwest"
        self.offer(authority=authority, path=path, server=self.alternate_http)
        self.offer(self.other_root)
        self.install(new_netdesk_ca_url=f"http://{authority}{path}",
                     new_bootfile_name="http://127.0.0.1/netdesk.efi",
                     new_dhcp_server_identifier="127.0.0.2")
        self.assert_installed()
        self.assertEqual(self.alternate_http.requests, [(authority, path)])
        self.assertEqual(self.http.requests, [])

    def test_option43_unavailable_url_does_not_probe_other_paths_or_servers(self):
        authority = f"127.0.0.1:{self.alternate_http.server_port}"
        for path in ("/company/root.pem", "/ca.crt", "/ca.pem"):
            self.offer(authority=authority, path=path, server=self.alternate_http)
        self.offer()
        self.offer(path="/ca.pem")
        result = self.install(new_netdesk_ca_url=f"http://{authority}/company/root.crt",
                              new_bootfile_name="http://127.0.0.1/netdesk.efi",
                              new_dhcp_server_identifier="127.0.0.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_not_installed()
        self.assertEqual(self.alternate_http.requests, [(authority, "/company/root.crt")])
        self.assertEqual(self.http.requests, [])

    def test_option43_invalid_certificate_does_not_probe_other_paths_or_servers(self):
        authority = f"127.0.0.1:{self.alternate_http.server_port}"
        self.offer(self.leaf, authority=authority, path="/certificate", server=self.alternate_http)
        self.offer(authority=authority, server=self.alternate_http)
        self.offer(authority=authority, path="/ca.pem", server=self.alternate_http)
        self.offer()
        self.offer(path="/ca.pem")
        result = self.install(new_netdesk_ca_url=f"http://{authority}/certificate",
                              new_dhcp_server_identifier="127.0.0.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_not_installed()
        self.assertEqual(self.alternate_http.requests, [(authority, "/certificate")])
        self.assertEqual(self.http.requests, [])

    def test_option43_malformed_or_unsupported_urls_make_no_requests(self):
        sentinel = Path("/tmp/netdesk-ca-untrusted-executed")
        sentinel.unlink(missing_ok=True)
        self.offer()
        self.offer(self.other_root, authority="127.0.0.2")
        for url in (
            "file:///tmp/root.crt", "ftp://127.0.0.2/ca.crt", "tftp://127.0.0.2/ca.crt",
            "http://user:password@127.0.0.2/ca.crt", "//127.0.0.2/ca.crt",
            "http://127.0.0.2:invalid/ca.crt", "http://127.0.0.2/ca.crt\nhttp://127.0.0.2/ca.pem",
            f"http://$(touch {sentinel})/ca.crt",
        ):
            with self.subTest(url=url):
                shutil.rmtree(STATE, ignore_errors=True)
                self.http.requests.clear()
                result = self.install(new_netdesk_ca_url=url, new_dhcp_server_identifier="127.0.0.1")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_not_installed()
                self.assertEqual(self.http.requests, [])
                self.assertFalse(sentinel.exists())

    def test_option43_redirect_is_not_followed(self):
        authority = f"127.0.0.1:{self.alternate_http.server_port}"
        self.alternate_http.routes[(authority, "/certificate")] = (
            302, b"", {"Location": "http://127.0.0.2/root.crt"},
        )
        self.offer(self.other_root, authority="127.0.0.2", path="/root.crt")
        self.offer()
        self.install(new_netdesk_ca_url=f"http://{authority}/certificate",
                     new_dhcp_server_identifier="127.0.0.1")
        self.assert_not_installed()
        self.assertEqual(self.alternate_http.requests, [(authority, "/certificate")])
        self.assertEqual(self.http.requests, [])

    def test_option43_does_not_use_proxy_environment(self):
        self.offer(path="/company/root.crt")
        self.install(new_netdesk_ca_url="http://127.0.0.1/company/root.crt",
                     http_proxy="http://127.0.0.2:1", ALL_PROXY="http://127.0.0.2:1")
        self.assert_installed()
        self.assertEqual(self.http.requests, [("127.0.0.1", "/company/root.crt")])

    def test_option43_https_accepts_a_server_with_existing_trust(self):
        shutil.copyfile(self.root, HTTPS_TRUST)
        run("update-ca-certificates")
        self.offer(self.other_root, authority=self.tls_authority, path="/root.pem", server=self.https)
        self.install(new_netdesk_ca_url=f"https://{self.tls_authority}/root.pem")
        self.assert_installed(self.other_root)
        self.assertEqual(self.https.requests, [(self.tls_authority, "/root.pem")])
        self.assertEqual(self.http.requests, [])

    def test_option43_https_rejects_untrusted_server_without_other_requests(self):
        self.offer(self.other_root, authority=self.tls_authority, path="/root.pem", server=self.https)
        self.offer()
        self.install(new_netdesk_ca_url=f"https://{self.tls_authority}/root.pem",
                     new_dhcp_server_identifier="127.0.0.1")
        self.assert_not_installed()
        self.assertEqual(self.https.requests, [])
        self.assertEqual(self.http.requests, [])

    def write_lease(self, address="192.0.2.10", server="127.0.0.2", ca_url=None):
        packet = bytearray(240)
        packet[0:3] = bytes((2, 1, 6))  # BOOTREPLY, Ethernet, MAC address length.
        packet[16:20] = socket.inet_aton(address)
        packet[20:24] = socket.inet_aton(server)
        packet[236:240] = bytes((99, 130, 83, 99))
        packet.extend(bytes((53, 1, 5, 54, 4, 127, 0, 0, 1)))  # DHCPACK and server identifier.
        if ca_url is not None:
            value = ca_url.encode()
            self.assertLessEqual(len(value), 255)
            packet.extend(bytes((43, len(value))) + value)
        packet.append(255)
        LEASE.parent.mkdir(parents=True, exist_ok=True)
        LEASE.write_bytes(packet)

    def test_real_dhcpcd_decodes_option43_and_hook_installs_exact_url(self):
        authority = f"127.0.0.1:{self.alternate_http.server_port}"
        path = "/pki/anchor.crt?site=west&encoded=%2F&label=a=b&quote='&dollar=$value&brackets=[a]"
        url = f"http://{authority}{path}"
        self.write_lease(ca_url=url)
        config = self.certificates / "dhcpcd.conf"
        shutil.copyfile(REPO / "overlay/etc/dhcpcd.conf", config)
        # Dump stdin rather than contacting a daemon. Output is raw key=value,
        # not shell syntax: quotes, dollar signs and equal signs are literal.
        decoded = subprocess.run(
            ["dhcpcd", "-4", "-f", str(config), "-U", "-"], input=LEASE.read_bytes(),
            check=True, capture_output=True,
        )
        lease_values = {}
        for line in decoded.stdout.decode().splitlines():
            name, separator, value = line.partition("=")
            if separator:
                lease_values[name] = value
        self.assertEqual(lease_values.get("netdesk_ca_url"), url, decoded.stdout.decode())
        self.offer(authority=authority, path=path, server=self.alternate_http)
        self.offer(self.other_root)
        environment = {
            **self.environment, **{f"new_{name}": value for name, value in lease_values.items()},
            "reason": "BOUND",
        }
        run("/bin/sh", "-c", f". {HOOK}", env=environment)
        self.assert_installed()
        self.assertEqual(self.alternate_http.requests, [(authority, path)])
        self.assertEqual(self.http.requests, [])

    def test_non_ca_and_intermediate_are_rejected(self):
        for certificate in (self.leaf, self.intermediate):
            with self.subTest(certificate=certificate.name):
                self.offer(certificate)
                self.http.requests.clear()
                self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
                self.assert_not_installed()
                self.assertEqual(self.http.requests, [("127.0.0.1", "/ca.crt")])

    def test_forged_root_multiple_certificates_and_malformed_input_are_rejected(self):
        for name, payload in (
            ("forged root", self.forged_root.read_bytes()),
            ("multiple roots", self.root.read_bytes() + self.other_root.read_bytes()),
            ("malformed", b"not a certificate"),
        ):
            with self.subTest(name=name):
                self.http.routes[("127.0.0.1", "/ca.crt")] = (200, payload, {})
                self.http.requests.clear()
                self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
                self.assert_not_installed()
                self.assertEqual(self.http.requests, [("127.0.0.1", "/ca.crt")])

    def test_oversized_download_is_rejected(self):
        self.http.routes[("127.0.0.1", "/ca.crt")] = (200, self.root.read_bytes() + b" " * (2 * 1024 * 1024), {})
        self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
        self.assert_not_installed()

    def test_stalled_download_times_out(self):
        self.offer()
        self.http.delays[("127.0.0.1", "/ca.crt")] = 8
        started = time.monotonic()
        self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
        self.assertLess(time.monotonic() - started, 7)
        self.assert_not_installed()

    def test_first_success_is_not_replaced_on_renewal(self):
        self.offer()
        self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
        self.assert_installed()
        requests = list(self.http.requests)
        self.offer(self.other_root)
        self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
        self.assert_installed()
        self.assertEqual(self.http.requests, requests)

    def test_unavailable_ca_can_be_retried(self):
        self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
        self.assert_not_installed()
        self.offer()
        self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
        self.assert_installed()

    def test_browser_import_failure_rolls_back_os_trust_and_allows_retry(self):
        self.offer()
        run("chown", "root:root", str(NSS))
        NSS.chmod(0o500)
        result = self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
        self.assertNotEqual(result.returncode, 0)
        self.assert_not_installed()
        untrusted = subprocess.run(
            ["curl", "--noproxy", "*", "--fail", "--silent", f"https://{self.tls_authority}/"],
            capture_output=True,
        )
        self.assertEqual(untrusted.returncode, 60)
        run("chown", "netdesk:netdesk", str(NSS))
        NSS.chmod(0o700)
        self.install(new_netdesk_ca_url="http://127.0.0.1/ca.crt")
        self.assert_installed()

    def test_hook_ignores_unrelated_events_and_retries_on_renewal(self):
        environment = {**self.environment, "new_netdesk_ca_url": "http://127.0.0.1/ca.crt"}
        for reason in ("PREINIT", "CARRIER", "NOCARRIER", "EXPIRE", "STOP", "BOUND6"):
            run("/bin/sh", "-c", f". {HOOK}", env={**environment, "reason": reason})
        self.assertEqual(self.http.requests, [])
        run("/bin/sh", "-c", f". {HOOK}", env={**environment, "reason": "BOUND"})
        self.assert_not_installed()
        self.offer()
        run("/bin/sh", "-c", f". {HOOK}", env={**environment, "reason": "RENEW"})
        deadline = time.monotonic() + 10
        while not (STATE / "installed").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assert_installed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in-container", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.in_container:
        if not shutil.which("docker"):
            parser.error("docker is required; tests only modify trust in a disposable container")
        image = "netdesk-ca-test:alpine-3.24.1"
        subprocess.run(["docker", "build", "-f", str(REPO / "scripts/Dockerfile.ca-test"),
                        "-t", image, str(REPO)], check=True)
        subprocess.run(["docker", "run", "--rm", "-v", f"{REPO}:/src:ro", image], check=True)
        return
    if not Path("/.dockerenv").exists() or not Path("/netdesk-ca-test-container").is_file():
        parser.error("--in-container must run inside the dedicated CA test image")
    for destination in (INSTALLER, HOOK):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / "overlay" / destination.relative_to("/"), destination)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CAInstallationTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
