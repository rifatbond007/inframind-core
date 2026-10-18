"""Repo-level invariants: ownership handles, agent model IDs, env template, project URLs."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GITHUB_USERS = {"@rifatbond007", "@moneem-07", "@promerayhan"}


def test_codeowners_handles_are_github_usernames() -> None:
    lines = (ROOT / ".github" / "CODEOWNERS").read_text().splitlines()
    handles = {
        h
        for line in lines
        if not line.lstrip().startswith("#")
        for h in re.findall(r"@[\w-]+", line)
    }
    assert handles
    assert handles <= GITHUB_USERS, f"unknown handles: {sorted(handles - GITHUB_USERS)}"


def test_agent_model_ids_are_valid() -> None:
    files = sorted((ROOT / ".claude" / "agents").rglob("*.md"))
    assert files
    for f in files:
        match = re.search(r"^model:\s*(.+)$", f.read_text(), re.MULTILINE)
        assert match, f"{f} has no model: line"
        assert re.fullmatch(r"claude-[a-z0-9-]+", match.group(1).strip()), (
            f"{f}: {match.group(1)!r}"
        )


def _env_example() -> dict[str, str]:
    pairs = (
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    return {k.strip(): v.strip() for k, v in pairs}


def test_env_example_urls_match_testbed_nodeports() -> None:
    env = _env_example()
    assert env["PROMETHEUS_URL"] == "http://localhost:30090"
    assert env["LOKI_URL"] == "http://localhost:30100"
    assert env["JAEGER_URL"] == "http://localhost:30686"
    assert env["ALERTMANAGER_URL"] == "http://localhost:30093"


def test_env_example_documents_the_collector_and_api_keys() -> None:
    env = _env_example()
    for key in (
        "INFRAMIND_ALERT_WEBHOOK_SECRET",
        "INFRAMIND_API_PORT",
        "PROM_INTERVAL_S",
        "LOKI_INTERVAL_S",
        "JAEGER_INTERVAL_S",
    ):
        assert key in env, key
    assert env["INFRAMIND_ALERT_WEBHOOK_SECRET"] == ""


def test_pyproject_homepage_matches_git_origin_name() -> None:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert meta["project"]["urls"]["Homepage"].endswith("/inframind-core")
