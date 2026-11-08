# Scout round 84 resume: B34 parked for authentication

2026-11-08. Branch: `fix/r84-b34-bounded-fleet`. Base: origin/main
`2c36d07b8386dfb89feacd82df9f34c1ad3e5678`. Origin verified as
`git@github.com:ephes/nyxmon.git`. Fresh SSH clone in
`/Users/jochen/workspaces/ws-cx-r84c-nyxmon/nyxmon`.

B33 (`nyxmon-late-lease-result-display-authority` and
`nyxmon-legacy-claim-repair-batched-writes`) is already merged into this base;
its implementation was not repeated.

## Implemented B34; review blocked

- `nyxmon-held-recheck-sql-parameter-ceiling`: reproduced on current main.
  Deduplicate IDs and chunk at `min(500, SQLITE_LIMIT_VARIABLE_NUMBER - 2)`
  in one IMMEDIATE transaction. Keep idle/enabled/earlier-due guards and exact
  changed-row counts. Reject a connection limit below three variables.
- `nyxmon-runner-bounded-check-fanout`: reproduced on current main.
  Refill a JoinSet with at most five active tasks. Cap collector execution
  admission at `min(NYXMON_CHECK_BATCH_SIZE, 5)`, leaving other due checks
  unclaimed. There are no waiting claimed execution waves to add to lease
  estimates. Shutdown preserves completed results and releases owned claims;
  unstarted checks do not produce results or stale incidents.

Synthetic, platform-independent regressions fail with the original source:
parameter-limit success and middle-chunk rollback both hit "too many SQL
variables"; 100-check runner concurrency exceeds five; collector admission
claims all 20 instead of five; an intake stop starts all 100 instead of five.
After repair these pass. Documentation and Unreleased notes describe the
intentional tightening of historical Python parity. No migration is needed.

Local checks (logs are outside the repository in the assigned workspace):

- `just test`: Python suite, full Rust workspace suite and live synthetic
  Django-to-Rust notification handoff.
- `just lint`: Ruff/format pre-commit hooks and Rust fmt/clippy, warnings denied.
- `just typecheck`: mypy and Cargo workspace/all-targets check.
- `just rs-deny`: advisories, bans, licenses and sources passed.
- Python frozen dependency audit and `cargo build --release --locked`.
- `git diff --check`.

Final validation: all commands above passed. `just test` reported 1,863 Python
passes, the full Rust workspace passed (the existing opt-in notification journey
remains ignored), and the explicit live migration handoff passed. Every new
regression fails with the original implementation. The optimized build and
frozen Python audit passed; no known vulnerabilities were found.
Rust validation uses the pinned 1.99.0 toolchain via
`PATH="$HOME/.cargo/bin:$PATH"`.

## Review blocker and continuation

The installed claude-review-loop harness was invoked with
`--model claude-opus-5-5 --effort high --record-baseline`. It exited 2,
`PROVIDER_ERROR`, before reviewing anything: **Failed to authenticate: OAuth
session expired and could not be refreshed**. No valid review round or verdict
exists. Artifacts: `../reviews/b34-r1/result.json`. The owner explicitly
requested a parking branch push and stop on this failure; this is not approval
to merge. No remaining bundle was started after the authentication failure.

Restore Claude Code authentication outside this task, fetch origin and rebase
this branch on current main. Reverify overlaps and rerun local checks if trunk
moved. Review the complete parked diff against current main: the harness needs
`--baseline-ref origin/main` to include already committed work; identify this
as the first valid B34 review. Fix required findings and review repair deltas,
stopping at four rounds or the same recurring required finding. Accept only a
valid CLEAN or proportionately accepted suggestion-only verdict for merging.
Before each merge, fetch again; if origin moved, rebase and rerun tests. Merge
locally with `--no-ff`, run full local checks after merging, push only main and
verify with `git ls-remote`. Never delete remote branches.

Then reverify against current main and implement these untouched bundles in
report order:

1. B35: `nyxmon-check-cookie-credential-isolation`, then
   `nyxmon-http-attempt-total-deadline`.
2. B36: `nyxmon-json-metrics-finite-clock-domain`, then
   `nyxmon-json-metrics-retry-execution-budget`.

The source report is `~/workspaces/ws-work-planning/reviews/scout-round84.md`.
B35/B36 have not been reverified, implemented, tested or reviewed in this run.
No Actions reliance or runs, PRs, deployments, releases, tags or host access.

Sandboxed tools need UV_CACHE_DIR, UV_TOOL_DIR, UV_TOOL_BIN_DIR and
PRE_COMMIT_HOME under the workspace `.cache/`. Set PYTHONDONTWRITEBYTECODE=1
when invoking the review harness from `~/projects`; never edit there. Override
the review ledger/lock paths to writable workspace or permitted state paths.

Promotion: incident — authentication failure repeated the known B33 blocker;
no new process rule is needed. The explicit owner stop-and-park policy applied.
