# Site connectivity: agent workflow record

Summary-safe record of how the site-connectivity feature was planned,
implemented and reviewed on 2026-09-06. No transcripts, prompts or secrets.

## Roles and models

| Role | Model | Invocation |
| --- | --- | --- |
| Orchestrator (context, plan, task split, integration, adjudication) | Claude Fable 5.1 | this session |
| Plan review and code review (independent, fresh context per round, read-only) | GPT-6 Astra | `codex exec --sandbox read-only -m gpt-6-astra`, high reasoning, detached tmux runner with completion sentinel |
| Implementation and repairs | Claude Opus 5 (`claude-opus-5`) | four implementation subagents (persistence, observer, handler/wiring, docs/UI) and five repair subagents |

Availability of both models was verified before the first round; no model
was substituted. Pi was not used.

## Phase 1 - plan (`docs/site-connectivity-plan.md`)

Seven independent review rounds. Findings per round: 15 (9 C / 6 W), 7
(4 C / 3 W), 3 (2 C / 1 W), 1 C, 1 C, 1 C, 1 C. Every finding was accepted.
The last four rounds narrowed one argument (recheck completion for samples
measured before a release) to ever smaller corners; the final repair was a
single parameter on an existing transactional method. The loop was stopped
with that repair carried as a mandatory verification item into the code
review instead of an eighth plan round. The owner approved the plan (v8)
before implementation started.

## Phase 2 - implementation

Tasks T1 (persistence) and T2 (observer) ran in parallel against an
interface fixed in the plan; T3 (handler, delivery retry, notifier,
bootstrap/collector wiring, acceptance tests) followed; T4 (docs,
CHANGELOG, dashboard) last. Subagents received ownership lists, acceptance
criteria and the rule not to commit, deploy or start reviewers. The
orchestrator re-ran every claimed validation on the integrated tree.

## Phase 3 - code review

| Round | Scope | Findings | Outcome |
| --- | --- | --- | --- |
| 1 | full slice | 8 C / 3 W | all accepted; three repair subagents |
| 2 | targeted (11 prior + delta) | 9 resolved, 2 partial; 1 C / 3 W new | all accepted; two repair subagents plus plan amendment |
| 3 | targeted (4 prior + delta) | all resolved; 0 new | CLEAN, cycle closed |

Notable Criticals from the code review: a failed restore could overwrite
persisted incident state; the collector's delivery acknowledgement was not
atomic; a retired undelivered site alert could never be re-attempted; an
unbounded summary message could exceed Telegram's limit forever;
maintenance suppression re-armed the hold budget; a failed probe round at
the release boundary closed the incident; freshness was evaluated per path
instead of per requirement (twice, the second time because a still-down
member contributed a zero watermark); the in-memory store exposed a
half-written completion to the observer thread.

Final validation on the integrated tree: `just test` 602 passed (baseline
353), `just typecheck` clean, `just lint` clean, pre-commit hooks also run
over every untracked file. `npm test` was not required (no JavaScript
changed).

## Lessons

- A design-review loop converges: 15 → 7 → 3 → 1 → 1 → 1 → 1. Once
  successive rounds narrow the same argument, the remaining repair is better
  verified in the code review than by another plan round.
- Fix the shared interface in the plan before parallel implementation; the
  two parallel subagents still needed one direct exchange (frozen-dataclass
  protocol members must be properties) and a rule to freeze their files once
  the integrating task starts.
- Ask repair subagents to prove that each regression test fails against the
  pre-fix code; several did so by reverting hunks into a scratch copy.
- `uvx pre-commit run --all-files` skips untracked files; run the hooks
  explicitly over new files before claiming lint is clean.
- Reviewer reproductions that drive the production wiring (`bootstrap()` plus
  handler over a real SQLite file) found defects that unit tests with fakes
  had missed; the acceptance tests now use that harness.
