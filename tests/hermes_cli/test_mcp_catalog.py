"""Tests for hermes_cli.mcp_catalog and hermes_cli.mcp_picker.

Manifest parsing, install/uninstall config writes, and picker plumbing
are exercised here. Anything that would actually clone a repo or
launch an MCP is mocked.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog_dir(tmp_path, monkeypatch):
    """Provide an isolated optional-mcps/ directory."""
    cat = tmp_path / "optional-mcps"
    cat.mkdir()
    monkeypatch.setenv("HERMES_OPTIONAL_MCPS", str(cat))
    return cat


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """Redirect all config I/O to a temp HERMES_HOME."""
    hh = tmp_path / "hermes-home"
    hh.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hh))
    monkeypatch.setattr(
        "hermes_cli.config.get_hermes_home", lambda: hh
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_config_path", lambda: hh / "config.yaml"
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_env_path", lambda: hh / ".env"
    )
    # mcp_catalog grabs get_hermes_home() lazily through hermes_constants
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: hh
    )
    return hh


def _write_manifest(catalog_dir: Path, name: str, body: dict) -> Path:
    entry_dir = catalog_dir / name
    entry_dir.mkdir(exist_ok=True)
    path = entry_dir / "manifest.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(body, f)
    return path


def _basic_manifest(name: str = "demo", **overrides) -> dict:
    body = {
        "manifest_version": 1,
        "name": name,
        "description": "Demo MCP",
        "source": "https://example.com",
        "transport": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "demo-mcp"],
        },
        "auth": {"type": "none"},
    }
    body.update(overrides)
    return body


def _entry(name: str):
    """Wrapper that asserts entry exists (satisfies type-checker + nicer failure msg)."""
    from hermes_cli.mcp_catalog import get_entry

    e = get_entry(name)
    assert e is not None, f"catalog entry {name!r} missing"
    return e



# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


class TestManifestParsing:
    def test_minimal_valid(self, catalog_dir):
        _write_manifest(catalog_dir, "demo", _basic_manifest())
        from hermes_cli.mcp_catalog import list_catalog

        entries = list_catalog()
        assert len(entries) == 1
        e = entries[0]
        assert e.name == "demo"
        assert e.transport.type == "stdio"
        assert e.transport.command == "npx"
        assert e.transport.args == ["-y", "demo-mcp"]
        assert e.auth.type == "none"
        assert e.install is None

    def test_api_key_auth(self, catalog_dir):
        body = _basic_manifest(
            auth={
                "type": "api_key",
                "env": [
                    {"name": "DEMO_KEY", "prompt": "API key", "secret": True},
                    {"name": "DEMO_URL", "prompt": "Base URL", "secret": False, "required": False},
                ],
            }
        )
        _write_manifest(catalog_dir, "demo", body)
        from hermes_cli.mcp_catalog import list_catalog

        e = list_catalog()[0]
        assert e.auth.type == "api_key"
        assert len(e.auth.env) == 2
        assert e.auth.env[0].name == "DEMO_KEY"
        assert e.auth.env[0].secret is True
        assert e.auth.env[1].required is False
        assert e.auth.env[1].secret is False

    def test_install_block(self, catalog_dir):
        body = _basic_manifest(
            install={
                "type": "git",
                "url": "https://example.com/demo.git",
                "ref": "v1.0.0",
                "bootstrap": ["pip install -r requirements.txt"],
            },
            transport={
                "type": "stdio",
                "command": "${INSTALL_DIR}/.venv/bin/python",
                "args": ["${INSTALL_DIR}/server.py"],
            },
        )
        _write_manifest(catalog_dir, "demo", body)
        from hermes_cli.mcp_catalog import list_catalog

        e = list_catalog()[0]
        assert e.install is not None
        assert e.install.url == "https://example.com/demo.git"
        assert e.install.ref == "v1.0.0"
        assert e.install.bootstrap == ["pip install -r requirements.txt"]

    def test_invalid_manifest_skipped(self, catalog_dir):
        # Broken: wrong manifest_version
        _write_manifest(catalog_dir, "bad", {
            "manifest_version": 99,
            "name": "bad",
            "description": "x",
            "transport": {"type": "stdio", "command": "x"},
        })
        # Good
        _write_manifest(catalog_dir, "demo", _basic_manifest())
        from hermes_cli.mcp_catalog import list_catalog

        entries = list_catalog()
        assert [e.name for e in entries] == ["demo"]

    def test_missing_transport_command_rejected(self, catalog_dir):
        body = _basic_manifest()
        body["transport"] = {"type": "stdio"}  # no command
        _write_manifest(catalog_dir, "demo", body)
        from hermes_cli.mcp_catalog import list_catalog

        assert list_catalog() == []

    def test_get_entry_strips_official_prefix(self, catalog_dir):
        _write_manifest(catalog_dir, "demo", _basic_manifest())
        from hermes_cli.mcp_catalog import get_entry

        assert get_entry("demo") is not None
        assert get_entry("official/demo") is not None
        assert get_entry("missing") is None


# ---------------------------------------------------------------------------
# Install flow
# ---------------------------------------------------------------------------


class TestInstall:
    def test_install_simple_stdio_writes_config(self, catalog_dir):
        _write_manifest(catalog_dir, "demo", _basic_manifest())
        from hermes_cli.mcp_catalog import install_entry, get_entry
        from hermes_cli.config import load_config

        install_entry(_entry("demo"), enable=True)

        cfg = load_config()
        servers = cfg["mcp_servers"]
        assert "demo" in servers
        assert servers["demo"]["command"] == "npx"
        assert servers["demo"]["args"] == ["-y", "demo-mcp"]
        assert servers["demo"]["enabled"] is True

    def test_install_with_install_dir_substitution(self, catalog_dir, tmp_path):
        body = _basic_manifest(
            install={
                "type": "git",
                "url": "https://example.com/demo.git",
                "ref": "main",
                "bootstrap": [],
            },
            transport={
                "type": "stdio",
                "command": "${INSTALL_DIR}/run.sh",
                "args": ["${INSTALL_DIR}/cfg.json"],
            },
        )
        _write_manifest(catalog_dir, "demo", body)

        # Mock the git clone — return a known directory
        fake_clone = tmp_path / "fake-clone"
        fake_clone.mkdir()

        from hermes_cli import mcp_catalog
        from hermes_cli.mcp_catalog import install_entry, get_entry
        from hermes_cli.config import load_config

        with patch.object(mcp_catalog, "_do_git_install", return_value=fake_clone):
            install_entry(_entry("demo"), enable=True)

        servers = load_config()["mcp_servers"]
        assert servers["demo"]["command"] == f"{fake_clone}/run.sh"
        assert servers["demo"]["args"] == [f"{fake_clone}/cfg.json"]

    def test_install_with_api_key_prompts_and_saves(self, catalog_dir, monkeypatch):
        body = _basic_manifest(
            auth={
                "type": "api_key",
                "env": [{"name": "DEMO_KEY", "prompt": "key", "secret": True}],
            }
        )
        _write_manifest(catalog_dir, "demo", body)

        from hermes_cli import mcp_catalog

        monkeypatch.setattr(mcp_catalog, "_prompt_input", lambda *a, **kw: "secret-val")

        from hermes_cli.mcp_catalog import install_entry, get_entry
        from hermes_cli.config import get_env_value, load_config

        install_entry(_entry("demo"), enable=True)

        assert get_env_value("DEMO_KEY") == "secret-val"
        assert "demo" in load_config()["mcp_servers"]

    def test_install_http_oauth_writes_auth_marker(self, catalog_dir):
        body = _basic_manifest(
            transport={"type": "http", "url": "https://mcp.example.com/sse"},
            auth={"type": "oauth"},
        )
        _write_manifest(catalog_dir, "demo", body)

        from hermes_cli.mcp_catalog import install_entry, get_entry
        from hermes_cli.config import load_config

        install_entry(_entry("demo"), enable=True)

        server = load_config()["mcp_servers"]["demo"]
        assert server["url"] == "https://mcp.example.com/sse"
        assert server["auth"] == "oauth"

    def test_install_required_env_missing_raises(self, catalog_dir, monkeypatch):
        body = _basic_manifest(
            auth={
                "type": "api_key",
                "env": [{"name": "MUST", "prompt": "x", "required": True, "secret": False}],
            }
        )
        _write_manifest(catalog_dir, "demo", body)

        from hermes_cli import mcp_catalog
        from hermes_cli.mcp_catalog import install_entry, get_entry, CatalogError

        # User hits enter — empty input, no default
        monkeypatch.setattr(mcp_catalog, "_prompt_input", lambda *a, **kw: "")

        with pytest.raises(CatalogError):
            install_entry(_entry("demo"), enable=True)


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_uninstall_removes_server_block(self, catalog_dir):
        _write_manifest(catalog_dir, "demo", _basic_manifest())
        from hermes_cli.mcp_catalog import install_entry, get_entry, uninstall_entry
        from hermes_cli.config import load_config

        install_entry(_entry("demo"), enable=True)
        assert "demo" in load_config().get("mcp_servers", {})

        assert uninstall_entry("demo") is True
        assert "demo" not in load_config().get("mcp_servers", {})

    def test_uninstall_missing_returns_false(self):
        from hermes_cli.mcp_catalog import uninstall_entry

        assert uninstall_entry("nonexistent") is False


# ---------------------------------------------------------------------------
# Picker (non-TTY paths only — interactive curses is integration-tested)
# ---------------------------------------------------------------------------


class TestPicker:
    def test_show_catalog_empty(self, catalog_dir, capsys):
        from hermes_cli.mcp_picker import show_catalog

        show_catalog()
        out = capsys.readouterr().out
        assert "No MCPs in the catalog" in out

    def test_show_catalog_lists_entry(self, catalog_dir, capsys):
        _write_manifest(catalog_dir, "demo", _basic_manifest())
        from hermes_cli.mcp_picker import show_catalog

        show_catalog()
        out = capsys.readouterr().out
        assert "demo" in out
        assert "available" in out

    def test_install_by_name_unknown(self, catalog_dir, capsys):
        from hermes_cli.mcp_picker import install_by_name

        rc = install_by_name("nope")
        assert rc == 1
        assert "not in the catalog" in capsys.readouterr().out

    def test_install_by_name_success(self, catalog_dir):
        _write_manifest(catalog_dir, "demo", _basic_manifest())
        from hermes_cli.mcp_picker import install_by_name
        from hermes_cli.config import load_config

        rc = install_by_name("demo")
        assert rc == 0
        assert "demo" in load_config().get("mcp_servers", {})

    def test_run_picker_non_tty_falls_back(self, catalog_dir, capsys, monkeypatch):
        _write_manifest(catalog_dir, "demo", _basic_manifest())
        # Force isatty false
        import sys as _sys
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
        from hermes_cli.mcp_picker import run_picker

        run_picker()
        out = capsys.readouterr().out
        assert "Nous-approved MCP catalog" in out


# ---------------------------------------------------------------------------
# Shipped catalog (sanity: every manifest in the repo's optional-mcps/ parses)
# ---------------------------------------------------------------------------


class TestShippedCatalog:
    def test_all_shipped_manifests_parse(self, monkeypatch):
        """Every manifest in optional-mcps/ must parse cleanly.

        This is a contract test — CI will fail if a PR adds a malformed
        manifest. Intentionally NOT a snapshot of catalog names (those are
        expected to change as PRs land).
        """
        # Use the actual repo's optional-mcps directory (no HERMES_OPTIONAL_MCPS
        # override) so this test catches real manifests.
        monkeypatch.delenv("HERMES_OPTIONAL_MCPS", raising=False)
        from hermes_cli.mcp_catalog import _catalog_root, _parse_manifest

        root = _catalog_root()
        if not root.exists():
            pytest.skip("optional-mcps/ not present in this checkout")

        manifests = list(root.glob("*/manifest.yaml"))
        # Don't assert minimum count — change-detector test rule. Just parse
        # whatever exists.
        for m in manifests:
            entry = _parse_manifest(m)
            assert entry.name
            assert entry.description
            assert entry.transport.type in ("stdio", "http")
