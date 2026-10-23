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


def test_env_example_documents_the_p7_evaluation_keys() -> None:
    """Every `INFRAMIND_EVAL_*` key `EvalParams` reads is present in the template.

    `load_eval_params` goes through `load_prefixed`, so a missing key silently falls back
    to the dataclass default. This test is what stops a typo in the prefix from going
    unnoticed until someone re-runs the matrix.
    """
    env = _env_example()
    for key in (
        "INFRAMIND_EVAL_SCENARIO_SET",
        "INFRAMIND_EVAL_REPEATS",
        "INFRAMIND_EVAL_FIXTURE_DIR",
        "INFRAMIND_EVAL_RESULTS_DIR",
        "INFRAMIND_EVAL_DETECT_WINDOW_S",
        "INFRAMIND_EVAL_ALLOW_BLOCKED",
        "INFRAMIND_EVAL_ALLOW_OVERWRITE",
        "INFRAMIND_EVAL_SEED",
    ):
        assert key in env, key
    # The two escape hatches and the seed override stay off/empty in the template: an eval
    # run must be a deliberate act, and the seed comes from the scenario YAML by default.
    assert env["INFRAMIND_EVAL_ALLOW_BLOCKED"] == "false"
    assert env["INFRAMIND_EVAL_ALLOW_OVERWRITE"] == "false"
    assert env["INFRAMIND_EVAL_SEED"] == ""


def test_eval_config_fields_are_all_documented_in_standard_10() -> None:
    """Standard 10 §10.7 lists every `INFRAMIND_EVAL_*` key the loader reads.

    PUKU.md requires `.env.example` and the standards to agree; this is the check that
    keeps the eval table from drifting away from the loader. `seed_override` is spelled
    `INFRAMIND_EVAL_SEED` on the wire — `load_eval_params` maps it explicitly — so the
    key list is written out rather than derived from the field names.
    """
    from inframind.evaluation.config import EvalParams, load_eval_params

    standard = _read("docs/standards/10-evaluation.md")
    # Every env-reachable key, minus the two that are handled outside `load_prefixed`
    # (`SEED` is merged explicitly; `SCENARIO_ID` is stamped by the live runner, not read
    # as a param) and minus `extra`, which is the catch-all for unrecognised keys.
    explicit = {"INFRAMIND_EVAL_SEED"}
    for key in explicit:
        assert f"`{key}`" in standard, f"standard 10 should document {key}"

    # Every other key the loader forwards must appear under its own name in the standard.
    for key in (
        "INFRAMIND_EVAL_SCENARIO_SET",
        "INFRAMIND_EVAL_REPEATS",
        "INFRAMIND_EVAL_FIXTURE_DIR",
        "INFRAMIND_EVAL_RESULTS_DIR",
        "INFRAMIND_EVAL_DETECT_WINDOW_S",
        "INFRAMIND_EVAL_ALLOW_BLOCKED",
        "INFRAMIND_EVAL_ALLOW_OVERWRITE",
    ):
        assert f"`{key}`" in standard, f"standard 10 should document {key}"

    # And the params loader must actually read them (a key in the standard that the
    # loader ignores is the same silent-default bug, one layer over).
    params = load_eval_params(
        {
            "INFRAMIND_EVAL_REPEATS": "7",
            "INFRAMIND_EVAL_SEED": "4242",
            "INFRAMIND_EVAL_ALLOW_BLOCKED": "true",
        }
    )
    assert params.repeats == 7
    assert params.seed_override == 4242
    assert params.allow_blocked is True
    assert EvalParams().repeats == 5  # the documented D6 default


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


# --- P8: chart + image metadata (standard 11) ---


def _chart_values() -> dict[str, object]:
    import yaml

    text = (ROOT / "deploy" / "helm" / "inframind" / "values.yaml").read_text()
    return yaml.safe_load(text)


def test_helm_chart_values_repository_matches_pyproject() -> None:
    """The chart's default image must point at this repo, not a drifted sibling."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    homepage = pyproject["project"]["urls"]["Homepage"]
    # https://github.com/<owner>/<repo> -> ghcr.io/<owner>/<repo>
    owner_repo = homepage.removeprefix("https://github.com/")
    assert _chart_values()["image"]["repository"] == f"ghcr.io/{owner_repo}", (
        "values.yaml image.repository drifted from pyproject.toml's Homepage"
    )


def test_helm_chart_ships_no_real_secret_in_secret_yaml() -> None:
    """`secret.yaml` renders an empty stringData. A real key here rejects the PR."""
    secret_tpl = (ROOT / "deploy/helm/inframind/templates/secret.yaml").read_text()
    values = _chart_values()
    assert values["secretRef"]["LLM_API_KEY"] == ""
    assert values["secretRef"]["INFRAMIND_ALERT_WEBHOOK_SECRET"] == ""
    # The template must not carry a literal non-empty secret on either branch.
    assert 'llm-api-key: ""' in secret_tpl
    assert "OPENAI_API_KEY" not in secret_tpl or "quote" in secret_tpl


def test_helm_chart_ships_four_deployments_per_d27() -> None:
    """D27: alerting and llm stay in-process, so the chart has 4 Deployments, not 7."""
    templates = ROOT / "deploy" / "helm" / "inframind" / "templates"
    deployments = sorted(t.name for t in templates.glob("deployment-*.yaml"))
    assert deployments == [
        "deployment-api.yaml",
        "deployment-detection.yaml",
        "deployment-ingestion.yaml",
        "deployment-rca.yaml",
    ]


def test_dockerfile_runs_as_non_root() -> None:
    """The Dockerfile must switch to a non-root USER before the CMD."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "\nUSER " in dockerfile, "Dockerfile has no USER directive"
    # The USER line must come after the last FROM (i.e. apply to the runtime stage).
    runtime_start = dockerfile.rindex("FROM python:")
    user_line = dockerfile.index("\nUSER ")
    assert user_line > runtime_start, "USER directive is not in the runtime stage"


def test_dockerignore_excludes_secrets_and_testbed() -> None:
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    entries = {line.strip() for line in ignore if line.strip() and not line.startswith("#")}
    for required in (".git", ".env", ".venv", "testbed/.kube", "data/"):
        assert required in entries, f".dockerignore must exclude {required}"
