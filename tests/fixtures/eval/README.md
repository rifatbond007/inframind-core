# Synthetic replay fixtures (tests only)

Hand-written fixtures that exercise the replay path in unit and integration
tests. They are **not** recordings: the signals are minimal and the graph
topology is invented (it does not match Online Boutique). Never point the
evaluation at this directory and never report a number replayed from it.

Real recordings live in `evaluation/fixtures/` and are written only by
`make eval-live`. The layout is the same: `<scenario_id>/r<repeat>/`.
