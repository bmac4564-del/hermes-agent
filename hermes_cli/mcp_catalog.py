"""MCP catalog — curated, Nous-approved MCP servers shipped with the repo.

Mirrors the optional-skills/ pattern: each catalog entry lives under
``optional-mcps/<name>/manifest.yaml`` and ships disabled. Users discover
entries via ``hermes mcp catalog`` or the interactive ``hermes mcp picker``,
and install them with ``hermes mcp install <name>`` (or by toggling in the
picker, which flows them through any required env/OAuth setup).

Catalog policy:
- Entries are added only by merging a PR into hermes-agent. Presence in the
  ``optional-mcps/`` directory = Nous approval. No community tier, no trust
  signals beyond "it's in the catalog".
- Manifests pin transport details (commands, args, refs). MCPs are never
  auto-updated; users explicitly re-run ``hermes mcp install <name>`` to
  pull a new manifest version after a repo update.
- Secrets prompted at install time go to ``~/.hermes/.env`` (the
  .env-is-for-secrets rule). Non-secret env vars also go to .env to keep
  one credential store.

See website/docs/user-guide/mcp-catalog.md for user docs.
See references/mcp-catalog.md (this repo's skill) for the manifest schema.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from hermes_constants import get_hermes_home, get_optional_mcps_dir
from hermes_cli.colors import Colors, color
from hermes_cli.config import (
    load_config,
    save_config,
    get_env_value,
    save_env_value,
)
from hermes_cli.cli_output import prompt as _prompt_input, prompt_yes_no

_MANIFEST_VERSION = 1

# Substituted at install time inside `transport.command` / `transport.args`.
_INSTALL_DIR_VAR = "${INSTALL_DIR}"


# ─── Data classes ────────────────────────────────────────────────────────────


@dataclass
class EnvVarSpec:
    name: str
    prompt: str
    required: bool = True
    secret: bool = True
    default: str = ""


@dataclass
class AuthSpec:
    type: str  # "api_key" | "oauth" | "none"
    env: List[EnvVarSpec] = field(default_factory=list)
    # OAuth-specific (case 2: third-party provider like Google)
    provider: Optional[str] = None
    scopes: List[str] = field(default_factory=list)
    env_var: Optional[str] = None


@dataclass
class TransportSpec:
    type: str  # "stdio" | "http"
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    url: Optional[str] = None
    version: Optional[str] = None  # informational, pinned


@dataclass
class InstallSpec:
    """Optional bootstrap step (git clone + dep install).

    Omit for one-shot launchable servers (npx, uvx).
    """
    type: str  # "git"
    url: str
    ref: str  # commit/tag/branch — pinned, never floats
    bootstrap: List[str] = field(default_factory=list)


@dataclass
class CatalogEntry:
    name: str
    description: str
    source: str
    transport: TransportSpec
    auth: AuthSpec
    install: Optional[InstallSpec] = None
    post_install: str = ""
    manifest_path: Path = field(default_factory=Path)


# ─── Manifest loader ─────────────────────────────────────────────────────────


class CatalogError(Exception):
    """Manifest parse/validation failure or install error."""


def _catalog_root() -> Path:
    """Return the optional-mcps/ directory shipped with this Hermes install."""
    # Prefer the env-var override / packaged location; fall back to the repo's
    # optional-mcps/ next to the package (source checkout).
    return get_optional_mcps_dir(Path(__file__).parent.parent / "optional-mcps")


def _parse_env_spec(raw: Any) -> EnvVarSpec:
    if not isinstance(raw, dict):
        raise CatalogError(f"env entry must be a mapping, got {type(raw).__name__}")
    name = raw.get("name") or ""
    if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise CatalogError(f"invalid env var name: {name!r}")
    return EnvVarSpec(
        name=name,
        prompt=raw.get("prompt") or name,
        required=bool(raw.get("required", True)),
        secret=bool(raw.get("secret", True)),
        default=str(raw.get("default") or ""),
    )


def _parse_manifest(path: Path) -> CatalogEntry:
    """Read and validate a manifest.yaml. Raise CatalogError on any problem."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        raise CatalogError(f"failed to read {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise CatalogError(f"{path}: manifest must be a mapping")

    mv = data.get("manifest_version")
    if mv != _MANIFEST_VERSION:
        raise CatalogError(
            f"{path}: manifest_version {mv!r} unsupported "
            f"(this Hermes understands version {_MANIFEST_VERSION})"
        )

    name = data.get("name") or ""
    if not name or not re.match(r"^[A-Za-z0-9_-]+$", name):
        raise CatalogError(f"{path}: invalid or missing 'name'")

    description = str(data.get("description") or "").strip()
    if not description:
        raise CatalogError(f"{path}: 'description' required")

    source = str(data.get("source") or "").strip()

    transport_raw = data.get("transport") or {}
    if not isinstance(transport_raw, dict):
        raise CatalogError(f"{path}: 'transport' must be a mapping")
    t_type = transport_raw.get("type")
    if t_type not in ("stdio", "http"):
        raise CatalogError(f"{path}: transport.type must be 'stdio' or 'http'")
    args = transport_raw.get("args") or []
    if not isinstance(args, list):
        raise CatalogError(f"{path}: transport.args must be a list")
    transport = TransportSpec(
        type=t_type,
        command=transport_raw.get("command"),
        args=[str(a) for a in args],
        url=transport_raw.get("url"),
        version=transport_raw.get("version"),
    )
    if t_type == "stdio" and not transport.command:
        raise CatalogError(f"{path}: stdio transport requires 'command'")
    if t_type == "http" and not transport.url:
        raise CatalogError(f"{path}: http transport requires 'url'")

    auth_raw = data.get("auth") or {"type": "none"}
    if not isinstance(auth_raw, dict):
        raise CatalogError(f"{path}: 'auth' must be a mapping")
    a_type = auth_raw.get("type") or "none"
    if a_type not in ("api_key", "oauth", "none"):
        raise CatalogError(f"{path}: auth.type must be 'api_key'|'oauth'|'none'")
    env_list_raw = auth_raw.get("env") or []
    if not isinstance(env_list_raw, list):
        raise CatalogError(f"{path}: auth.env must be a list")
    env_list = [_parse_env_spec(e) for e in env_list_raw]
    auth = AuthSpec(
        type=a_type,
        env=env_list,
        provider=auth_raw.get("provider"),
        scopes=list(auth_raw.get("scopes") or []),
        env_var=auth_raw.get("env_var"),
    )

    install: Optional[InstallSpec] = None
    install_raw = data.get("install")
    if install_raw is not None:
        if not isinstance(install_raw, dict):
            raise CatalogError(f"{path}: 'install' must be a mapping")
        i_type = install_raw.get("type")
        if i_type != "git":
            raise CatalogError(f"{path}: install.type must be 'git' (got {i_type!r})")
        url = install_raw.get("url") or ""
        ref = install_raw.get("ref") or ""
        if not url or not ref:
            raise CatalogError(f"{path}: install.url and install.ref are required")
        bootstrap = install_raw.get("bootstrap") or []
        if not isinstance(bootstrap, list):
            raise CatalogError(f"{path}: install.bootstrap must be a list")
        install = InstallSpec(
            type=i_type,
            url=url,
            ref=ref,
            bootstrap=[str(c) for c in bootstrap],
        )

    return CatalogEntry(
        name=name,
        description=description,
        source=source,
        transport=transport,
        auth=auth,
        install=install,
        post_install=str(data.get("post_install") or ""),
        manifest_path=path,
    )


def list_catalog() -> List[CatalogEntry]:
    """Return all valid catalog entries, sorted by name.

    Invalid manifests are skipped silently (they should be caught in CI tests).
    """
    root = _catalog_root()
    if not root.exists():
        return []
    entries: List[CatalogEntry] = []
    for child in sorted(root.iterdir()):
        manifest = child / "manifest.yaml"
        if not manifest.is_file():
            continue
        try:
            entries.append(_parse_manifest(manifest))
        except CatalogError:
            # Skip broken manifests; tests guard against this.
            continue
    return entries


def get_entry(name: str) -> Optional[CatalogEntry]:
    """Look up a single entry by name. ``official/<name>`` prefix accepted."""
    if name.startswith("official/"):
        name = name[len("official/"):]
    for entry in list_catalog():
        if entry.name == name:
            return entry
    return None


# ─── Status helpers ──────────────────────────────────────────────────────────


def installed_servers() -> Dict[str, dict]:
    """Return current ``mcp_servers`` block from config.yaml."""
    cfg = load_config()
    servers = cfg.get("mcp_servers") or {}
    return servers if isinstance(servers, dict) else {}


def is_installed(name: str) -> bool:
    return name in installed_servers()


def is_enabled(name: str) -> bool:
    servers = installed_servers()
    cfg = servers.get(name)
    if not cfg:
        return False
    enabled = cfg.get("enabled", True)
    if isinstance(enabled, str):
        return enabled.lower() in {"true", "1", "yes"}
    return bool(enabled)


# ─── Install ─────────────────────────────────────────────────────────────────


def _install_root() -> Path:
    """Where git-bootstrapped MCPs are cloned. Per-user, profile-aware."""
    root = get_hermes_home() / "mcp-installs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run_bootstrap(cwd: Path, commands: List[str]) -> None:
    """Execute bootstrap commands in *cwd*. Raise CatalogError on first failure.

    Each command runs through the shell (so `&&` etc. work). The output is
    streamed to the user's terminal for visibility.
    """
    for cmd in commands:
        print(color(f"  $ {cmd}", Colors.DIM))
        proc = subprocess.run(cmd, cwd=str(cwd), shell=True)
        if proc.returncode != 0:
            raise CatalogError(
                f"bootstrap step failed (exit {proc.returncode}): {cmd}"
            )


def _do_git_install(entry: CatalogEntry) -> Path:
    """Clone the entry's repo into ``~/.hermes/mcp-installs/<name>`` and run
    bootstrap commands. Returns the install directory."""
    assert entry.install is not None and entry.install.type == "git"
    install = entry.install
    dest = _install_root() / entry.name

    git = shutil.which("git")
    if not git:
        raise CatalogError("git is required to install this MCP but was not found on PATH")

    if dest.exists():
        # Fresh checkout each install — manifest version is the source of truth,
        # so wipe + re-clone for determinism.
        print(color(f"  Removing existing install at {dest}", Colors.DIM))
        shutil.rmtree(dest)

    print(color(f"  Cloning {install.url} ({install.ref}) → {dest}", Colors.CYAN))
    proc = subprocess.run(
        [git, "clone", "--depth", "1", "--branch", install.ref, install.url, str(dest)],
    )
    if proc.returncode != 0:
        # Fall back to fetching the ref by SHA (clone --branch only accepts
        # branches and tags, not arbitrary commits).
        proc = subprocess.run([git, "clone", install.url, str(dest)])
        if proc.returncode != 0:
            raise CatalogError(f"git clone failed for {install.url}")
        proc = subprocess.run([git, "-C", str(dest), "checkout", install.ref])
        if proc.returncode != 0:
            raise CatalogError(f"git checkout {install.ref} failed")

    if install.bootstrap:
        _run_bootstrap(dest, install.bootstrap)

    return dest


def _expand_install_dir(value: str, install_dir: Optional[Path]) -> str:
    if _INSTALL_DIR_VAR not in value:
        return value
    if install_dir is None:
        raise CatalogError(
            f"manifest references {_INSTALL_DIR_VAR} but no install block exists"
        )
    return value.replace(_INSTALL_DIR_VAR, str(install_dir))


def _prompt_env_vars(specs: List[EnvVarSpec]) -> Dict[str, str]:
    """Walk the env spec list, prompting the user for each. Writes secrets and
    non-secrets alike to ~/.hermes/.env via save_env_value()."""
    collected: Dict[str, str] = {}
    for spec in specs:
        existing = get_env_value(spec.name)
        if existing:
            print(color(f"  ✓ {spec.name} already set in .env", Colors.GREEN))
            collected[spec.name] = existing
            continue
        value = _prompt_input(
            spec.prompt,
            default=spec.default or None,
            password=spec.secret,
        )
        if not value:
            if spec.required:
                raise CatalogError(f"{spec.name} is required but no value was provided")
            continue
        save_env_value(spec.name, value)
        collected[spec.name] = value
    return collected


def _build_server_config(
    entry: CatalogEntry, install_dir: Optional[Path]
) -> dict:
    """Translate a manifest into the ``mcp_servers.<name>`` block format used
    by hermes_cli/mcp_config.py."""
    cfg: dict = {}
    t = entry.transport
    if t.type == "stdio":
        cfg["command"] = _expand_install_dir(t.command or "", install_dir)
        if t.args:
            cfg["args"] = [_expand_install_dir(a, install_dir) for a in t.args]
    elif t.type == "http":
        cfg["url"] = t.url
        if entry.auth.type == "oauth":
            cfg["auth"] = "oauth"
    return cfg


def install_entry(entry: CatalogEntry, *, enable: bool = True) -> None:
    """Install a catalog entry end-to-end.

    Steps:
        1. If ``install.type == git``, clone + run bootstrap commands.
        2. If ``auth.type == api_key``, prompt for env vars, save to .env.
        3. If ``auth.type == oauth`` (remote MCP / case 1), write the
           ``auth: oauth`` marker and let the MCP client handle browser flow
           on first connection.
        4. Translate the manifest into an ``mcp_servers.<name>`` block and
           save into config.yaml.
        5. Print post_install notes.
    """
    print()
    print(color(f"  Installing MCP '{entry.name}'", Colors.CYAN + Colors.BOLD))
    if entry.description:
        print(color(f"  {entry.description}", Colors.DIM))
    if entry.source:
        print(color(f"  Source: {entry.source}", Colors.DIM))
    print()

    install_dir: Optional[Path] = None
    if entry.install is not None:
        install_dir = _do_git_install(entry)

    # Auth
    if entry.auth.type == "api_key":
        print()
        print(color("  Configure credentials:", Colors.CYAN))
        _prompt_env_vars(entry.auth.env)
    elif entry.auth.type == "oauth":
        if entry.auth.provider:
            # Case 2: provider-mediated (Google, GitHub, etc.). We rely on
            # the existing `hermes auth <provider>` flow. Surface guidance
            # here rather than auto-running it — keeps the catalog install
            # decoupled from provider-auth lifecycle.
            print(color(
                f"  This MCP uses {entry.auth.provider} OAuth. Run "
                f"`hermes auth {entry.auth.provider}` if you have not "
                "already authenticated.",
                Colors.YELLOW,
            ))
        else:
            print(color(
                "  This MCP uses native OAuth 2.1; tokens will be acquired "
                "on first connection (browser flow).",
                Colors.DIM,
            ))
    # auth.type == "none": nothing to do.

    # Build and write the mcp_servers entry
    server_cfg = _build_server_config(entry, install_dir)
    server_cfg["enabled"] = enable

    cfg = load_config()
    cfg.setdefault("mcp_servers", {})[entry.name] = server_cfg
    save_config(cfg)

    print()
    print(color(
        f"  ✓ Installed '{entry.name}' "
        f"({'enabled' if enable else 'disabled'}). "
        f"Start a new Hermes session to load its tools.",
        Colors.GREEN,
    ))
    if entry.post_install:
        print()
        for line in entry.post_install.strip().splitlines():
            print(color(f"  {line}", Colors.DIM))
    print()


def uninstall_entry(name: str, *, purge_install_dir: bool = True) -> bool:
    """Remove a catalog-installed MCP from config and (optionally) wipe its
    clone directory. Returns True if anything was removed."""
    cfg = load_config()
    servers = cfg.get("mcp_servers") or {}
    removed = False
    if name in servers:
        del servers[name]
        if not servers:
            cfg.pop("mcp_servers", None)
        else:
            cfg["mcp_servers"] = servers
        save_config(cfg)
        removed = True

    if purge_install_dir:
        clone = _install_root() / name
        if clone.exists():
            shutil.rmtree(clone)
            removed = True

    return removed
