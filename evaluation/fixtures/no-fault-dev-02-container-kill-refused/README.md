# dev-02 (first attempt): recordings with NO fault

These five repeats were recorded on 2026-11-16 for `dev-podchaos-02-shippingservice-container-kill`,
but every `container-kill` CR was refused by the kind runtime (`AllInjected=False`,
`expected docker:// but got containerd://`, D26). shippingservice was never killed, so each
repeat is ~15 minutes of normal traffic.

- Every `meta.json` carries `"fault_injected": false`; the runner refuses to score them as a
  fault (`fixture_no_fault`), even if dev-02 is unblocked later.
- Useful as fault-free data: InfraMind opened no incident on any of them (each had one
  pre-fault noise blip that resolved on its own).
- This folder name is not a scenario id, so the runner never looks here. After D2-Chaos-runtime
  (containerd runtime, 2026-11-16) `container-kill` injects, dev-02 was unblocked and is
  re-recorded under its own id.
