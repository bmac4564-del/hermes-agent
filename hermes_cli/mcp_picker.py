"""MCP picker — interactive `hermes mcp picker` (also the default `hermes mcp`).

Lists every catalog entry plus user-installed servers, lets the user pick one,
and routes to install / enable / disable / uninstall flows.

Mirrors the `hermes plugin` picker UX: arrow keys to navigate, ENTER on a row
to act on it. The action depends on current status:

  not installed   → install  (clone/bootstrap if needed, prompt for creds)
  installed/off   → enable
  installed/on    → menu: disable / uninstall
"""

from __future__ import annotations

import sys
from typing import List, Optional, Tuple

from hermes_cli.colors import Colors, color
from hermes_cli.cli_output import prompt_yes_no
from hermes_cli.curses_ui import curses_single_select
from hermes_cli.mcp_catalog import (
    CatalogEntry,
    CatalogError,
    install_entry,
    is_enabled,
    is_installed,
    list_catalog,
    installed_servers,
    uninstall_entry,
)
from hermes_cli.config import load_config, save_config


# ─── Status badges ────────────────────────────────────────────────────────────

_STATUS_NOT_INSTALLED = "available"
_STATUS_DISABLED = "installed (disabled)"
_STATUS_ENABLED = "enabled"


def _status_for(entry: CatalogEntry) -> str:
    if not is_installed(entry.name):
        return _STATUS_NOT_INSTALLED
    if is_enabled(entry.name):
        return _STATUS_ENABLED
    return _STATUS_DISABLED


def _format_row(entry: CatalogEntry) -> str:
    status = _status_for(entry)
    return f"{entry.name:<18} {status:<22} {entry.description}"


def _enable_disable(name: str, *, enable: bool) -> None:
    cfg = load_config()
    servers = cfg.get("mcp_servers") or {}
    server = servers.get(name)
    if not server:
        print(color(f"  '{name}' is not installed.", Colors.RED))
        return
    server["enabled"] = enable
    cfg["mcp_servers"] = servers
    save_config(cfg)
    print(color(
        f"  ✓ '{name}' {'enabled' if enable else 'disabled'}. "
        "Start a new Hermes session for changes to take effect.",
        Colors.GREEN,
    ))


def _handle_entry(entry: CatalogEntry) -> None:
    """Act on the picked entry based on its current status."""
    if not is_installed(entry.name):
        try:
            install_entry(entry, enable=True)
        except CatalogError as exc:
            print(color(f"  ✗ install failed: {exc}", Colors.RED))
        return

    if not is_enabled(entry.name):
        _enable_disable(entry.name, enable=True)
        return

    # Installed + enabled — offer to disable or uninstall
    print()
    print(color(f"  '{entry.name}' is already enabled.", Colors.DIM))
    actions = [
        "Disable (keep config, stop loading on next session)",
        "Uninstall (remove config and any cloned files)",
        "Reinstall (re-clone, re-prompt for credentials)",
    ]
    choice = curses_single_select(f"Action for '{entry.name}'", actions)
    if choice is None:
        return
    if choice == 0:
        _enable_disable(entry.name, enable=False)
    elif choice == 1:
        if prompt_yes_no(f"Uninstall '{entry.name}'?", default=False):
            if uninstall_entry(entry.name):
                print(color(f"  ✓ Uninstalled '{entry.name}'", Colors.GREEN))
            else:
                print(color(f"  '{entry.name}' was not installed", Colors.DIM))
    elif choice == 2:
        try:
            install_entry(entry, enable=True)
        except CatalogError as exc:
            print(color(f"  ✗ reinstall failed: {exc}", Colors.RED))


def _print_catalog_text(entries: List[CatalogEntry]) -> None:
    """Plain-text catalog dump used as a fallback when curses can't run, and
    as the default output of `hermes mcp catalog`."""
    if not entries:
        print()
        print(color("  No MCPs in the catalog yet.", Colors.DIM))
        print()
        return

    print()
    print(color("  Nous-approved MCP catalog:", Colors.CYAN + Colors.BOLD))
    print()
    print(f"  {'Name':<18} {'Status':<22} Description")
    print(f"  {'-' * 18} {'-' * 22} {'-' * 11}")
    for entry in entries:
        print(f"  {_format_row(entry)}")
    print()
    print(color(
        "  Install: hermes mcp install <name>    Picker: hermes mcp",
        Colors.DIM,
    ))
    print()


def show_catalog() -> None:
    """`hermes mcp catalog` — print the curated list, no interaction."""
    _print_catalog_text(list_catalog())


def run_picker() -> None:
    """`hermes mcp picker` (and default `hermes mcp`) — interactive selector."""
    entries = list_catalog()
    if not entries:
        _print_catalog_text(entries)
        return

    if not sys.stdin.isatty():
        # Non-interactive shell: degrade to the text dump rather than failing.
        _print_catalog_text(entries)
        return

    rows = [_format_row(e) for e in entries]
    idx = curses_single_select(
        "MCP Catalog  —  ↑↓ navigate  ENTER act on entry  ESC/q quit",
        rows,
    )
    if idx is None:
        return
    _handle_entry(entries[idx])


def install_by_name(identifier: str) -> int:
    """`hermes mcp install <name>` — non-interactive entry-point.

    Returns 0 on success, non-zero on failure (so the CLI can propagate
    exit codes).
    """
    from hermes_cli.mcp_catalog import get_entry

    entry = get_entry(identifier)
    if entry is None:
        print(color(
            f"  ✗ '{identifier}' is not in the catalog. "
            "Run `hermes mcp catalog` to see available entries.",
            Colors.RED,
        ))
        return 1
    try:
        install_entry(entry, enable=True)
    except CatalogError as exc:
        print(color(f"  ✗ install failed: {exc}", Colors.RED))
        return 1
    return 0
