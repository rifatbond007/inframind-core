# Contributing

## Branching

Use one of these prefixes:

| Prefix      | Purpose                          | Example                       |
|-------------|----------------------------------|-------------------------------|
| `feat/`     | new functionality                | `feat/rca-pagerank-variant`   |
| `fix/`      | bug fix                          | `fix/dedup-window-off-by-one` |
| `docs/`     | docs only (no code change)       | `docs/architecture-diagram`   |
| `refactor/` | no behavior change               | `refactor/detector-factory`   |
| `eval/`     | evaluation harness / scenario    | `eval/scenario-pod-kill`      |
| `paper/`    | paper / LaTeX only               | `paper/related-work-section`  |
| `chore/`    | tooling, CI, deps                | `chore/bump-ruff`             |

Keep branches short-lived (< 5 days, ideally). Delete them after merge.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/) format:

```
<type>(<scope>): <short summary>

<body (optional, wrap at 72 cols)>

<footer (optional)>
```

Examples:

- `feat(rca): add personalized PageRank scoring`
- `fix(detection): freeze baseline while incident is open`
- `docs(progress): log Step 0 completion`

No direct commits to `main` — pre-commit hook will block them. If you somehow
see a direct commit, reset it and re-do as a PR.

## Pull requests

1. Branch from `main`.
2. Push and open a PR. The `ci` workflow runs `lint` and `test` jobs.
3. Auto-requested reviewers come from `.github/CODEOWNERS` — at least one approval
   from the relevant owner is required.
4. Address review comments in new commits (don't force-push during review).
5. Squash-merge once CI is green and approvals are in. Linear history is enforced.

## Local pre-flight

Before pushing, run:

```bash
pre-commit run --all-files
make lint
make test
```

The CI workflow runs the same checks.

## Coding standards

- Python 3.11+. Type hints on public functions. Docstrings on public functions.
- No new runtime dependency without a PR description that explains why.
- No `print()` for logging — use a structured logger (added in Step 2).
- Module-scope: every subpackage has a `README.md` describing what's in it and who owns it.

## What never goes in a commit

- Secrets, `.env`, kubeconfigs.
- Datasets larger than 5 MB.
- `__pycache__/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `.coverage*`.
- Build artifacts under `paper/build/`.

The `.gitignore` blocks the obvious cases, but you are the last line of defense.

## Working on RCA / evaluation

Per `CLAUDE.md` hard rules: anything that touches RCA, detection, or evaluation
**must be reproducible from a scenario id + seed**. Add the scenario under
`evaluation/scenarios/` and document the seed.
