// Single-owner session: key lives only in this module's memory, never storage.
const BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";
let token = "";
let generation = 0;
const active = new Set();
const listeners = new Set();
const KEY_ERROR = "Access key was not accepted. Please unlock again.";

export const getGeneration = () => generation;
export function subscribeAuth(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
export function logout(reason = "") {
  token = "";
  generation += 1;
  for (const controller of active) controller.abort();
  active.clear();
  for (const listener of listeners) listener(false, reason);
}
function assertCurrent(epoch, signal) {
  if (epoch !== generation || signal.aborted) {
    throw new DOMException("Session ended", "AbortError");
  }
}

// Consume within the request lifetime so logout aborts JSON reads AND SSE.
async function request(path, options = {}, consume = res => res.json(), key = token) {
  if (!key) throw new Error("Please unlock to continue.");
  const epoch = generation;
  const controller = new AbortController();
  active.add(controller);
  try {
    const headers = new Headers(options.headers);
    headers.set("Authorization", `Bearer ${key}`);
    const res = await fetch(`${BASE}${path}`, {
      ...options, headers, signal: controller.signal, cache: "no-store",
      credentials: "omit", redirect: "error",
    });
    assertCurrent(epoch, controller.signal);
    if (res.status === 401) {
      logout(KEY_ERROR);
      throw new Error(KEY_ERROR);
    }
    if (res.status === 503) throw new Error("Private access is not configured on the server. Contact the owner.");
    if (!res.ok) throw new Error(`Request failed (HTTP ${res.status}).`);
    const result = await consume(res, controller.signal, epoch);
    assertCurrent(epoch, controller.signal);
    return result;
  } finally {
    active.delete(controller);
  }
}

export async function unlock(key) {
  if ([...key].length < 32) throw new Error("Enter an access key of at least 32 characters.");
  logout();
  const epoch = generation;
  await request("/routes", {}, undefined, key);
  // request checked its epoch; check again after its promise continuation.
  if (epoch !== generation) throw new DOMException("Session ended", "AbortError");
  token = key;
  for (const listener of listeners) listener(true, "");
}

export async function warmup() {
  const res = await fetch(`${BASE}/warmup`, { method: "GET" });
  if (!res.ok) throw new Error(`warmup failed: ${res.status}`);
  return res.json();
}
export const getRoutes = () => request("/routes");
export function approve(callId, approved) {
  return request("/approve", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ call_id: callId, approved }),
  });
}

/** POST SSE rather than EventSource so all requests use the same bearer gate. */
export function streamChat({ message, threadId, route }, onEvent) {
  return request("/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, thread_id: threadId, route }),
  }, async (res, signal, epoch) => {
    const reader = res.body.getReader();
    const cancel = () => { void reader.cancel().catch(() => {}); };
    signal.addEventListener("abort", cancel, { once: true });
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      assertCurrent(epoch, signal);
      while (true) {
        const { done, value } = await reader.read();
        assertCurrent(epoch, signal);
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();
        for (const part of parts) {
          assertCurrent(epoch, signal);
          const line = part.trim();
          if (!line.startsWith("data:")) continue;
          let event;
          try { event = JSON.parse(line.slice(5).trim()); }
          catch { continue; }
          onEvent(event);
        }
      }
    } finally {
      signal.removeEventListener("abort", cancel);
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  });
}
