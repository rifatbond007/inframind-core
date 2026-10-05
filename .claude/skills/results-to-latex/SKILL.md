---
name: results-to-latex
description: How to turn evaluation results (CSVs) into LaTeX tables and figures for the paper. NEVER invent numbers.
---

# Results to LaTeX

You read **only** `evaluation/results/`. You never invent numbers. Every claim in the paper comes from a CSV in that directory.

## Per-metric table

```latex
\begin{table}[t]
  \centering
  \caption{Per-scenario MTTD (mean $\pm$ std, 5 runs). Lower is better.}
  \label{tab:mttd}
  \begin{tabular}{lrrrr}
    \toprule
    Scenario & InfraMind & Raw Alertmanager & PageRank & LLM-only \\
    \midrule
    001-pod-kill-frontend    & 12.3 $\pm$ 2.1  & 28.5 $\pm$ 4.2  & 19.0 $\pm$ 3.5  & 21.0 $\pm$ 2.8 \\
    002-cpu-stress           & 18.0 $\pm$ 3.5  & 33.0 $\pm$ 6.0  & 25.0 $\pm$ 4.1  & 22.5 $\pm$ 3.3 \\
    ... \\
    \bottomrule
  \end{tabular}
\end{table}
```

## Figure (matplotlib)

```python
import matplotlib.pyplot as plt
import pandas as pd

df = pd.read_csv("evaluation/results/main.csv")
fig, ax = plt.subplots(figsize=(3.5, 2.5))
# plot...
fig.savefig("paper/figures/mttd_by_scenario.pdf", bbox_inches="tight")
```

Export to PDF (vector) when possible; PNG (≥ 300 dpi) otherwise.

## Provenance footnote

For every table / figure, include a footnote with the CSV path:

```
\footnote{Source: \texttt{evaluation/results/main.csv}, columns \texttt{mttd\_mean, mttd\_std}.}
```

## Hard rules

- **Never invent a number.** If the CSV is missing a value, write "n/a" and explain in the caption.
- Mean ± std with 95% CI where the comparison is the story. Show N.
- For comparison claims (e.g. "InfraMind outperforms raw Alertmanager"), state the test (paired t-test / Wilcoxon) and the p-value if applicable.

## Output style

A markdown report:
- Each table reproducible from a CSV path.
- Each figure reproducible from a script.
- A list of follow-up analyses for the team.