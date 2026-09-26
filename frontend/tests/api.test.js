import { afterEach, beforeEach, expect, test, vi } from "vitest";
import * as api from "../src/lib/api";

const KEY = "owner-browser-test-key-0123456789abcdef";
const json = (data = {}, status = 200) => new Response(JSON.stringify(data), { status });
let requests;
beforeEach(() => {
  requests = [];
  vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
    requests.push({ url, ...options });
    return json({ routes: [] });
  }));
});
afterEach(() => { api.logout?.(); vi.unstubAllGlobals(); });

test("unlock verifies /routes, rejects short keys, and keeps the session memory-only", async () => {
  expect(api.unlock).toBeTypeOf("function");
  await expect(api.unlock("short")).rejects.toThrow(/32/);
  expect(fetch).not.toHaveBeenCalled();
  await api.unlock(KEY);
  expect(requests[0].url).toMatch(/\/routes$/);
  expect(new Headers(requests[0].headers).get("Authorization")).toBe(`Bearer ${KEY}`);
  await api.getRoutes();
  api.logout();
  await expect(api.getRoutes()).rejects.toThrow(/unlock/i);
  expect(requests).toHaveLength(2);
});

test("all protected fetches including SSE and approvals carry bearer; warmup is public", async () => {
  expect(api.unlock).toBeTypeOf("function");
  await api.unlock(KEY);
  await api.getRoutes();
  await api.approve("call", true);
  fetch.mockImplementationOnce(async (url, options) => {
    requests.push({ url, ...options });
    return new Response('data: {"type":"done"}\n\n');
  });
  const event = vi.fn();
  await api.streamChat({ message: "hi" }, event);
  expect(event).toHaveBeenCalledWith({ type: "done" });
  await api.warmup();
  for (const request of requests.slice(0, -1)) {
    expect(new Headers(request.headers).get("Authorization")).toBe(`Bearer ${KEY}`);
    expect(request.signal).toBeInstanceOf(AbortSignal);
  }
  expect(new Headers(requests.at(-1).headers).has("Authorization")).toBe(false);
});

test.each([[401, /key.*not accepted/i], [503, /not configured/i]])(
  "unlock gives friendly %i and leaves client locked", async (status, message) => {
    expect(api.unlock).toBeTypeOf("function");
    fetch.mockResolvedValueOnce(json({}, status));
    await expect(api.unlock(KEY)).rejects.toThrow(message);
    await expect(api.getRoutes()).rejects.toThrow(/unlock/i);
  },
);

test("logout cancels an active stream and suppresses late events", async () => {
  expect(api.unlock).toBeTypeOf("function");
  await api.unlock(KEY);
  let signal;
  const cancel = vi.fn();
  fetch.mockImplementationOnce(async (_url, options) => {
    signal = options.signal;
    return new Response(new ReadableStream({ cancel }));
  });
  const event = vi.fn();
  const streaming = api.streamChat({ message: "hi" }, event).catch(e => e);
  await vi.waitFor(() => expect(signal).toBeDefined());
  await new Promise(resolve => setTimeout(resolve, 0));
  api.logout();
  await streaming;
  expect(signal.aborted).toBe(true);
  expect(cancel).toHaveBeenCalled();
  expect(event).not.toHaveBeenCalled();
});

test("late unlock and JSON promises cannot restore a logged-out session", async () => {
  expect(api.unlock).toBeTypeOf("function");
  let finish;
  fetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const unlocking = api.unlock(KEY);
  api.logout();
  finish(json());
  await expect(unlocking).rejects.toMatchObject({ name: "AbortError" });
  await expect(api.getRoutes()).rejects.toThrow(/unlock/i);
  await api.unlock(KEY);
  fetch.mockResolvedValueOnce({ ok: true, json: () => new Promise(resolve => { finish = resolve; }) });
  const loading = api.getRoutes();
  await new Promise(resolve => setTimeout(resolve, 0));
  api.logout();
  finish({ secret: true });
  await expect(loading).rejects.toMatchObject({ name: "AbortError" });
});

test("401 on an unlocked session relocks and aborts other pending requests", async () => {
  expect(api.unlock).toBeTypeOf("function");
  await api.unlock(KEY);
  const changed = vi.fn();
  const unsubscribe = api.subscribeAuth(changed);
  let finish;
  let pendingSignal;
  fetch.mockImplementationOnce((_url, options) => {
    pendingSignal = options.signal;
    return new Promise(resolve => { finish = resolve; });
  });
  const pending = api.getRoutes();
  fetch.mockResolvedValueOnce(json({}, 401));
  await expect(api.approve("call", false)).rejects.toThrow(/key.*not accepted/i);
  expect(pendingSignal.aborted).toBe(true);
  finish(json());
  await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  expect(changed).toHaveBeenCalledWith(false, expect.any(String));
  await expect(api.getRoutes()).rejects.toThrow(/unlock/i);
  unsubscribe();
});
