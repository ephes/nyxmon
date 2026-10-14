# nyxmon-agent (Rust)

A Rust port of the nyxmon monitoring agent (`start-agent`). The contract is
[`PLAN.md`](PLAN.md); the Python agent in `src/nyxmon` stays the reference
implementation and the rollback target. **Nothing deploys this code yet**:
rsync and git deploys ship `src/` only, and the ops-library role ignores
`agent-rs/` until slice S14.

Status: slice S1 (foundations), step 1 of 2: workspace, CI, recipes, the
`pyjson` module and the fixture pipeline. Step 2 adds the environment
readers, domain types and TLS helpers. The binary only answers `--version`.

## Layout

```
agent-rs/
  Cargo.toml            workspace; release profile (thin LTO, stripped)
  Cargo.lock            committed; every build uses --locked
  rust-toolchain.toml   pinned toolchain (also the MSRV)
  PLAN.md               the implementation plan and parity contract
  TESTMAP.md            Python test -> Rust test coverage, per slice
  crates/
    nyxmon-core/        pure logic (no I/O): pyjson; env readers, domain types (S1 step 2)
    nyxmon-store/       SQLite store (S2)
    nyxmon-checks/      executors (S5-S7); TLS helpers (S1 step 2)
    nyxmon-notify/      Telegram, OpsGate, logging notifier (S8)
    nyxmon-agent/       the binary (S9+)
  fixtures/
    generate.py         writes the fixtures from the Python code
    *.json              generated fixtures (do not edit by hand)
```

## Requirements

- [rustup](https://rustup.rs); it installs the toolchain pinned in
  `rust-toolchain.toml` on first use. Run cargo from inside `agent-rs/` (the
  just recipes do) so the pin applies; a non-rustup `cargo` on `PATH` ignores
  the pin.
- `uv` for the fixture generator (`uv sync` at the repository root).
- Docker only for `just rs-build-linux`; on an ARM Mac the Docker VM needs
  amd64 emulation (Docker Desktop's default; colima: `--vz-rosetta` or
  QEMU binfmt), otherwise the container fails with `exec format error`.

## Commands

From the repository root:

| Command | What it does |
| --- | --- |
| `just rs-test` | `cargo test --workspace --locked` |
| `just rs-lint` | `cargo fmt --all --check` and `cargo clippy --workspace --all-targets --locked -- -D warnings` |
| `just rs-fmt` | format the workspace |
| `just rs-fixtures` | regenerate `fixtures/*.json` from the Python code |
| `just rs-build` | release build for the host (`agent-rs/target/release/nyxmon-agent`) |
| `just rs-build-linux` | release build for macmini (`x86_64-unknown-linux-gnu`, glibc) in a pinned `rust:<ver>-bookworm` container; output under `agent-rs/target/docker-linux-amd64/` |
| `just test` / `just lint` / `just typecheck` | the Python checks followed by `rs-test` / `rs-lint` / `cargo check --workspace --all-targets --locked` |

CI runs the same Rust checks in the `rust` job of `.github/workflows/ci.yml`
and reports the release binary size; the fixture drift test runs in the
`python` job as part of pytest.

## Fixtures: Python is the oracle

`fixtures/generate.py` calls the real Python functions on a fixed corpus
(clocks, nonces and the environment injected) and writes deterministic JSON
(`sort_keys`, `indent=1`, ASCII). Rust tests embed the files with
`include_str!` and must reproduce every case. `tests/unit/test_agent_rs_fixtures.py`
regenerates them in memory and fails when a committed file differs, so a
Python behaviour change cannot merge without `just rs-fixtures`, and the Rust
tests then show what the port must change. Each slice adds its builders to
`GENERATORS` in `generate.py`.

The oracle is the newest CPython 3.13 patch release (what CI's
`setup-python` installs; `uv python install 3.13` locally). Older patch
releases can differ in corner cases, which the drift test then reports.

Current fixtures:

- `pyjson.json`: `json.loads`/`json.dumps` (every option set the agent uses),
  float `repr`, `format(x, "g")`, `round`, `int()`/`float()` parsing, rich
  comparison over JSON values, truthiness, `repr(str)`/`repr(bytes)`.
- `unicode_nonprintable.json`: the code points `str.isprintable()` rejects.
  `nyxmon-core` embeds it, so `repr(str)` escapes exactly what the oracle's
  Unicode database (15.1 for CPython 3.13) escapes.

## Conventions for later slices

- JSON values are `nyxmon_core::PyJson`, never `serde_json::Value`: it keeps
  dict order, int/float distinction, big integers and `NaN` like CPython.
  Store and payload text is `PyJson::to_json_string()` (CPython's
  `json.dumps` defaults).
- `PyJson` parsing, serialisation, comparison, cloning and dropping are
  iterative, so hostile nesting (up to CPython's limit of 9998 levels) cannot
  overflow a thread stack. `PyJson` implements `Drop`: move contents out with
  `into_dict`/`into_list`/`into_string` instead of destructuring by value.
