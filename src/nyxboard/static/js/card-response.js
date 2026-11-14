// Admit a finite fallback fragment before any DOM parsing. Never trust length headers.
export const MAX_CARD_BYTES = 128 * 1024;

export async function readCardResponse(response, signal) {
  const reader = response.body?.getReader();
  if (!reader) throw new Error('Missing status stream');
  const cancel = () => { reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', cancel, { once: true });
  const decoder = new TextDecoder();
  let bytes = 0;
  let html = '';
  try {
    if (signal.aborted) throw new Error('Refresh cancelled');
    while (true) {
      const { done, value } = await reader.read();
      if (signal.aborted) throw new Error('Refresh cancelled');
      if (done) break;
      bytes += value.byteLength;
      if (bytes > MAX_CARD_BYTES) throw new Error('Status fragment too large');
      html += decoder.decode(value, { stream: true });
    }
    return html + decoder.decode();
  } catch (error) {
    cancel();
    throw error;
  } finally {
    signal.removeEventListener('abort', cancel);
    reader.releaseLock();
  }
}
