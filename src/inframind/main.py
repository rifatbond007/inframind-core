"""InfraMind composition root: the RCA process with its post-RCA chain wired in (D28).

``python -m inframind.main`` is what the deployed ``rca`` process runs (docker compose and
the Helm chart). It is the RCA worker of `inframind.rca.worker` plus an `IncidentPipeline`
hook that, for every ranked incident revision:

1. **stores it** in Postgres (`storage.write_incident`: the incident, its ranked
   candidates and their evidence — D20, Postgres is the source of truth);
2. when an alert is due (`AlertRouter.should_dispatch`: the first time, on severity
   escalation, or after the dedup window), **explains it** with the LLM
   (`llm.explain`, an OpenAI-compatible API such as Groq; D1 — the LLM only summarises
   the ranking it is given) and stores the summary plus an audit row of the call;
3. **alerts** through the router's sinks and stores each alert and its sink outcome.

Why a root module and not a package: the module DAG (standards 01 §1.3) lets no package
import `rca`, `llm`, `alerting` and `storage` together, yet standard 06b.4 puts the RCA
worker, the explainer and the alert router in one process with the caller persisting the
outcome. This module is the single, test-pinned place where that composition happens
(`tests/unit/test_module_dag.py`); the packages themselves keep their DAG edges.

The hand-off stays in-process with no new Redis stream key (06b.4). A crash between ranking
and storing is recovered by the bus: the incident entry is not ACKed, so it is redelivered,
re-ranked deterministically and stored again.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

import asyncpg

from inframind.alerting import AlertRouter, load_alerting_params
from inframind.common.incident import Incident
from inframind.common.logging import configure_logging, get_logger
from inframind.llm import LLMParams, LLMSummary, explain, load_llm_params
from inframind.llm.validator import template_summary
from inframind.metrics import start_http_server as metrics_start_http_server
from inframind.rca.evidence import Candidate
from inframind.rca.worker import IncidentAnalysis, run_worker, stop_on_signals
from inframind.storage import (
    build_params,
    close_pool,
    create_pool,
    init_schema,
    record_alerts,
    record_summary,
    write_incident,
)

from . import __version__

log = get_logger(__name__)

_CLOSED_STATES = ("resolved", "expired")

#: `llm.explain`'s signature, injectable so tests never reach a provider.
ExplainFn = Callable[..., tuple[LLMSummary, Any]]


class IncidentPipeline:
    """The `on_analysis` hook: store, explain, alert, store again (module docstring)."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        router: AlertRouter,
        *,
        llm_params: LLMParams,
        scenario_id: str | None = None,
        explain_fn: ExplainFn = explain,
    ) -> None:
        self._pool = pool
        self._router = router
        self._llm_params = llm_params
        self._scenario_id = scenario_id
        self._explain = explain_fn

    async def __call__(self, incident: Incident, analysis: IncidentAnalysis) -> None:
        """Handle one ranked incident revision. Storage errors propagate (bus redelivery)."""
        candidates = analysis.candidates
        await write_incident(self._pool, incident, candidates, scenario_id=self._scenario_id)

        if incident.state in _CLOSED_STATES:
            self._router.forget(incident.id)
            log.info("pipeline_incident_closed", incident_id=incident.id, state=incident.state)
            return
        if not self._router.should_dispatch(incident):
            return

        summary_text = await self._summarise(incident, candidates)
        result = await self._router.dispatch(incident, candidates, summary_text=summary_text)
        if result.alerted:
            await record_alerts(
                self._pool,
                incident.id,
                severity=str(incident.severity),
                payload=result.payload,
                outcomes=result.outcomes,
            )
        log.info(
            "pipeline_incident_alerted",
            incident_id=incident.id,
            revision=incident.revision,
            primary=candidates[0].service if candidates else None,
            summarised=summary_text is not None,
            sinks=[channel for _, channel, _ in result.outcomes],
        )

    async def _summarise(self, incident: Incident, candidates: Sequence[Candidate]) -> str | None:
        """Explain the ranking and store the summary; never blocks the alert (D1).

        No candidates means nothing to explain (`explain` requires a ranking). Any explainer
        failure degrades to the deterministic template summary, which is still stored and
        audited, so the alert always carries an explanation when there is a ranking.
        """
        if not candidates:
            return None
        provenance: dict[str, Any] = {
            "provider": self._llm_params.provider,
            "model": self._llm_params.openai_model,
            "base_url": self._llm_params.openai_base_url,
        }
        try:
            summary, result = await asyncio.to_thread(
                self._explain,
                incident,
                candidates,
                scenario_id=self._scenario_id or incident.id,
                params=self._llm_params,
            )
            if result is None:
                provenance.update(status="cache_hit", raw="")
            else:
                provenance.update(status=result.status, raw=result.raw)
        except Exception as e:  # the LLM is presentation-only: never lose the alert over it
            log.warning("pipeline_llm_failed", incident_id=incident.id, error=str(e))
            summary = template_summary(candidates)
            provenance.update(status="template_fallback", raw="", error=str(e)[:500])

        await record_summary(
            self._pool,
            incident.id,
            summary_text=summary.summary_text,
            provenance=provenance,
        )
        log.info(
            "pipeline_incident_summarised",
            incident_id=incident.id,
            status=provenance["status"],
            model=provenance["model"],
        )
        return summary.summary_text


async def amain() -> None:
    """Run the RCA process with the full post-RCA chain until SIGINT/SIGTERM.

    Reads REDIS_URL, POSTGRES_URL, RCA_GRAPH_PATH, METRICS_PORT and the INFRAMIND_RCA_*,
    INFRAMIND_ALERTING_*, INFRAMIND_LLM_* / LLM_* settings. Postgres must be reachable at
    start; if it is not, the process exits non-zero and the supervisor restarts it.
    """
    configure_logging()
    metrics_start_http_server(int(os.environ.get("METRICS_PORT", "9100")))
    stop = stop_on_signals()

    storage_params, dsn, scenario_id = build_params()
    pool = await create_pool(storage_params, dsn)
    try:
        await init_schema(pool)
        llm_params = load_llm_params()
        pipeline = IncidentPipeline(
            pool,
            AlertRouter(load_alerting_params()),
            llm_params=llm_params,
            scenario_id=scenario_id,
        )
        log.info(
            "pipeline_started",
            version=__version__,
            llm_provider=llm_params.provider,
            llm_model=llm_params.openai_model,
            llm_base_url=llm_params.openai_base_url,
            ollama_fallback=llm_params.ollama_fallback,
        )
        await run_worker(stop, on_analysis=pipeline)
    finally:
        await close_pool(pool)


def main(argv: Sequence[str] | None = None) -> int:
    """Console entrypoint. ``--version`` prints the version and exits without connecting."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        print(f"inframind {__version__}")
        return 0
    asyncio.run(amain())
    return 0


if __name__ == "__main__":
    sys.exit(main())
