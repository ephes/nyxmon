// Own only dashboard card transactions; use HTMX's public events and ajax API.
const pending = new Map();
const mutations = new WeakMap();
const focus = new WeakMap();
const validated = new WeakMap();
const replacements = new WeakMap();
const requests = new WeakMap();

function validFragment(html, card) {
  // A full document must not become a card, even if its body contains one.
  if (typeof html !== 'string' || /<!doctype|<(?:html|head|body)(?:\s|>)/i.test(html)) return false;
  const template = document.createElement('template');
  template.innerHTML = html;
  const replacement = template.content.firstElementChild;
  return template.content.children.length === 1 &&
    ![...template.content.childNodes].some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim()) &&
    replacement?.matches('health-check') && replacement.id === card.id &&
    replacement.getAttribute('check-id') === card.getAttribute('check-id') &&
    template.content.querySelectorAll('health-check').length === 1;
}
function confirmedReplacement(card, xhr) {
  const fresh = replacements.get(xhr);
  return validated.get(xhr) === card && fresh && document.getElementById(card.id) === fresh &&
    fresh !== card && !card.isConnected &&
    fresh.matches('health-check') && fresh.getAttribute('check-id') === card.getAttribute('check-id') ? fresh : null;
}

function cardFor(element) { return element?.closest?.('health-check'); }
function captureFocus(card) {
  const active = document.activeElement;
  if (!card?.contains(active)) return null;
  return { cardId: card.id, id: active.id, start: active.selectionStart, end: active.selectionEnd };
}
function restoreFocus(saved) {
  if (!saved) return;
  const current = document.activeElement;
  if (current && current !== document.body && current.isConnected && current.id !== saved.id) return;
  const card = document.getElementById(saved.cardId);
  if (!card) return;
  const control = saved.id && document.getElementById(saved.id);
  const destination = control && card.contains(control) && !control.disabled ? control : card;
  destination.focus({ preventScroll: true });
  if (destination === control && typeof saved.start === 'number' && control.setSelectionRange) {
    control.setSelectionRange(saved.start, saved.end);
  }
}
function busy(card, value) {
  card.setAttribute('aria-busy', String(value));
  card.querySelectorAll('input, button').forEach(control => { control.disabled = value; });
}
function feedback(card, text) {
  let message = card.querySelector('[data-toggle-feedback]');
  if (!message) {
    message = document.createElement('div');
    message.dataset.toggleFeedback = '';
    message.setAttribute('role', 'alert');
    card.append(message);
  }
  message.textContent = text;
  return message;
}
function restoreConfirmed(state) {
  const checkbox = state.card.querySelector('[data-check-toggle]');
  if (checkbox) checkbox.checked = state.confirmed;
  const label = state.card.querySelector('.toggle-label');
  if (label) label.textContent = state.confirmed ? 'Disabled' : 'Enabled';
}
function owns(state) { return pending.get(state.card.id) === state; }
function release(state) { if (owns(state)) pending.delete(state.card.id); }
function canComplete(state, xhr, fresh) {
  return owns(state) || (fresh && state.swapXhr === xhr && !pending.has(state.card.id));
}

// HTMX emits this for each element it cleans. Child cleanup does not end a card lease.
document.addEventListener('htmx:beforeCleanupElement', event => {
  const card = event.detail.elt;
  const state = pending.get(card?.id);
  if (!state || state.card !== card) return;
  release(state);
  for (const xhr of [state.xhr, state.reconcileXhr]) {
    if (xhr && xhr !== state.swapXhr) focus.delete(xhr);
  }
});

async function reconcile(state) {
  if (!owns(state) || !state.card.isConnected || state.reconciling) return;
  state.reconciling = true;
  const ownedFocus = state.card.contains(document.activeElement) ||
    (document.activeElement === document.body && Boolean(state.focus));
  state.card.setAttribute('aria-busy', 'true');
  feedback(state.card, 'Request could not be confirmed. Refreshing server state…');
  try {
    // Let HTMX release the completed POST's synchronization lock first.
    await Promise.resolve();
    if (!owns(state) || !state.card.isConnected) return;
    await window.htmx.ajax('GET', state.url, {
      source: state.card, target: `#${state.card.id}`, swap: 'outerHTML',
      headers: { 'X-Nyxmon-Reconcile': '1' },
    });
    const fresh = confirmedReplacement(state.card, state.reconcileXhr);
    if (!canComplete(state, state.reconcileXhr, fresh)) return;
    if (!fresh) throw new Error('No confirmed card');
    release(state);
    busy(fresh, false);
    feedback(fresh, 'Request response failed; server state refreshed and confirmed.');
    restoreFocus(state.focus);
  } catch {
    if (!owns(state) || !state.card.isConnected) return;
    state.card.setAttribute('aria-busy', 'false');
    restoreConfirmed(state);
    const message = feedback(state.card, 'State is uncertain. Refresh status before changing again. ');
    const retry = document.createElement('button');
    retry.type = 'button'; retry.textContent = 'Refresh status';
    retry.addEventListener('click', () => reconcile(state));
    message.append(retry);
    if (ownedFocus && document.activeElement === document.body) retry.focus({ preventScroll: true });
  } finally { state.reconciling = false; }
}

document.addEventListener('htmx:beforeRequest', event => {
  const { elt, xhr, requestConfig } = event.detail;
  const card = cardFor(elt) || cardFor(event.detail.target);
  if (!card) return;
  const existing = pending.get(card.id);
  if (existing) {
    if (existing.card === card && existing.reconciling && requestConfig?.headers?.['X-Nyxmon-Reconcile'] === '1') {
      existing.reconcileXhr = xhr;
      requests.set(xhr, card);
    }
    else event.preventDefault();
    return;
  }
  requests.set(xhr, card);
  if (!elt.matches('[data-check-toggle], [data-check-run]')) return;
  const state = { card, xhr, confirmed: card.querySelector('[data-check-toggle]')?.dataset.confirmedDisabled === 'true', url: card.dataset.statusUrl, focus: captureFocus(card) };
  pending.set(card.id, state);
  mutations.set(xhr, state);
  if (state.focus) focus.set(xhr, state.focus);
  busy(card, true);
  feedback(card, elt.matches('[data-check-toggle]') ? 'Saving check state…' : 'Requesting check run…');
});

document.addEventListener('htmx:afterRequest', event => {
  const state = mutations.get(event.detail.xhr);
  if (!state) return;
  mutations.delete(event.detail.xhr);
  // HTMX handles authentication navigation before beforeSwap.
  if (event.detail.xhr.getResponseHeader?.('HX-Redirect')) return;
  const fresh = confirmedReplacement(state.card, event.detail.xhr);
  if (!canComplete(state, event.detail.xhr, fresh)) return;
  if ((event.detail.successful || event.detail.xhr.status === 409) && fresh) {
    release(state);
    busy(fresh, false);
  } else {
    restoreConfirmed(state);
    const code = event.detail.xhr.status;
    if (!event.detail.successful && code >= 400 && code < 500 && code !== 408) {
      release(state);
      busy(state.card, false);
      feedback(state.card, 'Request rejected; previous server state restored. Try again.');
      restoreFocus(state.focus);
      // A mutation may have superseded the component's due GET.
      if (state.card.getAttribute('check-mode') === 'due') {
        state.card.setAttribute('check-mode', 'normal');
        state.card.handleCheckDue?.();
      }
    } else { reconcile(state); }
  }
});

document.addEventListener('htmx:beforeSwap', event => {
  const { xhr, target, elt, serverResponse, shouldSwap } = event.detail;
  const card = requests.get(xhr) || mutations.get(xhr)?.card || cardFor(elt) || cardFor(target);
  const conflict = xhr.status === 409 && mutations.has(xhr);
  if (card && (shouldSwap || conflict)) {
    if (!card.isConnected || document.getElementById(card.id) !== card ||
        target !== card || !validFragment(serverResponse, card)) {
      event.detail.shouldSwap = false;
      return;
    }
    if (conflict) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
    validated.set(xhr, card);
    const state = pending.get(card.id);
    if (state?.card === card && (state.xhr === xhr || state.reconcileXhr === xhr)) {
      state.swapXhr = xhr;
    }
  }
  const saved = captureFocus(card);
  if (saved) focus.set(event.detail.xhr, saved);
});
document.addEventListener('htmx:afterSwap', event => {
  const card = validated.get(event.detail.xhr);
  const fresh = cardFor(event.target);
  if (card && fresh && !card.isConnected && fresh.id === card.id &&
      fresh.getAttribute('check-id') === card.getAttribute('check-id')) {
    replacements.set(event.detail.xhr, fresh);
  }
  const saved = focus.get(event.detail.xhr);
  focus.delete(event.detail.xhr);
  const state = saved && pending.get(saved.cardId);
  if (!state || state.xhr === event.detail.xhr || state.reconcileXhr === event.detail.xhr) restoreFocus(saved);
});
