// API client. Calls the Container Apps backend directly with CORS -- SWA's
// "linked backend" proxy requires the Standard tier ($9/mo), which this avoids.

const BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

export async function warmup() {
  const res = await fetch(`${BASE}/warmup`, { method: "GET" });
  if (!res.ok) throw new Error(`warmup failed: ${res.status}`);
  return res.json();
}

export async function getRoutes() {
  const res = await fetch(`${BASE}/routes`);
  return res.json();
}

export async function approve(callId, approved) {
  const res = await fetch(`${BASE}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ call_id: callId, approved }),
  });
  return res.json();
}

/**
 * POST /chat and parse the SSE stream.
 * Uses fetch + ReadableStream because EventSource cannot issue POST requests.
 */
export async function streamChat({ message, threadId, route }, onEvent) {
  const res = await fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, thread_id: threadId, route }),
  });

  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop(); // keep the trailing partial frame

    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith("data:")) continue;
      try {
        onEvent(JSON.parse(line.slice(5).trim()));
      } catch {
        // ignore malformed frame
      }
    }
  }
}
