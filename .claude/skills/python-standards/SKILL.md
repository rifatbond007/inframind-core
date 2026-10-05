---
name: python-standards
description: Python coding standards for InfraMind — type hints, docstrings, logging, error handling, dependencies.
---

# Python standards

## Type hints

- Required on every public function.
- Use modern union syntax (`int | None`, not `Optional[int]`).
- `from __future__ import annotations` at the top of every module.

## Docstrings

- Google-style docstrings on every public function.
- First line is a one-sentence summary.
- Args, Returns, Raises sections where applicable.

## Logging

- No `print()`. Use a structured JSON logger (`inframind.common.logging`).
- INFO for state changes. DEBUG for per-signal detail. WARNING for retries. ERROR for failures.

## Error handling

- Raise domain-specific exceptions (e.g. `IngestionError`, `RcaError`).
- Never `except Exception` without re-raising.
- Top-level service loops catch and log; they do not crash.

## Dependencies

- Add a dependency only when it lands in code. Update `pyproject.toml` in the same patch.
- Pin lower bounds. No upper bounds unless there is a real reason.
- Heavy deps (torch, tensorflow) **forbidden** (D3).

## Formatting

- `ruff format` and `ruff check` (run via `make format` / `make lint`).
- Line length 100.
- Double-quoted strings.

## Imports

- Sorted by ruff (`I` rule).
- Relative imports inside the package (`from .common import Signal`).

## What never goes in code

- `print()` for logging.
- `assert` for input validation (use Pydantic).
- `# noqa` without a reason in the comment.
- Bare `except:`.