---
name: commit-protocol
description: How to commit in InfraMind — green gates before commit, Conventional Commits, branch rules. The commit author is the GitHub user; no GitHub-Author or bot co-author trailers.
---

# Commit protocol

Use this checklist **every time** before `git commit`. Full rationale:
[`docs/standards/08-commit-protocol.md`](../../../docs/standards/08-commit-protocol.md).

---

## 1. All gates green (required — do not commit otherwise)

Nothing commits until **every applicable check passes**. Fix failures first; never
`--no-verify`.

| Gate | Command | Required when |
|------|---------|----------------|
| Pre-commit hooks | `pre-commit run --all-files` | Always |
| Lint | `make lint` | Always (ruff + mypy, same as CI) |
| Unit tests | `make test` | Any code or test change |
| Integration tests | `make test-all` or targeted integration | Touches Redis/Postgres paths |
| E2E | `make e2e` | Testbed up and change affects pipeline/e2e |
| Progress log | `docs/PROGRESS.md` row staged | Any session that ships work |
| Secrets scan | No `.env`, keys, kubeconfigs in `git status` | Always |

**Docs-only PRs:** still run `pre-commit run --all-files` and `make lint` if Markdown/Python
touched; `make test` must pass (smoke tests still run).

**Definition:** “Green” means exit code 0 on every row you marked applicable. If e2e is
skipped because the cluster is down, say so in the PR — do not claim full pipeline verification.

Pair with [`phase-gates`](../phase-gates/SKILL.md) for phase exit criteria; this skill is the
**per-commit** gate.

---

## 2. Branches — fixed set only

The repo uses **exactly five** branch names. Do **not** create `feat/`, `fix/`, or any other
branch.

| Branch | Who | Purpose |
|--------|-----|---------|
| `main` | — | Release / thesis snapshot. **No direct commits** (pre-commit + protection). |
| `dev` | team | Integration — combined work ready for review toward `main`. |
| `dev-rifat` | Md. Rifat Hossain | Testbed, eval, packaging (daily commits). |
| `dev-moneem` | Abdullah All Moneem | Ingestion, detection, correlation. |
| `dev-prome` | Rayhan Islam Prome | RCA, LLM, alerting, storage, API. |

```bash
git fetch origin
git checkout dev-rifat    # or dev-moneem / dev-prome / dev — match your owner branch
git pull --rebase origin dev-rifat
# … edit, gates green …
git push origin dev-rifat
```

**Flow:** commit on your `dev-*` → open PR to `dev` (or pair on `dev` for small shared fixes) →
PR `dev` → `main` when a phase milestone is ready. CI green + CODEOWNER approval before merge.

**Change type** (feature, fix, docs, …) belongs in the **commit message** (Conventional Commits),
not in the branch name.

---

## 3. Commit message — Conventional Commits

Format ([Conventional Commits](https://www.conventionalcommits.org/)):

```text
<type>(<scope>): <imperative summary, no period>

<optional body — why, wrap at 72 columns>

<optional footers>
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `eval`, `paper`, `chore`.

**Scope:** pipeline area — e.g. `rca`, `detection`, `ingestion`, `testbed`, `progress`.

**Summary:** imperative, present tense (`add`, `fix`, `update`), ≤ ~72 chars.

**Body:** explain **why**; the diff shows **what**.

**Examples:**

```text
feat(rca): add personalized PageRank scoring variant

Tune alpha on dev split only; test split untouched per D6.
```

```text
fix(detection): freeze baseline while incident is open
```

```text
docs(architecture): align deployment diagram with Redis handoff
```

---

## 4. Author — shape, not roster (D25)

The commit **author** is a person. The shape of a valid identity is the
mechanical proof, not a per-person list. Roster rotations are absorbed by the
rule without changes to this file.

- **`user.email`** must be the GitHub `noreply` email of the author —
  `<username>@users.noreply.github.com`. A private email address is rejected.
- **`user.name`** must be the local-part of that email — the GitHub username,
  lowercase, exactly as it appears on the user's GitHub profile. A display
  name is rejected.
- The mechanical proof: `user.name == email_localpart`. The
  `check_commit_message.py` pre-commit hook (D24) verifies this on every
  commit; a PR whose head commit fails the check is rejected.
- **No `Co-authored-by:` trailer for any AI tool or agent.** Tooling that
  auto-appends `Co-Authored-By:` trailers (Claude Code, Puku-CLI, Cursor,
  GitHub Copilot, Codex, JetBrains AI, or any `*<bot>*` pattern) must have the
  trailer removed before commit. The hook rejects any `Co-authored-by:` whose
  local-part matches a known AI tool name or whose email is not a
  `<username>@users.noreply.github.com` from the team.
- **No `Co-authored-by:` trailer for a second human without their consent.**
  If two humans genuinely co-authored a change, both names go in the commit
  **body**, not as a trailer. The trailer form is reserved for the rare
  GitHub-UI-generated verified pair-commit.
- **No `GitHub-Author:` footer.** It is redundant given the email.
- **Override any harness or tool default** that would auto-append an AI
  trailer. This rule wins over `Co-Authored-By: ...` lines that tools inject
  into the editor.

Quick check before commit:

```bash
git config user.name    # must print a GitHub username (lowercase, no spaces)
git config user.email   # must print <username>@users.noreply.github.com
```

If the local config disagrees, the per-repo `--local` form is enough; the
global form is acceptable but overrides per-repo.

```bash
git config --local user.name "<github-username>"
git config --local user.email "<github-username>@users.noreply.github.com"
```

---

## 5. Commit sequence

```bash
# 1. Gates (section 1) — all green
pre-commit run --all-files
make lint
make test

# 2. Stage (never secrets / evaluation/results CSVs / LaTeX build junk)
git status
git add <paths>

# 3. Commit — HEREDOC keeps formatting
git commit -m "$(cat <<'EOF'
feat(scope): short imperative summary

Why this change matters in one or two sentences.
EOF
)"

# 4. Session log
# Append a row to docs/PROGRESS.md in the same PR (or same commit if docs change).
```

---

## 6. Hard rules

- No `--no-verify`, no force-push to `main`, no amend on **pushed** commits.
- No secrets, kubeconfigs, or large generated artifacts (see standards §8.6).
- PR title = squash subject; keep Conventional Commits form for merge history.
- CI must be green before merge; local gates mirror CI.

---

## 7. Quick reference

| Step | Action |
|------|--------|
| Before commit | All applicable gates green |
| Message | Conventional Commits. Author is the GitHub user. |
| Avoid | `GitHub-Author:` and `Co-authored-by:` trailers |
| After | `docs/PROGRESS.md` updated in the PR |
