# Dashboard components

Run `npm ci && npm test` on Node 22.12 or newer. Tests import each registered
production element once in jsdom, use synthetic DOM fragments and fake clocks,
and remove elements before restoring timers and request globals. The regular
`npm test` also runs Playwright Chromium against production Django templates,
shipped HTMX and component modules. Install with `uv sync --frozen` and
`npx playwright install chromium` (Linux CI uses `--with-deps`). All records live
in an in-memory synthetic database; HTTP fixtures never use external services
or accounts. `npm run test:browser` runs the browser gate alone. Progress tests pin the before-start,
halfway, due and overdue percentages, the SVG offset, rounding and timing edits.
Health-check tests exercise observed attributes and the real due event path.

Templates use the autonomous `health-check` and `service-card` tags registered
by the dashboard module, with list-item roles where appropriate. A check's
`status` describes displayed health; `execution-status` preserves the processing
indicator independently. Disabled checks never initiate a component refresh.
Connected checks notify their containing service when inserted or when health
changes; the service recomputes from current siblings, including after a fragment
replacement. Overview cards with the same `service-id` mirror the group status,
including badge and border classes. Only the component owns due dispatch (no
second global or hidden HTMX trigger). The service removes the same listener on disconnect and installs it once on
reconnect. Detached checks do not notify former parents.

The default refresh boundary is HTMX. Duplicate due events issue one request for
the owning check. JavaScript changes take effect after a later dashboard/static
asset deployment; this work does not deploy them.


## Fetch fallback and clock updates

If HTMX is unavailable, a refresh accepts only an OK, non-redirected response
from the exact same-origin status URL, containing one `health-check` element
with the requested DOM ID and check ID. Whitespace around the fragment is fine.
Invalid responses retain the current check and show a status message with a
manual **Retry status** button, enabled after five seconds. No automatic request
loop is added. Each fallback request has a ten-second deadline covering headers
and response text, one AbortController, and one active owner. Disconnect aborts
and clears its timers; reconnect starts a fresh request after an error or cancelled fetch/HTMX delay.
Successfully replaced nodes retire their request state before disconnect.
Late results from prior requests cannot replace the current check.

Progress rings cancel delayed due events on timing/mode edits and disconnect,
and recheck attachment and the current schedule before dispatch. Each schedule
notifies once; reconnect of an overdue normal ring starts a fresh schedule.
Timer headings update with the countdown at the deadline and at local midnight.
Tomorrow means the next calendar date, including 23/25-hour DST days. Unchanged
heading text retains its DOM node. Tests fix the DST timezone in a child Node
process, independent of the machine's local zone.

## Controls, theme and form navigation

The theme checkbox is named **Dark theme**; checked means dark. Check toggles
are named **Disable check <name>**, where checked means disabled, and run buttons
include the check name. Native checkbox keyboard behavior is retained and
slider decorations are excluded from accessible names.
Theme changes apply to the document and notify components before best-effort
storage/session persistence. Storage denial uses the current in-memory choice;
session failures do not undo it or create an unhandled promise rejection.
Saved preference wins over the system preference at initial load.
Service forms return to service detail when editing and the service list when
creating. Check forms return to check detail when editing, the originating
service when creating there, or the check list for global creation. Back and
Cancel share a Django-resolved destination, including invalid bound POSTs.

## Live check interaction

A mutating toggle or Run now action owns its card while pending; its controls are disabled and duplicate
card requests are suppressed. A definite 4xx rejection restores the last
confirmed checkbox/text and announces failure. Network, timeout and 5xx outcomes
are uncertain: the browser fetches authoritative card state with GET, never
retries the inversion POST. If that GET fails, Refresh status retries only GET;
changing the check stays disabled until its state is confirmed. Requests have a
ten-second timeout. Other cards remain usable.
A successful HTTP status confirms a mutation only after the old card is actually
replaced by one `health-check` with the same DOM ID and `check-id`. Before HTMX
swaps, full pages, empty/multiple fragments and mismatched identities are refused.
The expected identity belongs to the request, including when response headers
try to retarget the swap to another card.
A 204 or other success without replacement follows the same GET reconciliation
path. Reconciliation validates identity and replacement too; an invalid response
keeps mutation controls locked and offers Refresh status. Authentication
`HX-Redirect` responses continue through HTMX navigation without reconciliation.
Per-check controls have stable IDs. HTMX card swaps preserve focused controls
and selection; a removed/disabled action falls back to its labelled card. A
background swap never moves focus from another connected control.
One persistent polite Monitoring updates region announces changed service/check
health with identity. Initial render, unchanged polls, timer ticks and mirrored
service updates do not repeat announcements; simultaneous changes are combined.

HTMX's per-card synchronization marks polls abortable: overlapping polls are
dropped, while a deliberate mutation supersedes an older poll. Mutations use
queue-last and lock native controls, so they do not discard one another's POST
responses. A late pre-mutation poll cannot overwrite confirmed state. Component GETs use the owning card as their HTMX source. Reconciliation
starts after the completed POST releases its synchronization lock.

The pending guard also checks HTMX's request target for body-sourced requests.
Failed status recovery keeps focus on the replacement Refresh status action
when focus belonged to that card, including repeated failures; it never takes
focus back from an unrelated input/card.

A rejected mutation resumes a due component poll that it superseded. Run now
participates in the same pending/error ownership, rather than inheriting a poll
strategy that could silently discard the action.
