# dev-02: recordings with NO fault

These five repeats were recorded on 2026-11-16 for `dev-podchaos-02-shippingservice-container-kill`,
but every `container-kill` CR was refused by the kind runtime (`AllInjected=False`,
`expected docker:// but got containerd://`, D26). shippingservice was never killed, so each
repeat is ~15 minutes of normal traffic.

- Every `meta.json` carries `"fault_injected": false`; the runner refuses to score them as a
  fault (`fixture_no_fault`), even if dev-02 is unblocked later.
- Useful as fault-free data: InfraMind opened no incident on any of them (each had one
  pre-fault noise blip that resolved on its own).
- dev-02 is `status: blocked`; dev-11 replaced it in the runnable dev set.
