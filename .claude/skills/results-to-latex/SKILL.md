---
name: results-to-latex
description: How to turn evaluation results (CSVs) into LaTeX tables and figure placeholders for the paper.
---

# Results to LaTeX (skill entrypoint)

**Canonical procedure:** [`docs/standards/12-paper-writing.md`](../../../docs/standards/12-paper-writing.md) (results section)

Before generating tables or figures:

1. Read CSVs only from `evaluation/results/` — never hand-type numbers.
2. Pair with the `results-analyst` agent for claims and captions.
3. Keep generated TeX snippets under `paper/` and cite the source CSV path in a comment.

Owner: results-analyst agent (`Prome`).
