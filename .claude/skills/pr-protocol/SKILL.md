---
name: pr-protocol
description: How to open, review, and merge a pull request in InfraMind — dev-* to dev, dev to main at milestones, Conventional Commits title, filled template, CI green, CODEOWNER approval, squash-merge, D25-clean squash message.
---

# PR protocol

Use this checklist **every time** you open, review, or merge a pull request. It starts where
[`commit-protocol`](../commit-protocol/SKILL.md) ends (commits are green and pushed). Prose and
rationale: [`docs/standards/08-commit-protocol.md`](../../../docs/standards/08-commit-protocol.md)
sections 8.7 and 8.8, and [`docs/CONTRIBUTING.md`](../../../docs/CONTRIBUTING.md) "Pull requests".

Opening, commenting on, approving, or merging a PR is outward-facing. An agent does it only
when the user has asked for it in the current session.

---

## 1. Which PR, which base

Only two kinds of PR exist. No topic branches, ever (see `commit-protocol` section 2).

| PR | Head → base | When | Who merges |
|----|-------------|------|------------|
| **Work PR** | `dev-rifat` / `dev-moneem` / `dev-prome` → `dev` | A coherent unit of work (a phase slice, a fix, a doc change) is done and green | Any CODEOWNER of the touched area, after approval |
| **Milestone PR** | `dev` → `main` | A phase gate is green (see [`phase-gates`](../phase-gates/SKILL.md)) or a thesis snapshot is needed | Team agreement; supervisor review first if it feeds a submission |

Never open `dev-*` → `main`. Never push to `main`. Never open a PR from someone else's
`dev-*` branch.

---

## 2. Pre-flight (before `gh pr create`)

All rows must hold. If one fails, fix it first; do not open a draft to "fix later".

| Check | Command / evidence |
|-------|--------------------|
| Every commit followed `commit-protocol` | gates green, Conventional Commits, D25 author |
| Branch contains latest `dev` | `git fetch origin && git merge origin/dev` (merge, not rebase: the branch is pushed, and rewriting it needs a force-push) |
| Gates still green after the merge | `pre-commit run --all-files && make lint && make test` |
| Pushed | `git push origin <your dev-* branch>` |
| CI green on the head commit | `gh run list --branch <branch> --limit 3` (see note below) |
| `docs/PROGRESS.md` row present | newest-first row in the PR diff |
| `CHANGELOG.md` `Unreleased` entry | required for user-visible changes; docs-only may skip and untick the box |
| Reproducibility | anything touching RCA / detection / evaluation runs from a scenario id + seed |
| No forbidden files | `git diff --stat origin/dev...HEAD` shows nothing from standards section 8.6 |
| D25 on every commit in the range | `git log origin/dev..HEAD --format='%an|%ae'` all `<user>|<user>@users.noreply.github.com` |
| No AI `Co-authored-by:` in the range | `git log origin/dev..HEAD --format='%B' \| grep -i co-authored-by` prints nothing AI-shaped |

**CI note.** `.github/workflows/ci.yml` triggers on `push` to every branch but on
`pull_request` only for `main`. A work PR into `dev` therefore shows the **push** run of the
head branch, not a PR run. Check that run; do not wait for a PR check that never comes.

Optional but recommended: ask the `code-reviewer` agent to review the range
`origin/dev..HEAD` before opening, and fix what it confirms.

---

## 3. Title — it becomes the squash commit subject

The PR title is a Conventional Commits subject (`commit-protocol` section 3), because squash-merge
turns it into the one commit that lands on `dev`.

```text
<type>(<scope>): <imperative summary, no period>
```

- One type for the whole PR. If the PR honestly needs two types, it is two PRs.
- Milestone PRs: `chore(release): promote P8 packaging to main` (or the phase in question).
- Keep it ≤ ~72 characters.

---

## 4. Body — fill the template, honestly

Start from [`.github/PULL_REQUEST_TEMPLATE.md`](../../../.github/PULL_REQUEST_TEMPLATE.md) and fill
every section:

- **Summary** — what changed and **why**, one or two sentences. Name the D-numbers and
  standards touched.
- **Type of change** — tick exactly the one that matches the title.
- **Checklist** — tick only what is true. An unticked box gets a one-line reason
  (e.g. "e2e skipped: testbed down"). Never claim pipeline verification that did not run.
- **Owner review** — replace the placeholder line with the real CODEOWNERS handles for the
  touched paths (`.github/CODEOWNERS`). Do not leave a bare placeholder handle: GitHub notifies
  whatever follows `@`.
- **Risks / rollback** — what could break, and how to revert (usually "revert the squash commit").
- **Evidence** — paste the relevant gate output tail (pytest summary line, smoke gate result).
  Numbers come only from real runs; never invent one (`CLAUDE.md` hard rule).

If a standard under `docs/standards/` changed or a D-number was added, the PR must also update
the owning agent file(s) and the matching skill in the same PR (`phase-gates`, top section).

---

## 5. Open it

```bash
# Work PR (example: Rifat)
gh pr create --base dev --head dev-rifat \
  --title "feat(evaluation): add soak-run scoring" \
  --body-file /path/to/pr-body.md

# Milestone PR
gh pr create --base main --head dev \
  --title "chore(release): promote P8 packaging to main" \
  --body-file /path/to/pr-body.md
```

Write the body to a file (scratch dir, never the repo) and pass `--body-file`; heredocs in
`--body` mangle Markdown tables. Reviewers are auto-requested from CODEOWNERS; add any missing
owner with `--reviewer <handle>`.

---

## 6. During review

- Reply to every review comment, either with a fix commit or a reason.
- Fixes land as **new commits** on the same `dev-*` branch. No amend on pushed commits, no
  force-push (`commit-protocol` section 6).
- Each fix commit passes the same gates. CI must be green on the final head.
- A reviewer checks the PR with the `code-reviewer` agent's checklist
  (`.claude/agents/quality/code-reviewer.md`): D1–D27 compliance, D25 identity, tests,
  reproducibility, `PROGRESS.md` row.
- Do not approve your own PR. Approval comes from a CODEOWNER of the touched area other than
  the author whenever one exists.

---

## 7. Merge — squash only

Preconditions: CI green on the head commit, at least one CODEOWNER approval, no unresolved
review threads, branch contains latest `dev` (re-run section 2's merge step if `dev` moved).

```bash
gh pr merge <number> --squash --subject "<the PR title>" --body-file /path/to/squash-body.md
```

Squash message rules (it becomes a real commit on `dev`, so D25 applies):

- Subject = PR title (Conventional Commits).
- Body explains **why**, wrapped at 72 columns. Do not paste the whole PR template.
- **Remove every AI `Co-authored-by:` line.** GitHub auto-collects `Co-authored-by:` trailers
  from the squashed commits into the default message; strip any that match an AI tool or bot
  (list in `commit-protocol` section 4).
- No `GitHub-Author:` footer.

Never use "Create a merge commit" or "Rebase and merge" into `main` (linear history). Use
squash into `dev` as well.

After merge, verify the landed commit:

```bash
git fetch origin
git log -1 --format='%an|%ae%n%B' origin/dev   # author is <user>|<user>@users.noreply.github.com
```

GitHub sets the squash author's email from the account's commit-email setting. If it is not the
noreply address, fix the GitHub setting ("Keep my email addresses private") before the next merge.

---

## 8. After merge — resync your branch

Personal `dev-*` branches are long-lived; do not delete them. Bring the squash commit back so
the next PR's diff starts clean:

```bash
git checkout dev-rifat          # your own branch
git fetch origin
git merge origin/dev            # merge commit on dev-* is fine; never rebase pushed history
git push origin dev-rifat
```

---

## 9. Milestone PR (`dev` → `main`) extras

On top of sections 2–7:

- [ ] The phase gate in [`phase-gates`](../phase-gates/SKILL.md) is green and ticked in
      `docs/PROGRESS.md` "Phase checklist".
- [ ] `CHANGELOG.md`: move `Unreleased` items under a new version heading with today's date.
- [ ] CI `pull_request` run on `main` is green (this one does trigger).
- [ ] The team agrees in the sync; supervisor (Golam Moktader Nayeem) has reviewed if the
      snapshot feeds a submission, report, or paper number.
- [ ] Evaluation numbers cited in the PR come from `evaluation/results/` only.

---

## 10. Hard rules

- No PR from a topic branch, no `dev-*` → `main`, no direct push to `main`.
- No merge with red or missing CI, or without a CODEOWNER approval.
- No force-push or amend during review; fixes are new commits.
- No AI `Co-authored-by:` in commits or the squash message (D25).
- No invented numbers or results in the PR body.
- No opening, approving, or merging a PR on the user's behalf without their explicit ask.

---

## 11. Quick reference

| Step | Action |
|------|--------|
| Base | `dev-*` → `dev`; `dev` → `main` at milestones only |
| Before opening | merge `origin/dev`, gates green, pushed, CI push run green |
| Title | Conventional Commits; becomes the squash subject |
| Body | Full template, honest ticks, real handles, evidence |
| Review | Fix in new commits, reply to every thread |
| Merge | Squash; strip AI `Co-authored-by:`; verify D25 author |
| After | `git merge origin/dev` into your `dev-*`, push |
