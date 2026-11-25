"""Interactive, resumable pilot coordinator; never stores account credentials."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
import pwd
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

from . import __version__
from .installers import InstallerError, detect_platform, ensure_codex, ensure_packages, ensure_tailscale
from .network import NetworkError, ProfileError, apply_client, load_profile, rollback, validate_profile, verify
from .runner import Runner, TRUSTED_PATH
from .state import StateError, StateStore

COMMANDS = ("wizard", "configure", "install", "connect", "apply", "verify", "status",
            "rollback", "login", "launch", "hub-check")
DEFAULT_STATE = "/var/lib/tailscale-codex-bootstrap"


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        raise RuntimeError("Нужен интерактивный терминал; запустите bash bin/bootstrap через SSH -t.")
    return input(question + " [y/N]: ").strip().lower() in {"y", "yes", "да"}


def prompt(label: str, default: str = "") -> str:
    value = input(label + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


def emit(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def atomic_json(path: Path, data: dict) -> None:
    """Create a private data file without following an existing symlink."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise StateError("Небезопасный путь JSON; существующий объект сохранён.")
    fd, temporary = tempfile.mkstemp(prefix=".bootstrap-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def trusted_root_directory(path: Path) -> None:
    """Root state must not traverse symlinks or directories writable by others."""
    for ancestor in (path, *path.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise StateError("Состояние root требует доверенных каталогов root без записи группы/прочих.")


@contextlib.contextmanager
def operation_lock(directory: Path):
    fd = os.open(directory / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise StateError("Небезопасный файл блокировки операции.")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateError("Другой процесс настройки уже работает. Подключитесь к его tmux.") from exc
        yield
    finally:
        os.close(fd)


def configure(store: StateStore, profile_path: Path) -> dict:
    if not sys.stdin.isatty():
        raise RuntimeError("Профиль создаётся в интерактивном терминале либо заранее владельцем.")
    old = load_profile(profile_path) if profile_path.exists() else {}
    print("Рабочий профиль хранится локально, вне Git. Нужен подтверждённый публичный IPv4 хаба.")
    print("Список доменов — начальный инвентарь, его полноту проверяют при реальном OAuth/запросе Codex.")
    data = {
        "version": 1, "name": prompt("Имя пилота", old.get("name", "cloudru-codex-pilot")),
        "hub_ip": prompt("Tailscale IPv4 согласованного хаба", old.get("hub_ip", "")),
        "expected_tailnet": prompt("Проверенный MagicDNS suffix tailnet вида tailXXXX.ts.net", old.get("expected_tailnet", "")),
        "connector_tag": prompt("Согласованный тег App Connector", old.get("connector_tag", "tag:ai-egress-hub")),
        "expected_egress_ipv4": prompt("Проверенный публичный IPv4 хаба", old.get("expected_egress_ipv4", "")),
        "domains": [item.strip() for item in prompt("Точные FQDN через запятую", ",".join(old.get("domains", [
            "chatgpt.com", "auth.openai.com", "api.openai.com", "api.ipify.org"]))).split(",")],
        "probe_domain": "api.ipify.org", "probe_url": "https://api.ipify.org",
        "non_ai_probe_url": "https://checkip.amazonaws.com",
    }
    data = validate_profile(data)
    if old and old != data:
        network = store.load().get("network") or {}
        applied = network.get("apply") or {}
        if applied and applied.get("status") not in {"rolled_back", "not_applied"}:
            raise StateError("Сначала выполните rollback со старым профилем. Активный профиль не заменён.")
    emit(data)
    if not confirm("Сохранить этот профиль?"):
        raise RuntimeError("Профиль не сохранён.")
    atomic_json(profile_path, data)
    def save_profile_metadata(value):
        value["profile_path"] = str(profile_path)
        if old and old != data and value.get("network"):
            value.setdefault("network_history", []).append(value.pop("network"))
    store.update(save_profile_metadata)
    return data


def install(store: StateStore, runner: Runner) -> dict:
    platform = detect_platform()
    print("Будут установлены только отсутствующие зависимости, Tailscale и полный пакет Codex.")
    print("Обновления ОС, изменение SSH/firewall и авторизация Codex сюда не входят.")
    if not confirm("Начать установку на этой VM?"):
        raise RuntimeError("Установка отменена.")
    results = {}
    for step, function in (("packages", ensure_packages), ("tailscale", ensure_tailscale), ("codex", ensure_codex)):
        print(f"Этап {step}: проверка фактического состояния…", flush=True)
        result = function(runner, user_confirmed=True, platform_info=platform)
        results[step] = result
        store.update(lambda value, step=step, result=result: value.setdefault("installation", {}).update({step: result}))
        emit({step: result})
    return results


def connect(runner: Runner, profile: dict) -> dict:
    response = runner.run(["tailscale", "status", "--json"])
    try:
        status = json.loads(response.stdout)
    except ValueError as exc:
        raise NetworkError("Не удалось прочитать статус tailscaled; проверьте службу.") from exc
    suffix = (status.get("CurrentTailnet") or {}).get("MagicDNSSuffix") or status.get("MagicDNSSuffix")
    if status.get("BackendState") == "Running":
        if suffix != profile["expected_tailnet"]:
            raise NetworkError("Подключён другой tailnet. Существующая регистрация сохранена.")
        return {"connected": True, "changed": False, "tailnet": suffix}
    if (status.get("Self") or {}).get("ID") or suffix:
        raise NetworkError("Найдена прежняя регистрация Tailscale. Возобновите её вручную; сброс не выполнялся.")
    if not confirm("Подключить новую VM к tailnet через tailscale up и браузер владельца?"):
        raise RuntimeError("Регистрация отложена.")
    print("Авторизуйте именно " + profile["expected_tailnet"] + ". Ссылку входа и коды не публикуйте.")
    code = subprocess.call(["tailscale", "up"], timeout=600)
    if code:
        raise NetworkError("Tailscale up не завершился успешно; проверьте регистрацию и повторите.")
    return connect(runner, profile)


def check_network(profile: dict, store: StateStore, runner: Runner) -> dict:
    report = verify(profile, runner.run)
    report["checked_at"] = datetime.now(timezone.utc).isoformat()
    report["codex_acceptance"] = "NOT_TESTED"
    atomic_json(store.directory / "network-report.json", report)
    store.update(lambda data: data.update({"last_network_check": {
        "readiness": report["readiness"], "checked_at": report["checked_at"]}}))
    emit(report)
    return report


def codex_action(action: str, profile: dict, store: StateStore, runner: Runner) -> int:
    overrides = sorted(name for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_KEY",
        "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS") if os.environ.get(name))
    if overrides:
        raise RuntimeError("Проверьте переопределения окружения перед штатным Codex: " + ", ".join(overrides))
    if not {"chatgpt.com", "auth.openai.com"}.issubset(profile["domains"]):
        raise ProfileError("Для пилота Codex нужны как минимум chatgpt.com и auth.openai.com в инвентаре FQDN.")
    if check_network(profile, store, runner)["readiness"] != "NETWORK_VERIFIED":
        return 2
    candidate = Path.home() / ".local/bin/codex"
    binary = str(candidate) if candidate.is_file() else shutil.which("codex")
    if not binary:
        raise RuntimeError("Codex не установлен.")
    print("Это пилот: контрольный IP не доказывает egress OpenAI. Сверьте домены OAuth, inference и streaming.")
    if not confirm("Выполнить вход своей учётной записью?" if action == "login" else "Запустить нативный Codex для ручной приёмки?"):
        return 2
    args = [binary, "login", "--device-auth"] if action == "login" else [binary]
    code = subprocess.call(args)
    store.update(lambda data: data.update({"last_codex_action": {"action": action, "exit_code": code,
        "acceptance": "MANUAL_VERIFICATION_REQUIRED"}}))
    return code


def hub_check(runner: Runner, profile: dict) -> dict:
    """Inspect selected public facts only; do not dump debug preferences wholesale."""
    status = json.loads(runner.run(["tailscale", "status", "--json"], check=True).stdout)
    prefs = json.loads(runner.run(["tailscale", "debug", "prefs"], check=True).stdout)
    self_node = status.get("Self") or {}
    connector = prefs.get("AppConnector") or {}
    ready = (profile["hub_ip"] in (self_node.get("TailscaleIPs") or []) and
             profile["connector_tag"] in (self_node.get("Tags") or []) and connector.get("Advertise") is True)
    return {"readiness": "HUB_ROLE_PRESENT_POLICY_NOT_VERIFIED" if ready else "NEEDS_HUB_ADMIN",
            "self_ips": self_node.get("TailscaleIPs") or [], "tags": self_node.get("Tags") or [],
            "connector_enabled": connector.get("Advertise") is True,
            "next": "docs/HUB-SETUP.md; работающий SSH/консоль владельца и проверка действующей политики обязательны"}


def guard_worker(runner: Runner, profile: dict) -> None:
    """Protect the shared hub before installers, registration or route changes."""
    try:
        result = runner.run(["ip", "-4", "-j", "address", "show", "dev", "tailscale0"])
    except FileNotFoundError:
        return  # A pristine supported VM can lack iproute2 before installation.
    if result.returncode:
        return  # Interface does not exist yet on a pristine VM.
    interfaces = json.loads(result.stdout)
    if any(entry.get("local") == profile["hub_ip"] for interface in interfaces for entry in interface.get("addr_info", [])):
        raise NetworkError("Обнаружен адрес центрального хаба. Клиентские изменения запрещены.")


def execute(command: str, profile_path: Path, store: StateStore, runner: Runner) -> int:
    if command in {"install", "connect", "apply", "rollback", "login", "launch"}:
        guard_worker(runner, load_profile(profile_path))
    if command == "hub-check":
        result = hub_check(runner, load_profile(profile_path))
        emit(result)
        return 2 if result["readiness"] == "NEEDS_HUB_ADMIN" else 0
    if command == "status":
        data = store.load()
        emit({"bootstrap_version": __version__, "progress": data,
              "note": "Это сохранённый прогресс; свежая проверка сети: verify. Codex принимается отдельно."})
        return 0
    if command == "configure":
        configure(store, profile_path)
        return 0
    if command == "install":
        install(store, runner)
        return 0
    if command == "connect":
        emit(connect(runner, load_profile(profile_path)))
        return 0
    if command == "wizard":
        print("Пилот Tailscale → Amsterdam → выбранные HTTPS-домены. Инструкция: README.md")
        if not profile_path.exists():
            configure(store, profile_path)
        profile = load_profile(profile_path)
        guard_worker(runner, profile)
        install(store, runner)
        emit(connect(runner, profile))
        if not confirm("Проверить область маршрутов и разрешить их приём на VM? Весь tailnet требует проверки."):
            return 2
        applied = apply_client(profile, store, runner.run, confirmed=True)
        emit(applied)
        if applied.get("blockers"):
            print("Настройка приостановлена. Проверьте docs/HUB-SETUP.md; повторный запуск безопасен.")
            return 2
        if check_network(profile, store, runner)["readiness"] != "NETWORK_VERIFIED":
            return 2
        print("Сеть проверена на текущем снимке. Codex ещё не принят. Следующие команды: login, launch.")
        return 0
    profile = load_profile(profile_path)
    if command == "apply":
        accepted = confirm("Проверить область всех предлагаемых маршрутов и включить accept-routes при отсутствии блокировок?")
        result = apply_client(profile, store, runner.run, confirmed=accepted)
        emit(result)
        return 2 if result.get("blockers") else 0
    if command == "verify":
        return 0 if check_network(profile, store, runner)["readiness"] == "NETWORK_VERIFIED" else 2
    if command == "rollback":
        if not confirm("Вернуть только изменённый этим установщиком accept-routes к исходному значению?"):
            return 2
        result = rollback(profile, store, runner.run)
        emit(result)
        return 2 if result.get("blockers") else 0
    return codex_action(command, profile, store, runner)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Диалоговый пилот Codex через Tailscale App Connector; без секретов в state.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--state-dir", default=DEFAULT_STATE)
    parser.add_argument("--profile", type=Path, help="Локальный JSON-профиль, вне Git")
    parser.add_argument("--launcher-preflight", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs="?", choices=COMMANDS, default="wizard")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("Запустите через sudo bash bin/bootstrap. Для --help права root не нужны.", file=sys.stderr)
        return 2
    os.environ["PATH"] = TRUSTED_PATH
    os.environ["HOME"] = pwd.getpwuid(0).pw_dir
    store = None
    try:
        trusted_root_directory(Path(args.state_dir).expanduser().absolute())
        store = StateStore(args.state_dir)
        path = args.profile.expanduser().absolute() if args.profile else store.directory / "profile.json"
        with operation_lock(store.directory):
            if args.launcher_preflight:
                if args.command in {"status", "configure", "verify", "hub-check"}:
                    return 3  # Read-only/local configuration: no need to install tmux first.
                if args.command == "wizard" and not path.exists():
                    configure(store, path)
                guard_worker(Runner(), load_profile(path))
                return 0
            return execute(args.command, path, store, Runner())
    except (InstallerError, NetworkError, ProfileError, StateError, RuntimeError, OSError,
            subprocess.SubprocessError, ValueError) as exc:
        # Messages from our components are deliberately free of raw command output.
        safe_type = type(exc).__name__
        if store:
            with contextlib.suppress(StateError, OSError):
                store.update(lambda data: data.update({"last_stop": {"kind": safe_type,
                    "at": datetime.now(timezone.utc).isoformat()}}))
        print(f"ОСТАНОВЛЕНО ({safe_type}): {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nПриостановлено. Повторите запуск; сохранённые данные не удалены.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
