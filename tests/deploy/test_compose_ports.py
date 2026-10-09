"""Spec 038, R1: the application's port is published on the host's loopback interface only.

Remote access comes through the Cloudflare tunnel, whose connector runs on the host and
reaches the application at ``127.0.0.1``. Published on every interface, as it was, the port
served plain HTTP to the whole local network -- the password and the session cookie in the
clear -- and Docker's own forwarding rules are applied before a host firewall such as ufw
sees the packet, so a firewall like that could not have closed it again.

Read with the same text reader as the backup volume's tests, for the reason that module
gives: a test of what a file says should not need the parser whose behaviour it might be
checking.
"""

from __future__ import annotations

import unittest

from test_backup_volume import block, manifest

LOOPBACK_ONLY = '- "127.0.0.1:${PORTFOLIO_PORT:?A host port is required}:8000"'


class PublishedPortTests(unittest.TestCase):
    def test_the_port_is_published_on_loopback_only(self) -> None:
        self.assertEqual(block(manifest(), ["services", "app", "ports"]), [LOOPBACK_ONLY])

    def test_the_container_still_listens_where_the_health_check_asks(self) -> None:
        """The health check runs inside the container, so the host binding cannot fail it."""
        check = " ".join(block(manifest(), ["services", "app", "healthcheck"]))
        self.assertIn("http://127.0.0.1:8000/api/health", check)
        self.assertNotIn("8083", check)


if __name__ == "__main__":
    unittest.main()
