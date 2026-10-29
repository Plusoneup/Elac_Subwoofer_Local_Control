import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run  # noqa: E402
import server_core as sc  # noqa: E402

HOSTS = sc.default_allowed_hosts(own_name="minipc")


class HostRuleTests(unittest.TestCase):
    def test_own_names_accepted(self):
        for name in ("minipc", "minipc.local", "minipc.lan", "minipc.home", "localhost", "127.0.0.1"):
            self.assertIn(name, HOSTS)

    def test_fqdn_hostname(self):
        hosts = sc.default_allowed_hosts(own_name="minipc.example.lan")
        self.assertIn("minipc.example.lan", hosts)
        self.assertIn("minipc.local", hosts)

    def test_ip_literals_accepted(self):
        self.assertIsNone(sc.check_request("GET", "192.168.1.50:8765", None, HOSTS))
        self.assertIsNone(sc.check_request("GET", "[fe80::1]:8765", None, HOSTS))
        self.assertIsNone(sc.check_request("POST", "192.168.1.50:8765", "http://192.168.1.50:8765", HOSTS, "same-origin"))

    def test_foreign_names_and_cross_origin_still_blocked(self):
        self.assertIsNotNone(sc.check_request("GET", "evil.example:8765", None, HOSTS))
        self.assertIsNotNone(sc.check_request("GET", "minipc.evil.example", None, HOSTS))
        self.assertIsNotNone(sc.check_request("POST", "192.168.1.50:8765", "http://evil.example", HOSTS))
        self.assertIsNotNone(sc.check_request("POST", "192.168.1.50:8765", "http://192.168.1.51:8765", HOSTS))

    def test_is_ip_literal(self):
        self.assertTrue(sc.is_ip_literal("10.0.0.5"))
        self.assertTrue(sc.is_ip_literal("::1"))
        self.assertFalse(sc.is_ip_literal("minipc"))
        self.assertFalse(sc.is_ip_literal("1.2.3.4.evil.example"))


class TokenTests(unittest.TestCase):
    def test_no_token_configured_means_open(self):
        self.assertTrue(sc.token_matches("", None))

    def test_cookie_or_header(self):
        self.assertTrue(sc.token_matches("correct-horse-battery", None, "correct-horse-battery"))
        self.assertTrue(sc.token_matches("correct-horse-battery", "correct-horse-battery", None))
        self.assertFalse(sc.token_matches("correct-horse-battery", None, None))
        self.assertFalse(sc.token_matches("correct-horse-battery", "wrong", ""))

    def test_settings_read_token_and_tls(self):
        s = sc.Settings("/nonexistent-dir", {"ELAC_TOKEN": "  abcdefghijkl  ", "ELAC_TLS_CERT": "/x.pem"})
        self.assertEqual(s.token, "abcdefghijkl")
        self.assertTrue(s.tls)


class LauncherTests(unittest.TestCase):
    def test_defaults_to_this_computer_only(self):
        self.assertEqual(run.options({}), {"host": "127.0.0.1", "port": 8765, "log_level": "info"})

    def test_network_needs_a_token(self):
        with self.assertRaises(SystemExit):
            run.options({"ELAC_HOST": "0.0.0.0"})
        self.assertEqual(run.options({"ELAC_HOST": "0.0.0.0", "ELAC_TOKEN": "x" * 16})["host"], "0.0.0.0")
        self.assertEqual(run.options({"ELAC_HOST": "0.0.0.0", "ELAC_ALLOW_NO_TOKEN": "1"})["host"], "0.0.0.0")

    def test_loopback_variants_need_no_token(self):
        for host in ("127.0.0.1", "::1", "localhost", "127.0.0.2"):
            self.assertEqual(run.options({"ELAC_HOST": host})["host"], host)

    def test_short_token_and_bad_port(self):
        with self.assertRaises(SystemExit):
            run.options({"ELAC_TOKEN": "short"})
        for port in ("abc", "0", "70000"):
            with self.assertRaises(SystemExit):
                run.options({"ELAC_PORT": port})

    def test_tls_pair(self):
        here = str(Path(__file__).resolve())
        opts = run.options({"ELAC_TLS_CERT": here, "ELAC_TLS_KEY": here})
        self.assertEqual(opts["ssl_certfile"], here)
        with self.assertRaises(SystemExit):
            run.options({"ELAC_TLS_CERT": here})
        with self.assertRaises(SystemExit):
            run.options({"ELAC_TLS_CERT": "/missing.pem", "ELAC_TLS_KEY": "/missing.key"})


if __name__ == "__main__":
    unittest.main()
