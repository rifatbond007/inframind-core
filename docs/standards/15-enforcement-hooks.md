# 15. Enforcement hooks — making the standards enforceable
> standards · Process · InfraMind. Pairs with the `commit-protocol` skill in `.claude/skills/commit-protocol/SKILL.md` (the human-facing checklist this standard automates). This file pins three pre-commit hooks, the `scripts/` directory they live in, and the rollout plan. D23-Scripts, D24-Hooks.

## 15.1 Why this standard exists

The standards set is unusually thorough, but most rules are enforced by **human review**, not by tooling. Three rules have failed silently in past projects because of this:

1. **The dependency DAG** (`01-project-structure.md` section 1.3) — a hard rule. A reviewer has to read every PR's import block to catch a violation.
2. **The author-trailer ban** (`08-commit-protocol.md` section 8.2.1) — forbids `Co-authored-by:` for AI tools and any `GitHub-Author:` trailer. The current pre-commit config does not grep the commit message.
3. **The PROGRESS.md requirement** (`08-commit-protocol.md` section 8.4) — every session ends with a row in `docs/PROGRESS.md`. Nothing in the Makefile or pre-commit config enforces it.

D24 turns these three rules from "good intentions" into "the commit fails." D23-Scripts creates the directory the hook scripts live in.

## 15.2 The three hooks (D24)

| Hook | Stage | What it enforces | Existing standard |
|---|---|---|---|
| `check_imports.py` | `pre-commit` (staged files) | The dependency DAG — forbidden import directions across the 9 subpackages | `01-project-structure.md` section 1.3 |
| `check_commit_message.py` | `commit-msg` (commit message) | The trailer ban — no `Co-authored-by:` for tools, no `GitHub-Author:` | `08-commit-protocol.md` section 8.2.1 |
| `check_progress.py` | `pre-commit` (staged files) | The PROGRESS.md row requirement — code-touching commits must touch `docs/PROGRESS.md` (or recent row) | `08-commit-protocol.md` section 8.4 |

All three run **locally** before every commit. All three run again in **CI** as a second pass — local enforcement is the first line of defense, CI is the second.

## 15.3 The `scripts/` directory (D23)

A new top-level directory `scripts/` is permitted for the three hook scripts (and future dev-time tooling). The directory is **not part of the runtime**:

- Not in the Docker image (`Dockerfile` excludes it).
- Not in the package (not under `src/inframind/`).
- Not in the Helm chart (`deploy/helm/inframind/` does not mount it).
- Not in the testbed (not applied to the kind cluster).

```
scripts/
├── check_imports.py
├── check_commit_message.py
└── check_progress.py
```

Per `01-project-structure.md` section 1.6 ("no new top-level directory without a D-number"), this directory requires D23-Scripts. The justification: the hooks are real Python files with logic that deserves version control and a README; the alternative (inline them as bash in `.pre-commit-config.yaml`) is unmaintainable past ~30 lines.

The directory is owned by **testbed-engineer** (`Rifat`) by default, since the hooks are dev-time tooling. A different owner can be set in `CODEOWNERS` if the team prefers.

## 15.4 Hook 1 — dependency DAG check

### 15.4.1 Behaviour

For each staged `.py` file under `src/inframind/`, walk the import statements. Fail the commit if a file in `subpackage X` imports anything from a forbidden subpackage.

### 15.4.2 The forbidden edges (from section 1.3)

| Source | May NOT import from |
|---|---|
| `common/` | anything in `inframind.*` |
| `ingestion/` | `detection`, `correlation`, `rca`, `llm`, `alerting`, `storage`, `api` |
| `detection/` | `correlation`, `rca`, `llm`, `alerting`, `storage`, `api` |
| `correlation/` | `rca`, `llm`, `alerting`, `storage`, `api` |
| `rca/` | `llm`, `alerting`, `storage`, `api` |
| `llm/` | `alerting`, `storage`, `api` |
| `alerting/` | `storage`, `api` |
| `storage/` | `alerting`, `api` |
| `api/` | (no restrictions; it is the only FastAPI importer) |

### 15.4.3 Implementation outline

- Script: `scripts/check_imports.py`. Uses Python's `ast` module (stdlib).
- The script does NOT require the module to be importable — it parses the AST. This means it works on partial code, on broken code, and on staged-but-uncommitted code.
- For each `import x` or `from x import y`, the script normalises `x` to a top-level subpackage (e.g. `inframind.rca.graph` -> `rca`) and checks against the table in section 15.4.2.
- Exemptions:
  - `__init__.py` files are skipped — re-exports across subpackages are allowed in `__init__.py`.
  - The script itself is skipped.
  - A line-level `# noqa: dag-violation` comment bypasses the check. This matches the existing `noqa` discipline in section 1.7 ("add a comment explaining the exception"). A `dag-violation` exemption is rare; the PR description should call it out.

### 15.4.4 Failure message

```
Forbidden import: src/inframind/ingestion/prometheus.py:42
   imports inframind.detection.zscore
See section 1.3 of docs/standards/01-project-structure.md.
To bypass (rare), add `# noqa: dag-violation` on the line and explain why in the PR description.
```

## 15.5 Hook 2 — forbidden commit trailers

### 15.5.1 Behaviour

Read the staged commit message from `.git/COMMIT_EDITMSG`. Fail if it contains any of:

- `Co-authored-by:` followed by a non-allow-listed author.
- `GitHub-Author:` (any case).

### 15.5.2 The `Co-authored-by:` allow-list

The standards forbid `Co-authored-by:` for *tools* (Cursor, Copilot, Claude, etc.) but allow it for **human co-authors** (a pair-programmed commit). The hook enforces the spirit, not the letter:

- The allow-list is a single Python list in `check_commit_message.py`. Each entry is a GitHub username (e.g. `rifatbond007`, `moneem`, `prome`).
- New human co-authors are added to the list by a PR that updates the script. The PR description should name the new co-author.
- A `Co-authored-by:` trailer for a username **not** in the allow-list fails the commit. The error message names the trailer and the rejected author.

### 15.5.3 Failure message

```
Forbidden trailer: 'Co-authored-by: Cursor <noreply@cursor.sh>'
See section 8.2.1 of docs/standards/08-commit-protocol.md.
Tools (Cursor, Copilot, Claude, etc.) must not appear as co-authors.
For a human co-author, add their GitHub username to the allow-list in scripts/check_commit_message.py.
```

## 15.6 Hook 3 — PROGRESS.md row check

### 15.6.1 Behaviour

If the staged commit touches any path under `src/`, `tests/`, `evaluation/`, `testbed/`, or `deploy/`, the commit must also touch `docs/PROGRESS.md` in the same commit OR the most recent row in `docs/PROGRESS.md` must be dated within the last `PROGRESS_GRACE_DAYS` days (default 7).

### 15.6.2 Why a grace period

The standard says "every session appends a row". A session is not the same as a commit — a 5-commit session might add 1 row at the end. Enforcing per-commit is too strict; per-session is the intent. The grace period is a proxy for "did anyone in the team work on this in the last week? then a row is recent enough."

### 15.6.3 Implementation outline

- Script: `scripts/check_progress.py`. Stdlib only.
- Path patterns that trigger the check: `^(src|tests|evaluation|testbed|deploy)/.*`.
- If the trigger fires:
    - If `docs/PROGRESS.md` is in the staged files: pass.
    - Else, parse the most recent row of `docs/PROGRESS.md` (the file is a Markdown table). If the date in the first column is within `PROGRESS_GRACE_DAYS` of today: pass.
    - Else: fail.
- The 7-day threshold is configurable per-repo in `.pre-commit-config.yaml` (`PROGRESS_GRACE_DAYS` env var).

### 15.6.4 Failure message

```
This commit touches code (src/) but docs/PROGRESS.md was not updated
and the most recent row is older than PROGRESS_GRACE_DAYS (default 7).
See section 8.4 of docs/standards/08-commit-protocol.md.
To fix, add a row to docs/PROGRESS.md in this commit (or in a follow-up
if this is one of several commits in a session — the row can land at
the end of the session).
```

## 15.7 The rollout plan

A buggy hook is more painful than no hook. The rollout:

1. Land `check_imports.py` in `--simulate` mode. Run for one week. Fix any false positives.
2. Land `check_commit_message.py` in `--simulate` mode. Same.
3. Land `check_progress.py` in `--simulate` mode. Same.
4. After one week of clean `--simulate` runs, flip all three to enforce (return non-zero on violation).
5. Wire the three scripts into `.pre-commit-config.yaml` as `local` hooks.
6. Add the same scripts to CI as a second pass — a PR that somehow slips past local hooks is still caught on `dev`.

`--simulate` mode prints what *would* fail without failing the commit. The first week of `--simulate` is the team's chance to tune the rules.

## 15.8 Trade-offs and limits

### 15.8.1 What these hooks do NOT catch

- **Type-hint lies.** `mypy --strict` (already in CI) catches those.
- **The wrong owner branch.** The `no-commit-to-branch` pre-commit hook (already configured) catches `main`; it does not catch "committed to `dev-rifat` when you meant `dev-moneem`". That is a social contract, not a hook problem.
- **A passing local hook but failing CI.** CI runs the same hooks + `mypy`. Local enforcement is the first line of defense, not the only one.
- **A reviewer who approves a PR anyway.** The hooks make the *commit* fail; they do not make the *PR* fail. A bad PR can still slip through if the reviewer is asleep.

### 15.8.2 What could go wrong

- **False positives in the DAG check.** A circular-but-tolerated import (e.g. for type hints only) trips the check. The `__init__.py` exemption covers re-exports; the `# noqa: dag-violation` comment covers everything else.
- **The PROGRESS.md grace period masks missed updates.** A 7-day grace means a contributor who hasn't updated PROGRESS for a week can land code without updating it. This is the *intended* trade-off (per-commit is too strict). The default is tunable via `PROGRESS_GRACE_DAYS`.
- **The commit-message hook is local.** A force-push with `--no-verify` (banned per `08-commit-protocol.md` section 8.9, but possible) bypasses it. The branch protection rule on `main` is the second line of defense.
- **The hooks themselves can have bugs.** They run before every commit. A bug that incorrectly flags a legitimate import is more painful than no hook. The rollout plan (section 15.7) mitigates this.

## 15.9 Hard rules

- **D23-Scripts — `scripts/` is dev-time only.** It is not in the Docker image, not in the package, not in the Helm chart, not in the testbed.
- **D24-Hooks — the three hooks are required.** Removing or weakening one is a D-change.
- **D24-Hooks — rollout is staged.** No hook enforces until one week of clean `--simulate` has elapsed.
- **D24-Hooks — CI runs the same hooks.** Local is first line, CI is second.
- **D24-Hooks — `Co-authored-by:` allow-list is a Python list in the script.** Not a config file. The script and the list change in lockstep.
- **No force-push to bypass.** `08-commit-protocol.md` section 8.9 forbids `--no-verify` on `main` and `dev`. The hooks inherit that rule.

## 15.10 Tests

```
scripts/test_check_imports.py            # unit tests for the DAG walker
scripts/test_check_commit_message.py    # unit tests for the trailer filter
scripts/test_check_progress.py          # unit tests for the date check

tests/integration/test_precommit_hooks.sh
    # end-to-end: a bad commit fails, a good commit passes
```

## 15.11 Cross-reference

- **Locked decision:** `docs/decision-tree.md` — D23-Scripts (the directory), D24-Hooks (the enforcement).
- **Enforces:** `01-project-structure.md` section 1.3 (DAG), `08-commit-protocol.md` section 8.2.1 (trailers), `08-commit-protocol.md` section 8.4 (PROGRESS.md).
- **Config:** `.pre-commit-config.yaml` (wires the scripts), `.github/workflows/ci.yml` (runs them in CI).
- **Skill:** `.claude/skills/commit-protocol/SKILL.md` (the human-facing checklist this standard automates).
