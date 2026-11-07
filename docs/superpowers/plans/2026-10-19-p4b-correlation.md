# P4b Correlation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace P4's per-service tracker with real correlation: one incident per underlying fault across services, a stable fingerprint, the full lifecycle, and rate-absorbing publishing.

**Architecture:** Pure helpers (`config`, `fingerprint`, `state_machine`, `window`) feed an upgraded `IncidentTracker` that keeps its interface (`on_anomaly`, `tick`, `open_services`). The worker routes Alertmanager signals to the tracker. `Incident` gains a required `fingerprint`.

**Tech Stack:** Python 3.11+, pydantic v2, pytest + fakeredis, ruff, mypy. **No new dependencies.**

**Spec:** `docs/superpowers/specs/2026-10-19-p4b-correlation-design.md` (its eight rulings are binding). Builds on `docs/superpowers/specs/2026-10-19-p4-detection-design.md`.

## Global Constraints

- Python `>=3.11`; ruff line length 100, rules `E,F,W,I,B,UP,SIM,RUF`; `mypy src` clean; pytest `--strict-markers`, `asyncio_mode=auto`.
- Module DAG (enforced by `tests/unit/test_module_dag.py`): `correlation` may import `common` and `detection` only; never `rca`.
- **D4:** every service in an `open` or `acknowledged` incident stays frozen; resolve and expire release them. **D6:** all times are signal time (`Signal.ts` / `Anomaly.ts`); the wall clock is used only by the worker's idle tick.
- Pinned names: streams `stream:signals`, `stream:incidents`; states `open | acknowledged | resolved | expired`; "updating" is a `revision` bump.
- Branch `dev-rifat`; Conventional Commits; no AI `Co-Authored-By` trailer (D25); never `--no-verify`, force-push, or push to `main`; nothing is pushed unless the user asks.
- Gates after every task: `ruff check src tests testbed`, `mypy src`, `pytest -q -m ""` via the scratch venv `$V`. Clear `__pycache__` after any mutation experiment. If the commit hook reformats files, re-stage and recommit; check `git log` shows a new commit before recording a task.

## Review Focus

1. **A fault that flaps** (resolves, then fires again inside the same window) must keep the same fingerprint — Task 2.
2. **Two unrelated anomalies far apart in time** must not be grouped, and one far from an open incident's last anomaly must open a new one — Task 4.
3. **A long incident**: quiet services stay frozen while the incident is open, and it expires at `max_age_s` instead of freezing baselines forever — Task 5.
4. **An anomaly flood**: 900 anomalies must publish a handful of snapshots, not 900 — Task 5.
5. **Out-of-order anomalies** (an older `ts` after a newer one) must not move the quiet clock backwards or reopen a resolved incident — Task 5.

---

### Task 1: `Incident.fingerprint` and the correlation parameters

**Files:**
- Modify: `src/inframind/common/incident.py`, `src/inframind/detection/config.py` (generalise the env loader), every test that builds an `Incident` (`tests/unit/common/test_incident.py`, `test_bus_incident.py`, `tests/unit/correlation/test_worker.py`, `tests/unit/test_smoke_detection.py`)
- Create: `src/inframind/correlation/config.py`
- Test: `tests/unit/common/test_incident.py`, `tests/unit/correlation/test_config.py`, `tests/unit/detection/test_config.py`

**Interfaces:**
- Produces: `Incident.fingerprint: str` (required, 8 to 64 hex characters, lower-case). In `detection/config.py`: `load_prefixed(cls: type[T], prefix: str, env: Mapping[str, str] | None = None) -> T` reading `<PREFIX>_<FIELD>` (upper case) and casting by the field's type, with `bool` accepting `true/false/1/0` (any case); `load_params` becomes a thin caller (`prefix = f"INFRAMIND_DETECTION_{detector.upper()}"`) and `cooldown_s()` stays for compatibility. In `correlation/config.py`:
  ```python
  @dataclass(frozen=True) class CorrelationParams:
      window_s: float = 300.0; cooldown_s: float = 120.0; max_age_s: float = 3600.0
      update_interval_s: float = 30.0; critical_services: int = 3; critical_anomalies: int = 20
      enabled: bool = True
  def load_correlation_params(env: Mapping[str, str] | None = None) -> CorrelationParams   # INFRAMIND_CORRELATION_<FIELD>
  ```

- [ ] **Step 1: Write the failing tests.** `test_incident.py`: a valid fingerprint (`"a1b2c3d4e5f60718"`) round-trips; empty, 7-character, upper-case and non-hex fingerprints are rejected. `test_config.py` (correlation): defaults equal the values above; `INFRAMIND_CORRELATION_WINDOW_S=60` → `window_s == 60.0`; `INFRAMIND_CORRELATION_COOLDOWN_S=45` → `cooldown_s == 45.0` (the existing documented key); `INFRAMIND_CORRELATION_ENABLED=false` → `enabled is False` and `TRUE`/`1` → `True`; `INFRAMIND_CORRELATION_ENABLED=maybe` raises `ValueError` naming the variable; a bad float names the variable. `test_config.py` (detection): the existing `load_params` cases still pass unchanged.
- [ ] **Step 2: Run** `pytest tests/unit/common/test_incident.py tests/unit/correlation/test_config.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the field (with a validator for lower-case hex of length 8 to 64), `load_prefixed`, the refactor of `load_params`, and `CorrelationParams`. Update the test builders that construct an `Incident` to pass `fingerprint="0123456789abcdef"`.
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(correlation): add the incident fingerprint field and the correlation parameters`.

---

### Task 2: `fingerprint`

**Files:**
- Create: `src/inframind/correlation/fingerprint.py`
- Test: `tests/unit/correlation/test_fingerprint.py`

**Interfaces:**
- Produces: `def fingerprint(first_service: str, opened_at: datetime, window_s: float) -> str` — `sha256(f"{first_service}|{bucket}")` hex, first 16 characters, where `bucket = int(opened_at.timestamp() // window_s) * int(window_s)`; the service is normalised with `normalise_service` first.

- [ ] **Step 1: Write the failing tests:** deterministic (same inputs twice); 16 lower-case hex characters; two openings in the same 300 s bucket for the same service match, **a flapping fault keeps its fingerprint** (Review Focus 1: opened at bucket start + 10 s and + 250 s); a different service, or the next bucket, differs; `"FrontEnd"` equals `"frontend"`; a known value is pinned (`fingerprint("frontend", datetime(2026,10,19,12,0,0,tzinfo=UTC), 300.0)` equals the digest the implementer computes once and writes into the test, so a future change is deliberate).
- [ ] **Step 2: Run** `pytest tests/unit/correlation/test_fingerprint.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `fingerprint`.
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(correlation): add the stable incident fingerprint`.

---

### Task 3: The incident state machine

**Files:**
- Create: `src/inframind/correlation/state_machine.py`
- Test: `tests/unit/correlation/test_state_machine.py`

**Interfaces:**
- Produces:
  ```python
  Event = Literal["acknowledge", "resolve", "expire"]
  class InvalidTransition(InfraMindError): ...        # subclass of common.exceptions.InfraMindError
  def transition(state: IncidentState, event: Event) -> IncidentState
  def is_active(state: IncidentState) -> bool          # open or acknowledged: baselines stay frozen
  def is_terminal(state: IncidentState) -> bool        # resolved or expired
  ```
  Table: `open --acknowledge--> acknowledged`; `open|acknowledged --resolve--> resolved`; `open|acknowledged --expire--> expired`; everything else raises `InvalidTransition` (including `acknowledge` on `acknowledged`, and any event on a terminal state).

- [ ] **Step 1: Write the failing tests:** every legal transition returns the expected state; parametrised illegal ones raise `InvalidTransition` whose message names the state and event; `is_active`/`is_terminal` for all four states; `InvalidTransition` is an `InfraMindError`.
- [ ] **Step 2: Run** `pytest tests/unit/correlation/test_state_machine.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the table as a dict and the helpers.
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(correlation): add the incident state machine`.

---

### Task 4: Window grouping

**Files:**
- Create: `src/inframind/correlation/window.py`
- Test: `tests/unit/correlation/test_window.py`

**Interfaces:**
- Produces:
  ```python
  class Candidate(Protocol):                  # what the tracker's open records expose
      services: frozenset[str]; last_anomaly_ts: datetime
  def choose_incident(candidates: Sequence[T], service: str, ts: datetime, window_s: float) -> T | None   # T bound to Candidate
  ```
  Rules (spec ruling 1): among candidates whose `services` contain `service`, return the one with the latest `last_anomaly_ts`; otherwise among candidates with `abs(ts - last_anomaly_ts) <= window_s` return the one with the latest `last_anomaly_ts`; otherwise `None`. Ties break by the candidate's position in `candidates`.

- [ ] **Step 1: Write the failing tests** with a tiny dataclass candidate: same-service wins over a more recent other-service incident; within-window other service joins; **beyond the window opens nothing to join** (`None`, Review Focus 2); the boundary (exactly `window_s`) joins and `window_s + 1 s` does not; an older `ts` than `last_anomaly_ts` within the window still joins; an empty list gives `None`; two same-service candidates choose the most recently active.
- [ ] **Step 2: Run** `pytest tests/unit/correlation/test_window.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `choose_incident`.
- [ ] **Step 4: Run** the full gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(correlation): add window grouping`.

---

### Task 5: Upgrade `IncidentTracker`

**Files:**
- Modify: `src/inframind/correlation/tracker.py`
- Test: `tests/unit/correlation/test_tracker.py` (extend; keep every P4 test passing unless a ruling in the ledger says why it changes)

**Interfaces:**
- Consumes: `CorrelationParams` (Task 1), `fingerprint` (Task 2), `transition`/`is_active` (Task 3), `choose_incident` (Task 4), `BaselineFreezeCoordinator`.
- Produces: `IncidentTracker(coordinator, params: CorrelationParams | None = None, *, max_anomalies: int = 50, max_per_detector: int = 10, max_signals: int = 200)`; the P4 keyword arguments `cooldown_s`, kept for compatibility, override `params.cooldown_s` when given. Methods:
  ```python
  def on_anomaly(self, anomaly: Anomaly) -> list[Incident]
  def attach_signal(self, signal: Signal) -> None            # Alertmanager corroboration; never opens, never publishes
  def acknowledge(self, incident_id: str, at: datetime) -> list[Incident]   # open -> acknowledged snapshot; unknown id -> []
  def tick(self, now: datetime) -> list[Incident]            # keepalive for active incidents; resolves by cooldown; expires by max_age_s
  @property open_services -> frozenset[str]
  ```
  Behaviour: an anomaly joins `choose_incident(active, service, ts, params.window_s)`; with `params.enabled` false it always opens its own incident. Joining adds the service and anomaly, freezes that service, and renews every other service of the incident. The incident's `services` is the sorted set; `severity` follows ruling 6; `fingerprint` is computed once at open from the opening service and `opened_at`. Publish rules (ruling 5): open; severity increase; a new service joins; acknowledge; resolve; expire; otherwise at most one evidence update per `update_interval_s` of signal time since the last publish. `tick(now)`: for each active incident renew all its services' leases at `now`; resolve when `now - last_anomaly_ts >= cooldown_s`; expire when `now - opened_at >= max_age_s` (checked first); both unfreeze every service.

- [ ] **Step 1: Write the failing tests** (real `BaselineFreezeCoordinator` with a recording detector; anomaly builder as in the P4 tests):
  - `test_six_services_firing_together_become_one_incident` (the motivating case): six anomalies for six services within 30 s → exactly one snapshot published on open plus one per new severity/service change as the rules dictate; the final snapshot has six sorted services, one fingerprint, and all six services frozen.
  - `test_anomalies_outside_the_window_open_a_separate_incident` (Review Focus 2): `frontend` at 0 s, `cartservice` at 301 s → two incidents; at 300 s → one.
  - `test_same_service_always_rejoins_even_beyond_the_window` while the incident is still open.
  - `test_the_fingerprint_is_fixed_at_open_and_survives_joins`; `test_a_flap_inside_the_bucket_reuses_the_fingerprint` (resolve, then re-fire 100 s later in the same bucket → new id, same fingerprint; Review Focus 1).
  - `test_severity_is_critical_for_three_services_or_twenty_anomalies_or_any_critical`.
  - `test_a_flood_publishes_a_handful_of_snapshots` (Review Focus 4): 900 `error_spike` anomalies over 900 s on one service publish at most `1 + 900/30 + 1` snapshots in total including the resolve, and far fewer than 900.
  - `test_quiet_services_stay_frozen_while_the_incident_is_open` (Review Focus 3): `frontend` fires at 0 s and `cartservice` joins at 10 s, then only `frontend` fires every 100 s for 1000 s with `lease_s=300`; `cartservice` is still frozen at 1000 s (keepalive in `on_anomaly`/`tick`).
  - `test_an_incident_expires_at_max_age`: anomalies every 100 s for 4000 s with `max_age_s=3600` → an `expired` snapshot with `closed_at` set at 3600 s, all services unfrozen; the next anomaly opens a new incident.
  - `test_acknowledge_moves_open_to_acknowledged_and_keeps_baselines_frozen`; acknowledging twice or an unknown id returns `[]` without raising; an acknowledged incident still resolves by cooldown.
  - `test_attach_signal_adds_an_alert_id_to_an_open_incident_only`: an Alertmanager signal for a service in an open incident adds its id to `signals` of the next snapshot and publishes nothing; for a service with no incident it is ignored.
  - `test_out_of_order_anomalies_do_not_move_the_quiet_clock_backwards_or_reopen` (Review Focus 5): keep the P4 test and add: an anomaly older than the incident's `opened_at` joins without lowering `opened_at`; a late anomaly for a service whose incident already resolved opens a new incident (it is not appended to the closed one).
  - `test_correlation_disabled_gives_one_incident_per_anomaly` (`enabled=False`, three services in 10 s → three incidents).
  - Keep the P4 tests (`test_first_anomaly_opens...`, renewal, caps, services independent) passing; where a P4 expectation conflicts with a ruling (per-service independence becomes time-window grouping), change the test and write a `Ruling:` line in the ledger.
- [ ] **Step 2: Run** `pytest tests/unit/correlation/test_tracker.py -q`. Expected: FAIL (new tests).
- [ ] **Step 3: Implement** the upgraded tracker using the Task 1–4 helpers; keep `_retain` (per-detector cap).
- [ ] **Step 4: Run** the full gates, then mutation-check three guarantees (drop the keepalive; drop the expiry; join regardless of window) — each must fail a named test; clear `__pycache__` after.
- [ ] **Step 5: Commit** `feat(correlation): group anomalies across services into one incident with a full lifecycle`.

---

### Task 6: Worker wiring

**Files:**
- Modify: `src/inframind/correlation/worker.py`
- Test: `tests/unit/correlation/test_worker.py`

**Interfaces:**
- Consumes: `IncidentTracker.attach_signal`, `load_correlation_params`.
- Produces: `CorrelationWorker.handle` routes `Source.ALERTMANAGER` signals to `tracker.attach_signal` before the usual flow; `amain` builds `IncidentTracker(engine.coordinator, load_correlation_params())`.

- [ ] **Step 1: Write the failing tests:** `test_an_alert_for_an_open_incident_is_attached` (spike opens an incident, an Alertmanager signal for the same service arrives, the resolve snapshot's `signals` contains its id); `test_an_alert_alone_opens_nothing`; `test_one_burst_across_services_publishes_one_incident` (warm six services, spike all six within a minute → the incidents stream holds snapshots of **one** incident id covering six services); keep the P4 worker tests.
- [ ] **Step 2: Run** `pytest tests/unit/correlation/test_worker.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the routing and `amain` change.
- [ ] **Step 4: Run** the full gates, then start the real worker for 4 s against the compose Redis on a throwaway group and stop it with SIGINT (clean exit, no traceback).
- [ ] **Step 5: Commit** `feat(correlation): route alerts to the tracker and load the correlation parameters`.

---

### Task 7: Extend the live gate

**Files:**
- Modify: `testbed/smoke_detection.py`, `tests/unit/test_smoke_detection.py`, `testbed/README.md`

**Interfaces:**
- Produces: `evaluate(...)` gains a fifth check named `grouped`, between `detected` and `frozen`: ok when **one** incident (the target's) covers at least `MIN_GROUPED_SERVICES = 3` services, and no other incident opened after the burst started. Detail names the services or the extra incidents.

- [ ] **Step 1: Write the failing tests:** healthy run passes five checks in order `quiet, detected, grouped, frozen, resolved`; two incidents opening after the burst (one `frontend`, one `cartservice`) fail `grouped` naming `cartservice`; one incident with two services fails `grouped`; the existing tests keep passing (adjust their expected check list).
- [ ] **Step 2: Run** `pytest tests/unit/test_smoke_detection.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the check and update the README table.
- [ ] **Step 4: Run** `make smoke-detection PY=$V` against the live testbed (about 7 minutes). Expected: `5/5 checks passed`, with the diagnostic table showing one incident. If it fails, read the table, fix the cause, and say so in the commit.
- [ ] **Step 5: Commit** `feat(testbed): check in the live gate that one burst becomes one incident`.

---

### Task 8: Align standards, agents, docs; log P4b

**Files:**
- Modify: `docs/standards/05-add-detector.md` (§5.11 becomes the correlation section), `docs/surface-map.md` (Incident wire: `fingerprint`, states, publish policy, `INFRAMIND_CORRELATION_*` keys), `docs/decision-tree.md` (one entry `P4b-correlation-rulings` listing the eight rulings), `.claude/agents/build/correlation-engineer.md`, `src/inframind/correlation/README.md`, `src/inframind/correlation/__init__.py`, `src/inframind/common/README.md`, `CLAUDE.md`, `docs/PROGRESS.md` (P4b row, checklist, phase line), `docs/standards/10-evaluation.md` (the `no-correlation` ablation is `INFRAMIND_CORRELATION_ENABLED=false`)
- Test: extend `tests/unit/test_repo_metadata.py`

- [ ] **Step 1: Write the failing tests:** `test_correlation_agent_matches_the_code` (the agent file names `window.py`, `fingerprint.py`, `state_machine.py`, `config.py`, `tracker.py` and each exists under `src/inframind/correlation/`; the defaults it states, `window_s=300`, `cooldown_s=120`, `max_age_s=3600`, match `CorrelationParams()`; it no longer says `Still yours (P4b)`); `test_surface_map_documents_the_correlation_wire_model` (contains `fingerprint` and `INFRAMIND_CORRELATION_WINDOW_S`); `test_standard_10_names_the_ablation_switch` (contains `INFRAMIND_CORRELATION_ENABLED`).
- [ ] **Step 2: Run** `pytest tests/unit/test_repo_metadata.py -q`. Expected: FAIL listing what is stale.
- [ ] **Step 3: Edit each file** until the tests pass; change only what is wrong.
- [ ] **Step 4: Run every gate:** `pre-commit run --all-files`, `make lint`, `make test`, `make test-all`, `make smoke-testbed` (6/6), `make smoke-ingestion` (5/5), `make smoke-detection` (5/5). Record each result in the PROGRESS row.
- [ ] **Step 5: Commit** `docs(correlation): align standards, agents and surface map with P4b and log the phase`.

---

## Self-review

- **Spec coverage:** goals 1 → Tasks 4, 5; 2 → Tasks 1, 2; 3 → Tasks 3, 5; 4 → Task 5; 5 → Tasks 1, 5; 6 → Tasks 5, 6; rulings 1 to 8 → Tasks 4, 2, 3, 5, 5, 5, 6, 5; verification (grouping and live) → Tasks 5, 7.
- **Type consistency:** `CorrelationParams` (Task 1) is what Tasks 5 and 6 read; `fingerprint(first_service, opened_at, window_s)` (Task 2) is called once at open in Task 5; `Candidate`/`choose_incident` (Task 4) receive the tracker's records, which therefore expose `services` as a `frozenset[str]` and `last_anomaly_ts`; `Incident.fingerprint` (Task 1) is set by Task 5 and shown by Task 7's diagnostics.
- **Ordering:** 1 → 2, 3, 4 (independent) → 5 → 6 → 7 → 8; Task 1 changes `Incident` construction in existing tests, so it goes first.
- **Open after this plan:** persistence of incidents and baselines (P6), the acknowledge API route (P6), graph-aware severity (P5), P7 tuning of window, cooldown and thresholds, and the chaos blockers.
