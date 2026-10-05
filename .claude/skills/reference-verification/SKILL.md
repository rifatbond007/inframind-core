---
name: reference-verification
description: How to verify every reference in paper/references.bib and proposal.pdf. Authors, title, year, venue, DOI/URL.
---

# Reference verification

Examiners check references. A fake citation will get a paper desk-rejected.

## How to verify

For each entry in `paper/references.bib` (and the Reference list in `docs/proposal.pdf`):

1. Search Google Scholar / arXiv / DOI resolver.
2. Confirm:
   - Author list (pay attention to the last author — that's usually the advisor).
   - Title.
   - Year.
   - Venue.
   - DOI / arXiv ID / URL.
3. Tag the entry:
   - **verified** — confirmed.
   - **fix** — here is the corrected BibTeX.
   - **remove** — cannot be found; recommend dropping.
   - **placeholder** — appears to be a made-up arXiv ID; flag for review.

## Common issues to watch

- **Placeholder arXiv IDs** — IDs that look suspicious (`2405.12345` with 5 digits).
- **Wrong author lists** — the proposal reviewer found this: `[12]` should be `Hou et al.`, not `Li, Wang, Zhu`.
- **Claims that need source verification** — e.g. "90% of alerts are false positives" — find the source.
- **DOIs with typos** — one digit off.
- **Websites that no longer exist** — replace with archived snapshot or remove.

## Output

A markdown table:

```
| # | Original BibTeX                | Status        | Action              |
|---|--------------------------------|---------------|---------------------|
| 1 | jiang2024microrca              | verified      | -                   |
| 2 | li2023llm4se                   | fix           | hou2023llm4se       |
| 3 | chen2024llmdiag                | placeholder   | remove              |
```

End with a count of verified vs. flagged.

## Hard rules

- **Never invent a citation** to fill a gap. Drop the reference.
- Be conservative. Reviewers find fake citations fast.
- Cross-check claims that aren't obviously true — a reviewer will.