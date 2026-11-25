"""Bounded, explicitly authorized installers for the supported VM matrix.

Every command is an argument list. Nothing here enrolls Tailscale, changes the
firewall, reads Codex credentials, or starts an interactive Codex session.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit


class InstallerError(RuntimeError):
    """An installation stopped without silently replacing existing state."""


class Runner(Protocol):
    def run(self, args: list[str], check: bool = False,
            timeout: int = 30) -> subprocess.CompletedProcess[str]: ...


SUPPORTED = {
    ("ubuntu", "22.04"): "jammy", ("ubuntu", "24.04"): "noble",
    ("debian", "12"): "bookworm", ("debian", "13"): "trixie",
}
ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64",
                 "aarch64": "arm64", "arm64": "arm64"}
PACKAGES = ("curl", "ca-certificates", "gnupg", "tmux", "iproute2",
            "procps", "dnsutils", "git")
APT_SOURCE = Path("/etc/apt/sources.list.d/tailscale.list")
TAILSCALE_KEY = Path("/usr/share/keyrings/tailscale-archive-keyring.gpg")
APT_SOURCE_DIRECTORY = Path("/etc/apt/sources.list.d")
APT_SOURCE_MAIN = Path("/etc/apt/sources.list")
LOCK_PATH = Path(__file__).with_name("installer-lock.json")
TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def detect_platform(os_release_path: str | Path = "/etc/os-release",
                    machine: str | None = None) -> dict[str, str]:
    """Read os-release as data; accept only reviewed Debian/Ubuntu releases."""
    values: dict[str, str] = {}
    try:
        for line in Path(os_release_path).read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            name, raw = line.split("=", 1)
            parsed = shlex.split(raw, comments=True)
            if len(parsed) == 1:
                values[name] = parsed[0]
    except (OSError, ValueError) as exc:
        raise InstallerError("Cannot read /etc/os-release safely; use a supported Ubuntu/Debian image.") from exc
    return _validate_platform({"id": values.get("ID", ""),
                               "version": values.get("VERSION_ID", ""),
                               "arch": machine or platform.machine()})


def _validate_platform(info: dict[str, str]) -> dict[str, str]:
    os_id, version = info.get("id", ""), info.get("version", "")
    codename = SUPPORTED.get((os_id, version))
    arch = ARCHITECTURES.get(info.get("arch", ""))
    if codename is None or arch is None:
        raise InstallerError(
            "Unsupported platform: this bootstrap supports Ubuntu 22.04/24.04 and "
            "Debian 12/13 on amd64/arm64 only. No package changes were made.")
    if info.get("codename", codename) != codename:
        raise InstallerError("OS version and codename disagree; refusing to select an apt repository.")
    return {"id": os_id, "version": version, "arch": arch, "codename": codename}


def _authorized(user_confirmed: bool) -> None:
    if user_confirmed is not True:
        raise InstallerError("Installation needs explicit confirmation (user_confirmed=True).")


def _platform(info: dict[str, str] | None) -> dict[str, str]:
    return _validate_platform(info) if info is not None else detect_platform()


def _run(runner: Runner, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return runner.run(args, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Do not echo captured output: installer environment and tool output may
        # contain information that does not belong in a support log.
        raise InstallerError(f"{Path(args[0]).name} timed out after {timeout}s; inspect the host and rerun.") from exc
    except OSError as exc:
        raise InstallerError(f"Cannot execute {Path(args[0]).name}; install prerequisites or check PATH and rerun.") from exc


def _required(runner: Runner, args: list[str], description: str,
              timeout: int = 30) -> subprocess.CompletedProcess[str]:
    result = _run(runner, args, timeout)
    if result.returncode:
        raise InstallerError(f"{description} failed (exit {result.returncode}); inspect the host and rerun.")
    return result


def _installed_packages(runner: Runner, names: tuple[str, ...]) -> set[str]:
    result = _run(runner, ["dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Status}\n", *names])
    if result.returncode not in (0, 1):
        raise InstallerError("dpkg-query failed; check dpkg --audit before retrying installation.")
    installed: set[str] = set()
    for line in (result.stdout or "").splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and fields[1].strip() == "installed":
            installed.add(fields[0].split(":", 1)[0])
    return installed


def _apt(runner: Runner, operation: str, packages: tuple[str, ...] = ()) -> None:
    args = ["env", "DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=l", "apt-get",
            "-o", "DPkg::Lock::Timeout=60", "-o", "Acquire::Retries=2",
            "-o", "Acquire::http::Timeout=30", "-o", "Acquire::https::Timeout=30",
            "-o", "APT::Update::Error-Mode=any"]
    if operation == "install":
        args += ["--yes", "--no-install-recommends", "--no-upgrade", "--no-remove",
                 "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]
    args += [operation, *packages]
    for attempt in range(3):
        try:
            result = _run(runner, args, timeout=480)
        except InstallerError:
            if attempt == 2:
                raise InstallerError("apt-get timed out or could not start. Wait for cloud-init/unattended-upgrades, "
                                     "check DNS/HTTPS access and dpkg --audit, then rerun; never delete apt lock files.") from None
        else:
            if result.returncode == 0:
                return
            if attempt == 2:
                raise InstallerError(
                    f"apt-get {operation} failed after 3 attempts (exit {result.returncode}). "
                    "Wait for cloud-init/unattended-upgrades and apt locks, check DNS/HTTPS access, "
                    "repository signatures and dpkg --audit, then rerun. Do not delete lock files.")
        time.sleep(2 * (attempt + 1))


def ensure_packages(runner: Runner, *, user_confirmed: bool = False,
                    platform_info: dict[str, str] | None = None) -> dict[str, Any]:
    """Install missing prerequisites only; never perform an OS upgrade."""
    _authorized(user_confirmed)
    _platform(platform_info)
    installed = _installed_packages(runner, PACKAGES)
    missing = tuple(name for name in PACKAGES if name not in installed)
    if missing:
        _apt(runner, "update")
        _apt(runner, "install", missing)
        if not set(missing).issubset(_installed_packages(runner, missing)):
            raise InstallerError("apt returned success but prerequisites are missing; run dpkg --audit and rerun.")
    return {"changed": bool(missing), "installed": list(missing),
            "already_installed": [name for name in PACKAGES if name in installed]}


def _locks() -> dict[str, Any]:
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallerError("Installer integrity lock is missing or invalid; reinstall this bootstrap package.") from exc


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(runner: Runner, url: str, target: Path,
              record: dict[str, Any]) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password or
            parsed.query or parsed.fragment or parsed.port is not None or
            not (parsed.netloc == "pkgs.tailscale.com" and parsed.path.startswith("/stable/") or
                 parsed.netloc == "github.com" and parsed.path.startswith("/openai/codex/releases/download/"))):
        raise InstallerError("Download URL is outside the reviewed official HTTPS upstreams.")
    if not re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")):
        raise InstallerError("Invalid checksum in installer integrity lock.")
    _required(runner, ["curl", "--disable", "--fail", "--silent", "--show-error", "--location",
                      "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
                      "--connect-timeout", "10", "--max-time", "90", "--retry", "2",
                      "--retry-max-time", "180", "--output", str(target), "--url", url],
              "Official HTTPS download", timeout=210)
    if (not target.is_file() or target.stat().st_size != record.get("size") or
            _sha256(target) != record["sha256"]):
        raise InstallerError("Downloaded installer/repository checksum mismatch; nothing downloaded was executed. "
                             "Obtain a reviewed bootstrap update; do not bypass integrity checks.")


def _existing_file_matches(path: Path, record: dict[str, Any]) -> bool:
    if path.is_symlink():
        raise InstallerError(f"Refusing to replace an existing symlink at {path}; reconcile it manually and rerun.")
    if not path.exists():
        return False
    if not path.is_file() or _sha256(path) != record["sha256"]:
        raise InstallerError(f"Existing {path} differs from the reviewed Tailscale repository/key. "
                             "Review and reconcile that file manually; it was left unchanged.")
    return True


def _install_new_file(runner: Runner, source: Path, destination: Path) -> None:
    # The hard link publishes a fully written same-filesystem file atomically
    # and refuses to replace any file created by another process in the gap.
    staging = destination.with_name(f".{destination.name}.bootstrap-{uuid.uuid4().hex}")
    _required(runner, ["install", "-d", "-m", "0755", str(destination.parent)], "Repository directory creation")
    try:
        _required(runner, ["install", "-m", "0644", str(source), str(staging)], "Repository staging")
        _required(runner, ["ln", "-T", "--", str(staging), str(destination)],
                  f"Atomic repository publication at {destination}")
    finally:
        _run(runner, ["rm", "-f", "--", str(staging)])


def _check_other_tailscale_sources() -> None:
    candidates = [APT_SOURCE_MAIN]
    if APT_SOURCE_DIRECTORY.is_dir():
        candidates += list(APT_SOURCE_DIRECTORY.glob("*.list"))
        candidates += list(APT_SOURCE_DIRECTORY.glob("*.sources"))
    for path in candidates:
        if path == APT_SOURCE or not path.is_file():
            continue
        try:
            active = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                               if not line.lstrip().startswith("#"))
        except (OSError, UnicodeError) as exc:
            raise InstallerError(f"Cannot inspect apt source {path}; inspect it before rerunning.") from exc
        if "pkgs.tailscale.com" in active:
            raise InstallerError(f"Another Tailscale apt source exists in {path}; reconcile it manually "
                                 "before rerunning to avoid duplicate or conflicting sources.")


def _tailscale_version(runner: Runner) -> str | None:
    try:
        result = runner.run(["tailscale", "version"], check=False, timeout=15)
    except FileNotFoundError:
        return None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallerError("Existing tailscale could not be checked; inspect it before rerunning.") from exc
    if result.returncode:
        # A missing program can be represented by a wrapping runner as 127.
        if result.returncode == 127:
            return None
        raise InstallerError("Existing tailscale version check failed; inspect it before rerunning.")
    first = (result.stdout or "").splitlines()
    if not first or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9._-]+)?", first[0].strip()):
        raise InstallerError("Unexpected tailscale version output; inspect the existing installation.")
    return first[0].strip()


def ensure_tailscale(runner: Runner, *, user_confirmed: bool = False,
                     platform_info: dict[str, str] | None = None) -> dict[str, Any]:
    """Install signed stable packages if absent, then enable only tailscaled."""
    _authorized(user_confirmed)
    info = _platform(platform_info)
    version = _tailscale_version(runner)
    changed = version is None
    repo = "existing_installation"
    if changed:
        _check_other_tailscale_sources()
        base = f"https://pkgs.tailscale.com/stable/{info['id']}/{info['codename']}"
        upstreams = _locks()["tailscale"]
        key_url, source_url = base + ".noarmor.gpg", base + ".tailscale-keyring.list"
        key_exists = _existing_file_matches(TAILSCALE_KEY, upstreams[key_url])
        source_exists = _existing_file_matches(APT_SOURCE, upstreams[source_url])
        with tempfile.TemporaryDirectory(prefix="tailnet-repository-") as temp:
            # Verify all downloaded inputs before publishing either one.
            key, source = Path(temp) / "key.gpg", Path(temp) / "tailscale.list"
            if not key_exists:
                _download(runner, key_url, key, upstreams[key_url])
            if not source_exists:
                _download(runner, source_url, source, upstreams[source_url])
                expected = (f"deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] "
                            f"https://pkgs.tailscale.com/stable/{info['id']} {info['codename']} main")
                entries = [line.strip() for line in source.read_text(encoding="utf-8").splitlines()
                           if line.strip() and not line.lstrip().startswith("#")]
                if entries != [expected]:
                    raise InstallerError("Unexpected Tailscale apt source content; refusing to enable it.")
            if not key_exists:
                _install_new_file(runner, key, TAILSCALE_KEY)
            if not source_exists:
                _install_new_file(runner, source, APT_SOURCE)
        _apt(runner, "update")
        _apt(runner, "install", ("tailscale",))
        version = _tailscale_version(runner)
        if version is None:
            raise InstallerError("apt returned success but tailscale is absent; inspect dpkg --audit and rerun.")
        repo = f"stable/{info['id']}/{info['codename']}"
    _required(runner, ["systemctl", "enable", "--now", "tailscaled.service"],
              "Enabling/starting tailscaled.service", timeout=60)
    active = _run(runner, ["systemctl", "is-active", "--quiet", "tailscaled.service"], timeout=15)
    if active.returncode:
        raise InstallerError("tailscaled.service is not active; inspect journalctl -u tailscaled --no-pager and rerun.")
    return {"changed": changed, "version": version, "service": "active", "repo": repo}


def _probe_codex(runner: Runner, executable: str) -> str:
    version = _required(runner, [executable, "--version"], "Codex version check", timeout=20)
    match = re.fullmatch(r"codex-cli ([0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9._-]+)?)\s*",
                         version.stdout or "")
    help_result = _required(runner, [executable, "--help"], "Codex help check", timeout=20)
    if not match or "Usage:" not in (help_result.stdout or "") or "Codex" not in (help_result.stdout or ""):
        raise InstallerError("Existing Codex returned unexpected version/help output; inspect it before replacement.")
    return match.group(1)


def _package_verified(executable: str) -> bool:
    """Recognize a complete canonical standalone package, when applicable."""
    try:
        binary = Path(executable).resolve(strict=True)
        root = binary.parent.parent
        manifest = root / "codex-package.json"
        if not manifest.is_file():
            return False
        metadata_text = manifest.read_text(encoding="utf-8")
        data = json.loads(metadata_text)
        if not isinstance(data, dict):
            return False
        required = [root / "bin/codex", root / "bin/codex-code-mode-host",
                    root / "codex-path/rg", root / "codex-resources/bwrap"]
        if re.search(r'"execServer"\s*:', metadata_text):
            required.append(root / "bin/codex-exec-server")
        return all(path.is_file() and os.access(path, os.X_OK) for path in required)
    except (OSError, ValueError):
        return False


def _canonical_package_root(executable: str) -> Path | None:
    """Identify canonical installs even when their declared package is broken."""
    try:
        root = Path(executable).resolve(strict=True).parent.parent
        manifest = root / "codex-package.json"
        if manifest.exists() or manifest.is_symlink():
            return root
    except OSError:
        pass
    return None


def _existing_codex(destination: Path) -> str | None:
    # Bootstrap intentionally installs into the selected account's home.
    # In particular, a root caller must never probe a command supplied by an
    # inherited PATH entry such as a working directory or another user's bin.
    if destination.exists() or destination.is_symlink():
        return str(destination)
    return shutil.which("codex", path=TRUSTED_PATH)


def ensure_codex(runner: Runner, *, user_confirmed: bool = False,
                 platform_info: dict[str, str] | None = None,
                 user_home: str | Path | None = None) -> dict[str, Any]:
    """Preserve a working CLI, or install the pinned full official package."""
    _authorized(user_confirmed)
    _platform(platform_info)
    home = Path(user_home) if user_home is not None else Path.home()
    if not home.is_absolute() or not home.is_dir():
        raise InstallerError("The installation home must be an existing absolute directory.")
    destination = home / ".local/bin/codex"
    existing = _existing_codex(destination)
    if existing is not None:
        complete = _package_verified(existing)
        canonical_root = _canonical_package_root(existing)
        if canonical_root is not None and not complete:
            raise InstallerError(
                f"Existing canonical Codex package at {canonical_root} is incomplete. "
                "Review its manifest and missing executable helpers, then repair it with the official "
                "installer or explicitly remove the broken installation before rerunning. "
                "No existing files were replaced.")
        version = _probe_codex(runner, existing)
        return {"changed": False, "path": existing, "version": version,
                "status": "existing_not_reinstalled", "package_verified": complete}
    lock = _locks()["codex"]
    with tempfile.TemporaryDirectory(prefix="codex-installer-") as temp:
        script = Path(temp) / "install.sh"
        _download(runner, lock["url"], script, lock)
        # env -i ensures installer behavior cannot be altered by inherited
        # daemon/update knobs, API credentials, HOME or CODEX_HOME settings.
        args = ["env", "-i", f"HOME={home}", f"PATH={TRUSTED_PATH}", "LANG=C.UTF-8",
                "CODEX_NON_INTERACTIVE=1", "CODEX_INSTALL_DAEMON_ONLY=0",
                f"CODEX_RELEASE={lock['version']}", f"CODEX_INSTALL_DIR={destination.parent}",
                f"CODEX_HOME={home / '.codex'}", "/bin/sh", str(script),
                "--release", lock["version"]]
        _required(runner, args, "Verified official Codex package installation", timeout=900)
    if not destination.exists():
        raise InstallerError("Official installer completed without a Codex command at the requested path.")
    version = _probe_codex(runner, str(destination))
    if version != lock["version"] or not _package_verified(str(destination)):
        raise InstallerError("Installed Codex package is incomplete or has the wrong version; "
                             "inspect the official installer output before using it.")
    return {"changed": True, "path": str(destination), "version": version,
            "status": "installed_official_package", "package_verified": True}
