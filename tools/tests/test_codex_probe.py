"""No-turn, billing and output safety gates for the Windows probe."""

import json
import subprocess
import unittest
from unittest.mock import patch

from tools import codex_probe


class FakeAppServer:
    instances = []
    account_type = "chatgpt"

    def __init__(self, executable, env):
        self.calls = []
        self.env = env
        self.instances.append(self)

    def request(self, method, params=None):
        self.calls.append(method)
        if method == "initialize":
            return {}
        if method == "account/read":
            return {"account": {"type": self.account_type, "planType": "plus",
                                "email": "secret@example.com", "id": "account-secret"}}
        if method == "model/list":
            if params.get("cursor"):
                return {"data": [{"id": "gpt-6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]}
            return {"data": [{"id": "gpt-6-luna", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}],
                    "nextCursor": "more"}
        if method == "account/rateLimits/read":
            return {"rateLimits": {"primary": {"usedPercent": 8}, "accountId": "account-secret"}}
        raise AssertionError("Unexpected method: " + method)

    def notify(self, method, params=None):
        self.calls.append(method)

    def close(self):
        pass


class ProbeTests(unittest.TestCase):
    def setUp(self):
        FakeAppServer.instances = []
        FakeAppServer.account_type = "chatgpt"

    def fake_run(self, command, **kwargs):
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("CODEX_ACCESS_TOKEN", kwargs["env"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", kwargs["env"])
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", kwargs["env"])
        self.assertNotIn("NODE_OPTIONS", kwargs["env"])
        stdout = "codex-cli 0.158.0-alpha.2.1\n" if command[-1] == "--version" else "private account secret"
        return subprocess.CompletedProcess(command, 0, stdout, "private secret")

    def test_default_discovery_makes_no_turn_and_allowlists_report(self):
        with patch("tools.codex_probe.shutil.which", return_value="/fake/codex"), \
             patch("tools.codex_probe.subprocess.run", side_effect=self.fake_run), \
             patch("tools.codex_probe.AppServerTransport", FakeAppServer), \
             patch("tools.codex_probe.os.environ", {"OPENAI_API_KEY": "secret", "CODEX_ACCESS_TOKEN": "secret",
                                                     "AWS_SECRET_ACCESS_KEY": "secret",
                                                     "GOOGLE_APPLICATION_CREDENTIALS": "secret",
                                                     "NODE_OPTIONS": "secret", "PATH": "/safe"}):
            report = codex_probe.probe()
        self.assertEqual(report["handshake_check"], "passed")
        self.assertEqual(report["subscription_check"], "passed")
        self.assertEqual([m["id"] for m in report["models"]], ["gpt-6-luna", "gpt-6-sol"])
        self.assertEqual(report["turn_check"], "not_requested")
        self.assertNotIn("thread/start", FakeAppServer.instances[0].calls)
        self.assertEqual(FakeAppServer.instances[0].calls[:3], ["initialize", "initialized", "account/read"])
        self.assertNotIn("secret", json.dumps(report))
        self.assertNotIn("OPENAI_API_KEY", FakeAppServer.instances[0].env)
        self.assertEqual(FakeAppServer.instances[0].env, {"PATH": "/safe"})

    def test_api_key_account_fails_before_model_or_turn(self):
        FakeAppServer.account_type = "apiKey"
        with patch("tools.codex_probe.shutil.which", return_value="/fake/codex"), \
             patch("tools.codex_probe.subprocess.run", side_effect=self.fake_run), \
             patch("tools.codex_probe.AppServerTransport", FakeAppServer):
            report = codex_probe.probe(with_turn=True)
        self.assertEqual(report["subscription_check"], "failed")
        self.assertNotIn("model/list", FakeAppServer.instances[0].calls)
        self.assertNotIn("turn/start", FakeAppServer.instances[0].calls)
        self.assertNotIn("secret", json.dumps(report))

    def test_version_mismatch_does_not_make_model_turn(self):
        def newer(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.158.0-alpha.2.2\n", "")

        with patch("tools.codex_probe.shutil.which", return_value="/fake/codex"), \
             patch("tools.codex_probe.subprocess.run", side_effect=newer), \
             patch("tools.codex_probe.AppServerTransport", FakeAppServer):
            report = codex_probe.probe(with_turn=True)
        self.assertEqual(report["version_check"], "different_version")
        self.assertEqual(report["turn_check"], "refused_version_or_catalog")
        self.assertNotIn("thread/start", FakeAppServer.instances[0].calls)

    def test_allowlist_rejects_account_like_strings(self):
        self.assertEqual(codex_probe.safe_model_id("secret@example.com"), "unlisted")
        self.assertEqual(codex_probe.safe_model_id("gpt-6-sol"), "gpt-6-sol")
        self.assertEqual(codex_probe.safe_model_id("priv8x7q"), "unlisted")
        self.assertEqual(codex_probe.safe_child_env({"Path": "bin", "OPENAI_API_KEY": "priv8x7q",
                                                     "AWS_PROFILE": "priv8x7q", "SOME_FUTURE_CLOUD_SECRET": "priv8x7q"}),
                         {"Path": "bin"})
        self.assertIsNone(codex_probe.version_id("no version"))
        self.assertEqual(codex_probe.version_id("codex-cli 0.158.0-alpha.2.1"),
                         "0.158.0-alpha.2.1")
        self.assertNotEqual(codex_probe.version_id("codex-cli 0.158.0"), codex_probe.TESTED_VERSION)

    def test_short_private_marker_in_catalog_and_error_is_not_reported(self):
        original = FakeAppServer.request

        def marked(server, method, params=None):
            if method == "model/list":
                return {"data": [{"id": "priv8x7q", "supportedReasoningEfforts": [
                    {"reasoningEffort": "priv8x7q"}]}]}
            if method == "account/read":
                return {"account": {"type": "chatgpt", "planType": "priv8x7q",
                                    "email": "priv8x7q"}}
            if method == "account/rateLimits/read":
                return {"rateLimits": {"accountId": "priv8x7q"}}
            return original(server, method, params)

        with patch("tools.codex_probe.shutil.which", return_value="/fake/codex"), \
             patch("tools.codex_probe.subprocess.run", side_effect=self.fake_run), \
             patch("tools.codex_probe.AppServerTransport", FakeAppServer), \
             patch.object(FakeAppServer, "request", marked):
            report = codex_probe.probe()
        self.assertNotIn("priv8x7q", json.dumps(report))
        self.assertEqual(report["models"], [{"id": "unlisted", "efforts": []}])
        self.assertEqual(report["plan_category"], "unreported")


if __name__ == "__main__":
    unittest.main()
