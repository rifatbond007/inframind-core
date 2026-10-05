# 2. Python standards — type hints, docstrings, logging, errors
> standards · Python conventions · InfraMind. Pairs with the `python-standards` skill in `.claude/skills/python-standards/SKILL.md` (the procedure). This file is the rationale and the rules on one screen.

## 2.1 Every rule on one screen

| Area | Rule |
|---|---|
| Version | Python 3.11+. Pinned in `pyproject.toml`. CI runs on 3.11. |
| Future import | `from __future__ import annotations` at the top of every module. |
| Type hints | Required on every public function (parameters + return). |
| Union syntax | Modern: `int \| None`. Never `Optional[int]`. |
| Docstrings | Google-style. First line is a one-sentence summary. Args / Returns / Raises where applicable. |
| Logging | Structured JSON via `inframind.common.logging.get_logger(__name__)`. No `print()`. |
| Error handling | Raise a domain exception. Top-level service loops catch and log; they do not crash. |
| Dependencies | Added when code lands. Pinned lower bound. No upper bound without a real reason. |
| Formatting | `ruff format` + `ruff check` (run via `make format` / `make lint`). |
| Line length | 100. |
| Strings | Double-quoted. |
| Imports | Sorted by ruff (`I` rule). Relative imports inside the package. |
| Forbidden | `print()` for logging, `assert` for validation, bare `except`, swallowed exceptions, `# noqa` without a reason. |

## 2.2 Why modern union syntax

`int | None` is the form the language ships in 3.10+. The `Optional[int]` form is a typing-only
notion and has to be imported. The new form is also forward-compatible with PEP 604 generics.
There is no upside to keeping the old form.

## 2.3 Why `from __future__ import annotations`

Postponed evaluation means the type hint does not run at module import. That matters when a hint
refers to a class defined later in the same module, or to a name that would otherwise force an
import order. The cost is zero.

## 2.4 Why Google-style docstrings

- Consistent section names (`Args`, `Returns`, `Raises`).
- Renders cleanly in Sphinx and in IDE tooltips.
- The `mkdocstrings` config in `mkdocs.yml` is set to Google style. No reformatting needed.

A docstring is owed only where the name and signature do not already say it. A one-liner above
a `def` is a soft requirement; a full Args/Returns/Raises block is owed for any public function
that ships.

## 2.5 Why structured logging

- **Searchable.** JSON in Loki / Elasticsearch beats a wall of free text.
- **Structured for the agent team.** `docs-writer` reads log lines; a JSON `request_id` field
  makes correlation possible.
- **Honest level discipline.** `INFO` is for state changes. `DEBUG` is for per-signal detail.
  `WARNING` is for retries. `ERROR` is for failures.

The level guide:

| Level | Use when |
|---|---|
| `DEBUG` | Per-signal detail, e.g. `signal_received` for every `Signal` on the bus. Off by default. |
| `INFO` | State change, e.g. `incident_opened`, `collector_started`, `alert_sent`. |
| `WARNING` | Retry, e.g. `prometheus_query_timeout_retry`, `ollama_unavailable_fallback`. |
| `ERROR` | Failure, e.g. `postgres_unavailable`, `chart_lint_failed`. |
| `CRITICAL` | The process is about to exit. The on-call gets paged. |

## 2.6 Why no `assert`

`assert` is stripped under `python -O`. If you use it for input validation, the validation
disappears in optimised mode. Pydantic validators are the right tool for input shape; explicit
`if ... raise ValueError(...)` is the right tool for run-time guards.

## 2.7 Why top-level service loops catch and log

A long-lived worker in K8s should not crash. A crash restarts the pod; if the cause is a single
malformed payload, the restart loop continues until liveness probe fails. The discipline is:
**catch at the boundary, log with context, continue**. The crash-restart path is reserved for
configuration errors that cannot recover.

```python
# Good
async def run(self) -> None:
    while True:
        try:
            signal = await self.collect_one()
        except IngestionError as e:
            self.log.warning("ingestion_failed", service=self.service, error=str(e))
            continue
        await self._publish(signal)
```

```python
# Bad
async def run(self) -> None:
    while True:
        signal = await self.collect_one()  # unhandled -> pod crash -> restart loop
        await self._publish(signal)
```

## 2.8 Why no DL dependencies

D3. No PyTorch, no TensorFlow, no JAX. The detector layer is statistical (Z-score, EWMA,
percentile, error-rate spike). The LLM is a remote API call; the only local-fallback is Ollama.
Heavy deps bloat the image and have no role in the project.

## 2.9 What goes in `pyproject.toml`

- `dependencies` — what the runtime needs.
- `[project.optional-dependencies]` — `dev` (ruff, mypy, pytest, pre-commit, ipython),
  `k8s` (helm, kubernetes), `eval` (pandas, matplotlib, scipy), `docs` (mkdocstrings, ...).
- `requires-python` — `>=3.11`. No upper bound.
- `[tool.ruff]` — line-length 100, target-version `py311`, select `E F W I N UP B SIM RUF`.
- `[tool.mypy]` — strict, ignore-missing-imports (third-party stubs are missing for some libs).
- `[tool.pytest.ini_options]` — `testpaths = ["tests/unit"]` for CI, with markers for
  `integration` and `e2e`.

## 2.10 The lint contract

A pre-commit hook runs `ruff format --check` and `ruff check` on every staged file. CI runs the
same plus `mypy src/`. A red lint blocks merge.

`make lint` runs the same locally. Before every commit, run `make lint` and `make test`. The
five gates are listed in `phase-gates`.

## 2.11 Hard rules

- No `print()`. No `assert`. No bare `except:`.
- No silent failure. Every `except` either re-raises, returns a domain exception, or logs with
  context. Catch-and-ignore is rejected.
- No `Optional[X]` — use `X | None`.
- No mutable default arguments. Use `field(default_factory=list)` in Pydantic; use `None` as the
  default and build inside the function for plain Python.
- No global state. Module-level constants are fine; module-level mutable state is not.

## 2.12 Cross-reference

- **Skill:** `.claude/skills/python-standards/SKILL.md` — the procedural how-to.
- **Locked decisions:** `docs/decision-tree.md` — D3 (no DL), D-Setup (no functional code in
  Step 0).
- **Linting / pre-commit:** `08-commit-protocol.md` in this set.
- **Test layout:** `tests/` mirrors `src/inframind/`. CI runs `tests/unit/`.
