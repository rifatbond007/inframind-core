# 12. Paper writing — IEEE conference, 6–8 pages, LaTeX
> standards · Paper · InfraMind. Pairs with the `paper-writing`, `reference-verification`, and `results-to-latex` skills. This file is the rationale.

## 12.1 The structure

IEEE conference format, 6–8 pages:

| # | Section | Length | Purpose |
|---|---|---|---|
| 1 | Abstract | ≤150 words | Problem, contribution, key result. |
| 2 | Introduction | 1 page | Alert fatigue + fragmented visibility + manual RCA. Hypothesis. Four objectives. |
| 3 | Related Work | 1 page | SRE / observability, anomaly detection, RCA, LLM diagnosis. Narrow the novelty claim. |
| 4 | System Design | 1.5 pages | Five-stage pipeline. Deterministic RCA. Evidence bundle. LLM-as-summariser. |
| 5 | Experimental Setup | 1 page | Testbed, scenarios, baselines, metrics, protocol. |
| 6 | Results | 1 page | Tables + figures, each tied to a CSV in `evaluation/results/`. |
| 7 | Discussion + Threats to Validity | 1 page | Internal, external, construct. |
| 8 | Conclusion + Future Work | 0.5 page | What we built, what we did not. |

The literature-scout agent keeps the novelty claim honest. The paper-reviewer agent checks
the draft before submission. The reference-verifier agent confirms every citation.

## 12.2 Hard rules

- **Never invent a number.** Every figure is from `evaluation/results/`. Cite the CSV in a
  footnote.
- **Never invent a citation.** Use the `reference-verifier` skill to confirm every entry in
  `paper/references.bib` and `docs/proposal.pdf`.
- **Honor D1 in the wording.** "The LLM summarises evidence; the graph decides." Never "the
  LLM identifies the root cause."
- **Honor D2.** "Edges are `caller -> callee`; we walk from symptom toward callees."
- **Honor D7.** Name all six baselines in §Experimental Setup.
- **All references verified.** The paper-reviewer agent runs `reference-verifier` on the
  draft before submission.
- **All numbers traced to CSVs.** The results-analyst agent produces a mapping table from
  every claim in §Results to the source CSV column.

## 12.3 The contribution story

The paper has one headline contribution: **deterministic, graph-guided RCA beats the
baselines on accuracy and false-alarm rate at the same time**. The supporting contributions
are:

1. A multi-signal ingestion pipeline that turns Prometheus + Loki + Jaeger + Alertmanager
   into a single `Signal` stream (section 4).
2. A deterministic ranker that walks the call graph from the symptom toward the callees
   and bundles evidence as it goes (section 4 + 6).
3. An LLM explainer that summarises evidence — never picks — with a validator that catches
   hallucinated evidence ids (section 4 + 7).
4. A reproducible evaluation harness: ≥12 scenarios × 5 reps + fault-free soak, six
   baselines, four ablations (section 5).

The §Introduction and §Conclusion lean on these four. The §System Design maps each to a
module in `src/inframind/`. The §Results backs each with a number.

## 12.4 Tables and figures — the CSV path

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

Every table carries a footnote that points at the source CSV:

```
\footnote{Source: \texttt{evaluation/results/main.csv}, columns \texttt{mttd\_mean, mttd\_std}.}
```

If the CSV is missing a value, the cell is `n/a` and the caption explains why. **Never
invent a number to fill a missing cell.**

Figures are vector PDF when possible; raster PNG (≥ 300 dpi) otherwise. Every figure has a
self-contained caption that names the metric.

## 12.5 Submission process

1. **Choose a venue.**
   - Regional IEEE conference (ICCIT, ECCE, TENSYMP) — easier acceptance, IEEE Xplore indexed.
   - International workshop co-located with ICSE / FSE / ASE / ISSRE — competitive.
   - Journal — IEEE Access, Journal of Cloud Computing.
   - Preprint — arXiv (cs.SE / cs.DC). First-time submissions may need endorsement; the
     supervisor can help.
2. **Avoid predatory venues.** "Guaranteed acceptance", "published in 7 days" → predatory.
   Verify indexing on IEEE Xplore / Scopus / DBLP. Use Think.Check.Submit.
3. **Submission via EasyChair / HotCRP / Microsoft CMT.**
4. **Supervisor as corresponding / co-author.** Reviews before any submission.
5. **Rebuttal / revision → camera-ready → presentation.**
6. **On rejection:** use the review comments to improve and submit to the next venue. Do not
   resubmit the same draft to a similar venue.

## 12.6 Ethics

- **Plagiarism check** before submission (Turnitin / iThenticate).
- **AI tool disclosure** per venue policy. The agentic workflow that built InfraMind is
  disclosed in §Acknowledgements or §Methods.
- **All references verified** by the reference-verifier agent.
- **All numbers traced to CSVs** by the results-analyst agent.

## 12.7 Timeline

After evaluation finishes (P7 → P9 transition):

- **4–6 weeks** — paper draft. The paper-writer agent drafts each section. The team reviews.
- **1 week** — supervisor review.
- **Submit.** Allow at least one buffer week before the deadline.

## 12.8 Hard rules

- **D1 wording.** "Summarises", never "identifies".
- **D2 wording.** "Caller -> callee", "walk toward callees", never "upstream" without a
  definition.
- **D7 wording.** All six baselines named in §Experimental Setup.
- **Never invent a number.** Cite it.
- **Never invent a citation.** Verify it.
- **Never tune on the test set.** The numbers are sacred.

## 12.9 Cross-reference

- **Skills:** `.claude/skills/paper-writing/SKILL.md`, `reference-verification/SKILL.md`,
  `results-to-latex/SKILL.md`.
- **Locked decisions:** D1 (LLM summarises), D2 (graph orientation), D6 (evaluation matrix),
  D7 (baselines).
- **Numbers source:** `evaluation/results/`. The paper-reviewer agent rejects any number
  without a CSV footnote.
- **References source:** `paper/references.bib` + `docs/proposal.pdf`. The reference-verifier
  agent rejects any entry it cannot confirm.
