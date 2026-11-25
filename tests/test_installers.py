"""Installer contract tests: every external action is simulated."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tailnet_bootstrap import installers as m


UBUNTU = {"id": "ubuntu", "version": "24.04", "arch": "amd64", "codename": "noble"}


class FakeRunner:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def run(self, args, check=False, timeout=30):
        self.calls.append((list(args), timeout))
        if self.handler:
            value = self.handler(args)
            if value is not None:
                return value
        return subprocess.CompletedProcess(args, 0, "", "")


def completed(args, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, rc, stdout, stderr)


def record(data):
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def make_package(home):
    root = home / ".codex/packages/standalone/releases/0.172.0-x86_64-unknown-linux-musl"
    for name in ("bin/codex", "bin/codex-code-mode-host", "bin/codex-exec-server",
                 "codex-path/rg", "codex-resources/bwrap"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test executable; never executed\n")
        path.chmod(0o755)
    (root / "codex-package.json").write_text(json.dumps({"execServer": "bin/codex-exec-server"}))
    binary = home / ".local/bin/codex"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.symlink_to(root / "bin/codex")
    return binary, root


class PlatformTests(unittest.TestCase):
    def test_explicit_supported_matrix_and_architectures(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "os-release"
            for (os_id, version), code in m.SUPPORTED.items():
                path.write_text(f'ID={os_id}\nVERSION_ID="{version}"\n')
                for machine, arch in (("x86_64", "amd64"), ("aarch64", "arm64")):
                    self.assertEqual(m.detect_platform(path, machine),
                                     {"id": os_id, "version": version, "arch": arch, "codename": code})

    def test_wrong_distribution_or_architecture_fails(self):
        for info in ({"id": "ubuntu", "version": "26.04", "arch": "amd64"},
                     {"id": "linuxmint", "version": "22", "arch": "amd64"},
                     {"id": "debian", "version": "13", "arch": "riscv64"},
                     {**UBUNTU, "codename": "jammy"}):
            runner = FakeRunner()
            with self.assertRaises(m.InstallerError):
                m.ensure_packages(runner, user_confirmed=True, platform_info=info)
            self.assertEqual(runner.calls, [])

    def test_os_release_is_not_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "marker"
            path = Path(temp) / "os-release"
            path.write_text(f'ID=ubuntu\nVERSION_ID="$(touch {marker})"\n')
            with self.assertRaises(m.InstallerError):
                m.detect_platform(path, "x86_64")
            self.assertFalse(marker.exists())


class AuthorizationAndAptTests(unittest.TestCase):
    def test_no_commands_without_explicit_confirmation(self):
        for func in (m.ensure_packages, m.ensure_tailscale, m.ensure_codex):
            runner = FakeRunner()
            for value in (False, None, 1, "yes"):
                with self.assertRaises(m.InstallerError):
                    func(runner, user_confirmed=value, platform_info=UBUNTU)
            self.assertEqual(runner.calls, [])

    def test_existing_packages_do_not_run_apt(self):
        runner = FakeRunner(lambda args: completed(args, stdout="".join(f"{p}\tinstalled\n" for p in m.PACKAGES)))
        facts = m.ensure_packages(runner, user_confirmed=True, platform_info=UBUNTU)
        self.assertFalse(facts["changed"])
        self.assertEqual(len(runner.calls), 1)

    def test_only_missing_packages_installed_and_checked(self):
        installed = set(m.PACKAGES) - {"tmux", "dnsutils"}

        def handle(args):
            if args[0] == "dpkg-query":
                return completed(args, stdout="".join(f"{p}\tinstalled\n" for p in sorted(installed)))
            if "apt-get" in args and "install" in args:
                installed.update(args[args.index("install") + 1:])
            return None

        runner = FakeRunner(handle)
        facts = m.ensure_packages(runner, user_confirmed=True, platform_info=UBUNTU)
        self.assertEqual(facts["installed"], ["tmux", "dnsutils"])
        apt_calls = [args for args, _ in runner.calls if "apt-get" in args]
        self.assertEqual(len(apt_calls), 2)
        self.assertEqual(apt_calls[-1][-3:], ["install", "tmux", "dnsutils"])
        for args in apt_calls:
            self.assertIn("DPkg::Lock::Timeout=60", args)
            self.assertNotIn("upgrade", args)
        self.assertIn("--no-upgrade", apt_calls[-1])

    @patch.object(m.time, "sleep")
    def test_apt_errors_are_bounded_and_actionable(self, sleep):
        runner = FakeRunner(lambda args: completed(args, 100, stderr="Could not get lock: possible secret"))
        with self.assertRaises(m.InstallerError) as ctx:
            m._apt(runner, "update")
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("Do not delete lock files", str(ctx.exception))
        self.assertNotIn("possible secret", str(ctx.exception))
        self.assertTrue(all(timeout == 480 for _, timeout in runner.calls))

    @patch.object(m.time, "sleep")
    def test_apt_timeout_has_bounded_retry(self, sleep):
        def timeout(args):
            raise subprocess.TimeoutExpired(args, 480, output="sensitive captured output")
        runner = FakeRunner(timeout)
        with self.assertRaises(m.InstallerError) as ctx:
            m._apt(runner, "install", ("tmux",))
        self.assertEqual(len(runner.calls), 3)
        self.assertNotIn("sensitive", str(ctx.exception))


class DownloadTests(unittest.TestCase):
    def test_tampered_script_never_executes(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "install.sh"
            def handle(args):
                Path(args[args.index("--output") + 1]).write_bytes(b"tampered")
            runner = FakeRunner(handle)
            with self.assertRaisesRegex(m.InstallerError, "checksum mismatch"):
                m._download(runner, "https://github.com/openai/codex/releases/download/rust-v0.172.0/install.sh",
                            script, record(b"reviewed"))
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][0][0], "curl")
            self.assertEqual(runner.calls[0][0][1], "--disable")

    def test_unapproved_or_insecure_urls_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            for url in ("http://pkgs.tailscale.com/stable/key", "https://example.com/install.sh",
                        "https://github.com/other/codex/releases/download/v1/install.sh",
                        "https://pkgs.tailscale.com:444/stable/key", "https://pkgs.tailscale.com/stable/key?redirect=x"):
                runner = FakeRunner()
                with self.assertRaises(m.InstallerError):
                    m._download(runner, url, Path(temp) / "file", record(b"expected"))
                self.assertEqual(runner.calls, [])

    def test_reviewed_lock_contains_all_supported_sources(self):
        lock = m._locks()
        for (os_id, _), code in m.SUPPORTED.items():
            for suffix in ("noarmor.gpg", "tailscale-keyring.list"):
                self.assertIn(f"https://pkgs.tailscale.com/stable/{os_id}/{code}.{suffix}", lock["tailscale"])
        self.assertEqual(lock["codex"]["version"], "0.172.0")


class TailscaleTests(unittest.TestCase):
    def test_installed_tailscale_only_enables_its_service(self):
        runner = FakeRunner(lambda args: completed(args, stdout="1.104.7\n") if args[:2] == ["tailscale", "version"] else None)
        facts = m.ensure_tailscale(runner, user_confirmed=True, platform_info=UBUNTU)
        self.assertFalse(facts["changed"])
        self.assertEqual([args for args, _ in runner.calls], [
            ["tailscale", "version"], ["systemctl", "enable", "--now", "tailscaled.service"],
            ["systemctl", "is-active", "--quiet", "tailscaled.service"]])

    def test_existing_different_key_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / "key.gpg"
            key.write_bytes(b"pre-existing third-party key")
            runner = FakeRunner(lambda args: completed(args, 127))
            with patch.object(m, "TAILSCALE_KEY", key), patch.object(m, "_check_other_tailscale_sources"):
                with self.assertRaisesRegex(m.InstallerError, "left unchanged"):
                    m.ensure_tailscale(runner, user_confirmed=True, platform_info=UBUNTU)
            self.assertEqual(key.read_bytes(), b"pre-existing third-party key")
            self.assertEqual(len(runner.calls), 1)

    def test_another_source_is_reported_without_modification(self):
        with tempfile.TemporaryDirectory() as temp:
            source_dir = Path(temp) / "sources"
            source_dir.mkdir()
            other = source_dir / "vendor.sources"
            other.write_text("Types: deb\nURIs: https://pkgs.tailscale.com/stable/debian\nSuites: bookworm\n")
            with patch.object(m, "APT_SOURCE_DIRECTORY", source_dir), patch.object(m, "APT_SOURCE_MAIN", Path(temp) / "main"):
                with self.assertRaisesRegex(m.InstallerError, "vendor.sources"):
                    m._check_other_tailscale_sources()

    def test_fresh_install_and_rerun_are_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            base_dir = Path(temp)
            source, key = base_dir / "sources/tailscale.list", base_dir / "keyrings/key.gpg"
            base_url = "https://pkgs.tailscale.com/stable/ubuntu/noble"
            source_bytes = ("# Tailscale packages for ubuntu noble\n"
                            "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] "
                            "https://pkgs.tailscale.com/stable/ubuntu noble main\n\n").encode()
            downloads = {base_url + ".noarmor.gpg": b"test reviewed key",
                         base_url + ".tailscale-keyring.list": source_bytes}
            installed = False
            def handle(args):
                nonlocal installed
                if args[:2] == ["tailscale", "version"]:
                    return completed(args, 0 if installed else 127, "1.104.7\n" if installed else "")
                if args[0] == "curl":
                    Path(args[args.index("--output") + 1]).write_bytes(downloads[args[-1]])
                elif args[0] == "install" and "-d" in args:
                    Path(args[-1]).mkdir(parents=True, exist_ok=True)
                elif args[0] == "install":
                    shutil.copyfile(args[-2], args[-1])
                elif args[0] == "ln":
                    os.link(args[-2], args[-1])
                elif args[0] == "rm":
                    Path(args[-1]).unlink(missing_ok=True)
                elif "apt-get" in args and "install" in args:
                    installed = True
                return None
            runner = FakeRunner(handle)
            with patch.object(m, "TAILSCALE_KEY", key), patch.object(m, "APT_SOURCE", source), \
                 patch.object(m, "_check_other_tailscale_sources"), \
                 patch.object(m, "_locks", return_value={"tailscale": {url: record(data) for url, data in downloads.items()}}):
                facts = m.ensure_tailscale(runner, user_confirmed=True, platform_info=UBUNTU)
                self.assertTrue(facts["changed"])
                before = len(runner.calls)
                facts = m.ensure_tailscale(runner, user_confirmed=True, platform_info=UBUNTU)
                self.assertFalse(facts["changed"])
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(len(runner.calls) - before, 3)
            all_args = [args for args, _ in runner.calls]
            self.assertFalse(any("up" in args or "--ssh" in args or "--advertise-tags" in args for args in all_args))

    def test_new_file_publication_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            source, destination = Path(temp) / "new", Path(temp) / "existing"
            source.write_text("reviewed")
            destination.write_text("other")
            def handle(args):
                if args[0] == "ln":
                    return completed(args, 1)
                return None
            with self.assertRaises(m.InstallerError):
                m._install_new_file(FakeRunner(handle), source, destination)
            self.assertEqual(destination.read_text(), "other")


class CodexTests(unittest.TestCase):
    def test_canonical_partial_install_is_blocked_before_probing(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            _, root = make_package(home)
            (root / "codex-path/rg").unlink()
            runner = FakeRunner()
            with patch.object(m.shutil, "which", return_value=None):
                with self.assertRaisesRegex(m.InstallerError, "canonical Codex package.*incomplete") as ctx:
                    m.ensure_codex(runner, user_confirmed=True, platform_info=UBUNTU, user_home=home)
            self.assertIn("repair it with the official installer", str(ctx.exception))
            self.assertEqual(runner.calls, [])

    def test_canonical_malformed_manifest_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            _, root = make_package(home)
            (root / "codex-package.json").write_text("not valid JSON")
            runner = FakeRunner()
            with self.assertRaisesRegex(m.InstallerError, "incomplete"):
                m.ensure_codex(runner, user_confirmed=True, platform_info=UBUNTU, user_home=home)
            self.assertEqual(runner.calls, [])

    def test_only_trusted_system_path_used_for_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / ".local/bin/codex"
            with patch.dict(os.environ, {"PATH": "/tmp/arbitrary-user-bin:/tmp"}), \
                 patch.object(m.shutil, "which", return_value="/usr/bin/codex") as which:
                self.assertEqual(m._existing_codex(destination), "/usr/bin/codex")
            which.assert_called_once_with("codex", path=m.TRUSTED_PATH)

    def test_expected_home_install_precedes_path(self):
        with tempfile.TemporaryDirectory() as temp:
            binary, _ = make_package(Path(temp))
            with patch.object(m.shutil, "which", return_value="/usr/bin/codex") as which:
                self.assertEqual(m._existing_codex(binary), str(binary))
            which.assert_not_called()

    def test_existing_working_cli_is_preserved(self):
        def handle(args):
            return completed(args, stdout="codex-cli 0.170.0\n" if args[-1] == "--version" else "Codex CLI\nUsage: codex [OPTIONS]\n")
        runner = FakeRunner(handle)
        with tempfile.TemporaryDirectory() as temp, patch.object(m.shutil, "which", return_value="/usr/local/bin/codex"):
            facts = m.ensure_codex(runner, user_confirmed=True, platform_info=UBUNTU, user_home=temp)
        self.assertEqual(facts["status"], "existing_not_reinstalled")
        self.assertFalse(facts["changed"])
        self.assertFalse(facts["package_verified"])
        self.assertEqual(len(runner.calls), 2)

    def test_broken_existing_codex_is_not_silently_replaced(self):
        runner = FakeRunner(lambda args: completed(args, 1))
        with tempfile.TemporaryDirectory() as temp, patch.object(m.shutil, "which", return_value="/opt/codex"):
            with self.assertRaises(m.InstallerError):
                m.ensure_codex(runner, user_confirmed=True, platform_info=UBUNTU, user_home=temp)
        self.assertEqual(len(runner.calls), 1)

    def test_official_installer_runs_only_after_verification_with_clean_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            script_bytes = b"# test fixture, never executed\n"
            lock = {"codex": {**record(script_bytes), "version": "0.172.0",
                             "url": "https://github.com/openai/codex/releases/download/rust-v0.172.0/install.sh"}}
            def handle(args):
                if args[0] == "curl":
                    Path(args[args.index("--output") + 1]).write_bytes(script_bytes)
                elif args[:2] == ["env", "-i"]:
                    self.assertIn("CODEX_NON_INTERACTIVE=1", args)
                    self.assertIn(f"CODEX_INSTALL_DIR={home / '.local/bin'}", args)
                    self.assertIn(f"CODEX_HOME={home / '.codex'}", args)
                    self.assertIn("CODEX_INSTALL_DAEMON_ONLY=0", args)
                    self.assertEqual(Path(args[args.index("/bin/sh") + 1]).read_bytes(), script_bytes)
                    make_package(home)
                elif args[-1] == "--version":
                    return completed(args, stdout="codex-cli 0.172.0\n")
                elif args[-1] == "--help":
                    return completed(args, stdout="Codex CLI\nUsage: codex [OPTIONS]\n")
                return None
            runner = FakeRunner(handle)
            with patch.object(m.shutil, "which", return_value=None), patch.object(m, "_locks", return_value=lock):
                facts = m.ensure_codex(runner, user_confirmed=True, platform_info=UBUNTU, user_home=home)
            self.assertEqual(facts["status"], "installed_official_package")
            self.assertTrue(facts["package_verified"])
            self.assertEqual([args[-1] for args, _ in runner.calls[-2:]], ["--version", "--help"])
            self.assertFalse(any("login" in args or "exec" in args for args, _ in runner.calls))

    def test_missing_required_package_helper_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            binary, root = make_package(Path(temp))
            self.assertTrue(m._package_verified(str(binary)))
            (root / "codex-resources/bwrap").unlink()
            self.assertFalse(m._package_verified(str(binary)))


if __name__ == "__main__":
    unittest.main()
