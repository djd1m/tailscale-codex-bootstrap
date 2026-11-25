import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tailnet_bootstrap import cli
from tailnet_bootstrap.state import StateError, StateStore


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = StateStore(self.directory)
        self.runner = Mock()

    def tearDown(self):
        self.temp.cleanup()

    def test_private_atomic_write_and_symlink_rejected(self):
        path = self.directory / "profile.json"
        cli.atomic_json(path, {"name": "pilot"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.unlink()
        target = self.directory / "other"
        target.write_text("original")
        path.symlink_to(target)
        with self.assertRaises(StateError):
            cli.atomic_json(path, {})
        self.assertEqual(target.read_text(), "original")

    def test_parallel_operation_refuses_second_process(self):
        with cli.operation_lock(self.directory):
            with self.assertRaises(StateError):
                with cli.operation_lock(self.directory):
                    self.fail("second operation acquired lock")

    def test_network_failure_is_nonzero_and_saved(self):
        report = {"readiness": "NEEDS_HUB_ADMIN", "blockers": [{"code": "connector_not_identified"}]}
        with patch.object(cli, "load_profile", return_value={}), patch.object(cli, "verify", return_value=report):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.execute("verify", self.directory / "profile.json", self.store, self.runner), 2)
        self.assertEqual(json.loads((self.directory / "network-report.json").read_text())["codex_acceptance"], "NOT_TESTED")

    def test_successful_network_does_not_mark_codex_ready(self):
        report = {"readiness": "NETWORK_VERIFIED", "blockers": []}
        with patch.object(cli, "verify", return_value=report), contextlib.redirect_stdout(io.StringIO()):
            actual = cli.check_network({}, self.store, self.runner)
        self.assertEqual(actual["codex_acceptance"], "NOT_TESTED")

    def test_wrong_tailnet_never_resets_or_reenrolls(self):
        self.runner.run.return_value.stdout = json.dumps({"BackendState": "Running", "CurrentTailnet": {"MagicDNSSuffix": "other.ts.net"}})
        with self.assertRaises(cli.NetworkError), patch.object(cli.subprocess, "call") as interactive:
            cli.connect(self.runner, {"expected_tailnet": "tail000000.ts.net"})
        interactive.assert_not_called()

    def test_existing_stopped_identity_needs_owner_review(self):
        self.runner.run.return_value.stdout = json.dumps({"BackendState": "Stopped", "Self": {"ID": "existing"}})
        with self.assertRaises(cli.NetworkError), patch.object(cli.subprocess, "call") as interactive:
            cli.connect(self.runner, {"expected_tailnet": "tail000000.ts.net"})
        interactive.assert_not_called()

    def test_install_decline_never_calls_installers(self):
        with patch.object(cli, "detect_platform", return_value={}), patch.object(cli, "confirm", return_value=False), patch.object(cli, "ensure_packages") as install:
            with self.assertRaises(RuntimeError), contextlib.redirect_stdout(io.StringIO()):
                cli.install(self.store, self.runner)
        install.assert_not_called()

    def test_hub_guard_precedes_installer(self):
        with patch.object(cli, "load_profile", return_value={}), patch.object(cli, "guard_worker", side_effect=cli.NetworkError("hub")), patch.object(cli, "install") as installer:
            with self.assertRaises(cli.NetworkError):
                cli.execute("install", self.directory / "profile.json", self.store, self.runner)
        installer.assert_not_called()

    def test_codex_overridden_endpoint_blocks_before_probe(self):
        with patch.dict(cli.os.environ, {"OPENAI_BASE_URL": "https://other.example.org"}), patch.object(cli, "verify") as check:
            with self.assertRaises(RuntimeError):
                cli.codex_action("launch", {"domains": []}, self.store, self.runner)
        check.assert_not_called()

    def test_launch_rejects_control_only_domain_inventory(self):
        with self.assertRaises(cli.ProfileError), patch.object(cli, "verify") as verify:
            cli.codex_action("launch", {"domains": ["api.ipify.org"]}, self.store, self.runner)
        verify.assert_not_called()

    def test_launch_not_called_on_failed_network(self):
        profile = {"domains": ["chatgpt.com", "auth.openai.com"]}
        with patch.object(cli, "check_network", return_value={"readiness": "NETWORK_NOT_VERIFIED"}), patch.object(cli.subprocess, "call") as interactive:
            self.assertEqual(cli.codex_action("launch", profile, self.store, self.runner), 2)
        interactive.assert_not_called()

    def test_hub_check_untagged_is_not_ready(self):
        self.runner.run.side_effect = [Mock(stdout=json.dumps({"Self": {"TailscaleIPs": ["100.64.0.10"]}})), Mock(stdout=json.dumps({"AppConnector": {"Advertise": False}}))]
        self.assertEqual(cli.hub_check(self.runner, {"hub_ip": "100.64.0.10", "connector_tag": "tag:ai-egress-hub"})["readiness"], "NEEDS_HUB_ADMIN")

    def test_nonroot_refused_before_any_state_change(self):
        directory = self.directory / "uncreated"
        with patch.object(cli.os, "geteuid", return_value=1000), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--state-dir", str(directory), "install"]), 2)
        self.assertFalse(directory.exists())

    def test_profile_can_change_after_completed_rollback(self):
        profile = {"version": 1, "name": "old", "hub_ip": "100.64.0.10",
            "expected_tailnet": "tail000000.ts.net", "connector_tag": "tag:ai-egress-hub",
            "expected_egress_ipv4": "8.8.8.8", "domains": ["chatgpt.com", "auth.openai.com", "api.ipify.org"],
            "probe_domain": "api.ipify.org", "probe_url": "https://api.ipify.org", "non_ai_probe_url": "https://checkip.amazonaws.com"}
        path = self.directory / "profile.json"
        cli.atomic_json(path, profile)
        old_network = {"profile_id": "old-hash", "apply": {"status": "rolled_back"}}
        self.store.save({"network": old_network})
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(cli, "prompt", side_effect=["new", "100.64.0.10", "tail000000.ts.net", "tag:ai-egress-hub", "8.8.8.8", "chatgpt.com,auth.openai.com,api.ipify.org"]), patch.object(cli, "confirm", return_value=True), contextlib.redirect_stdout(io.StringIO()):
            cli.configure(self.store, path)
        self.assertEqual(json.loads(path.read_text())["name"], "new")
        actual = self.store.load()
        self.assertNotIn("network", actual)
        self.assertEqual(actual["network_history"], [old_network])

    def test_active_network_prevents_profile_replacement(self):
        profile = {"version": 1, "name": "old", "hub_ip": "100.64.0.10",
            "expected_tailnet": "tail000000.ts.net", "connector_tag": "tag:ai-egress-hub",
            "expected_egress_ipv4": "8.8.8.8", "domains": ["api.ipify.org"],
            "probe_domain": "api.ipify.org", "probe_url": "https://api.ipify.org"}
        path = self.directory / "profile.json"
        cli.atomic_json(path, profile)
        self.store.save({"network": {"apply": {"status": "applied"}}})
        before = path.read_bytes()
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(cli, "prompt", side_effect=["new", "100.64.0.10", "tail000000.ts.net", "tag:ai-egress-hub", "8.8.8.8", "api.ipify.org"]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(StateError):
                cli.configure(self.store, path)
        self.assertEqual(path.read_bytes(), before)

    def test_launcher_preflight_honors_custom_profile_and_state(self):
        path = self.directory / "custom-profile.json"
        profile = {"hub_ip": "100.64.0.10"}
        with patch.dict(cli.os.environ), patch.object(cli.os, "geteuid", return_value=0), patch.object(cli, "operation_lock", return_value=contextlib.nullcontext()), patch.object(cli, "trusted_root_directory"), patch.object(cli, "StateStore", return_value=self.store) as state_type, patch.object(cli, "load_profile", return_value=profile) as loader, patch.object(cli, "guard_worker") as guard:
            code = cli.main(["--state-dir", str(self.directory), "--profile", str(path), "--launcher-preflight", "install"])
        self.assertEqual(code, 0)
        state_type.assert_called_once_with(str(self.directory))
        loader.assert_called_once_with(path)
        self.assertEqual(guard.call_args.args[1], profile)

    def test_readonly_launcher_preflight_does_not_require_tmux_install(self):
        with patch.dict(cli.os.environ), patch.object(cli.os, "geteuid", return_value=0), patch.object(cli, "operation_lock", return_value=contextlib.nullcontext()), patch.object(cli, "trusted_root_directory"), patch.object(cli, "StateStore", return_value=self.store), patch.object(cli, "guard_worker") as guard:
            code = cli.main(["--state-dir", str(self.directory), "--launcher-preflight", "status"])
        self.assertEqual(code, 3)
        guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
