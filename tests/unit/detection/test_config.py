"""Detector parameters, per-metric profiles and environment overrides."""

from __future__ import annotations

import pytest

from inframind.detection.config import (
    ErrorSpikeParams,
    EwmaParams,
    LatencyParams,
    MetricProfile,
    ZScoreParams,
    cooldown_s,
    freeze_window_s,
    load_params,
    profile_for,
)


def test_known_metric_profiles() -> None:
    assert profile_for("cpu_cores") == MetricProfile(0.005, 0.10, direction="up")
    assert profile_for("memory_working_set_bytes") == MetricProfile(8 * 2**20, 0.05, direction="up")
    assert profile_for("container_restarts_total") == MetricProfile(
        0.25, 0.0, counter=True, direction="up"
    )


def test_unknown_metric_gets_a_relative_floor_only_and_both_directions() -> None:
    assert profile_for("something_new") == MetricProfile(0.0, 0.10, direction="both")


def test_defaults_match_the_design() -> None:
    assert load_params(ZScoreParams, "zscore", {}) == ZScoreParams(60, 3.0, 10)
    assert load_params(EwmaParams, "ewma", {}) == EwmaParams(0.3, 3.0, 10)
    assert load_params(ErrorSpikeParams, "error_spike", {}) == ErrorSpikeParams(300, 3, 3.0, 12)
    assert load_params(LatencyParams, "latency_regression", {}) == LatencyParams(
        60, 50.0, 10.0, 10, 2, 10
    )


def test_environment_overrides_one_field_and_keeps_the_rest() -> None:
    params = load_params(ZScoreParams, "zscore", {"INFRAMIND_DETECTION_ZSCORE_THRESHOLD": "4.5"})
    assert params == ZScoreParams(window=60, threshold=4.5, min_samples=10)


def test_overrides_are_cast_to_the_field_type() -> None:
    params = load_params(
        LatencyParams,
        "latency_regression",
        {"INFRAMIND_DETECTION_LATENCY_REGRESSION_CONSECUTIVE": "3"},
    )
    assert params.consecutive == 3
    assert isinstance(params.consecutive, int)


def test_a_bad_value_names_the_variable() -> None:
    with pytest.raises(ValueError, match="INFRAMIND_DETECTION_ZSCORE_WINDOW"):
        load_params(ZScoreParams, "zscore", {"INFRAMIND_DETECTION_ZSCORE_WINDOW": "abc"})


def test_unrelated_variables_are_ignored() -> None:
    assert (
        load_params(ZScoreParams, "zscore", {"INFRAMIND_DETECTION_EWMA_ALPHA": "0.9"})
        == ZScoreParams()
    )


def test_the_real_environment_is_used_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFRAMIND_DETECTION_ZSCORE_WINDOW", "30")
    assert load_params(ZScoreParams, "zscore").window == 30


def test_freeze_window_and_cooldown_defaults_and_overrides() -> None:
    assert freeze_window_s({}) == 300.0
    assert freeze_window_s({"BASELINE_FREEZE_WINDOW": "60"}) == 60.0
    assert cooldown_s({}) == 120.0
    assert cooldown_s({"INFRAMIND_CORRELATION_COOLDOWN_S": "45"}) == 45.0
    with pytest.raises(ValueError, match="BASELINE_FREEZE_WINDOW"):
        freeze_window_s({"BASELINE_FREEZE_WINDOW": "soon"})
