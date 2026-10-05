---
name: paper-writing
description: How to write the InfraMind paper — IEEE conference format, 6–8 pages, LaTeX, contribution-first, claim-honest.
---

# Paper writing

## Structure (IEEE conference, 6–8 pages)

1. **Abstract** (≤150 words) — problem, contribution, key result.
2. **Introduction** — alert fatigue + fragmented visibility + manual RCA. Our hypothesis. Four objectives.
3. **Related Work** — SRE/observability, anomaly detection, RCA, LLM diagnosis. Narrow the novelty claim.
4. **System Design** — five-stage pipeline. Deterministic RCA. Evidence bundle. LLM-as-summarizer.
5. **Experimental Setup** — testbed, scenarios, baselines, metrics, protocol.
6. **Results** — tables + figures, each tied to a CSV.
7. **Discussion + Threats to Validity** — internal, external, construct.
8. **Conclusion + Future Work** — what we built, what we did not.

## Hard rules

- **Never invent a number.** Every figure is from `evaluation/results/`. Cite the CSV in a footnote.
- **Never invent a citation.** Use `reference-verifier` to confirm.
- **Honor D1** in the wording: "the LLM summarises evidence; the graph decides". Never "the LLM identifies the root cause".
- **Honor D2**: "edges are caller→callee; we walk from symptom toward callees".
- **Honor D7**: name all six baselines in §Experimental Setup.

## Submission process

1. Choose a venue:
   - Regional IEEE conference (ICCIT, ECCE, TENSYMP) — easier acceptance, IEEE Xplore indexed.
   - International workshop co-located with ICSE/FSE/ASE/ISSRE — competitive.
   - Journal — IEEE Access, Journal of Cloud Computing.
   - Preprint — arXiv (cs.SE / cs.DC). First-time submissions may need endorsement; supervisor can help.
2. Avoid predatory venues:
   - "Guaranteed acceptance" → predatory.
   - "Published in 7 days" → predatory.
   - Verify indexing on IEEE Xplore / Scopus / DBLP.
   - Use Think.Check.Submit (thinkchecksubmit.org).
3. Submission via EasyChair / HotCRP / Microsoft CMT.
4. Supervisor as corresponding / co-author. Reviews before any submission.
5. Rebuttal / revision → camera-ready → presentation.
6. On rejection: use the review comments to improve and submit to the next venue.

## Ethics

- Plagiarism check before submission (Turnitin / iThenticate).
- AI tool disclosure per venue policy.
- All references verified.
- All numbers traced to CSVs.

## Suggested timeline

After evaluation finishes:
- 4–6 weeks: paper draft.
- 1 week: supervisor review.
- Submit.

## Output style

- LaTeX source under `paper/`.
- Use IEEEtran class (`\documentclass[conference]{IEEEtran}`).
- BibTeX (`paper/references.bib`).
- Figures as PDF (vector) or PNG (raster, ≥ 300 dpi).
- Each figure / table has a self-contained caption.