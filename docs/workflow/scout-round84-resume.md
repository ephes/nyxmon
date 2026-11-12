# Scout round 84 resume: complete

2026-11-12. All round 84 bundles are merged into `main`; nothing is parked.

- B33 (`nyxmon-late-lease-result-display-authority`,
  `nyxmon-legacy-claim-repair-batched-writes`): merged 2026-11-08 in
  `2c36d07`.
- B34 (`nyxmon-held-recheck-sql-parameter-ceiling`,
  `nyxmon-runner-bounded-check-fanout`): the parked branch
  `fix/r84-b34-bounded-fleet` was rebased onto the agent 0.1.3 release,
  reviewed (suggestions only, applied) and merged in `42e3b63`.
- B35 (`nyxmon-check-cookie-credential-isolation`,
  `nyxmon-http-attempt-total-deadline`) and B36
  (`nyxmon-json-metrics-finite-clock-domain`,
  `nyxmon-json-metrics-retry-execution-budget`): implemented, reviewed and
  merged together on 2026-11-12.

The scout report (`scout-round84.md`) was not available when B35/B36 were
implemented; their scope was reconstructed from the slugs and verified
against the code. The behaviour is documented in `agent-rs/PLAN.md` 5.5,
`docs/configuration.md` and the Unreleased changelog. B33 shipped in
`agent-rs-v0.1.3`; B34–B36 ship in `agent-rs-v0.1.4`, and production runs
them once ops-control pins that release.
