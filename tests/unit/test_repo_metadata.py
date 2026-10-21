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


def test_env_example_documents_the_storage_and_alerting_keys() -> None:
    """Every P6 env key the config loaders read is present in the template.

    `load_storage_params` and `load_alerting_params` both go through `load_prefixed`, so a
    missing key silently falls back to the dataclass default — this test is what stops a
    typo in a prefix from going unnoticed until someone reads the README.
    """
    env = _env_example()
    for key in (
        "POSTGRES_URL",
        "INFRAMIND_STORAGE_POOL_MIN_SIZE",
        "INFRAMIND_STORAGE_POOL_MAX_SIZE",
        "INFRAMIND_STORAGE_COMMAND_TIMEOUT_S",
        "INFRAMIND_STORAGE_MAX_PAGE_SIZE",
        "INFRAMIND_STORAGE_DEFAULT_PAGE_SIZE",
        "INFRAMIND_EVAL_SCENARIO_ID",
        "INFRAMIND_ALERTING_DEDUP_WINDOW_S",
        "INFRAMIND_ALERTING_CONSOLE_ENABLED",
        "INFRAMIND_ALERTING_WEBHOOK_URL",
        "INFRAMIND_ALERTING_WEBHOOK_TIMEOUT_S",
    ):
        assert key in env, key
    # Secrets stay empty in the template.
    assert env["INFRAMIND_ALERTING_WEBHOOK_URL"] == ""
    assert env["INFRAMIND_EVAL_SCENARIO_ID"] == ""


def test_pyproject_homepage_matches_git_origin_name() -> None:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert meta["project"]["urls"]["Homepage"].endswith("/inframind-core")


DOC_FILES = [
    *sorted((ROOT / "docs" / "standards").glob("*.md")),
    ROOT / "docs" / "surface-map.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "CLAUDE.md",
    *sorted((ROOT / ".claude").rglob("*.md")),
    *sorted((ROOT / "src").rglob("README.md")),
    *sorted((ROOT / "src").rglob("__init__.py")),
]
REMOVED_NAMES = [
    r"SIGNALS_STREAM",
    r"get_redis",
    r"redis_client",
    r"inframind\.signals",
    r"alertmanager_webhook\.py",
    r"api\.webhooks",
    r"from dateutil",
    r"(?<!webhooks)(?<!api)/alertmanager\b(?!\.py)",
    r"or 1e-9",
    r"detection\.severity",
    r"severity_from_zscore",
    r"window=300s",
]


def test_docs_do_not_reference_removed_names() -> None:
    hits = []
    for path in DOC_FILES:
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for pattern in REMOVED_NAMES:
                if re.search(pattern, line):
                    hits.append(f"{path.relative_to(ROOT)}:{lineno}: /{pattern}/")
    assert not hits, "stale references:\n" + "\n".join(hits)


def test_ingestion_agent_lists_only_existing_collectors() -> None:
    agent = (ROOT / ".claude" / "agents" / "build" / "ingestion-engineer.md").read_text()
    table = [line for line in agent.splitlines() if line.startswith("| `") and ".py`" in line]
    assert table
    for line in table:
        name = re.search(r"`([\w/]+\.py)`", line)
        assert name
        candidates = [
            ROOT / "src" / "inframind" / "ingestion" / name.group(1),
            ROOT / "src" / "inframind" / "api" / name.group(1),
        ]
        assert any(c.exists() for c in candidates), f"{name.group(1)} does not exist"


def test_stale_route_pattern_catches_the_old_forms_and_spares_the_new_ones() -> None:
    pattern = next(p for p in REMOVED_NAMES if "(?!" in p)
    stale = ["`/alertmanager` (FastAPI)", 'webhookUrl: "http://api:8080/alertmanager"']
    fine = [
        "POST /webhooks/alertmanager",
        "src/inframind/api/alertmanager.py",
        "api/alertmanager.py",
    ]
    assert all(re.search(pattern, line) for line in stale)
    assert not any(re.search(pattern, line) for line in fine)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_standard_05_matches_the_detector_api() -> None:
    text = _read("docs/standards/05-add-detector.md")
    for needle in (
        "freeze(service, ts)",
        "BASELINE_FREEZE_WINDOW",
        "INFRAMIND_DETECTION_",
        "common/anomaly.py",
        "keeps scoring",
        "make smoke-detection",
    ):
        assert needle in text, f"standard 05 should mention {needle!r}"
    assert "return None  # D4" not in text


def test_detection_agent_matches_the_code() -> None:
    from inframind.detection.config import (
        ErrorSpikeParams,
        EwmaParams,
        LatencyParams,
        ZScoreParams,
    )

    agent = _read(".claude/agents/build/detection-engineer.md")
    rows = [line for line in agent.splitlines() if line.startswith("| `") and ".py`" in line]
    assert len(rows) == 4
    for row in rows:
        name = re.search(r"`(\w+\.py)`", row)
        assert name and (ROOT / "src" / "inframind" / "detection" / name.group(1)).exists()
    z, e, es, lat = ZScoreParams(), EwmaParams(), ErrorSpikeParams(), LatencyParams()
    for expected in (
        f"window={z.window}",
        f"threshold={z.threshold:g}",
        f"alpha={e.alpha:g}",
        f"sigma_k={e.sigma_k:g}",
        f"min_count={es.min_count}",
        f"regression_pct={lat.regression_pct:g}",
        f"consecutive={lat.consecutive}",
    ):
        assert expected in agent, f"the detection agent should state {expected}"


def test_correlation_agent_uses_the_surface_map_vocabulary() -> None:
    agent = _read(".claude/agents/build/correlation-engineer.md")
    assert "revision" in agent and "tracker.py" in agent and "worker.py" in agent
    assert "open → updating → resolved" not in agent


def test_surface_map_documents_the_new_wire_models() -> None:
    text = _read("docs/surface-map.md")
    assert "class Anomaly" in text and "revision" in text
    assert "INFRAMIND_DETECTION_" in text and "INFRAMIND_CORRELATION_COOLDOWN_S" in text


def test_common_modules_are_all_listed_in_the_structure_standard() -> None:
    listed = _read("docs/standards/01-project-structure.md")
    for module in sorted((ROOT / "src" / "inframind" / "common").glob("*.py")):
        if module.name != "__init__.py":
            assert f"`{module.name}`" in listed, f"standard 01 §1.4 should list {module.name}"
