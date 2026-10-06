# Contributing

## Branching

Only **five** branch names are used. Do not create topic branches (`feat/…`, `fix/…`, etc.).

| Branch | Use |
|--------|-----|
| `main` | Protected; release / milestone snapshots. No direct commits. |
| `dev` | Team integration before `main`. |
| `dev-rifat` | Rifat — testbed, evaluation, packaging. |
| `dev-moneem` | Moneem — ingestion, detection, correlation. |
| `dev-prome` | Prome — RCA, LLM, alerting, storage, API. |

Work on your `dev-*` branch, open PRs to `dev`, then `dev` → `main` when ready. See
`.claude/skills/commit-protocol/SKILL.md` and `docs/standards/08-commit-protocol.md`.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/) format:

```
<type>(<scope>): <short summary>

<body (optional, wrap at 72 cols)>
```

Examples:

- `feat(rca): add personalized PageRank scoring`
- `fix(detection): freeze baseline while incident is open`
- `docs(progress): log Step 0 completion`

The author shown on GitHub is the commit author. Do not add `GitHub-Author:` or
`Co-authored-by:` trailers. No direct commits to `main` — the pre-commit hook blocks them.

## Pull requests

1. Push your `dev-*` (or `dev`) branch and open a PR toward `dev` or `main` as appropriate.
2. The `ci` workflow runs `lint` and `test` jobs.
3. Auto-requested reviewers come from `.github/CODEOWNERS` — at least one approval
   from the relevant owner is required.
4. Address review comments in new commits (don't force-push to `dev`/`main` during review).
5. Squash-merge once CI is green and approvals are in. Linear history is enforced on `main`.

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
`evaluation/scenarios/` when the harness exists (Phase P7).
