# Site connectivity detection - implementation plan

Status: v8, submitted for owner approval (2026-09-06). Nothing in this
document is deployed. Seven independent review rounds were run; every
finding was accepted and folded in. See section 17 for the history and the
stop rationale.

## 1. Problem

Nyxmon runs on `macmini` inside the home network. When the provider
reconnects, the public IPv4 address and the IPv6 prefix change. For several
minutes every check that needs the internet fails at once, although the
monitored services are healthy. Fractal's Tailscale coordination link took
about sixteen minutes to recover after the 2026-09-06 reconnect. During such a
window Nyxmon cannot deliver Telegram messages either, because Telegram uses
the same internet path.

The first mitigation raised Fractal's combined Tailscale check to four
consecutive failed samples at a 300 s interval (roughly 15-20 minutes to the
first alert). That override stays in place and is out of scope here.

## 2. Goals and non-goals

Goals:

- A short internet outage produces no follow-up alert flood.
- Measurements stay truthful: failing results are stored as failures, with a
  visible reason when their notification was held.
- Local and independent failures (disk, service down on the LAN, collector
  health) stay alertable.
- After reconnection, failures that already recovered are never reported after
  the fact. Failures that persist are reported after a bounded grace,
  according to the check's own alert policy.
- A frozen or defective connectivity observer can never suppress alerts
  forever.
- State survives collector restarts and is deduplicated.
- The feature ships disabled and is activated in controlled steps.

Non-goals (explicitly out of scope): an external heartbeat service, router or
provider changes, kernel/MongoDB/Fractal storage work, OpsGate changes beyond
a "no ticket" flag inside Nyxmon, live deployment, commits, ops-control
changes (rollout steps are documented in section 13 instead).

## 3. Current architecture (what the design builds on)

- `handlers.add_check_result` persists every sample and decides notification
  through `_should_notify_check_result`. The decision is a compare-and-swap
  transition on `NotificationState` (`check_notification_state` table), so
  concurrent writers cannot silently overwrite each other. Streak threshold and
  elapsed-time reminders come from `notification_policy`.
- The transition is committed *before* the notifier is called. A failed or
  crashed send is currently lost until the next reminder window.
- `notification_suppression` (maintenance windows) annotates the result with
  `notification_suppressed` and resets the streak.
- `collector_internal` results (expired leases) are alert-inert.
- The collector (`AsyncCheckCollector`) owns persisted, deduplicated
  collector-level incidents in the `collector_incident` table.
  `claim_collector_incident_alert` creates a row *and* claims the first alert
  in one step; there is no silent open today. `_notify_collector_incident`
  returns `True` whenever the notifier does not raise.
- `AsyncTelegramNotifier.async_send` catches every exception and logs it, so
  no caller can currently tell a delivered message from a failed one.
- The `AsyncCheckRunner` buffers a batch's results until every task of the
  batch has finished, so a result can be handled minutes after it was
  measured.
- The dashboard shows `result.data` verbatim on the check detail page; there
  is no incident banner.

## 4. Vocabulary

| Term | Meaning |
| --- | --- |
| path | one observed connectivity dimension: `dns`, `ipv4`, `ipv6` |
| probe round | one concurrent attempt against every target of every observed path |
| path state | `up`, `failing` (unconfirmed), `down` (confirmed), `recovering` (grace after recovery), `unobserved` (no targets configured) |
| site incident | the persisted record that at least one observed path is `down` or `recovering` |
| outage duration | time from the first path's `down_since` to the moment the last path left `down`; grace is *not* part of it |
| release | the moment a path leaves `recovering`: `release_at = recovered_at + grace` |
| held | a failing sample whose notification was deferred because a dependency was `down`/`recovering`, or because the sample was measured before the dependency's release |
| dependency | the paths a check declares it needs (`data.site_dependency`) |
| fresh sample | a sample whose execution was claimed at or after every relevant `release_at` |

## 5. Observer: probing and path state machine

Module: `src/nyxmon/adapters/site_connectivity.py`.

### 5.1 Probing

- Runs as a second task on the collector's portal, started by
  `AsyncCheckCollector` when a `SiteConnectivityObserver` is attached. It does
  not block check collection. There is exactly one probe loop per process; no
  probe is ever issued per check result.
- Every `probe_interval` (default 60 s) one round runs all targets
  concurrently in an `anyio` task group with a per-target timeout
  (default 3 s) and a hard round budget of `timeout + 2 s`
  (`anyio.move_on_after`). A target that does not answer inside the budget
  counts as failed.
- Targets per path:
  - `ipv4`: TCP connect to IPv4 literals (default `1.1.1.1:443`,
    `8.8.8.8:443`, `9.9.9.9:443`). Literals avoid DNS entirely.
  - `ipv6`: TCP connect to IPv6 literals (default `[2606:4700:4700::1111]:443`,
    `[2001:4860:4860::8888]:443`, `[2620:fe::fe]:443`). An empty target list
    marks the path `unobserved` for sites without IPv6.
  - `dns`: `getaddrinfo` through the system resolver for names (default
    `cloudflare.com`, `google.com`, `quad9.net`). Success means at least one
    address of any family came back.
- A round result per path is `ok` if at least one target succeeded and
  `failed` only if every target failed. A single broken target can therefore
  never take a path down (requirement: one dead server, or the Tailscale
  coordination server, is not evidence of an outage).
- The probe function is injected (`ProbeRunner` protocol,
  `async probe(target) -> bool`) and the clock is injectable, so tests drive
  rounds deterministically with `await observer.run_once()`.

### 5.2 Path state machine

```
up         ──(round failed)──────────────────────────▶ failing
failing    ──(round ok)──────────────────────────────▶ up          (no incident, no grace)
failing    ──(down_after_failures consecutive fails)─▶ down        (down_since = now)
down       ──(round ok)──────────────────────────────▶ recovering  (recovered_at = now, release_at = now + grace)
recovering ──(round failed)──────────────────────────▶ down        (no re-confirmation: the incident is
                                                                    established; down_since is kept,
                                                                    recovered_at/release_at are cleared)
recovering ──(now >= release_at)─────────────────────▶ up          (last_release_at = release_at)
```

- `down_after_failures` default 2, so a path is confirmed down after roughly
  60-120 s. Holding starts only at `down`, never at `failing`: a single bad
  round must not defer every dependent alert for a full grace window.
- A failed round while `recovering` returns the path to `down` immediately.
  Holds continue, the incident stays open, and the next successful round
  starts a fresh grace. Closing therefore requires one uninterrupted grace of
  successful rounds after the last failure.
- `last_release_at` is kept per path until the next `down`. It is what lets
  the handler recognise a sample measured before the release (section 7.3).
- The snapshot exposed to the handler is immutable
  (`SiteConnectivitySnapshot`): per-path `state`, `down_since`,
  `recovered_at`, `release_at`, `last_release_at`; global `observed_at`
  (epoch of the last completed round), `mode`, and `incident_id`.

### 5.3 Persisted lifecycle and restart continuity

The observer's whole lifecycle lives in **one** row of `collector_incident`,
key `site:connectivity`. Every transition is a single atomic payload write
(`open_collector_incident` creates the row silently or replaces its payload;
`set_collector_incident_payload` replaces it), so no transition can be half
persisted. Once created the row is kept permanently (phase `idle` between
outages) because it carries the per-path release watermarks
(`last_release_at`) that the freshness rule needs for as long as any
execution claimed before a release can still deliver a result; a permanent
small row is simpler and safer than reasoning about when that can no longer
happen. The site incident does not use `claim_collector_incident_alert`; its
alert bookkeeping is part of the payload.

```json
{
  "incident_type": "site_connectivity",
  "version": 1,
  "phase": "active",                       // idle | active
  "incident_id": 1757123456,               // first down_since of the active outage; stable id
  "observed_at": 1757125000,
  "paths": {
    "ipv4": {"state": "recovering", "down_since": 1757123456, "recovered_at": 1757124900,
             "release_at": 1757125800, "last_release_at": 0},
    "ipv6": {"state": "down", "down_since": 1757123500, "last_release_at": 0},
    "dns":  {"state": "up", "last_release_at": 1757100000}
  },
  "ongoing": {"incident_id": 1757123456, "attempt_at": 1757124400, "attempts": 3,
              "delivered": false, "retired": false, "last_alert_at": 0},
  "summaries": [
    {"incident_id": 1757110000, "started_at": 1757110000, "ended_at": 1757112600,
     "paths": {"ipv4": {"down_since": 1757110000, "recovered_at": 1757112600}},
     "ongoing_delivered": false, "held_at_close": 7, "attempt_at": 1757112700, "attempts": 2}
  ],
  "recheck": {"pending": true, "release_at": 1757125800}
}
```

On start the observer restores every path state and timestamp from the
payload and sets the snapshot's `observed_at` to the persisted value, not to
"now". If that value is stale (section 7.5) nothing is held until the first
fresh round completes, which happens within seconds. A restart in the middle
of a grace therefore neither releases holds early nor restarts the grace: the
persisted `release_at` is honoured. Alert bookkeeping (`ongoing`,
`summaries`), `last_release_at` per path and the `recheck` work item are
restored as well, so an alert attempted before the restart is not attempted
again before its retry is due, a pending summary is retried, and a recheck
that had not finished continues.

## 6. Site incident lifecycle and notifications

All state is the single row of section 5.3. Phases and alerts:

- **Open (silent).** When the first path enters `down`: `phase = active`,
  `incident_id = down_since`, written with `open_collector_incident`. No
  notification: a short outage must produce nothing.
- **Ongoing alert (intent before I/O).** While at least one path is `down`
  (not merely `recovering`) and `now - incident_id >= site_incident_notify_after`
  (default 900 s) and no attempt is recorded for this `incident_id`, or the
  last delivered alert is older than `site_incident_reminder` (default 6 h):
  the observer first writes `ongoing = {incident_id, attempt_at: now,
  attempts + 1, delivered: false}` (one payload write), then sends. Status
  `error`, `error_type = site_connectivity_outage`, message names the down
  paths and their `down_since`. On success it writes `delivered: true,
  last_alert_at: attempt_at`. On failure or crash the intent stays;
  a retry is due when `now - attempt_at >= 60 s` **and a path is still
  `down`**. During a partial outage this delivers over the working path;
  during a full outage it fails until reconnection.
- **Retire on recovery.** In the same payload write in which the last `down`
  path becomes `recovering`, an undelivered ongoing alert is marked
  `retired: true`. Retired alerts are never retried, so a Telegram that
  becomes reachable during the grace cannot deliver the obsolete "outage
  ongoing" message. A path that flaps back to `down` does not un-retire; a
  fresh attempt is made only when the reminder cadence allows it.
- **Close.** When every observed path is `up` (all releases elapsed), the
  outage duration is `last_recovered_at - incident_id`. If it is at least
  `site_incident_notify_after`, or an ongoing alert was delivered, a summary
  record for this `incident_id` is appended to `summaries` in the same write
  that sets `phase = idle` and `recheck = {pending: true, release_at}`. Because
  the record is keyed by `incident_id`, re-running the close after a crash
  cannot append it twice. An outage shorter than the threshold without a
  delivered ongoing alert leaves no summary record and only a log line.
- **Summary delivery (intent before I/O, size-bounded).** While `summaries`
  is non-empty, the observer sends one message per round covering the oldest
  pending records, in order and stopping at the first record that is not yet
  individually eligible (`attempt_at == 0` or `now - attempt_at >= 60 s`),
  that fit the bounds `SITE_SUMMARY_MAX_RECORDS`
  (5) and `SITE_SUMMARY_MAX_CHARS` (3500, below Telegram's 4096 limit): it
  writes `attempt_at: now` on the covered records only, sends (`error_type =
  site_connectivity_summary`, status `warning`, `opsgate_ticket: false`), and
  on success removes only the covered records; the next batch goes out on the
  next round. A single record that alone exceeds the character bound has its
  path list capped and is truncated as a last resort, never dropped. On
  failure or crash the covered records keep their `attempt_at` and are
  retried when their own deadline elapses; records appended later do not
  make an earlier failed batch eligible sooner. A new outage opening while
  records are pending keeps them (`phase = active` again); when it closes,
  its own record is appended.
- **Idle.** After close the row stays with `phase = idle` (section 5.3). The
  observer never deletes it.
- **Delivery guarantee.** Both site messages are at-least-once: intent is
  durable before the send and cleared after a successful send. A crash
  between those steps, or an ambiguous outcome (Telegram accepted the request
  but the response was lost, which the notifier reports as failure), causes
  a repeat. Repeats are rate-bounded by the 60 s retry cadence and by the
  requirement that a path is still down (ongoing) or a record is pending
  (summary); there is no fixed count bound. Section 8 states the same for
  per-check alerts.
- Summary content: for each record the outage duration, each affected path
  with `down_since` and `recovered_at`, whether an ongoing alert had been
  delivered, the number of dependent checks still held at close
  (`count_held_checks` at close time), and a note that those checks are
  rechecked.

A full 40-minute outage therefore produces one Telegram message after
reconnection, unless a delivery outcome was ambiguous. A 3-minute reconnect
produces none, even though the row lives for 3 min + grace + recheck.

### 6.1 No remediation tickets for resolved events

`AsyncTelegramNotifier` honours `result.data["opsgate_ticket"] is False` and
skips ticket creation regardless of severity and of
`OPSGATE_SUBMIT_INCLUDE_WARNINGS`. `_notify_collector_incident` gains
`status` and `opsgate_ticket` parameters; existing callers keep `error` and
ticket creation. The OpsGate key for the ongoing alert stays
`nyxmon-collector-site:connectivity`.

### 6.2 Existing collector incidents: intent at claim time

The stale-lease and execution-paused incidents keep using
`claim_collector_incident_alert`, but the claim itself now records intent:
when it grants an alert it merges `{"delivery_pending": true,
"delivery_attempt": alert_count}` into the payload **inside the claim
transaction**. The two keys are repository-owned: every payload write made
by `claim_collector_incident_alert` (granted or not) and by
`open_collector_incident` carries the existing values of these keys forward
unless the claim grants a new alert, so a non-granting refresh of the
stale-batch payload (a second reclaimed lease before the retry is due) cannot
erase a pending intent. Only `_record_incident_send(key, sent=True, attempt)`
clears them, and only when `delivery_attempt` still equals the acknowledged
attempt; a failed send leaves them. A crash between claim and send therefore
leaves a durable retry intent, which `_rehydrate_incident_retry_state`
restores after restart, instead of a recently-alerted row that resolves
without ever paging.

## 7. Per-check hold rule

### 7.1 Declaring dependencies

`data.site_dependency` on a check, validated in the same fail-safe style as
`notification_policy` (malformed values warn once per check and fall back to
"unclassified"):

```json
{"site_dependency": "internet"}
{"site_dependency": {"requires": ["dns", ["ipv4", "ipv6"]]}}
{"site_dependency": {"requires": ["ipv6"]}}
{"site_dependency": "none"}
```

- absent, `"none"`: unclassified. Behaviour is byte-for-byte the current one.
- `requires` is a list of requirements. Each requirement is a path name or a
  list of alternative path names (any-of group). The check is affected when
  **any requirement is unmet**. A single-path requirement is unmet when that
  path is `down`/`recovering`. An any-of group ignores `unobserved` members
  and is met when at least one *observed* member is `up` **or `failing`**
  (an unconfirmed failure never holds, exactly as for a single path); it is
  unmet only when every observed member is `down`/`recovering`. A group
  whose members are all unobserved is met. On an IPv4-only site (empty IPv6
  targets) `["ipv4", "ipv6"]` therefore behaves exactly like `ipv4`, so a
  confirmed IPv4 outage still holds `internet` checks. A single-path requirement on an
  `unobserved` path is met and can never hold; the observer logs one warning
  per such check at startup so the misclassification is visible.
- `"internet"` is shorthand for `{"requires": ["dns", ["ipv4", "ipv6"]]}`.
  This matches the installed stack: HTTP and TCP checks connect through
  `anyio.connect_tcp`, which uses Happy Eyeballs, so a dual-stack host by
  name keeps working while only one address family is broken.
- Guidance by executor (documented in `docs/site-connectivity.md`):
  - HTTP/TCP/SMTP/IMAP to a dual-stack host by name: `"internet"`.
  - Target given as an IPv4 literal: `{"requires": ["ipv4"]}`; IPv6 literal:
    `{"requires": ["ipv6"]}`; IPv4-only host by name:
    `{"requires": ["dns", "ipv4"]}`.
  - DNS checks with an explicit `dns_server` IP: the path of that server's
    family, not `dns`. DNS checks through the system resolver: `"internet"`.
  - LAN-only targets (Fractal's LAN metrics endpoint itself): unclassified,
    unless the payload it reports is internet-dependent, in which case the
    operator decides; Fractal's combined check is classified `"internet"`
    because its failing conditions during a reconnect are the Tailnet ones.

Suppression is explicit per check. Nothing is inferred from URLs or from other
checks failing at the same time.

### 7.2 Hold decision

`site_state.hold_reason(dependency, claim_started_at, now)` returns `None` or
a reason dict. It is `None` when the mode is not `enforce`, the snapshot is
stale, the check is unclassified, or every requirement is met by a fresh
sample. It is a reason when

- a requirement is unmet (a required path is `down` or `recovering`; for an
  any-of group, every observed member is), or
- every requirement is met now but `claim_started_at` precedes a
  requirement's `usable_since` watermark: the earliest `last_release_at`
  among the requirement's observed members that are usable now (a member
  that never released contributes 0; members still `down`/`recovering` do
  not contribute). The sample was measured before the requirement became
  usable; it is not fresh.

Evaluated in `_should_notify_check_result` after the OK, `collector_internal`
and maintenance-suppression branches and before the immediate/threshold
logic:

```
hold = site_state.hold_reason(dependency, check.claim_started_at, now)
exhausted = state.held_since > 0 and now - state.held_since >= max_hold_seconds
if hold is not None and not exhausted:
    result.data["site_connectivity"] = {"held": True, **hold}
    next = state.evolve(failure_count + 1, first_failure_at or now,
                        held_since = state.held_since or now)
    return (False, state, next)                      # nothing else changes
# not held: ordinary policy follows
if hold is None and snapshot is fresh and dependency recovered (every requirement met by a fresh sample):
    next.held_since = 0                              # dependency recovered → hold budget re-armed
else:
    next.held_since = state.held_since               # stale, exhausted, or unclassified: untouched
```

Key properties:

- The streak keeps counting while held. When the check yields a fresh failing
  sample after the release, its own `notification_policy` decides: the held
  samples count toward `consecutive_failures`, and an already-open incident
  follows its reminder clock. A check whose held samples already reached the
  threshold alerts on the first fresh sample; an hourly check with threshold
  four that accumulated one held sample needs three more hourly samples, as it
  would without the outage. This is the deliberate meaning of "reliably
  according to policy". Maintenance-suppressed samples keep resetting the
  streak as today.
- The result is stored with its real status; `site_connectivity` is
  additional metadata, like `notification_suppressed`.
- `held_since` is the first held sample of the current hold. It is reset only
  by an OK sample (whole state cleared) or by a failing sample evaluated while
  the dependency is observed **recovered** with a fresh snapshot. A bypass
  because the snapshot is stale or because the hold is exhausted leaves
  `held_since` untouched, so the 3-hour budget cannot be re-armed while the
  outage persists. Once exhausted, the check follows ordinary policy for the
  rest of that incident, including delivery retries (section 8).
- **Conflict exhaustion.** `add_check_result` gives up the compare-and-swap
  after `MAX_NOTIFICATION_STATE_ATTEMPTS` conflicts and persists the result
  without a transition. That fallback must not lose a hold: when the sample
  would have been held, the handler passes `hold_marker=now` to
  `persist_check_result(check, result, None, complete_check=..., hold_marker=now)`.
  Inside the **same transaction** that completes the claim and inserts the
  result, and only when the completion was applied (a stale claim's result is
  stored but marks nothing), the store executes
  `held_since = hold_marker WHERE check_id = ? AND held_since = 0`
  (inserting the state row if absent) before commit. The recheck obligation
  is therefore atomic with the completion, so the observer's count-first
  ordering cannot observe an idle, unheld row for that claim, and a crash
  cannot separate the two writes. The streak bookkeeping of the exhausted
  transition stays dropped as today. (The fallback is defensive: results of
  one check are serialised by its processing claim, so three consecutive
  conflicts do not occur in normal operation.)
- In `observe` mode the metadata is written with `"held": false` and the
  decision is unchanged.
- Reminders for an already-open incident are held the same way.
- `force_notification` (collector-internal path) is never held.
  `notification_immediate` samples are held like any other failing sample of
  a classified check; stock code does not produce them.

### 7.3 Freshness of samples

The runner hands a batch's results to the handler only after every task of
the batch finished. A failing sample of a fast check can therefore be handled
minutes after it was measured, possibly after the release, while a slower
check in the same batch was still running. Such a sample must not alert:
`Check.claim_started_at` (the processing claim time, already preserved for
late-result fencing) is compared with the dependency's `last_release_at`. A
sample claimed before the release is held and marked
`{"held": true, "reason": "measured_before_release"}`; a fresh execution is
requested through the recheck below.

### 7.4 Recovery recheck

A recheck work item (`recheck.pending`) is opened in the same write that
closes an outage, or, for staggered recoveries, whenever a path is released
while others are still down. It stays pending while **either** a held
dependent with a usable dependency exists **or** any execution claimed
before the release is still in flight: `checks.count_processing_claims_before_async(release_at)`
counts `health_check` rows with `status = 'processing' AND
processing_started_at < release_at`. Such a claim ends in exactly two ways:
its result is handled (then the sample is held as `measured_before_release`
and the check joins the held set), or its lease is reclaimed (then
`processing_started_at` is rewritten and a later result from the old claim
fails the existing completion fence and cannot change notification state).
The condition is therefore exact and independent of lease lengths, effective
lease extensions, batch deadlines and result buffering. The release
watermarks themselves are permanent (section 5.3), so a not-fresh result is
recognised even if it arrives after the item completed. On **every** observer
round while the item is pending, the coordinator reschedules the dependents
whose complete dependency is usable again. **Ordering invariant:** the
in-flight count is read *before* the held set. A pre-release claim that
completes between the two reads was counted (so the item stays pending); one
that completed before the count is visible to the held query (the result
transaction that idles the check and writes `held_since` is atomic). The
two reads therefore never both miss the same check:

```
in_flight = checks.count_processing_claims_before_async(release_at)   # FIRST
held = checks.list_held_checks_async()                                # SECOND: held_since > 0, joined with check data
due  = [c for c in held
        if snapshot.dependency_usable(resolve_site_dependency(c.data))   # every requirement met now
        and c.status == "idle" and not c.disabled and c.next_check_time > now]
checks.reschedule_checks_async([c.check_id for c in due], run_at=now)
pending = in_flight > 0 or any(c for c in held if dependency usable)
```

- A held check leaves the set when its next evaluated sample is fresh: OK
  clears the state, a fresh failing sample resets `held_since` (section 7.2).
  A check that was `processing` at release time returns to idle with a
  not-fresh held sample and is picked up on the next round.
- A check whose dependency is still blocked by another requirement (for
  example `dns` still down after `ipv4` released) is **not** rescheduled and
  keeps its normal interval; it becomes due when its last requirement is
  released. The set of usable held checks therefore shrinks monotonically and
  the item completes.
- If the reschedule query fails, the next round repeats it; the work item is
  persisted, so a restart continues it (section 5.3) with the
  `last_release_at` values it needs for freshness.
- A late result that creates a new hold after the item completed (possible
  only if it was claimed before the release, which the count above excludes)
  cannot happen; if a hold nevertheless appears while idle (for example a
  path flapped through a short `down` that produced no release), the next
  release opens a new item. The per-round cost is two small queries.
- Completion bound: claims are batched (`NYXMON_CHECK_BATCH_SIZE`, default 5)
  and executed one batch per collector iteration, so N held checks are
  rechecked within roughly N / batch size iterations plus their runtime; with
  the homelab's 60 checks and mostly sub-10 s runtimes this is a few minutes.
  The plan documents this bound; it does not promise one iteration.

### 7.5 Observer freshness and failure modes

- The handler reads an in-memory snapshot (replaced atomically). If
  `now - observed_at > 3 × probe_interval` the snapshot is stale:
  `hold_reason` returns `None`, `held_since` is left untouched, and one
  warning is logged per staleness episode. A frozen observer therefore stops
  holding within about three minutes.
- If the observer task raises, the exception is logged and the loop continues
  on the next interval. If the process dies, restart continuity (5.3) applies.
- If the incident store fails during a round, the in-memory state still
  updates and persistence is retried next round. The handler never depends on
  the store for hold decisions.

### 7.6 Mode matrix

| behaviour | `off` (default) | `observe` | `enforce` |
| --- | --- | --- | --- |
| probe task, snapshot | no | yes | yes |
| incident rows, lifecycle persistence | no | yes | yes |
| `site_connectivity` metadata on failing results of classified checks | no | `held: false` | `held: true/false` |
| holding notifications | no | no | yes |
| recovery recheck / reschedule | no | no | yes |
| site ongoing alert and summary | no | yes (so the observer's judgement can be verified) | yes |
| per-check delivery retry (section 8) | independent knob, default off | same | same |

Rollback (`enforce` → `off`): the observer is not started; the row stays
until the next enable, where an `active` phase is restored only if its
`observed_at` is fresh (otherwise the active outage is set to `idle` with a
log line, keeping pending summaries, the recheck item and the release
watermarks; the row itself is never deleted). Held streaks are no longer held:
their next failing sample follows ordinary policy, which is the intended
"alert normally" rollback. Pending summaries are delivered on the next
enable.

## 8. Delivery failures and retry (per check)

Unclassified checks (a local disk failure, for example) keep alerting during
an outage. Today that alert is attempted once after the transition was
committed; a failed or crashed send is lost for six hours. New behaviour,
active only when `NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS > 0`:

- `NotificationState` gains `attempt_seq` (monotonic, never reset, survives
  `cleared()` and `with_streak_reset()`) and `attempt_at` (epoch of the
  current unacknowledged attempt, `0` when none).
- Whenever the transition decides `should_notify` (threshold, reminder,
  immediate, retry), it also sets `attempt_seq += 1` and `attempt_at = now`
  **inside the same compare-and-swap commit**, before any I/O. The intent is
  therefore durable before the send; a crash after the commit leaves
  `attempt_at > 0`.
- After the notifier returns anything other than `False`, the handler
  acknowledges with `checks.acknowledge_notification_attempt(check_id,
  attempt_seq)` which executes `SET attempt_at = 0 WHERE check_id = ? AND
  attempt_seq = ?`. A row that an intervening OK cleared or a newer attempt
  advanced does not match, so nothing is overwritten. On `False` nothing is
  written; the intent stays pending.
- On the next failing sample: if retry is enabled, `attempt_at > 0`,
  `now - attempt_at >= retry_seconds`, and the sample is not held, the alert
  is sent again (`should_notify = True`) irrespective of the reminder window.
  This is at-least-once: a crash between send and acknowledgement, or an
  ambiguous outcome (Telegram accepted the request, the response was lost,
  the notifier reports failure), causes a repeat on the next eligible sample.
  Repeats are rate-bounded by `retry_seconds` and end with the first
  acknowledged send or the first OK sample; there is no fixed count bound.
  This is the plan's answer to "no lost incidents" versus "no duplicates":
  loss is prevented, duplicates are confined to uncertain outcomes and
  documented (section 14).
- An OK sample clears the marker together with the rest of the state: a
  failure that recovered before delivery was possible is not reported after
  the fact. Its result history stays visible in the dashboard.
- `Notifier.notify_check_failed` returns `bool | None`.
  `AsyncTelegramNotifier` returns `False` when the Telegram request failed
  (OpsGate outcome does not count); `LoggingNotifier` returns `True`; `None`
  (custom notifiers, mocks) is treated as delivered. Both Telegram wrappers
  (sync and async) propagate the value. `_notify_collector_incident` treats a
  literal `False` as failure, which makes the existing `delivery_pending`
  retry of collector incidents work with the real notifier for the first
  time.
- The acknowledgement is written after every successful send **regardless of
  the knob**; the knob only gates retry eligibility. With the knob at `0`
  (default) a failed send therefore behaves exactly as today (lost until the
  reminder), and enabling the knob later cannot resend alerts that were
  already delivered while it was off. The rollout enables the retry
  explicitly. Immediate (`notification_immediate`) sends record intent and
  acknowledge the same way.

## 9. Persistence and migration

`check_notification_state` gains three `INTEGER NOT NULL DEFAULT 0` columns:

| column | meaning |
| --- | --- |
| `held_since` | epoch of the first held sample in the current hold, `0` if none |
| `attempt_seq` | monotonic counter of external notification attempts for this check |
| `attempt_at` | epoch of the current unacknowledged attempt, `0` if none |

- `NotificationState` gets the three fields; `from_row` pads missing columns;
  `NOTIFICATION_STATE_COLUMNS`, the select and the upsert are extended. The
  compare-and-swap keeps comparing whole records. `cleared()` and
  `with_streak_reset()` carry `attempt_seq` over.
- Worker-side idempotent upgrade in `_upgrade_notification_state_schema`
  adds the columns inside the existing single transaction. No backfill is
  needed (all default to 0); the streak-adoption backfill remains keyed to the
  columns it actually added, and the log line lists the columns added.
- Django migration `0013_site_connectivity_state` follows the
  `SeparateDatabaseAndState` pattern of 0012: `RunPython` adds the columns if
  absent, model state adds the fields. Both orders (migrate first or worker
  first) are safe; `tests/dashboard/test_notification_state_migration.py`
  gets a 0012 → 0013 case in both orders.
- The `collector_incident` table is reused unchanged.
- In-memory store mirrors every new field and method.

## 10. Repository interface (fixed before T1/T2 start)

```python
class CheckRepository(Protocol):
    async def list_held_checks_async(self) -> list[HeldCheck]:
        """Checks whose notification state has held_since > 0, with data/status/next_check_time."""
    async def reschedule_checks_async(self, check_ids: list[int], *, run_at: int) -> int:
        """Pull idle, enabled checks forward: next_check_time = run_at where later. Returns rows changed."""
    def acknowledge_notification_attempt(self, check_id: int, attempt_seq: int) -> bool:
        """attempt_at = 0 WHERE attempt_seq matches; returns whether a row changed."""
    def count_held_checks(self) -> int: ...
    async def count_processing_claims_before_async(self, epoch: int) -> int:
        """Number of health_check rows with status='processing' and processing_started_at < epoch."""

class RepositoryStore(Protocol):
    def persist_check_result(self, check, result, notification_transition, *, complete_check: bool = True,
                             hold_marker: int | None = None) -> bool:
        """As today; when hold_marker is given (only used with notification_transition=None) and the
        completion was applied, set held_since = hold_marker where it is still 0, in the same transaction."""
    def open_collector_incident(self, incident_key: str, *, now: int, payload: dict) -> CollectorIncident:
        """Create the row silently (opened_at=now, last_alert_at=0, alert_count=0) or replace its payload. Atomic."""
```

`claim_collector_incident_alert` changes in one respect (section 6.2): when
it grants an alert it merges `delivery_pending: true` and
`delivery_attempt: <alert_count>` into the payload it writes, atomically.
`set_collector_incident_payload`, `close_collector_incident` and
`get_collector_incident` are unchanged; async variants follow the existing
`_..._async` convention that the collector already discovers via `getattr`.

## 11. Configuration

Environment (validated like the existing reliability knobs: invalid values
warn once and use the default):

| variable | default | range / notes |
| --- | --- | --- |
| `NYXMON_SITE_CONNECTIVITY_MODE` | `off` | `off`, `observe`, `enforce` |
| `NYXMON_SITE_PROBE_INTERVAL_SECONDS` | `60` | 15-600 |
| `NYXMON_SITE_PROBE_TIMEOUT_SECONDS` | `3` | 1-10, per target |
| `NYXMON_SITE_PROBE_IPV4_TARGETS` | `1.1.1.1:443,8.8.8.8:443,9.9.9.9:443` | comma separated `ip:port`; empty = unobserved |
| `NYXMON_SITE_PROBE_IPV6_TARGETS` | `[2606:4700:4700::1111]:443,[2001:4860:4860::8888]:443,[2620:fe::fe]:443` | empty = unobserved |
| `NYXMON_SITE_PROBE_DNS_NAMES` | `cloudflare.com,google.com,quad9.net` | empty = unobserved |
| `NYXMON_SITE_DOWN_AFTER_FAILURES` | `2` | 1-10 consecutive failed rounds |
| `NYXMON_SITE_RECOVERY_GRACE_SECONDS` | `900` | 60-3600 |
| `NYXMON_SITE_MAX_HOLD_SECONDS` | `10800` | 600-86400; hard bound per check and hold |
| `NYXMON_SITE_INCIDENT_NOTIFY_AFTER_SECONDS` | `900` | 60-86400 |
| `NYXMON_SITE_INCIDENT_REMINDER_SECONDS` | `21600` | 60-2592000 |
| `NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS` | `0` (off) | 0, or 60-3600; per-check send retry |

Snapshot staleness is derived (`3 × probe interval`), not configured.

Per check: `data.site_dependency` as in section 7.1.

No manual override switch is added. The existing per-check
`notification_suppression` covers planned maintenance; a global time-boxed
switch is deferred until a concrete need appears (section 14).

## 12. Timing semantics (worked examples, 300 s checks, threshold 2)

- **3-minute reconnect**: failure at t=0. Path `down` at t≈1.5 min
  (`down_since`). Dependent check samples at t=0.5 (count 1, not held: not
  yet confirmed) and t=5.5 (held, count 2). Path `recovering` at t=3
  (`recovered_at`), `up` at t=18 (`release_at`). Outage duration
  = 3 − 1.5 = 1.5 min < 15 min, no ongoing alert was ever eligible (eligibility
  needs a path `down` for 15 min). Recheck at t=18: OK → cleared. Incident
  closed silently at t=18. Zero notifications.
- **40-minute outage, one service still broken afterwards**: ongoing alert
  intent written at t=16.5 (15 min after `down_since`), send fails, retried
  every 60 s while down, all failing. Reconnect at t=40: last path
  `recovering`, ongoing alert retired in the same write; no more retries even
  though Telegram is reachable again at t=40. Release at t=55: phase `idle`,
  summary record appended and recheck opened in one write; summary sent
  once; held checks rescheduled. The broken service's fresh sample at
  t≈55-57 has count ≥ 2 and alerts once. Recovered services send OK and are
  cleared. Total: one summary, one genuine alert (barring an ambiguous
  delivery outcome, section 14).
- **Flap during grace**: reconnect at t=40, failed round at t=47 → `down`
  again, holds continue, ongoing alert not re-claimed (reminder clock);
  success at t=49 → `recovering`, release at t=64. Outage duration counts to
  t=49.
- **IPv6-only outage for 3 hours**: `ipv6` down, `ipv4`/`dns` up. Checks
  requiring `ipv6` are held; `internet` checks are **not** held (Happy
  Eyeballs keeps them working); an `internet` check that genuinely fails
  over IPv4 alerts normally. The ongoing alert at 15 min is delivered over
  IPv4 and names `ipv6`; a reminder follows after 6 h; a summary follows on
  recovery because an ongoing alert was delivered.
- **Buffered failure that was never held before the release**: the only
  dependent check is claimed at t=54 (during the grace), fails, and its
  result is buffered behind a 25-minute batch member. At the release (t=55)
  no check is held, but the claim from t=54 is still `processing`, so the
  recheck item stays pending. The late result at t≈79 is held as
  `measured_before_release` (the watermark is permanent); the next observer
  round reschedules the check; its fresh sample decides.
- **Fast check in a slow batch**: a 60 s check fails at t=53 during the grace;
  its batch also holds a check that takes 4 minutes; the failing result is
  handled at t=57, after the release. Its claim started at t=53 <
  `last_release_at` = 55, so it is held as `measured_before_release` and
  rescheduled by the next observer round; its fresh sample at t≈58 decides.
- **Defective observer (targets firewalled, internet fine)**: every dependent
  alert is delayed at most `max_hold_seconds` (3 h), then follows ordinary
  policy and delivery retry. The ongoing site alert at 15 min is delivered and
  reveals the misconfiguration.
- **Frozen observer**: snapshot stale after 3 min; holds stop; `held_since`
  untouched; alerts proceed normally.
- **Restart at t=20 of a 40-minute outage**: the new process restores every
  path with its timestamps and `observed_at`=t≈19.5; the first fresh round at
  t≈20.1 confirms; the ongoing attempt from t=16.5 is restored as
  undelivered and retried while down; the summary is sent at close.
- **Restart during grace at t=50**: `release_at` = 55 restored; holds continue
  until 55; recheck and summary happen as without the restart.
- **Second outage while a summary is pending**: A's summary record is
  retried every 60 s; outage B sets `phase = active` again with its own
  `incident_id`, keeping A's record. When B closes while A's record is still
  pending, B's record is appended; the next successful send reports both and
  removes both records.
- **Restart right after close, recheck unfinished**: the row is still
  present (`recheck.pending`), `last_release_at` per path is restored; an
  idle held hourly check is rescheduled on the first round after restart; a
  check that was processing is picked up once it is idle again.
- **Crash between summary send and acknowledgement**: the record is still
  pending after restart and is sent again. Documented at-least-once.
- **Local disk failure during a full outage** (unclassified check, retry
  enabled at 300 s): alert at t=10 (intent persisted, send fails,
  `attempt_at` stays). Next failing samples at t=15, 20, ... retry every
  ≥ 300 s, each failing. First failing sample after reconnection (t≈45)
  delivers; acknowledgement clears `attempt_at`. If the disk recovered at
  t=30, its OK sample cleared everything and nothing is sent.

## 13. Rollout (documented; ops-control is not changed by this work)

1. Release Nyxmon with migration `0013_site_connectivity_state`; the
   deployment role runs `migrate` before the worker starts, and the worker's
   own idempotent upgrade covers the other order.
2. Set `NYXMON_SITE_CONNECTIVITY_MODE=observe` and
   `NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS=300` in the macmini worker
   environment (the ops-control variable that renders the mode-0600
   environment file for the `nyxmon-monitor` unit). Watch the worker log and
   the dashboard banner across a few days; verify that path states match
   reality and that no false `down` appears. Observe mode already sends the
   ongoing/summary site messages, which is the verification signal.
3. Classify checks in the playbooks that upsert them:
   `playbooks/tailscale/deploy.yml` (Fractal's combined check → `"internet"`),
   external HTTP/DNS/mail checks → `"internet"` or explicit requirements per
   section 7.1; LAN-only checks stay unclassified. A deployment rewrites the
   whole `data` blob, so the key must live in the playbook, not be edited in
   the database.
4. Switch to `enforce`.
5. Keep Fractal's `consecutive_failures: 4`. Revisit it separately once the
   site policy has been observed through a real reconnect.

Rollback: set the mode to `off` (section 7.6). The new columns are inert when
unused.

## 14. Open risks and deferred items

- At-least-once delivery: a crash between a successful send and its
  acknowledgement, or an ambiguous outcome (request accepted, response lost),
  yields a repeated message (per-check, ongoing, or summary). Telegram offers
  no idempotency key, so exactly-once is not achievable and no count bound
  can be promised; repeats are rate-bounded (60 s for site messages,
  `retry_seconds` per check) and stop at the first acknowledged send or OK
  sample. The plan chooses loss-prevention over duplicate-prevention. This
  is a decision for the owner to confirm.
- A partial outage that leaves Telegram reachable but breaks the probe
  targets (or the reverse) is possible in principle; three independent
  targets per path, the ongoing alert at 15 min, and the max-hold bound limit
  the damage.
- Holding only from `down` (not `failing`) means the first 1-2 minutes of an
  outage are unprotected. A check with a pre-existing failure, a due reminder,
  or `consecutive_failures: 1` can page in that window. Documented; the
  two-sample default and 300 s intervals make it rare.
- The grace (15 min) delays reporting a genuinely broken service by that
  much after reconnect, plus the recheck bound of section 7.4. That is the
  requested tolerance.
- Per-check delivery retry costs up to 10 s of notifier time per failing
  sample per retry interval while Telegram is unreachable, inside the
  existing per-check result-handling allowance of 60 s.
- No manual time-boxed override switch; deferred until needed.
- Dashboard changes are limited to a banner and a "held" indicator rendered
  from existing tables; no JavaScript changes, so `npm test` is not affected.

## 15. Tests (deterministic, acceptance mapping)

New files: `tests/unit/test_site_connectivity_observer.py`,
`tests/unit/test_site_connectivity_holds.py`,
`tests/unit/test_notification_delivery_retry.py`,
`tests/adapters/test_site_connectivity_persistence.py`, extension of
`tests/dashboard/test_notification_state_migration.py` and
`tests/unit/test_collector_incidents.py`.

| acceptance criterion | test |
| --- | --- |
| short reconnect, no follow-up alerts | full 3-minute timeline through close; zero sends; duration excludes grace; threshold boundary (duration = notify_after − 1 s and + 1 s) |
| long outage, deduplicated site incident | 40 min; ongoing intent written before the send, fails, retried while down, retired on recovery; a notifier that *succeeds* during the grace delivers nothing; one summary at close; no duplicate across rounds or restart; "accepted but response lost" (notifier returns False after a real send) repeats once and is documented |
| one probe target failing | path stays `up`, nothing held |
| DNS-only / IPv6-only partial outage | `requires` semantics incl. any-of groups; `internet` not held on IPv6-only; `["ipv6"]` held; IPv4-only site (empty IPv6 targets) with `ipv4` down and DNS cached-OK holds `internet` checks, and with `ipv4` merely `failing` holds nothing (same as the single-path form); single requirement on an unobserved path never holds and warns once; genuine application failure over the working family alerts |
| local disk failure during outage | unclassified check alerts, intent persisted before send, send fails, retry after interval, delivered after reconnect only if still failing; knob `0` keeps today's behaviour |
| stale observer, restart during incident | stale snapshot stops holding without touching `held_since`; restart restores `down`, `recovering` with its `release_at`, and `observed_at`; no second ongoing claim |
| recovered checks, no late alerts | OK samples after release clear state, no send; buffered sample measured before release is held and rechecked; a buffered failure of a check that was never held before the release is still held and rechecked because the item stays pending while a pre-release claim is in flight (also with an extended effective lease and a result arriving after the nominal lease but before reclaim); a result committed between the in-flight count and the held-set read keeps the item pending; a pre-release failing sample persisted through the conflict-exhaustion fallback (held_since still 0) is marked held atomically with its completion: the observer cannot observe an idle-and-unheld row between the two writes (interleaving test), a crash/reopen after the transaction shows both or neither, and a rejected (stale-claim) completion marks nothing |
| still-broken service after reconnect | fresh post-release sample alerts once when held samples met the threshold; hourly threshold-4 check needs its remaining samples; staggered release (`ipv4` recovers, `dns` down) reschedules only checks whose complete dependency is usable, and only once per hold; restart after close with an idle held check, a processing check and a failed reschedule still completes the recheck |
| send failure and restart | summary record survives restart and is delivered; crash between close and summary send (simulated by reopening the store after each single write) never loses or duplicates a record; per-check `attempt_at` survives SQLite reopen; ack fenced by `attempt_seq` (failure → OK → new alert in the same second does not mis-clear); acks written with retry knob `0`, then enabling `300` does not resend; second outage while a summary is pending; stale-lease incident crash between claim and send retries after restart; failed stale-lease send followed by a non-granting payload refresh and a restart still retries |
| feature disabled | mode `off`: no observer task, no metadata, existing suite unchanged |
| max hold bound | after 3 h the alert goes out although the observer still says `down`; a later failing sample does not re-arm the budget; the retry after exhaustion is not held |
| flap during grace | `recovering` → `down` keeps holds and the incident; close needs one clean grace |
| observe mode | metadata `held: false`, decisions unchanged, site messages sent |
| collector helper | `False` from the notifier marks stale-lease, paused and site incidents undelivered |
| OpsGate | summary with `opsgate_ticket: false` opens no ticket even with warnings enabled |
| malformed `site_dependency` | one warning, treated as unclassified |
| migration | 0012 → 0013 adds all three columns idempotently in either order |

## 16. Work breakdown for implementation

| task | scope | files | depends on |
| --- | --- | --- | --- |
| T1 persistence | `NotificationState` fields incl. carry-over in `cleared()`/`with_streak_reset()`, SQLite + in-memory schema/CAS, repository methods of section 10 incl. the claim-time `delivery_pending` merge, worker upgrade, Django model + migration 0013, adapter and migration tests | `repositories/interface.py`, `sqlite_repo.py`, `in_memory.py`, `nyxboard/models.py`, `nyxboard/migrations/0013_*.py`, `tests/adapters/*`, `tests/dashboard/*` | - |
| T2 observer | config parsing, probes, state machine, snapshot, persisted lifecycle, incident coordinator (silent open, ongoing/retire/close/summary records, retries, per-path recheck), restart restore, observer tests | `adapters/site_connectivity.py`, `tests/unit/test_site_connectivity_observer.py` | interface of section 10 |
| T3 handler and wiring | `site_dependency` parsing, hold rule with freshness, `held_since` rules, delivery intent/ack/retry, notifier return value and `opsgate_ticket` flag, `_notify_collector_incident` status/result handling, bootstrap injection, collector start of the observer, hold/retry tests, end-to-end acceptance tests | `service_layer/handlers.py`, `service_layer/site_dependency.py`, `adapters/notification.py`, `bootstrap.py`, `adapters/collector.py`, `tests/unit/test_site_connectivity_holds.py`, `tests/unit/test_notification_delivery_retry.py`, `tests/unit/test_collector_incidents.py` | T1, T2 |
| T4 docs and UI | `docs/site-connectivity.md`, `configuration.md`, `usage.md`, `CHANGELOG.md`, dashboard banner and check-detail indicator | `docs/*`, `src/nyxboard/views.py`, templates | T3 for final wording |

T1 and T2 run in parallel against the interface fixed in section 10. T3 runs
after both. T4 runs after T3.

## 17. Review history

- Round 1 (GPT-6 Astra, independent, read-only): 9 Critical, 6 Warning.
  All accepted. Changes: outage duration excludes grace (§4, §6);
  ongoing alert retired on recovery (§6); `recovering → down` on a failed
  round (§5.2); `held_since` reset only on observed recovery (§7.2);
  delivery intent persisted before I/O with a monotonic attempt sequence and
  fenced acknowledgement, at-least-once documented (§8, §14); collector
  helper propagates the notifier result (§8); freshness via
  `claim_started_at` vs `last_release_at` (§7.3); full persisted lifecycle and
  restart restore, separate summary row (§5.3, §6); silent-open repository
  operation (§10); per-path, repeated recheck with an honest bound (§7.4);
  post-release alerting follows the check's own policy (§7.2, §12);
  any-of dependency groups and Happy-Eyeballs-aware `internet` (§7.1); mode
  matrix and retry knob off by default (§7.6, §8, §11); explicit
  no-remediation flag (§6.1).
- Round 2 (GPT-6 Astra, independent, targeted re-review): 10 of 15 prior
  findings resolved, 5 partial; 4 new Critical, 3 Warning, all accepted.
  Changes: one-row phase machine for the site incident with atomic payload
  writes, outages keyed by `incident_id`, summaries as idempotent records,
  row kept until nothing is pending (§5.3, §6); intent-at-claim for the
  existing collector incidents (§6.2, §10); acknowledgements independent of
  the retry knob (§8); persisted recheck work item and `last_release_at`
  surviving close (§5.3, §7.4); any-of groups ignore unobserved members
  (§7.1); recheck only for checks whose complete dependency is usable
  (§7.4); honest at-least-once wording without a count bound (§6, §8, §14).
- Round 3 (GPT-6 Astra, independent, targeted re-review): 5 of 7 round-2
  findings resolved, 2 partial; 2 new Critical, 1 Warning, all accepted.
  Changes: recheck item stays pending until `settle_until` so release
  watermarks outlive every pre-release claim (§5.3, §6, §7.4);
  repository-owned delivery markers are carried forward by every payload
  write of the claim/open operations (§6.2); any-of groups treat `failing`
  members as usable, so holding starts only at `down` in every dependency
  form (§7.1).
- Round 4 (GPT-6 Astra, independent, targeted re-review): N2, N3 and
  round-1 #5/#13/#14 resolved; one Critical remained on N1 (a time-based
  `settle_until` is not an exact fence). Accepted. Changes: the recheck item
  stays pending while any execution claimed before the release is still
  `processing` (exact, via `count_processing_claims_before_async`), and the
  row with the release watermarks is kept permanently (§5.3, §6, §7.4, §10).
- Round 5 (GPT-6 Astra, independent, narrow re-review): the exact-fence
  argument was confirmed against the code; one Critical remained: the two
  completion predicates were read in an order that could miss a result
  committed between them. Accepted. Change: ordering invariant, in-flight
  count before held-set read (§7.4).
- Round 6 (GPT-6 Astra, independent, narrow re-review): the ordering
  invariant was confirmed; one Critical remained: the handler's
  conflict-exhaustion fallback persists without a transition and could
  complete a pre-release claim without writing `held_since`. Accepted.
  Change: hold marker after the fallback persist (§7.2, §10).
- Round 7 (GPT-6 Astra, independent, narrow re-review): the marker had to be
  atomic with the completion. Accepted. Change: `hold_marker` parameter of
  `persist_check_result`, applied inside the completion transaction (§7.2,
  §10, §15).
- Implementation-phase amendments (code review rounds 1-2, GPT-6 Astra):
  summary delivery is size-bounded and per-record eligible (§6);
  `with_streak_reset()` also carries `held_since` and `attempt_at`, so
  maintenance suppression neither re-arms the hold budget nor cancels a
  pending delivery intent (§7.2, §8, §9); the notifier returns `None` when
  delivery was not attempted (unconfigured) and `False` only for an attempted
  request that failed (§8); collector acknowledgements are one atomic
  repository operation fenced by the attempt (§6.2); a retired undelivered
  ongoing alert allows a new attempt after the reminder window measured from
  the last attempt (§6); the round outcome is applied before the release
  boundary (§5.2); freshness is evaluated per requirement with the
  `usable_since` watermark (§7.2); restoration is retried before any
  lifecycle write (§5.3); the in-memory store serialises completion and
  observer reads (§10); immediate notifications take part in delivery retry
  (§8).

**Stop rationale for the planning review loop.** Findings per round were
15 → 7 → 3 → 1 → 1 → 1 → 1. The last four rounds each narrowed the same
argument (recheck completion for a result measured before a release) to a
smaller corner; the final repair is a single parameter on an existing
transactional method. The reviewer itself rated the last two items Critical
only because they contradict the plan's own recheck bound, with no alert
lost or duplicated. Continuing would seek reviewer agreement rather than
reduce a demonstrated risk. The round-7 repair therefore goes into
implementation without a further plan round and is a mandatory verification
item of the code-review phase (transaction placement, interleaving test,
crash/reopen test, rejected-completion test).
