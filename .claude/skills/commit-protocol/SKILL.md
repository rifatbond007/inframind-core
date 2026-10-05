---
name: commit-protocol
description: How to commit changes in InfraMind — branch naming, commit message format, pre-commit checks, when to squash.
---

# Commit protocol

## Branch prefixes

| Prefix      | Purpose                          |
|-------------|----------------------------------|
| `feat/`     | new functionality                |
| `fix/`      | bug fix                          |
| `docs/`     | docs only                        |
| `refactor/` | no behavior change               |
| `eval/`     | evaluation harness / scenario    |
| `paper/`    | paper / LaTeX only               |
| `chore/`    | tooling, CI, deps                |

## Commit message format

Conventional Commits:

```
<type>(<scope>): <short summary>

<body (optional, wrap at 72 cols)>

<footer (optional)>
```

Examples:

- `feat(rca): add personalized PageRank scoring`
- `fix(detection): freeze baseline while incident is open`
- `docs(progress): log Step 0 completion`

## Before every commit

1. `pre-commit run --all-files` (auto-fixes most issues).
2. `make lint` (ruff + mypy clean).
3. `make test` (all unit tests pass).
4. `docs/PROGRESS.md` updated.

## What never goes in a commit

- `.env`, `*.pem`, kubeconfigs.
- Datasets > 5 MB.
- `__pycache__/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `.coverage*`.
- LaTeX build output.

## Pre-commit hook blocks

- Direct commit to `main` (`.pre-commit-config.yaml` has `no-commit-to-branch --branch=main`).

## Merging

- Linear history enforced.
- Squash-merge after CI green + approvals in.
- Delete branch after merge.