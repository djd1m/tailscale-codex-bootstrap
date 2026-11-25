"""Mock-only checks: these tests never execute network or preference commands."""
import copy
import json
import subprocess
import tempfile
import unittest

from tailnet_bootstrap.network import (
    NetworkError, ProfileError, apply_client, capture_baseline, rollback,
    validate_profile, verify,
)
from tailnet_bootstrap.state import StateStore


def profile():
    return {"version": 1, "name": "pilot", "hub_ip": "100.64.0.10",
            "expected_tailnet": "tail000000.ts.net", "connector_tag": "tag:ai-egress-hub",
            "domains": ["echo.operator.net"], "expected_egress_ipv4": "8.8.4.4",
            "probe_domain": "echo.operator.net", "probe_url": "https://echo.operator.net/ip"}


class FakeRunner:
    """Status shape follows official Tailscale ipn/ipnstate/ipnstate.go."""
    def __init__(self, route_all=True):
        self.calls = []
        self.status = {"BackendState": "Running", "CurrentTailnet": {"MagicDNSSuffix": "tail000000.ts.net"},
                       "Self": {"ID": "worker-1", "Online": True, "TailscaleIPs": ["100.77.1.2"]},
                       "Peer": {"nodekey:hub": {"ID": "hub-1", "Online": True, "TailscaleIPs": ["100.64.0.10"],
                                                "Tags": ["tag:ai-egress-hub"], "PrimaryRoutes": ["9.9.9.9/32"],
                                                "AllowedIPs": ["100.64.0.10/32", "9.9.9.9/32"]}}}
        self.prefs = {"RouteAll": route_all, "CorpDNS": True, "ExitNodeID": "", "AdvertiseTags": [],
                      "Persist": {"PrivateNodeKey": "DO_NOT_PERSIST"}}
        self.dns = {"echo.operator.net": ["9.9.9.9"], "plain.operator.net": ["1.1.1.1"]}
        self.dns6 = {}
        self.route_dev = {"9.9.9.9": "tailscale0", "1.1.1.1": "eth0"}
        self.v6_routes = {}
        self.v6_defaults = []
        self.echo = '"8.8.4.4"\n200'
        self.non_ai_echo = '{"ip":"8.8.8.8"}\n200'
        self.apply_returncode = 0
        self.apply_effect = True
        self.after_apply_error = False
        self.after_set = False

    def __call__(self, args, check=False, timeout=30):
        assert isinstance(args, list), "argv list required"
        self.calls.append(args)
        code, err = 0, ""
        if args == ["tailscale", "status", "--json"]:
            data = self.status
        elif args == ["tailscale", "debug", "prefs"]:
            if self.after_apply_error and self.after_set:
                raise subprocess.TimeoutExpired(args, timeout)
            data = self.prefs
        elif args[:2] == ["tailscale", "set"]:
            code = self.apply_returncode
            if self.apply_effect:
                self.prefs["RouteAll"] = args[-1].endswith("=true")
            self.after_set = True
            data = ""
        elif args[:2] == ["getent", "ahostsv4"]:
            data = "".join(f"{ip} STREAM {args[-1]}\n{ip} DGRAM\n" for ip in self.dns.get(args[-1], []))
            code = 0 if data else 2
        elif args[:2] == ["getent", "ahostsv6"]:
            data = "".join(f"{ip} STREAM {args[-1]}\n" for ip in self.dns6.get(args[-1], []))
            code = 0 if data else 2
        elif args == ["ip", "-6", "-j", "route", "show", "default"]:
            data = self.v6_defaults
        elif args in (["ip", "-j", "route", "show", "table", "all"], ["ip", "-6", "-j", "route", "show", "table", "all"]):
            data = [{"dst": "default", "dev": "eth0"}] if "-6" not in args else self.v6_defaults
        elif args[:5] == ["ip", "-6", "-j", "route", "get"]:
            data = self.v6_routes.get(args[-1], [])
            if not data:
                code, err = 2, "RTNETLINK answers: Network is unreachable"
        elif args[:4] == ["ip", "-j", "route", "get"]:
            data = [{"dst": args[-1], "dev": self.route_dev.get(args[-1], "eth0")}]
        elif args[0] == "curl":
            data = self.echo if "echo.operator.net" in args[-1] else self.non_ai_echo
        else:
            raise AssertionError(f"Unexpected command {args}")
        output = data if isinstance(data, str) else json.dumps(data)
        return subprocess.CompletedProcess(args, code, stdout=output, stderr=err)

    def mutations(self):
        return [args for args in self.calls if args[:2] == ["tailscale", "set"]]


class ProfileTests(unittest.TestCase):
    def test_valid_profile_copies_input(self):
        data = profile()
        parsed = validate_profile(data)
        parsed["domains"].append("another.operator.net")
        self.assertEqual(len(data["domains"]), 1)

    def test_tenant_identity_is_configurable_with_safe_formats(self):
        data = profile()
        data.update(hub_ip="100.127.255.254", expected_tailnet="another-tailnet.ts.net",
                    connector_tag="tag:custom-hub-2")
        self.assertEqual(validate_profile(data), data)
        for changes in ({"hub_ip": "100.63.255.255"}, {"hub_ip": "192.0.2.1"},
                        {"expected_tailnet": "https://tail000000.ts.net"},
                        {"expected_tailnet": "TAIL000000.ts.net"},
                        {"expected_tailnet": "tail000000.ts.net."},
                        {"expected_tailnet": "nested.tail000000.ts.net"},
                        {"connector_tag": "tag:$(whoami)"}, {"connector_tag": "tag:2hub"}):
            with self.subTest(changes=changes):
                invalid = profile(); invalid.update(changes)
                with self.assertRaises(ProfileError):
                    validate_profile(invalid)

    def test_rejects_unsafe_profiles(self):
        cases = [
            {"version": True}, {"version": 2}, {"name": "$(touch /tmp/evil)"},
            {"hub_ip": "100.128.0.1"}, {"expected_tailnet": "user@example.com"},
            {"connector_tag": "tag:Invalid"}, {"expected_egress_ipv4": "10.0.0.1"},
            {"expected_egress_ipv4": "224.0.0.1"}, {"expected_egress_ipv4": 134744072},
            {"domains": ["*.operator.net"]}, {"domains": ["9.9.9.9"]},
            {"domains": ["echo.operator.net", "echo.operator.net"]},
            {"probe_url": "http://echo.operator.net/ip"},
            {"probe_url": "https://user:password@echo.operator.net/ip"},
            {"probe_url": "https://echo.operator.net/ip?token=secret"},
            {"probe_url": "https://echo.operator.net:8443/ip"},
            {"probe_url": "https://different.operator.net/ip"},
            {"probe_domain": "outside.operator.net"}, {"unexpected": True},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                data = profile(); data.update(changes)
                with self.assertRaises(ProfileError):
                    validate_profile(data)


class VerificationTests(unittest.TestCase):
    def codes(self, result):
        return {item["code"] for item in result["blockers"]}

    def test_successful_ipv4_snapshot_is_read_only(self):
        runner = FakeRunner()
        result = verify(profile(), runner, environ={})
        self.assertEqual(result["readiness"], "NETWORK_VERIFIED", result)
        self.assertEqual(result["evidence"]["control_endpoint_observed_ipv4"], "8.8.4.4")
        self.assertFalse(runner.mutations())
        curl = next(args for args in runner.calls if args[0] == "curl")
        self.assertEqual(curl[1], "--disable")
        self.assertIn("--noproxy", curl); self.assertIn("--resolve", curl)
        self.assertIn("--max-filesize", curl); self.assertNotIn("--insecure", curl)
        self.assertTrue(any("OpenAI" in line for line in result["limitations"]))

    def test_plain_ipv4_echo_is_supported(self):
        runner = FakeRunner(); runner.echo = "8.8.4.4\n\n200"
        self.assertEqual(verify(profile(), runner, environ={})["readiness"], "NETWORK_VERIFIED")

    def test_checks_every_dns_answer(self):
        runner = FakeRunner(); runner.dns["echo.operator.net"].append("1.0.0.1")
        result = verify(profile(), runner, environ={})
        self.assertIn("route_not_tailscale", self.codes(result))
        self.assertIn("primary_route_wrong_connector", self.codes(result))
        self.assertFalse(any(args[0] == "curl" for args in runner.calls))

    def test_wrong_tailnet_and_proxy_and_exit_block_probe(self):
        runner = FakeRunner(); runner.status["CurrentTailnet"]["MagicDNSSuffix"] = "wrong.ts.net"
        runner.prefs["ExitNodeID"] = "exit-123"
        result = verify(profile(), runner, environ={"HTTPS_PROXY": "SECRET", "NO_PROXY": "local"})
        self.assertTrue({"wrong_tailnet", "proxy_environment", "exit_node_active"} <= self.codes(result))
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertFalse(any(args[0] == "curl" for args in runner.calls))

    def test_tls_environment_override_is_reported_by_name_only(self):
        runner = FakeRunner()
        result = verify(profile(), runner, environ={"CURL_CA_BUNDLE": "/secret/private/path.pem"})
        self.assertIn("tls_override_environment", self.codes(result))
        self.assertEqual(result["evidence"]["tls_override_variable_names"], ["CURL_CA_BUNDLE"])
        self.assertNotIn("private/path", json.dumps(result))
        self.assertFalse(any(args[0] == "curl" for args in runner.calls))

    def test_hub_tag_and_online_both_required(self):
        for change, code in [({"Online": False}, "hub_offline"), ({"Tags": ["tag:wrong"]}, "connector_not_identified")]:
            runner = FakeRunner(); runner.status["Peer"]["nodekey:hub"].update(change)
            result = verify(profile(), runner, environ={})
            self.assertIn(code, self.codes(result))
            self.assertEqual(result["readiness"], "NEEDS_HUB_ADMIN")

    def test_self_can_identify_connector_for_diagnostics(self):
        runner = FakeRunner()
        runner.status["Self"] = runner.status["Peer"].pop("nodekey:hub")
        self.assertEqual(verify(profile(), runner, environ={})["readiness"], "NETWORK_VERIFIED")

    def test_other_connector_cannot_satisfy_primary_route(self):
        runner = FakeRunner()
        runner.status["Peer"]["nodekey:hub"]["PrimaryRoutes"] = []
        runner.status["Peer"]["nodekey:other"] = {"ID": "other", "PrimaryRoutes": ["9.9.9.9/32"]}
        result = verify(profile(), runner, environ={})
        self.assertIn("primary_route_wrong_connector", self.codes(result))
        self.assertIn("competing_connector", self.codes(result))

    def test_unrelated_primary_and_offered_routes_are_exposed(self):
        for field in ("PrimaryRoutes", "AllowedIPs"):
            runner = FakeRunner(); runner.status["Peer"]["nodekey:hub"][field].append("10.0.0.0/8")
            result = verify(profile(), runner, environ={})
            self.assertIn("unexpected_routes", self.codes(result))

    def test_unselected_exit_node_offer_does_not_select_it(self):
        runner = FakeRunner()
        runner.status["Peer"]["nodekey:exit"] = {"ID": "exit", "ExitNodeOption": True, "AllowedIPs": ["0.0.0.0/0", "::/0"]}
        self.assertEqual(verify(profile(), runner, environ={})["readiness"], "NETWORK_VERIFIED")

    def test_route_and_prefs_and_public_dns_are_mandatory(self):
        runner = FakeRunner(); runner.prefs["RouteAll"] = False; runner.prefs["CorpDNS"] = False
        runner.dns["echo.operator.net"] = ["127.0.0.1"]
        result = verify(profile(), runner, environ={})
        self.assertTrue({"route_acceptance_disabled", "tailscale_dns_disabled", "nonpublic_destination"} <= self.codes(result))

    def test_ipv6_unreachable_on_ipv4_only_vm_is_a_limited_snapshot(self):
        runner = FakeRunner(); runner.dns6["echo.operator.net"] = ["2606:4700:4700::1111"]
        result = verify(profile(), runner, environ={})
        self.assertEqual(result["readiness"], "NETWORK_VERIFIED", result)

    def test_ipv6_usable_or_default_egress_blocks_ipv4_acceptance(self):
        runner = FakeRunner(); runner.dns6["echo.operator.net"] = ["2606:4700:4700::1111"]
        runner.v6_routes["2606:4700:4700::1111"] = [{"dev": "tailscale0"}]
        runner.v6_defaults = [{"dst": "default", "dev": "eth0"}]
        result = verify(profile(), runner, environ={})
        self.assertTrue({"ipv6_requires_separate_acceptance", "ipv6_egress_available"} <= self.codes(result))

    def test_v4_mapped_getent_ipv6_answers_do_not_make_false_ipv6_claims(self):
        runner = FakeRunner(); runner.dns6["echo.operator.net"] = ["::ffff:9.9.9.9"]
        self.assertEqual(verify(profile(), runner, environ={})["readiness"], "NETWORK_VERIFIED")

    def test_probe_redirect_malformed_private_and_wrong_egress_fail(self):
        for echo, code in [('"8.8.4.4"\n302', "probe_failed"), ('not-json\n200', "probe_failed"),
                           ('{"ip":"10.0.0.1"}\n200', "probe_failed"), ('{"ip":134744072}\n200', "probe_failed"),
                           ('"8.8.8.8"\n200', "egress_mismatch"), ('x' * 65543, "probe_failed")]:
            with self.subTest(code=code):
                runner = FakeRunner(); runner.echo = echo
                self.assertIn(code, self.codes(verify(profile(), runner, environ={})))

    def test_optional_non_ai_probe_must_show_different_normal_egress(self):
        data = profile(); data["non_ai_probe_url"] = "https://plain.operator.net/ip"
        runner = FakeRunner()
        self.assertEqual(verify(data, runner, environ={})["readiness"], "NETWORK_VERIFIED")
        runner.non_ai_echo = '"8.8.4.4"\n200'
        self.assertIn("non_ai_egress_matches_hub", self.codes(verify(data, runner, environ={})))

    def test_non_ai_ip_collision_is_blocked(self):
        data = profile(); data["non_ai_probe_url"] = "https://plain.operator.net/ip"
        runner = FakeRunner(); runner.route_dev["1.1.1.1"] = "tailscale0"
        self.assertIn("non_ai_route_tailscale", self.codes(verify(data, runner, environ={})))

    def test_missing_program_is_structured_failure(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError()
        result = verify(profile(), missing, environ={})
        self.assertIn("inspection_failed", self.codes(result))
        self.assertEqual(result["readiness"], "NETWORK_NOT_VERIFIED")


class ApplyRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = StateStore(self.temp.name)

    def test_no_confirmation_no_read_or_mutation(self):
        runner = FakeRunner(False)
        result = apply_client(profile(), self.state, runner, environ={})
        self.assertEqual(result["blockers"][0]["code"], "confirmation_required")
        self.assertEqual(runner.calls, [])

    def test_baseline_omits_daemon_secrets_and_proxy_values(self):
        data = capture_baseline(profile(), FakeRunner(False), environ={"HTTPS_PROXY": "TOP_SECRET"})
        self.assertEqual(set(data["prefs"]), {"RouteAll", "CorpDNS", "ExitNodeID", "AdvertiseTags"})
        self.assertEqual(data["proxy_variable_names"], ["HTTPS_PROXY"])
        self.assertNotIn("SECRET", json.dumps(data))
        self.assertNotIn("PrivateNodeKey", json.dumps(data))

    def test_apply_is_idempotent_and_rollback_preserves_other_preferences(self):
        runner = FakeRunner(False)
        result = apply_client(profile(), self.state, runner, confirmed=True, environ={})
        self.assertTrue(result["changed"])
        self.assertFalse(self.state.load()["network"]["baseline"]["prefs"]["RouteAll"])
        apply_client(profile(), self.state, runner, confirmed=True, environ={})
        self.assertEqual(len(runner.mutations()), 1)
        runner.prefs["CorpDNS"] = False; runner.prefs["AdvertiseTags"] = ["tag:new"]
        self.assertTrue(rollback(profile(), self.state, runner)["changed"])
        self.assertFalse(runner.prefs["RouteAll"])
        self.assertFalse(runner.prefs["CorpDNS"])
        self.assertEqual(runner.prefs["AdvertiseTags"], ["tag:new"])
        rollback(profile(), self.state, runner)
        self.assertEqual(len(runner.mutations()), 2)
        self.assertEqual(runner.mutations(), [["tailscale", "set", "--accept-routes=true"], ["tailscale", "set", "--accept-routes=false"]])

    def test_existing_enabled_setting_is_not_owned_or_rolled_back(self):
        runner = FakeRunner(True)
        result = apply_client(profile(), self.state, runner, confirmed=True, environ={})
        self.assertFalse(result["changed"])
        self.assertTrue(result["already_enabled"])
        rollback(profile(), self.state, runner)
        self.assertFalse(runner.mutations())

    def test_crash_after_mutation_keeps_rollback_intent_and_original_baseline(self):
        runner = FakeRunner(False); runner.after_apply_error = True
        with self.assertRaises(NetworkError):
            apply_client(profile(), self.state, runner, confirmed=True, environ={})
        self.assertEqual(self.state.load()["network"]["apply"]["status"], "pending")
        runner.after_apply_error = False
        apply_client(profile(), self.state, runner, confirmed=True, environ={})
        self.assertFalse(self.state.load()["network"]["baseline"]["prefs"]["RouteAll"])
        self.assertTrue(rollback(profile(), self.state, runner)["changed"])

    def test_rollback_does_not_cross_node_identity(self):
        runner = FakeRunner(False)
        apply_client(profile(), self.state, runner, confirmed=True, environ={})
        runner.status["Self"]["ID"] = "replacement-node"
        result = rollback(profile(), self.state, runner)
        self.assertEqual(result["blockers"][0]["code"], "identity_changed")
        self.assertEqual(len(runner.mutations()), 1)

    def test_route_extras_and_proxy_block_before_any_write(self):
        runner = FakeRunner(False); runner.status["Peer"]["nodekey:hub"]["AllowedIPs"].append("192.168.0.0/16")
        result = apply_client(profile(), self.state, runner, confirmed=True, environ={"ALL_PROXY": "socks://SECRET"})
        self.assertEqual({b["code"] for b in result["blockers"]}, {"unexpected_routes", "proxy_environment"})
        self.assertFalse(runner.mutations()); self.assertEqual(self.state.load(), {})

    def test_failed_cli_can_still_be_safely_rolled_back(self):
        runner = FakeRunner(False); runner.apply_returncode = 1
        result = apply_client(profile(), self.state, runner, confirmed=True, environ={})
        self.assertTrue(result["changed"])
        self.assertEqual(result["blockers"][0]["code"], "apply_failed")
        runner.apply_returncode = 0
        self.assertTrue(rollback(profile(), self.state, runner)["changed"])

    def test_dns_disabled_or_missing_connector_blocks_apply(self):
        for field in ("dns", "tag", "offline", "self"):
            runner = FakeRunner(False)
            if field == "dns":
                runner.prefs["CorpDNS"] = False
            elif field == "tag":
                runner.status["Peer"]["nodekey:hub"]["Tags"] = []
            elif field == "offline":
                runner.status["Peer"]["nodekey:hub"]["Online"] = False
            else:
                runner.status["Self"] = runner.status["Peer"].pop("nodekey:hub")
            result = apply_client(profile(), self.state, runner, confirmed=True, environ={})
            self.assertTrue(result["blockers"])
            self.assertFalse(runner.mutations())

    def test_missing_selected_route_and_ipv6_path_block_before_mutation(self):
        for problem in ("route", "ipv6_route", "ipv6_default", "private_dns"):
            runner = FakeRunner(False)
            if problem == "route":
                runner.status["Peer"]["nodekey:hub"]["PrimaryRoutes"] = []
            elif problem == "ipv6_route":
                runner.dns6["echo.operator.net"] = ["2606:4700:4700::1111"]
                runner.v6_routes["2606:4700:4700::1111"] = [{"dev": "eth0"}]
            elif problem == "ipv6_default":
                runner.v6_defaults = [{"dst": "default", "dev": "eth0", "table": 123}]
            else:
                runner.dns["echo.operator.net"] = ["10.0.0.1"]
            result = apply_client(profile(), self.state, runner, confirmed=True, environ={})
            self.assertTrue(result["blockers"], problem)
            self.assertFalse(runner.mutations())

    def test_offered_selected_ipv6_route_cannot_become_usable_after_apply(self):
        for field in ("PrimaryRoutes", "AllowedIPs"):
            with self.subTest(field=field):
                runner = FakeRunner(False)
                # IPv6 is currently unreachable, but accepting the advertised
                # host route would itself create a usable IPv6 path.
                runner.dns6["echo.operator.net"] = ["2606:4700:4700::1111"]
                runner.status["Peer"]["nodekey:hub"][field].append("2606:4700:4700::1111/128")
                result = apply_client(profile(), self.state, runner, confirmed=True, environ={})
                self.assertIn("ipv6_routes_require_separate_acceptance", {item["code"] for item in result["blockers"]})
                self.assertFalse(runner.mutations())
                self.assertEqual(self.state.load(), {})

    def test_overlay_ipv6_hosts_and_unselected_exit_offer_do_not_install_subnet_paths(self):
        runner = FakeRunner(False)
        hub = runner.status["Peer"]["nodekey:hub"]
        hub["TailscaleIPs"].append("fd7a:115c:a1e0::1234")
        hub["AllowedIPs"].append("fd7a:115c:a1e0::1234/128")
        runner.status["Peer"]["nodekey:exit"] = {"ID": "exit", "ExitNodeOption": True,
                                                     "AllowedIPs": ["0.0.0.0/0", "::/0"]}
        result = apply_client(profile(), self.state, runner, confirmed=True, environ={})
        self.assertTrue(result["changed"], result)
        self.assertFalse(result["blockers"])

    def test_route_change_during_apply_is_rechecked_under_lock(self):
        runner = FakeRunner(False)
        status_reads = 0
        def changed(args, **kwargs):
            nonlocal status_reads
            if args == ["tailscale", "status", "--json"]:
                status_reads += 1
                if status_reads == 2:
                    runner.status["Peer"]["nodekey:hub"]["PrimaryRoutes"] = []
            return runner(args, **kwargs)
        result = apply_client(profile(), self.state, changed, confirmed=True, environ={})
        self.assertEqual(result["blockers"][0]["code"], "primary_route_wrong_connector")
        self.assertFalse(runner.mutations())

    def test_new_mutation_after_initial_noop_captures_its_actual_baseline(self):
        runner = FakeRunner(True)
        apply_client(profile(), self.state, runner, confirmed=True, environ={})
        runner.prefs["RouteAll"] = False
        self.assertTrue(apply_client(profile(), self.state, runner, confirmed=True, environ={})["changed"])
        self.assertTrue(rollback(profile(), self.state, runner)["changed"])
        self.assertFalse(runner.prefs["RouteAll"])

    def test_no_actual_change_does_not_create_rollback_mutation(self):
        runner = FakeRunner(False); runner.apply_effect = False
        result = apply_client(profile(), self.state, runner, confirmed=True, environ={})
        self.assertFalse(result["changed"])
        rollback(profile(), self.state, runner)
        self.assertEqual(len(runner.mutations()), 1)


if __name__ == "__main__":
    unittest.main()
