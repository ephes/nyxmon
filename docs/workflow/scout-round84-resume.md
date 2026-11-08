# Scout round 84 resume: B33 parked for authentication

2026-11-08. Branch: `fix/r84-b33-lease-authority`. Base: origin/main
`97ab462524794897e3f13123ecc9ed41843c8a96`. Origin verified as
`git@github.com:ephes/nyxmon.git`. Fresh clone in the assigned workspace.

## Completed implementation, blocked review

- `nyxmon-late-lease-result-display-authority`: reverified on current main.
  Refuse superseded claim completions before insertion, so a late failure cannot
  replace the latest healthy observation. No schema or dashboard source change.
- `nyxmon-legacy-claim-repair-batched-writes`: reverified on current main.
  Positive row cap shared by reclaim and legacy repair; expired claims first,
  remaining repairs by ID, with full leases from stamping time. Empty reclaimed
  output ends the sweep; subsequent collector ticks continue repair work.

Regression evidence before fixes: latest status was error rather than ok after
replacement success; a cap of three stamped all 1,000 legacy rows; zero/negative
caps were accepted. New tests are synthetic and platform independent. Updated
prior late-result retention assertions and behavior docs/Unreleased notes.

Final local validation passed:

- `just test`: 1,863 Python tests; full Rust workspace suite; live synthetic
  Django-to-Rust notification handoff (one test).
- `just lint`: pre-commit Ruff/format and Rust fmt/clippy, warnings denied.
- `just typecheck`: mypy and Cargo workspace/all-targets check.
- `just rs-deny`: advisories, bans, licenses, sources.
- `uv run python scripts/audit_python.py`: no known vulnerabilities.
- `cargo build --release --locked`: optimized binary build only.
- `git diff --check`.

Review command used the installed claude-review-loop harness with
`--model claude-opus-5-5 --effort high`. It exited 2, `PROVIDER_ERROR`, before
reviewing anything: **Failed to authenticate: OAuth session expired and could
not be refreshed**. Zero review findings means no review, not CLEAN. Original
artifacts are in the workspace's `review-b33-r1/` directory. No valid review
round was consumed. This parking commit/push is the owner's explicit exception
for expired review authentication; it is not approval to merge.

## Resume

Restore Claude Code authentication outside this task, fetch origin, rebase this
branch on current main, and reverify/retest any changed base. Review the complete
parked diff against origin/main (the harness can include committed changes with
`--baseline-ref origin/main`; tell it this is the first valid review). Include
this note and the two slice acceptance criteria as context. Fix required findings
and re-review repair deltas, at most four rounds or until a finding recurs.
Do not merge without a valid clean or accepted suggestion-only review.

Before merging, fetch again; if origin moved, rebase and rerun local checks.
Merge locally into main with `--no-ff`, rerun full local checks before pushing
main, and verify with `git ls-remote`. No Actions, PRs, releases, tags or deploys.
Never delete remote branches.

Then reverify and implement these untouched slices against current main in this
report order (none has yet been checked, implemented, tested or reviewed):

1. B34: `nyxmon-held-recheck-sql-parameter-ceiling` then
   `nyxmon-runner-bounded-check-fanout`.
2. B35: `nyxmon-check-cookie-credential-isolation` then
   `nyxmon-http-attempt-total-deadline`.
3. B36: `nyxmon-json-metrics-finite-clock-domain` then
   `nyxmon-json-metrics-retry-execution-budget`.

Read every full slice and validation/caution section in
`~/workspaces/ws-work-planning/reviews/scout-round84.md`. Stop and park if review
authentication is unavailable again. Do not fall back to another reviewer.

For sandboxed local tools, set UV_CACHE_DIR, UV_TOOL_DIR, UV_TOOL_BIN_DIR and
PRE_COMMIT_HOME under the assigned workspace's `.cache/`; set
PYTHONDONTWRITEBYTECODE=1 when invoking the review harness from `~/projects`.
Review ledger/lock directories were also under workspace `.cache/`.
