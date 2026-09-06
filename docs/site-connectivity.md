# Site Connectivity and Held Alerts

When the internet connection of the machine Nyxmon runs on breaks, every check
that needs the internet fails at the same moment, although the monitored
services are healthy. A provider reconnect that changes the public IPv4 address
and the IPv6 prefix can keep dependent paths broken for ten to twenty minutes.
During that window Nyxmon cannot deliver Telegram messages either, because
Telegram uses the same path.

Site connectivity detection makes Nyxmon observe its own internet connection and
*hold* the alerts of checks that declared they depend on it, instead of paging
for each of them. The site outage itself is reported once. Measurements are
never falsified: a failing sample is still stored as a failure, with metadata
naming why its notification was deferred.

The feature ships **disabled**. Nothing changes until
`NYXMON_SITE_CONNECTIVITY_MODE` is set.

## What it does and does not do

It does:

- suppress the alert flood of a short internet outage,
- report a long outage once, with a recovery summary,
- report a service that is genuinely still broken after the reconnect,
- keep local and independent failures (disk, a LAN service, collector health)
  alertable throughout.

It does not:

- infer anything from URLs or from many checks failing at once. Suppression is
  explicit per check;
- hide failures. Every sample is stored with its real status;
- hold anything forever. Every hold is bounded, and a frozen or defective
  observer stops holding within about three minutes.

## How detection works

### Paths and targets

The observer measures three independent **paths**:

| Path | Targets | Probe |
| --- | --- | --- |
| `ipv4` | `1.1.1.1:443`, `8.8.8.8:443`, `9.9.9.9:443` | TCP connect to an IPv4 literal, so DNS is not involved |
| `ipv6` | `[2606:4700:4700::1111]:443`, `[2001:4860:4860::8888]:443`, `[2620:fe::fe]:443` | TCP connect to an IPv6 literal |
| `dns` | `cloudflare.com`, `google.com`, `quad9.net` | `getaddrinfo` through the system resolver |

All target lists are configurable, and an empty list marks its path
`unobserved` — which is how a site without IPv6 is configured. See
{doc}`configuration` for the variables.

### Probe rounds

One probe loop runs per worker process, as a second task beside check
collection. It never issues a probe per check result.

Every `NYXMON_SITE_PROBE_INTERVAL_SECONDS` (60 s by default) one **round** tries
every target of every observed path concurrently, with a per-target timeout
(`NYXMON_SITE_PROBE_TIMEOUT_SECONDS`, 3 s) and a hard round budget of timeout
plus two seconds. A target that does not answer inside the budget counts as
failed.

A path's round is `ok` when **at least one** of its targets answered, and
`failed` only when every target failed. One dead server, or one unreachable
coordination service, can therefore never take a path down on its own.

### Path state machine

```text
up         --(round failed)-----------------------> failing
failing    --(round ok)-------------------------->  up          (no incident, no grace)
failing    --(down_after_failures consecutive)--->  down        (down_since = now)
down       --(round ok)-------------------------->  recovering  (recovered_at = now,
                                                                 release_at = now + grace)
recovering --(round failed)---------------------->  down        (down_since kept,
                                                                 recovered_at/release_at cleared)
recovering --(now >= release_at)----------------->  up          (last_release_at = release_at)
```

With the defaults, a path is confirmed `down` after two consecutive failed
rounds, roughly 60 to 120 seconds after the failure started. **Holding starts
only at `down`, never at `failing`**: a single bad round must never defer every
dependent alert for a full grace window.

A failed round while `recovering` returns the path to `down` immediately. The
incident stays open, holds continue, `down_since` is kept, and the next
successful round starts a fresh grace. Closing therefore requires one
uninterrupted grace of successful rounds after the last failure.

Within a round the outcome is applied **before** the release is evaluated. A
round that fails exactly at the release boundary therefore returns the path to
`down` instead of releasing it: a path that is broken again can never close the
incident and drop the holds by virtue of the grace having expired.

`last_release_at` is the watermark of the last release of a path. It is kept
permanently, because it is what lets the handler recognise a sample that was
measured before the release.

## Declaring a dependency

A check declares what it needs through `data.site_dependency`. Validation is
fail-safe in the style of `notification_policy`: anything malformed warns once
per check and falls back to "unclassified", which is byte for byte today's
behaviour.

```json
{"site_dependency": "internet"}
{"site_dependency": {"requires": ["dns", ["ipv4", "ipv6"]]}}
{"site_dependency": {"requires": ["ipv6"]}}
{"site_dependency": "none"}
```

- **absent** or `"none"`: unclassified. The check is never held and never
  rescheduled by the recovery recheck.
- `requires` is a list of requirements. Each entry is a path name or a list of
  alternative path names (an any-of group). A check is affected when **any**
  requirement is unmet.
- A single-path requirement is unmet when that path is `down` or `recovering`.
- An any-of group ignores `unobserved` members and is met as soon as one
  *observed* member is `up` **or `failing`** — an unconfirmed failure never
  holds, exactly as for a single path. It is unmet only when every observed
  member is `down` or `recovering`. A group whose members are all unobserved is
  met.
- `"internet"` is shorthand for `{"requires": ["dns", ["ipv4", "ipv6"]]}`. HTTP
  and TCP checks connect through Happy Eyeballs, so a dual-stack host addressed
  by name keeps working while only one address family is broken.
- A single-path requirement on an `unobserved` path is always met and can never
  hold. The observer logs one warning per such check at startup, so the
  misclassification is visible rather than silent.

On an IPv4-only site (empty IPv6 targets) `["ipv4", "ipv6"]` therefore behaves
exactly like `ipv4`, so a confirmed IPv4 outage still holds `internet` checks.

### Choosing the right declaration

Classify by what the check's executor actually needs:

| Check | Declaration |
| --- | --- |
| HTTP, TCP, SMTP or IMAP to a dual-stack host **by name** | `"internet"` |
| Target given as an IPv4 literal | `{"requires": ["ipv4"]}` |
| Target given as an IPv6 literal | `{"requires": ["ipv6"]}` |
| IPv4-only host by name | `{"requires": ["dns", "ipv4"]}` |
| DNS check with an explicit `dns_server` IP | the path of that server's address family, **not** `dns` |
| DNS check through the system resolver | `"internet"` |
| LAN-only target | leave unclassified |

A LAN endpoint whose *payload* is internet-dependent is an operator decision. A
combined Tailscale check, for example, is classified `"internet"` because the
conditions that make it fail during a reconnect are the tailnet ones.

Because a deployment that upserts a check rewrites its whole `data` blob, put
`site_dependency` in the playbook that owns the check. An edit made directly in
the database is lost on the next deploy.

## What "held" means

A held sample is stored with its real status. Only its *notification* is
deferred. The failure streak keeps counting while the check is held.

The handler evaluates the hold after the OK, collector-internal and
maintenance-suppression branches, and before the threshold and reminder logic.
A held sample writes `site_connectivity` metadata into the stored result, in the
same spirit as `notification_suppressed`:

```json
{
  "site_connectivity": {
    "held": true,
    "reason": "dependency_down",
    "paths": ["dns", "ipv4"],
    "down_since": 1757123456,
    "state": "down",
    "incident_id": 1757123456
  }
}
```

`reason` is one of:

| Reason | Meaning |
| --- | --- |
| `dependency_down` | at least one required path is confirmed `down` |
| `dependency_recovering` | every blocking required path is in the recovery grace |
| `measured_before_release` | every requirement is met now, but this sample's execution was claimed before the release; it is not fresh |

For `measured_before_release` the metadata carries `release_at` (the watermark
the claim was compared against) instead of a `down_since`.

Freshness is evaluated **per requirement**, not per path. A requirement is
usable since the *earliest* release among the members that are usable **now**,
because those are the members that carry it. Members that are `down` or
`recovering` do not contribute: their old watermark says nothing about a group
they are not currently serving. A currently usable member that never released
contributes `0`, which says the group was never unmet: during an IPv6-only
outage the `["ipv4", "ipv6"]` group of `internet` kept working over IPv4, so no
sample of an `internet` check is stale because of that outage. When both
families were down and only `ipv4` was released, the group is usable only since
that release, so a sample claimed before it is held. A requirement without
observed members is always met and therefore never stale; one without a usable
member is unmet, and `dependency_down` or `dependency_recovering` applies
instead. The reported `release_at` is the highest such watermark the sample's
claim was late for. A sample whose claim time is unknown (`0`) is never held as
stale, so a permanent watermark cannot hold it forever.

Two variants carry `"held": false`:

- In `observe` mode the metadata records the judgement without acting on it, so
  the observer can be verified before it is allowed to suppress anything.
- In `enforce` mode, once a hold is exhausted (see below), the metadata is
  written with `"held": false` and an extra `"exhausted": true`, so the
  dashboard and the result history show that the observer still said "down"
  while the alert went out anyway.

What is *never* held:

- an OK sample — it clears the whole notification state,
- a collector-internal sample (an expired processing lease),
- a maintenance-suppressed sample,
- a `force_notification` sample from the collector-internal path,
- an unclassified check, in any mode.

### After the release

When the dependency is observed recovered and the sample is fresh, the check's
own `notification_policy` decides, exactly as it would have without the outage:

- held samples count toward `consecutive_failures`, so a check that already
  reached its threshold while held alerts on its first fresh sample;
- an hourly check with threshold four that accumulated one held sample still
  needs three more hourly samples;
- an already-open incident follows its own reminder clock.

That is the deliberate meaning of "reported reliably, according to policy".

### The hold budget

`held_since` records the first held sample of the current hold. Once
`now - held_since` reaches `NYXMON_SITE_MAX_HOLD_SECONDS` (three hours by
default), the hold is **exhausted**: the check follows ordinary policy again for
the rest of that incident, including delivery retries.

`held_since` is reset only by an OK sample (which clears the whole state) or by
a failing sample evaluated while the dependency is observed recovered on a fresh
snapshot. A bypass because the snapshot was stale, or because the budget was
already spent, leaves it untouched, so a persisting outage can never re-arm the
budget.

A maintenance-suppressed sample breaks the failure streak and nothing else: it
carries `held_since` and any pending delivery intent through unchanged. Zeroing
`held_since` there would re-arm the budget for the rest of the outage *and* drop
the check from the observer's recheck set, and zeroing the intent would cancel
an alert nobody has acknowledged. So a maintenance window inside an outage
neither extends a hold nor loses a send.

## Recovery grace and recheck

A path that starts answering again does not go straight back to `up`. It enters
`recovering` for `NYXMON_SITE_RECOVERY_GRACE_SECONDS` (15 minutes by default)
and only then **releases**. Holds continue during the grace, so a flapping
reconnect does not page.

At a release Nyxmon opens a **recovery recheck** work item, persisted with the
rest of the lifecycle. On every observer round while the item is pending, the
coordinator:

1. counts executions claimed before the release that are still `processing`,
2. reads the set of held checks (`held_since > 0`) with their check data,
3. reschedules the held checks whose *complete* dependency is usable again and
   that are idle, enabled and not already due.

The count is read **before** the held set on purpose. A pre-release claim that
commits between the two reads was still counted, so the item stays pending; one
that committed before the count is already visible to the held query. The two
reads can therefore never both miss the same check.

The item stays pending while either a usable held dependent exists or any
pre-release claim is still in flight. A check whose dependency is still blocked
by another requirement — `dns` still down after `ipv4` released — is not
rescheduled and keeps its normal interval. The set of usable held checks
therefore shrinks monotonically, and the item completes.

Rechecks are claimed in ordinary batches (`NYXMON_CHECK_BATCH_SIZE`, five per
collector iteration by default), so N held checks are rechecked within roughly
N / batch size iterations plus their runtime. With around sixty checks and
mostly sub-ten-second runtimes that is a few minutes. It is a bound, not a
promise of one iteration.

## Bounds against a broken observer

Two bounds exist so that a defective observer can never silence Nyxmon:

- **Snapshot staleness.** The handler reads an in-memory snapshot. If
  `now - observed_at` exceeds three probe intervals (three minutes with the
  defaults), the snapshot is stale, nothing is held, and `held_since` is left
  untouched. A frozen observer therefore stops holding within about three
  minutes. Staleness is derived from the probe interval and is not configurable.
- **Maximum hold.** Even with a perfectly fresh snapshot, no single hold lasts
  longer than `NYXMON_SITE_MAX_HOLD_SECONDS`.

Beyond that: an observer task that raises logs the exception and continues on
the next interval; a failed incident-store write is retried on the next round
while the in-memory state moves on; and the handler never consults the store for
a hold decision.

The observer also never writes a lifecycle it has not read. Restoration is
retried at the start of every round until it succeeds, and a round that starts
unrestored does nothing at all: it neither probes nor writes, and the snapshot
stays exactly as stale as it was, so nothing new is held while the lifecycle is
unknown. Writing an empty lifecycle over the persisted row would erase the
pending summaries, the undelivered ongoing intent and the release watermarks. An
absent row counts as restored — there is simply nothing to load.

If the probe targets themselves are firewalled while the internet is fine, the
site alert at fifteen minutes reveals the misconfiguration, and every dependent
alert is delayed at most the max-hold bound.

## The site incident and its messages

The whole lifecycle lives in **one** row of the `collector_incident` table under
the key `site:connectivity`. Every transition is a single atomic payload write,
so no transition can be half persisted. The row is created silently and then
kept permanently (phase `idle` between outages), because it carries the release
watermarks the freshness rule needs.

```json
{
  "incident_type": "site_connectivity",
  "version": 1,
  "phase": "active",
  "incident_id": 1757123456,
  "observed_at": 1757125000,
  "paths": {
    "ipv4": {"state": "recovering", "down_since": 1757123456,
             "recovered_at": 1757124900, "release_at": 1757125800,
             "last_release_at": 0},
    "dns": {"state": "up", "down_since": 0, "recovered_at": 0,
            "release_at": 0, "last_release_at": 1757100000}
  },
  "ongoing": {"incident_id": 1757123456, "attempt_at": 1757124400,
              "attempts": 3, "delivered": false, "retired": false,
              "last_alert_at": 0},
  "summaries": [],
  "recheck": {"pending": true, "release_at": 1757125800}
}
```

Two messages exist.

**Ongoing outage alert.** While at least one path is `down` (not merely
`recovering`) and the outage is at least
`NYXMON_SITE_INCIDENT_NOTIFY_AFTER_SECONDS` old (15 minutes by default), Nyxmon
writes the attempt intent to the payload *first* and only then sends. The
message has status `error` and error type `site_connectivity_outage`; it names
the down paths with their `down_since` and states the maximum hold. It does open
an OpsGate ticket, under the key `nyxmon-collector-site:connectivity`. A
delivered alert repeats after `NYXMON_SITE_INCIDENT_REMINDER_SECONDS` (six
hours). A failed one is retried every 60 seconds **while a path is still down**:
during a partial outage that delivers over the working path, during a full
outage it keeps failing until reconnection.

**Retire on recovery.** In the same payload write in which the last `down` path
becomes `recovering`, an undelivered ongoing alert is marked retired. The
retired attempt itself is never retried, so a Telegram connection that comes
back during the grace cannot deliver an obsolete "outage ongoing" message.

A path that flaps back to `down` does not un-retire that attempt, but it may
earn a **new** one: the next attempt is allowed once
`NYXMON_SITE_INCIDENT_REMINDER_SECONDS` have elapsed since the later of the last
delivery and the last attempt. Without that clock an undelivered, retired alert
would silence the incident for good — the outage that flaps in and out of `down`
would never alert again.

**Recovery summary.** When every observed path is up again, the outage duration
is measured from the first `down_since` to the last `recovered_at` — the grace
is **not** part of it. If the duration reaches the notify-after threshold, or an
ongoing alert had been delivered, a summary record is appended in the same write
that sets the phase back to `idle` and opens the recheck. Because records are
keyed by `incident_id`, re-running the close after a crash cannot append one
twice. A shorter outage with no delivered ongoing alert leaves only a log line.

Summaries are sent with status `warning`, error type
`site_connectivity_summary`, and `opsgate_ticket: false` — a resolved event must
never open a remediation ticket, whatever the severity and whatever
`OPSGATE_SUBMIT_INCLUDE_WARNINGS` says. For each record the message reports the
outage duration, each affected path with its `down_since` and `recovered_at`,
whether an ongoing alert had been delivered, and how many dependent checks were
still held at close and are being rechecked.

Pending records are drained oldest first in **size-bounded batches**, because
Telegram rejects a message above 4096 characters and a backlog concatenated into
one message could never be delivered at all. One message covers at most five
records (`SITE_SUMMARY_MAX_RECORDS`) and at most 3500 characters
(`SITE_SUMMARY_MAX_CHARS`); a single record that alone exceeds the character
budget is truncated rather than dropped, so it cannot block the backlog forever.
A batch is built strictly oldest first from records that are individually due —
never attempted, or last attempted at least `SITE_DELIVERY_RETRY_SECONDS` ago —
and the scan stops at the first record that is not due yet. A record appended
one probe tick after a failed send therefore neither jumps the queue nor
shortens the retry bound of the older records ahead of it. Only the records a
batch actually covered get an attempt stamp, and only they are removed after a
successful send: a record appended by a later round, and every record of a later
batch, survives untouched and goes out on a following round. In the ordinary
case of a single pending record that is exactly one message.

A 40-minute full outage therefore produces one Telegram message after
reconnection. A 3-minute reconnect produces none.

### At-least-once delivery

Both site messages are at-least-once. The intent is made durable before the
send and cleared after a successful one. A crash between those steps, or an
ambiguous outcome — Telegram accepted the request but the response was lost,
which the notifier reports as a failure — causes a repeat.

The same rule governs the other collector-scoped incidents (the wedged executor,
the stale-lease sweep). Their intent is written by the claim transaction that
grants the alert, and a successful send is acknowledged by a *single* repository
operation, `acknowledge_collector_incident_delivery`, which compares and clears
in one critical section — one `BEGIN IMMEDIATE` transaction in SQLite, the store
lock in memory. It is fenced by the `alert_count` the claim granted, so a claim
committed in between keeps its own, newer intent, and it removes only the
`delivery_pending` and `delivery_attempt` markers, so incident details written
between the claim and the send are never rolled back. A failed send writes
nothing at all: the claim already recorded exactly the state a retry needs.

Telegram offers no idempotency key, so exactly-once is not achievable and no
count bound can be promised. Repeats are rate-bounded by the 60-second retry
cadence and by the requirement that a path is still down (ongoing) or a record
is still pending (summary). Nyxmon deliberately chooses loss-prevention over
duplicate-prevention.

## Per-check delivery retry

Unclassified checks — a local disk failure, for example — keep alerting during
an outage. Historically that alert was attempted once after the state
transition was committed, and a failed or crashed send was lost until the next
reminder six hours later.

With `NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS` set (off by default), the
notification state carries a monotonic `attempt_seq` and an `attempt_at`:

- whenever a transition decides to notify, it stamps `attempt_seq + 1` and
  `attempt_at = now` inside the same compare-and-swap commit, before any I/O;
- after the notifier returns anything other than `False`, the handler
  acknowledges with `attempt_at = 0 WHERE attempt_seq = <the stamped one>`. A
  row that an intervening OK cleared, or that a newer attempt advanced, does not
  match, so nothing is overwritten;
- on the next failing sample, an unacknowledged attempt older than the retry
  interval is sent again irrespective of the reminder window — unless the
  sample is held;
- an immediate alert (`data.notification_immediate`) participates on the same
  terms: a due retry sends it again even inside its cooldown, because the
  cooldown bounds *new* alerts, not the redelivery of one that may never have
  arrived;
- a maintenance-suppressed sample carries the pending intent through unchanged,
  so a maintenance window cannot cancel an alert nobody acknowledged;
- an OK sample clears the marker with the rest of the state, so a failure that
  recovered before delivery was possible is not reported after the fact. Its
  result history stays visible on the dashboard.

A notifier that never *attempted* a send reports `None`, not `False`.
`AsyncTelegramNotifier.notify_check_failed` returns `None` when it has no portal
provider or no credentials; only an attempted request that failed returns
`False`. An installation that never configured Telegram therefore keeps the
behaviour it had before the retry existed, instead of accumulating an intent it
would retry on every failing sample forever.

The acknowledgement is written after every successful send **regardless of the
knob**. With the knob at `0` a failed send therefore behaves exactly as before,
and enabling the knob later cannot resend an alert that was already delivered
while it was off.

This too is at-least-once, with the same reasoning as above; repeats are
rate-bounded by the retry interval and end at the first acknowledged send or the
first OK sample.

## Modes

| Behaviour | `off` (default) | `observe` | `enforce` |
| --- | --- | --- | --- |
| probe task, snapshot | no | yes | yes |
| incident row, lifecycle persistence | no | yes | yes |
| `site_connectivity` metadata on affected samples | no | `held: false` | `held: true` / `false` |
| holding notifications | no | no | yes |
| recovery recheck and reschedule | no | no | yes |
| site ongoing alert and summary | no | yes | yes |
| per-check delivery retry | independent knob, default off | same | same |

`observe` sends the site messages on purpose: they are the signal by which the
observer's judgement is verified before it is allowed to suppress anything.

## Rollout

1. Release Nyxmon with migration `0013_site_connectivity_state`. The deployment
   role runs `migrate` before the worker starts, and the worker's own idempotent
   schema upgrade covers the other order.
2. Set `NYXMON_SITE_CONNECTIVITY_MODE=observe` and
   `NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS=300` in the worker environment. On the
   homelab that is the ops-control variable rendering the mode-0600 environment
   file for the `nyxmon-monitor` unit. Watch the worker log and the dashboard
   banner across a few days: path states must match reality and no false `down`
   may appear.
3. Classify the checks in the playbooks that upsert them. External HTTP, DNS and
   mail checks become `"internet"` or carry explicit requirements per the table
   above; LAN-only checks stay unclassified. **A deployment rewrites the whole
   `data` blob, so `site_dependency` must live in the playbook, not be edited in
   the database.**
4. Switch `NYXMON_SITE_CONNECTIVITY_MODE` to `enforce`.
5. Revisit any per-check `consecutive_failures` override that was raised as an
   interim mitigation only after the site policy has been observed through a
   real reconnect.

### Rollback

Set the mode back to `off`. The observer is not started; the incident row stays
until the next enable. On that next enable an `active` phase is restored only
when its `observed_at` is still fresh; otherwise the stale outage is set to
`idle` with a log line, keeping the release watermarks, the pending summaries
and the recheck item. The row is never deleted. Held streaks are no longer held:
their next failing sample follows ordinary policy, which is the intended "alert
normally" rollback. Pending summaries are delivered on the next enable.

The three new columns are inert when the feature is unused.

## Worked timelines

Assume 300-second checks with an alert threshold of 2, and the defaults above.

**Three-minute reconnect.** Failure at t=0. Path `down` at t≈1.5 min. The
dependent check samples at t=0.5 (counted, not held — not yet confirmed) and at
t=5.5 (held, count 2). Path `recovering` at t=3, `up` at t=18. The outage
duration is 1.5 min, below the 15-minute threshold, and no ongoing alert was ever
eligible. The recheck at t=18 gets an OK and clears the state. **Zero
notifications.**

**Forty-minute outage, one service still broken afterwards.** The ongoing intent
is written at t=16.5 and every send fails while the internet is down. At t=40
the last path becomes `recovering` and the undelivered alert is retired, so
nothing is sent even though Telegram is reachable again. At t=55 the release
sets the phase to `idle`, appends the summary record, opens the recheck, and the
summary goes out once. Held checks are rescheduled; recovered services report OK
and are cleared. The still-broken service's fresh sample at t≈55–57 already has
count ≥ 2 and alerts once. **One summary, one genuine alert.**

**Flap during the grace.** Reconnect at t=40, a failed round at t=47 returns the
path to `down`, holds continue, the ongoing alert is not re-claimed. Success at
t=49 starts a fresh grace, release at t=64. The outage duration counts to t=49.

**IPv6-only outage for three hours.** Checks requiring `ipv6` are held.
`internet` checks are **not** held, because Happy Eyeballs keeps them working,
and one that genuinely fails over IPv4 alerts normally. The ongoing alert at 15
minutes is delivered over IPv4 and names `ipv6`; a reminder follows after six
hours; a summary follows on recovery because an ongoing alert was delivered.

**Fast check in a slow batch.** A 60-second check fails at t=53, during the
grace, but its batch also holds a check that takes four minutes, so the result
is handled at t=57, after the release at t=55. Its claim started before the
watermark, so it is held as `measured_before_release` and rescheduled by the
next observer round. Its fresh sample at t≈58 decides.

**Buffered failure that was never held.** The only dependent check is claimed at
t=54 and its result is buffered behind a 25-minute batch member. At the release
no check is held, but that claim is still `processing`, so the recheck item
stays pending. The late result at t≈79 is held as `measured_before_release`,
because the watermark is permanent, and the next round reschedules the check.

**Restart in the middle.** A restart at t=20 of a 40-minute outage restores
every path with its timestamps and the persisted `observed_at`; the first fresh
round confirms; the undelivered ongoing attempt is retried while down. A restart
during the grace restores `release_at`, so holds continue to exactly the same
release and the grace is neither restarted nor cut short.

**Local disk failure during a full outage.** An unclassified check with the
retry knob at 300 s alerts at t=10; the intent is persisted, the send fails, and
each following failing sample retries at least 300 seconds apart. The first
failing sample after reconnection delivers and is acknowledged. Had the disk
recovered at t=30, its OK sample would have cleared everything and nothing would
have been sent.

## Dashboard

The dashboard reads the persisted row; it cannot see the worker's mode and
cannot re-run a hold decision. Both surfaces therefore report what was
*observed*, and state holding as the condition it is.

When the site incident row exists and is either active or carries pending
summaries, the dashboard shows a banner naming the affected paths with their
state and `down_since`, noting when a recovery summary is still pending
delivery, and adding that dependent alerts are held **only while the worker runs
in `enforce` mode**. The banner also reports when the payload was last observed.
If `observed_at` is more than ten minutes old, or is missing or unusable, it
says so and adds that the observer may be stopped — which is what an `off` mode,
a stopped worker or a stale persisted incident looks like from the dashboard. A
malformed payload produces no banner rather than an error.

A check detail page reports a hold from the **newest stored result**, not from
`held_since`:

- the newest result's `site_connectivity` metadata says `held: true` — the page
  shows that the latest sample was held, names when the hold budget started, and
  says the check will notify according to its own threshold and reminder policy
  once the path is released or the maximum hold expires;
- `held_since` is non-zero but the newest result does not say `held: true` — the
  page shows "hold budget started …; alerts follow normal policy". That is the
  exhausted hold, the stale-snapshot bypass, an `observe`-mode judgement and a
  hold marker carried through a maintenance window;
- neither — nothing is shown.

`held_since` alone is deliberately not evidence of a hold: it stays non-zero
after exhaustion and after a bypass, and it is carried through maintenance
suppression, precisely so a persisting outage cannot re-arm the budget.

Individual results whose data carry `site_connectivity` are marked with a small
**held** or **observed** badge.

## Limits and known risks

- **Duplicate messages are possible.** At-least-once delivery means a crash
  between a successful send and its acknowledgement, or an ambiguous Telegram
  outcome, produces a repeat. Loss-prevention was chosen over
  duplicate-prevention.
- **A partial outage can fool the observer.** Telegram reachable while the probe
  targets are broken, or the reverse, is possible in principle. Three
  independent targets per path, the ongoing alert at fifteen minutes and the
  max-hold bound limit the damage.
- **The first one to two minutes of an outage are unprotected**, because holding
  starts at `down` and not at `failing`. A check with a pre-existing failure, a
  due reminder, or `consecutive_failures: 1` can page in that window. The
  two-sample default and 300-second intervals make it rare.
- **The grace delays a genuine failure report** by up to the grace window after
  the reconnect, plus the recheck bound. That is the intended tolerance.
- **Delivery retry costs notifier time.** Up to ten seconds per failing sample
  per retry interval while Telegram is unreachable, inside the existing per-check
  result-handling allowance of one minute.
- **There is no manual, time-boxed override switch.** Planned maintenance is
  covered by the existing per-check `notification_suppression`; a global switch
  is deferred until a concrete need appears.
