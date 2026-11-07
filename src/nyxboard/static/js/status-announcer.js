// One persistent live region owns deduplication across mirrored cards and swaps.
const regions = new WeakMap();
export function announceStatus(kind, id, name, status) {
  const region = document.getElementById('monitoring-status');
  if (!region || !id || !name || !status) return;
  let state = regions.get(region);
  if (!state) { state = { values: new Map(), pending: new Map(), scheduled: false }; regions.set(region, state); }
  const key = `${kind}:${id}`;
  const previous = state.values.get(key);
  state.values.set(key, status);
  if (previous === undefined || previous === status) return;
  state.pending.set(key, `${name}: ${status.charAt(0).toUpperCase()}${status.slice(1)}.`);
  if (state.scheduled) return;
  state.scheduled = true;
  queueMicrotask(() => {
    state.scheduled = false;
    if (region.isConnected) region.textContent = [...state.pending.values()].join(' ');
    state.pending.clear();
  });
}
