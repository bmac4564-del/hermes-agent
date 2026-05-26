"""Regression tests for gateway runtime authority status freshness."""

from __future__ import annotations

import json


def test_runtime_status_rewrites_authority_fields_and_drops_stale_plugin_snapshot(
    tmp_path, monkeypatch,
):
    from gateway import status

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_AGENT_REPO", "/tmp/current-hermes")
    monkeypatch.setenv("HERMES_RELEASE_SHA", "release-sha")

    state_path = tmp_path / "gateway_state.json"
    state_path.write_text(
        json.dumps(
            {
                "gateway_status_schema_version": 2,
                "gateway_state": "running",
                "runtime_plugins": {
                    "entries": [
                        {
                            "name": "old",
                            "path": "/home/jeremy/work/repos/hermes-agent-may22-rollback-9af54c567/plugins/web/tavily",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    status.write_runtime_status(gateway_state="running")

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert "runtime_plugins" not in payload
    authority = payload["runtime_authority"]
    assert authority["authority_path"] == "/tmp/current-hermes"
    assert authority["release_sha"] == "release-sha"
    assert authority["bundled_plugins_dir"].endswith("/plugins")
