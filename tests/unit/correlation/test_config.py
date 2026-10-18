"""Correlation parameters and their INFRAMIND_CORRELATION_* overrides."""

from __future__ import annotations

import pytest

from inframind.correlation.config import CorrelationParams, load_correlation_params


def test_defaults_match_the_design() -> None:
    assert load_correlation_params({}) == CorrelationParams(
        window_s=300.0,
        cooldown_s=120.0,
        max_age_s=3600.0,
        update_interval_s=30.0,
        critical_services=3,
        critical_anomalies=20,
        enabled=True,
    )


def test_environment_overrides_one_field_and_keeps_the_rest() -> None:
    params = load_correlation_params({"INFRAMIND_CORRELATION_WINDOW_S": "60"})
    assert params.window_s == 60.0 and params.cooldown_s == 120.0


def test_the_existing_cooldown_key_is_honoured() -> None:
    assert load_correlation_params({"INFRAMIND_CORRELATION_COOLDOWN_S": "45"}).cooldown_s == 45.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("false", False), ("FALSE", False), ("0", False), ("true", True), ("True", True), ("1", True)],
)
def test_enabled_parses_booleans(raw: str, expected: bool) -> None:
    assert load_correlation_params({"INFRAMIND_CORRELATION_ENABLED": raw}).enabled is expected


def test_a_bad_boolean_or_number_names_the_variable() -> None:
    with pytest.raises(ValueError, match="INFRAMIND_CORRELATION_ENABLED"):
        load_correlation_params({"INFRAMIND_CORRELATION_ENABLED": "maybe"})
    with pytest.raises(ValueError, match="INFRAMIND_CORRELATION_WINDOW_S"):
        load_correlation_params({"INFRAMIND_CORRELATION_WINDOW_S": "soon"})
    with pytest.raises(ValueError, match="INFRAMIND_CORRELATION_CRITICAL_SERVICES"):
        load_correlation_params({"INFRAMIND_CORRELATION_CRITICAL_SERVICES": "2.5"})


def test_the_real_environment_is_used_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFRAMIND_CORRELATION_MAX_AGE_S", "900")
    assert load_correlation_params().max_age_s == 900.0
