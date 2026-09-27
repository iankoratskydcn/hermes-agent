"""Regression tests for provider/auth readiness diagnosis.

The 401 fixture mirrors the recurring worker failure while asserting that no
credential value is returned or required.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).parents[2] / "scripts" / "provider_readiness.py"
spec = importlib.util.spec_from_file_location("provider_readiness", SCRIPT)
assert spec and spec.loader
provider_readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider_readiness)


class ProviderReadinessTests(unittest.TestCase):
    def test_self_test_cli_does_not_require_board_access(self) -> None:
        self.assertEqual(provider_readiness.main(["--self-test"]), 0)

    def test_http_401_with_missing_profile_auth_is_actionable_without_secret(self) -> None:
        diagnosis, remediation = provider_readiness.classify_failure(
            "HTTP 401 Unauthorized: invalid API key",
            auth_ok=False,
        )
        self.assertEqual(diagnosis, "missing_auth")
        self.assertIn("profile-scoped", remediation)
        self.assertNotIn("api key", remediation.lower().replace("api key", ""))

    def test_quota_wall_never_becomes_requeue_recommendation(self) -> None:
        diagnosis, remediation = provider_readiness.classify_failure("HTTP 429 rate limit exceeded")
        self.assertEqual(diagnosis, "rate_limit_quota")
        self.assertIn("do not requeue", remediation)

    def test_model_pin_failure_is_stale_pin(self) -> None:
        diagnosis, _ = provider_readiness.classify_failure("404 model not found", has_pin=True)
        self.assertEqual(diagnosis, "stale_card_pin")


if __name__ == "__main__":
    unittest.main()
