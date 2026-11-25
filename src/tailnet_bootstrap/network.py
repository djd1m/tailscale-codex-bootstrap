"""Conservative, IPv4 App Connector checks with no daemon or firewall setup.

The only mutation is ``tailscale set --accept-routes=...``. An independent
HTTPS echo proves the control endpoint's source IP only, never a provider's.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .state import StateStore

Run = Callable[..., subprocess.CompletedProcess[str]]
PREF_KEYS = ("RouteAll", "CorpDNS", "ExitNodeID", "AdvertiseTags")
TLS_OVERRIDE_KEYS = {"CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_SSL_BACKEND", "SSLKEYLOGFILE"}
PROFILE_KEYS = {"version", "name", "hub_ip", "expected_tailnet", "connector_tag", "domains",
                "expected_egress_ipv4", "probe_url", "probe_domain", "non_ai_probe_url"}
LIMITATIONS = [
    "This is an IPv4 network snapshot for the listed DNS answers, not continuous enforcement.",
    "The independent echo endpoint proves its own observed source IP; OpenAI/provider-observed IP was not measured.",
    "App Connector route removal can restore direct routing; fail-closed behavior needs separate acceptance and enforcement.",
    "IPv6, UDP/443, client-specific DNS/DoH, OAuth, inference and streaming require separate client acceptance.",
    "Shared destination IPs affect all processes and can also route other hostnames through the hub.",
]


class ProfileError(ValueError):
    """A profile is unsafe or outside the supported v1 schema."""


class NetworkError(RuntimeError):
    """A local read-only prerequisite failed (message contains no raw output)."""


def run(args: list[str], check: bool = False, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Default injectable argv-only runner. Never invoke a shell."""
    return subprocess.run(args, check=check, timeout=timeout, text=True, capture_output=True)


def _public(address: Any) -> bool:
    return bool(address.is_global and not address.is_multicast and not address.is_reserved
                and not address.is_unspecified and not address.is_loopback and not address.is_link_local)


def _domain(value: Any) -> str:
    if not isinstance(value, str) or value != value.lower() or len(value) > 253:
        raise ProfileError("Domains must be lowercase exact ASCII FQDNs")
    if value.endswith(".") or "." not in value or any(c in value for c in "*:/@\\ \t\r\n"):
        raise ProfileError("Domains must be exact FQDNs without wildcards, ports or trailing dots")
    labels = value.split(".")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ProfileError("Domains must be exact ASCII FQDNs")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ProfileError("IP-literal domains are prohibited")
    if labels[-1].isdigit() or labels[-1] in {"localhost", "local", "internal", "invalid", "test", "example"}:
        raise ProfileError("A public DNS domain is required")
    return value


def _https_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ProfileError("Probe must be a plain HTTPS URL")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError as exc:
        raise ProfileError("Probe URL is malformed") from exc
    if (url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None
            or url.query or url.fragment or "\\" in value or port not in (None, 443)):
        raise ProfileError("Probe URL requires HTTPS:443 and no credentials, query or fragment")
    _domain(url.hostname)
    if url.hostname != url.netloc.split(":", 1)[0]:
        raise ProfileError("Probe URL hostname must use its canonical lowercase form")
    return value


def validate_profile(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) - PROFILE_KEYS:
        raise ProfileError("Profile must be an object with only supported v1 fields")
    required = PROFILE_KEYS - {"non_ai_probe_url"}
    if required - set(data):
        raise ProfileError("Profile is missing required fields: " + ", ".join(sorted(required - set(data))))
    if type(data["version"]) is not int or data["version"] != 1:
        raise ProfileError("Only profile version 1 is supported")
    if not isinstance(data["name"], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", data["name"]):
        raise ProfileError("Profile name must be a short safe identifier")
    if not isinstance(data["hub_ip"], str) or not isinstance(data["expected_egress_ipv4"], str):
        raise ProfileError("Hub and egress IPv4 addresses must be strings")
    try:
        hub = ipaddress.IPv4Address(data["hub_ip"])
        egress = ipaddress.IPv4Address(data["expected_egress_ipv4"])
    except (ValueError, ipaddress.AddressValueError, TypeError) as exc:
        raise ProfileError("Hub and egress must be IPv4 addresses") from exc
    if hub not in ipaddress.IPv4Network("100.64.0.0/10"):
        raise ProfileError("Hub IPv4 must be a Tailscale address in 100.64.0.0/10")
    if not _public(egress):
        raise ProfileError("Expected egress must be a public IPv4 address")
    if (not isinstance(data["expected_tailnet"], str)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ts\.net", data["expected_tailnet"])):
        raise ProfileError("Expected tailnet must be a canonical lowercase MagicDNS suffix: <name>.ts.net")
    if (not isinstance(data["connector_tag"], str)
            or not re.fullmatch(r"tag:[a-z][a-z0-9-]*", data["connector_tag"])):
        raise ProfileError("Connector tag must match tag:[a-z][a-z0-9-]*")
    domains = data["domains"]
    if not isinstance(domains, list) or not domains or len(domains) > 128:
        raise ProfileError("domains must be a nonempty list of at most 128 exact FQDNs")
    clean_domains = [_domain(domain) for domain in domains]
    if len(set(clean_domains)) != len(clean_domains):
        raise ProfileError("Duplicate domains are prohibited")
    probe_domain = _domain(data["probe_domain"])
    probe_url = _https_url(data["probe_url"])
    if probe_domain not in clean_domains or urlsplit(probe_url).hostname != probe_domain:
        raise ProfileError("probe_domain must be in domains and exactly match the probe URL hostname")
    if data.get("non_ai_probe_url") is not None:
        _https_url(data["non_ai_probe_url"])
        if urlsplit(data["non_ai_probe_url"]).hostname in clean_domains:
            raise ProfileError("Non-AI probe must have a hostname outside domains")
    return json.loads(json.dumps(data))


def load_profile(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProfileError("Cannot read profile JSON") from exc
    return validate_profile(data)


def _invoke(runner: Run, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return runner(args, check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NetworkError("Required local command failed: " + args[0]) from exc


def _json_command(runner: Run, args: list[str]) -> Any:
    result = _invoke(runner, args)
    if result.returncode:
        raise NetworkError("Required local inspection failed: " + " ".join(args[:3]))
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise NetworkError("Local inspection did not return valid JSON: " + args[0]) from exc


def _status(runner: Run) -> dict[str, Any]:
    data = _json_command(runner, ["tailscale", "status", "--json"])
    if not isinstance(data, dict):
        raise NetworkError("Tailscale status is not an object")
    return data


def _prefs(runner: Run) -> dict[str, Any]:
    data = _json_command(runner, ["tailscale", "debug", "prefs"])
    if not isinstance(data, dict) or type(data.get("RouteAll")) is not bool:
        raise NetworkError("Cannot establish daemon RouteAll preference")
    if type(data.get("CorpDNS")) is not bool or not isinstance(data.get("ExitNodeID", ""), str):
        raise NetworkError("Cannot establish daemon DNS/exit-node preferences")
    tags = data.get("AdvertiseTags") or []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise NetworkError("Cannot establish daemon advertised tags")
    return {"RouteAll": data["RouteAll"], "CorpDNS": data["CorpDNS"],
            "ExitNodeID": data.get("ExitNodeID", ""), "AdvertiseTags": tags}


def _tailnet(status: dict[str, Any]) -> str | None:
    current = status.get("CurrentTailnet")
    if isinstance(current, dict) and current.get("MagicDNSSuffix"):
        return current["MagicDNSSuffix"]
    return status.get("MagicDNSSuffix")


def proxy_variable_names(environ: Mapping[str, str] | None = None) -> list[str]:
    values = os.environ if environ is None else environ
    return sorted(name for name, value in values.items() if value and name.lower().endswith("_proxy"))


def _active_proxies(environ: Mapping[str, str] | None) -> list[str]:
    return [name for name in proxy_variable_names(environ) if name.lower() != "no_proxy"]


def tls_override_variable_names(environ: Mapping[str, str] | None = None) -> list[str]:
    values = os.environ if environ is None else environ
    return sorted(name for name, value in values.items() if value and name.upper() in TLS_OVERRIDE_KEYS)


def _nodes(status: dict[str, Any]) -> list[dict[str, Any]]:
    peers = status.get("Peer") or {}
    if not isinstance(peers, dict):
        raise NetworkError("Cannot inspect Tailscale peers")
    return [status.get("Self") or {}] + [p for p in peers.values() if isinstance(p, dict)]


def _dns(runner: Run, domain: str, version: int) -> tuple[list[str], bool]:
    result = _invoke(runner, ["getent", "ahostsv4" if version == 4 else "ahostsv6", domain])
    if result.returncode not in (0, 2):
        return [], False
    addresses: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.split():
            continue
        try:
            address = ipaddress.ip_address(line.split()[0])
        except ValueError:
            continue
        if address.version == version and not getattr(address, "ipv4_mapped", None):
            addresses.add(str(address))
    return sorted(addresses), True


def _primary_routes(status: dict[str, Any]) -> list[dict[str, str]]:
    routes = []
    for node in _nodes(status):
        for prefix in node.get("PrimaryRoutes") or []:
            try:
                network = ipaddress.ip_network(prefix, strict=True)
            except (ValueError, TypeError) as exc:
                raise NetworkError("Tailscale PrimaryRoutes contained an invalid prefix") from exc
            routes.append({"node_id": str(node.get("ID", "")), "prefix": str(network)})
    return sorted(routes, key=lambda entry: (entry["prefix"], entry["node_id"]))


def _offered_routes(status: dict[str, Any]) -> list[dict[str, Any]]:
    """Approved AllowedIPs exclude a peer's own overlay host addresses.

    Exit-node default offers are inventoried but not treated as subnet routes:
    RouteAll does not select an exit node, and a selected exit is blocked earlier.
    """
    offered = []
    for node in _nodes(status):
        own = set()
        for address in node.get("TailscaleIPs") or []:
            try:
                parsed = ipaddress.ip_address(address)
                own.add(str(ipaddress.ip_network(f"{parsed}/{parsed.max_prefixlen}")))
            except ValueError as exc:
                raise NetworkError("Tailscale node contained an invalid overlay IP") from exc
        for prefix in node.get("AllowedIPs") or []:
            try:
                network = ipaddress.ip_network(prefix, strict=True)
            except (ValueError, TypeError) as exc:
                raise NetworkError("Tailscale AllowedIPs contained an invalid prefix") from exc
            if str(network) not in own:
                offered.append({"node_id": str(node.get("ID", "")), "prefix": str(network),
                                "exit_node_offer": bool(network.prefixlen == 0 and node.get("ExitNodeOption"))})
    return sorted(offered, key=lambda entry: (entry["prefix"], entry["node_id"]))


def audit_routes(profile: dict[str, Any], status: dict[str, Any], runner: Run = run) -> dict[str, Any]:
    """Inventory route acceptance scope before the single client preference change."""
    profile = validate_profile(profile)
    known: set[str] = set()
    unresolved = []
    ipv6_unknown = []
    nonpublic = []
    for domain in profile["domains"]:
        addresses, ok = _dns(runner, domain, 4)
        if not ok or not addresses:
            unresolved.append(domain)
        known.update(f"{ip}/32" for ip in addresses)
        v6, v6_ok = _dns(runner, domain, 6)
        if not v6_ok:
            ipv6_unknown.append(domain)
        known.update(f"{ip}/128" for ip in v6)
        nonpublic.extend({"domain": domain, "address": address} for address in addresses + v6
                         if not _public(ipaddress.ip_address(address)))
    advertised = _primary_routes(status)
    offered = _offered_routes(status)
    unexpected = [entry for entry in advertised if entry["prefix"] not in known]
    unexpected_offered = [entry for entry in offered if entry["prefix"] not in known and not entry["exit_node_offer"]]
    return {"primary_routes": advertised, "offered_routes": offered,
            "unexpected_offered_routes": unexpected_offered, "selected_host_routes": sorted(known),
            "unexpected_primary_routes": unexpected, "unresolved_domains": unresolved,
            "ipv6_dns_unknown": ipv6_unknown, "nonpublic_answers": nonpublic,
            "accept_routes_scope": "All offered subnet/app routes, not only this profile's domains"}


def capture_baseline(profile: dict[str, Any], runner: Run = run,
                     environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    profile = validate_profile(profile)
    status = _status(runner)
    if status.get("BackendState") != "Running" or _tailnet(status) != profile["expected_tailnet"]:
        raise NetworkError("Tailscale must be running in the expected tailnet before baseline capture")
    self_id = (status.get("Self") or {}).get("ID")
    if not isinstance(self_id, str) or not self_id:
        raise NetworkError("Tailscale self identity is unavailable")
    hubs = [node for node in _nodes(status) if profile["hub_ip"] in (node.get("TailscaleIPs") or [])]
    hub = hubs[0] if len(hubs) == 1 else None
    connector = ({"id": hub.get("ID"), "online": bool(hub.get("Online")),
                  "connector_tag_present": profile["connector_tag"] in (hub.get("Tags") or []),
                  "is_self": hub.get("ID") == self_id} if hub else None)
    return {"captured_at": datetime.now(timezone.utc).isoformat(), "self_id": self_id,
            "connector": connector, "exit_node_active": bool(status.get("ExitNodeStatus")),
            "tailnet": _tailnet(status), "prefs": _prefs(runner),
            "routes_ipv4": _json_command(runner, ["ip", "-j", "route", "show", "table", "all"]),
            "routes_ipv6": _json_command(runner, ["ip", "-6", "-j", "route", "show", "table", "all"]),
            "proxy_variable_names": proxy_variable_names(environ),
            "tls_override_variable_names": tls_override_variable_names(environ),
            "route_audit": audit_routes(profile, status, runner)}


def _block(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _profile_id(profile: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _apply_blockers(baseline: dict[str, Any], runner: Run,
                    environ: Mapping[str, str] | None) -> list[dict[str, Any]]:
    blockers = []
    connector = baseline["connector"]
    if not baseline["prefs"]["CorpDNS"]:
        blockers.append(_block("tailscale_dns_disabled", "Daemon CorpDNS must already be enabled for connector discovery"))
    if connector is None or not connector["connector_tag_present"]:
        blockers.append(_block("connector_not_identified", "Expected hub IP and connector tag must identify the same node before applying"))
    elif not connector["online"]:
        blockers.append(_block("hub_offline", "Expected connector is offline; inspect hub/tailnet administration"))
    elif connector["is_self"]:
        blockers.append(_block("refusing_hub_mutation", "Client bootstrap must run on the fresh worker VM, not the existing hub"))
    if baseline["prefs"]["ExitNodeID"] or baseline["exit_node_active"]:
        blockers.append(_block("exit_node_active", "An exit node is configured; review it manually before applying"))
    if _active_proxies(environ):
        blockers.append(_block("proxy_environment", "Proxy variables are active", names=_active_proxies(environ)))
    if tls_override_variable_names(environ):
        blockers.append(_block("tls_override_environment", "TLS environment overrides require separate review", names=tls_override_variable_names(environ)))
    audit = baseline["route_audit"]
    if audit["unresolved_domains"]:
        blockers.append(_block("dns_unresolved", "Resolve every selected domain before route acceptance", domains=audit["unresolved_domains"]))
    if audit["unexpected_primary_routes"] or audit["unexpected_offered_routes"]:
        blockers.append(_block("unexpected_routes", "Other offered routes require tailnet administrator review", routes=audit["unexpected_primary_routes"] + audit["unexpected_offered_routes"]))
    # RouteAll may install a currently absent IPv6 route immediately. Checking
    # only the pre-change kernel lookup would miss that future usable path.
    # Overlay host IPs were excluded by _offered_routes. An unselected exit-node
    # default offer is not installed by RouteAll and is inventoried separately.
    ipv6_subnet_routes = [entry for entry in audit["primary_routes"] + audit["offered_routes"]
                          if ipaddress.ip_network(entry["prefix"]).version == 6
                          and not entry.get("exit_node_offer", False)]
    if ipv6_subnet_routes:
        blockers.append(_block("ipv6_routes_require_separate_acceptance",
                               "IPv4-only pilot refuses offered IPv6 subnet routes even when currently unreachable",
                               routes=ipv6_subnet_routes))
    if audit["nonpublic_answers"]:
        blockers.append(_block("nonpublic_destination", "Selected DNS answers must all be public", answers=audit["nonpublic_answers"]))
    if audit["ipv6_dns_unknown"]:
        blockers.append(_block("ipv6_dns_unknown", "IPv6 resolver inspection failed", domains=audit["ipv6_dns_unknown"]))
    for prefix in audit["selected_host_routes"]:
        network = ipaddress.ip_network(prefix)
        if network.version == 4:
            exact = [entry for entry in audit["primary_routes"] if entry["prefix"] == prefix]
            if connector is None or not exact or any(entry["node_id"] != connector["id"] for entry in exact):
                blockers.append(_block("primary_route_wrong_connector", "Every selected IPv4 host PrimaryRoute must already select the expected hub", prefix=prefix))
        elif _public(network.network_address):
            entries, ok = _route(runner, str(network.network_address), 6)
            unreachable = ok and (not entries or all(entry.get("type") in {"unreachable", "blackhole", "prohibit"} for entry in entries))
            if not unreachable:
                blockers.append(_block("ipv6_requires_separate_acceptance", "A usable or unknown IPv6 path requires separate acceptance before applying", prefix=prefix))
    if _ipv6_defaults(baseline["routes_ipv6"]):
        blockers.append(_block("ipv6_egress_available", "IPv6 default egress must be reviewed before IPv4-only route acceptance"))
    return blockers


def _ipv6_defaults(inventory: Any) -> list[dict[str, Any]]:
    if not isinstance(inventory, list) or any(not isinstance(entry, dict) for entry in inventory):
        raise NetworkError("IPv6 route inventory is malformed")
    return [entry for entry in inventory if entry.get("dst") in {"default", "::/0"}
            and entry.get("type") not in {"unreachable", "blackhole", "prohibit"}]


def apply_client(profile: dict[str, Any], state: StateStore, runner: Run = run, *,
                 confirmed: bool = False, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Enable route acceptance only after CLI confirmation; persist rollback intent first."""
    profile = validate_profile(profile)
    if not confirmed:
        return {"changed": False, "blockers": [_block("confirmation_required", "Explicit CLI confirmation is required before accepting routes")]}
    baseline = capture_baseline(profile, runner, environ)
    blockers = _apply_blockers(baseline, runner, environ)
    audit = baseline["route_audit"]
    if blockers:
        return {"changed": False, "blockers": blockers, "route_audit": audit}
    identity = _profile_id(profile)
    result: dict[str, Any] = {"changed": False, "blockers": [], "route_audit": audit}

    with state.transaction() as transaction:
        data = transaction.data
        network = data.setdefault("network", {})
        # Re-read under the state lock: apply/rollback from another process may
        # have completed since the first baseline inspection.
        current = capture_baseline(profile, runner, environ)
        if current["self_id"] != baseline["self_id"]:
            raise NetworkError("Tailscale self identity changed during apply")
        current_blockers = _apply_blockers(current, runner, environ)
        if current_blockers:
            return {"changed": False, "blockers": current_blockers, "route_audit": current["route_audit"]}
        baseline = current
        current_prefs = current["prefs"]
        previous = network.get("baseline")
        if previous and previous.get("self_id") != baseline["self_id"]:
            raise NetworkError("Saved baseline belongs to another Tailscale node; refusing mutation")
        if network.get("profile_id") not in (None, identity):
            raise NetworkError("Saved network transaction belongs to another profile")
        prior_apply = network.get("apply") or {}
        if not prior_apply or prior_apply.get("status") in {"rolled_back", "not_applied"}:
            network["baseline"] = baseline
        else:
            network.setdefault("baseline", baseline)
        network["profile_id"] = identity
        if current_prefs["RouteAll"]:
            result["already_enabled"] = True
            return result
        network["apply"] = {"status": "pending", "self_id": baseline["self_id"],
                            "before": False, "after": True}
        transaction.checkpoint()
        operation = _invoke(runner, ["tailscale", "set", "--accept-routes=true"])
        observed = _prefs(runner)["RouteAll"]
        network["apply"]["status"] = "applied" if observed else "not_applied"
        result["changed"] = observed
        if operation.returncode or not observed:
            result["blockers"].append(_block("apply_failed", "Route acceptance command failed or daemon did not confirm it"))
    return result


def _route(runner: Run, address: str, version: int = 4) -> tuple[list[dict[str, Any]], bool]:
    args = ["ip"] + (["-6"] if version == 6 else []) + ["-j", "route", "get", address]
    result = _invoke(runner, args)
    try:
        entries = json.loads(result.stdout) if result.stdout.strip() else []
    except ValueError:
        return [], False
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        return [], False
    if result.returncode:
        # A negative IPv6 route check is evidence only for a definite kernel rejection.
        unreachable = version == 6 and ("network is unreachable" in result.stderr.lower()
                                       or "no route to host" in result.stderr.lower())
        return entries, unreachable
    return entries, True


def _probe(profile_url: str, addresses: list[str], runner: Run) -> tuple[str | None, str | None]:
    """Pin a previously audited public IPv4 answer to prevent DNS-rebinding/SSRF."""
    domain = urlsplit(profile_url).hostname
    if not addresses or any(not _public(ipaddress.IPv4Address(value)) for value in addresses):
        return None, "Probe DNS must contain public IPv4 addresses only"
    command = ["curl", "--disable", "--silent", "--show-error", "--ipv4", "--noproxy", "*", "--proto", "=https",
               "--max-redirs", "0", "--connect-timeout", "5", "--max-time", "15",
               "--max-filesize", "65536", "--resolve", f"{domain}:443:{addresses[0]}",
               "--write-out", "\n%{http_code}", "--url", profile_url]
    result = _invoke(runner, command, timeout=20)
    if result.returncode or len(result.stdout.encode("utf-8")) > 65542:
        return None, "HTTPS probe failed or exceeded the 64 KiB limit"
    body, separator, code = result.stdout.rpartition("\n")
    if not separator or code != "200":
        return None, "HTTPS probe must return HTTP 200 without redirects"
    try:
        try:
            value = json.loads(body)
        except ValueError:
            value = body.strip()
        address = value if isinstance(value, str) else value.get("ip") if isinstance(value, dict) else None
        parsed = ipaddress.IPv4Address(address)
    except (ValueError, TypeError, ipaddress.AddressValueError):
        return None, "HTTPS probe must return a plain IPv4, JSON IP string, or object with an IPv4 ip string"
    if not isinstance(address, str) or not _public(parsed):
        return None, "HTTPS probe did not report a public IPv4 string"
    return str(parsed), None


def verify(profile: dict[str, Any], runner: Run = run, *,
           environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read-only checks. Failures return structured blockers and never readiness."""
    profile = validate_profile(profile)
    blockers: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}
    result = {"readiness": "NETWORK_NOT_VERIFIED", "blockers": blockers,
              "evidence": evidence, "limitations": list(LIMITATIONS)}
    try:
        status, prefs = _status(runner), _prefs(runner)
        evidence["self_id"] = (status.get("Self") or {}).get("ID")
        evidence["tailnet"] = _tailnet(status)
        evidence["prefs"] = prefs
        if status.get("BackendState") != "Running" or not evidence["self_id"]:
            blockers.append(_block("tailscale_not_running", "Tailscale is not connected"))
        if _tailnet(status) != profile["expected_tailnet"]:
            blockers.append(_block("wrong_tailnet", "Connected node does not belong to the expected MagicDNS tailnet"))
        if prefs["ExitNodeID"] or status.get("ExitNodeStatus"):
            blockers.append(_block("exit_node_active", "An active exit node invalidates selected-domain routing"))
        if not prefs["RouteAll"]:
            blockers.append(_block("route_acceptance_disabled", "Daemon RouteAll is false"))
        if not prefs["CorpDNS"]:
            blockers.append(_block("tailscale_dns_disabled", "Daemon CorpDNS is false; connector DNS discovery is not established"))
        proxies = _active_proxies(environ)
        evidence["proxy_variable_names"] = proxy_variable_names(environ)
        if proxies:
            blockers.append(_block("proxy_environment", "Proxy variables invalidate transparent-client verification", names=proxies))
        tls_overrides = tls_override_variable_names(environ)
        evidence["tls_override_variable_names"] = tls_overrides
        if tls_overrides:
            blockers.append(_block("tls_override_environment", "TLS environment overrides invalidate normal trust verification", names=tls_overrides))
        nodes = _nodes(status)
        hubs = [node for node in nodes if profile["hub_ip"] in (node.get("TailscaleIPs") or [])]
        hub = hubs[0] if len(hubs) == 1 else None
        if hub is None or profile["connector_tag"] not in (hub.get("Tags") or []):
            blockers.append(_block("connector_not_identified", "Expected hub IP and connector tag must identify the same unique node"))
        elif not hub.get("Online", False):
            blockers.append(_block("hub_offline", "Hub is offline in Tailscale status; inspect hub/tailnet administration"))
        evidence["hub"] = ({"id": hub.get("ID"), "online": hub.get("Online", False),
                            "tags": hub.get("Tags") or []} if hub else None)
        primary = _primary_routes(status)
        evidence["primary_routes"] = primary
        dns: dict[str, Any] = {}
        route_checks = []
        expected_prefixes: set[str] = set()
        for domain in profile["domains"]:
            ipv4, dns_ok = _dns(runner, domain, 4)
            ipv6, v6_ok = _dns(runner, domain, 6)
            dns[domain] = {"ipv4": ipv4, "ipv6": ipv6}
            if not dns_ok or not ipv4:
                blockers.append(_block("dns_unresolved", "Domain has no usable IPv4 DNS answers", domain=domain))
            if not v6_ok:
                blockers.append(_block("ipv6_dns_unknown", "IPv6 resolver inspection failed", domain=domain))
            for address in ipv4:
                expected_prefixes.add(address + "/32")
                if not _public(ipaddress.IPv4Address(address)):
                    blockers.append(_block("nonpublic_destination", "Selected DNS answer is not public", domain=domain, address=address))
                    continue
                entries, ok = _route(runner, address)
                route_checks.append({"domain": domain, "address": address, "routes": entries})
                if not ok or not entries or any(entry.get("dev") != "tailscale0" or entry.get("type") in {"unreachable", "blackhole", "prohibit"} for entry in entries):
                    blockers.append(_block("route_not_tailscale", "Every IPv4 answer must route through tailscale0", domain=domain, address=address))
                exact = [entry for entry in primary if entry["prefix"] == address + "/32"]
                if hub is None or not exact or any(entry["node_id"] != hub.get("ID") for entry in exact):
                    blockers.append(_block("primary_route_wrong_connector", "Exact host PrimaryRoute must select the expected hub", domain=domain, address=address))
                covering_other = [entry for entry in primary if ipaddress.ip_network(entry["prefix"]).version == 4
                                  and ipaddress.IPv4Address(address) in ipaddress.ip_network(entry["prefix"])
                                  and (hub is None or entry["node_id"] != hub.get("ID"))]
                if covering_other:
                    blockers.append(_block("competing_connector", "Another node offers a covering selected route", address=address, routes=covering_other))
            global_v6 = [address for address in ipv6 if _public(ipaddress.IPv6Address(address))]
            for address in ipv6:
                if not _public(ipaddress.IPv6Address(address)):
                    blockers.append(_block("nonpublic_destination", "Selected IPv6 DNS answer is not public", domain=domain, address=address))
            for address in ipv6:
                expected_prefixes.add(address + "/128")
            for address in global_v6:
                entries, ok = _route(runner, address, 6)
                unreachable = ok and (not entries or all(entry.get("type") in {"unreachable", "blackhole", "prohibit"} for entry in entries))
                route_checks.append({"domain": domain, "address": address, "routes": entries, "unreachable": unreachable})
                if not unreachable:
                    blockers.append(_block("ipv6_requires_separate_acceptance", "A usable or unknown IPv6 path requires separate IPv6 acceptance", domain=domain, address=address))
        evidence["dns"] = dns
        evidence["route_checks"] = route_checks
        v6_inventory = _json_command(runner, ["ip", "-6", "-j", "route", "show", "table", "all"])
        v6_defaults = _ipv6_defaults(v6_inventory)
        evidence["ipv6_default_routes"] = v6_defaults
        if v6_defaults:
            blockers.append(_block("ipv6_egress_available", "IPv6 default egress exists; IPv4-only acceptance cannot establish the selected-domain contract"))
        else:
            result["limitations"].append("No IPv6 default route was observed; negative per-answer IPv6 routes, where present, apply only to this snapshot.")
        unexpected = [entry for entry in primary if entry["prefix"] not in expected_prefixes]
        evidence["unexpected_primary_routes"] = unexpected
        offered = _offered_routes(status)
        evidence["offered_routes"] = offered
        unexpected_offered = [entry for entry in offered if entry["prefix"] not in expected_prefixes and not entry["exit_node_offer"]]
        evidence["unexpected_offered_routes"] = unexpected_offered
        if unexpected or unexpected_offered:
            blockers.append(_block("unexpected_routes", "Accepted route scope contains destinations outside the selected DNS snapshot", routes=unexpected + unexpected_offered))
        # Never send the echo when routing, identity or environment prerequisites fail.
        if not blockers:
            observed, error = _probe(profile["probe_url"], dns[profile["probe_domain"]]["ipv4"], runner)
            evidence["control_endpoint_observed_ipv4"] = observed
            if error:
                blockers.append(_block("probe_failed", error))
            elif observed != profile["expected_egress_ipv4"]:
                blockers.append(_block("egress_mismatch", "Independent control endpoint did not observe expected hub public IPv4", observed=observed))
            if profile.get("non_ai_probe_url"):
                domain = urlsplit(profile["non_ai_probe_url"]).hostname
                addresses, ok = _dns(runner, domain, 4)
                if not ok or not addresses:
                    blockers.append(_block("non_ai_probe_failed", "Non-AI control has no IPv4 DNS answers"))
                else:
                    non_ai_routes = [_route(runner, address) for address in addresses]
                    if any(not ok or not entries or any(entry.get("dev") == "tailscale0" or entry.get("type") in {"unreachable", "blackhole", "prohibit"} for entry in entries)
                           for entries, ok in non_ai_routes):
                        blockers.append(_block("non_ai_route_tailscale", "Non-AI control must use normal VM egress"))
                    else:
                        non_ai, error = _probe(profile["non_ai_probe_url"], addresses, runner)
                        evidence["non_ai_observed_ipv4"] = non_ai
                        if error:
                            blockers.append(_block("non_ai_probe_failed", error))
                        elif non_ai == profile["expected_egress_ipv4"]:
                            blockers.append(_block("non_ai_egress_matches_hub", "Non-AI endpoint observed hub egress; selective routing is not established"))
            else:
                result["limitations"].append("No independent non-AI endpoint was configured; ordinary VM egress was not externally measured.")
    except NetworkError as exc:
        blockers.append(_block("inspection_failed", str(exc)))
    if not blockers:
        result["readiness"] = "NETWORK_VERIFIED"
    elif any(item["code"] in {"connector_not_identified", "hub_offline", "primary_route_wrong_connector", "competing_connector", "unexpected_routes"} for item in blockers):
        result["readiness"] = "NEEDS_HUB_ADMIN"
    return result


def rollback(profile: dict[str, Any], state: StateStore, runner: Run = run) -> dict[str, Any]:
    """Restore RouteAll only when node identity and our last applied value match."""
    profile = validate_profile(profile)
    result: dict[str, Any] = {"changed": False, "blockers": []}
    # Hold the state transaction lock across the compare and preference mutation.
    def transaction(data: dict[str, Any]) -> None:
        network = data.get("network") or {}
        baseline, applied = network.get("baseline"), network.get("apply")
        if not baseline or not applied or applied.get("status") in {"rolled_back", "not_applied"}:
            result["already_restored"] = True
            return
        if network.get("profile_id") != _profile_id(profile):
            result["blockers"].append(_block("profile_mismatch", "Rollback profile does not match the original transaction"))
            return
        status = _status(runner)
        self_id = (status.get("Self") or {}).get("ID")
        if self_id != baseline.get("self_id") or _tailnet(status) != baseline.get("tailnet"):
            result["blockers"].append(_block("identity_changed", "Tailscale identity changed; refusing rollback"))
            return
        current = _prefs(runner)["RouteAll"]
        original = (baseline.get("prefs") or {}).get("RouteAll")
        if type(original) is not bool or applied.get("after") is not True:
            result["blockers"].append(_block("baseline_invalid", "Saved RouteAll baseline is invalid"))
            return
        if current == original:
            applied["status"] = "rolled_back"
            result["already_restored"] = True
            return
        if current != applied["after"]:
            result["blockers"].append(_block("preference_changed", "RouteAll differs from the applied value; refusing rollback"))
            return
        operation = _invoke(runner, ["tailscale", "set", "--accept-routes=" + str(original).lower()])
        observed = _prefs(runner)["RouteAll"]
        if observed == original:
            applied["status"] = "rolled_back"
            result["changed"] = True
        if operation.returncode or observed != original:
            result["blockers"].append(_block("rollback_failed", "Daemon did not confirm the rollback"))
    try:
        state.update(transaction)
    except NetworkError as exc:
        result["blockers"].append(_block("inspection_failed", str(exc)))
    return result
