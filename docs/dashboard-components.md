# Dashboard components

Run `npm ci && npm test` on Node 22.12 or newer. Tests import each registered
production element once in jsdom, use synthetic DOM fragments and fake clocks,
and remove elements before restoring timers and request globals. No services,
accounts or browser installation are needed. Progress tests pin the before-start,
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
