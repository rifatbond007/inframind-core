# 8. Commit protocol — branches, messages, pre-commit, what not to commit
> standards · Process · InfraMind. Pairs with the `commit-protocol` skill in `.claude/skills/commit-protocol/SKILL.md`. This file is the rationale.

## 8.1 Branch prefixes

| Prefix      | Purpose                          | Example                       |
|-------------|----------------------------------|-------------------------------|
| `feat/`     | new functionality                | `feat/rca-pagerank-variant`   |
| `fix/`      | bug fix                          | `fix/dedup-window-off-by-one` |
| `docs/`     | docs only (no code change)       | `docs/architecture-diagram`   |
| `refactor/` | no behavior change               | `refactor/detector-factory`   |
| `eval/`     | evaluation harness / scenario    | `eval/scenario-pod-kill`      |
| `paper/`    | paper / LaTeX only               | `paper/related-work-section`  |
| `chore/`    | tooling, CI, deps                | `chore/bump-ruff`             |

Keep branches short-lived (< 5 days, ideally). Delete the head branch after merge — GitHub
does this automatically when "Automatically delete head branches" is enabled.

## 8.2 Commit message format

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

<body (optional, wrap at 72 cols)>

<footer (optional)>
```

The summary is imperative, present tense, no period. The body explains **why** — the diff
itself shows the what.

Examples:

- `feat(rca): add personalized PageRank scoring`
- `fix(detection): freeze baseline while incident is open`
- `docs(progress): log Step 0 completion`
- `eval(scenario): add 001-pod-kill-frontend`
- `chore(ruff): bump to 0.6.0`

## 8.3 Before every commit — the local pre-flight

```bash
pre-commit run --all-files
make lint
make test
```

`pre-commit` runs:

1. `ruff format` and `ruff check` (the two-step on touched files).
2. Trailing-whitespace fixer.
3. End-of-file fixer.
4. `check-yaml`, `check-toml`.
5. `no-commit-to-branch --branch=main` (blocks direct commits to `main`).

CI runs the same plus `mypy src/`. A red CI blocks merge.

## 8.4 After every commit — update `docs/PROGRESS.md`

Every work session ends with a row in `PROGRESS.md` — date, who, done, next, blockers. The
row is the smallest possible summary that lets the next contributor pick up without
re-deriving the context. See the `progress` skill / `docs-writer` agent for the row format.

## 8.5 The five gates

Before any claim of "done":

1. `make lint` — ruff + mypy clean.
2. `make test` — unit tests pass.
3. `make e2e` — e2e (when the testbed is up; otherwise skipped).
4. `pre-commit run --all-files` — clean.
5. `docs/PROGRESS.md` updated.

These are the `phase-gates` skill's per-PR version.

## 8.6 What never goes in a commit

| Forbidden | Why |
|---|---|
| `.env`, `*.env`, `*.env.local` | Real secrets. |
| `*.pem`, `*.key`, `*.kubeconfig` | Keys and credentials. |
| `kubeconfig*` | Even copies. `.gitignore` blocks these. |
| Datasets > 5 MB | Bloats the repo. Put them in `data/` and gitignore. |
| `__pycache__/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `.coverage*` | Generated. |
| `paper/build/`, `*.aux`, `*.bbl`, `*.blg`, `*.log`, `*.out`, `*.toc` | LaTeX build output. |
| `evaluation/results/*.csv` | Generated. Only `.gitkeep` is committed. |
| `node_modules/` | JavaScript deps. Not used by InfraMind. |
| Images, models, binaries > 1 MB | Use a release artifact, not git. |

The `.gitignore` blocks the obvious cases; you are the last line of defense.

## 8.7 The pre-commit hook that matters

`no-commit-to-branch --branch=main` is the most important pre-commit hook. It refuses to
let a `git commit` land on `main` even by accident. The team's flow is:

1. Branch from `main`.
2. Push and open a PR.
3. CI runs.
4. Squash-merge after CI green + approvals.
5. Head branch auto-deletes.

## 8.8 Merging

- **Linear history** is enforced. No merge commits on `main`; only fast-forward or squash.
- **Squash-merge** is the default for feature branches. The PR title becomes the
  conventional-commits-formatted commit subject. The body becomes the body.
- **At least one approval** from a CODEOWNER on the touched area.
- **`main` is protected.** Force-push, direct push, and deletion are blocked.

## 8.9 Hard rules

- **No direct commits to `main`.** The pre-commit hook blocks; the branch protection rule
  enforces.
- **No `--no-verify`.** If a hook fails, fix the cause, do not bypass.
- **No force-push to a shared branch.** Only your own short-lived branch.
- **No `git commit --amend` on a pushed commit.** New commits only. A pre-commit hook failure
  is fixed in a new commit, not an amend.
- **The commit message body explains why**, not what. The diff shows what.

## 8.10 Cross-reference

- **Skill:** `.claude/skills/commit-protocol/SKILL.md`.
- **Pre-commit config:** `.pre-commit-config.yaml` at the repo root.
- **CI:** `.github/workflows/ci.yml` — runs the same checks as the local pre-flight.
- **CODEOWNERS:** `.github/CODEOWNERS` — auto-requested reviewers per directory.
- **Phase gates:** `.claude/skills/phase-gates/SKILL.md` — per-phase exit criteria.
