// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "../src/App";
import * as api from "../src/lib/api";
vi.mock("../src/lib/api", async importOriginal => ({
  ...await importOriginal(), streamChat: vi.fn(),
}));
let container, root;
const KEY = "owner-ui-test-key-0123456789abcdef";
const json = (status = 200) => new Response("{}", { status });
beforeEach(async () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async () => json()));
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<App />));
});
afterEach(async () => {
  await act(async () => { api.logout(); root.unmount(); });
  container.remove();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});
async function type(element, value) {
  await act(async () => {
    const prototype = element.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value").set.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function submit(form) {
  await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
}
async function unlock() {
  const input = container.querySelector('input[type="password"]');
  expect(input).not.toBeNull();
  await type(input, KEY);
  await submit(input.closest("form"));
}

test("password unlock shows friendly errors, never persists key, and opens chat", async () => {
  const storage = vi.spyOn(Storage.prototype, "setItem");
  expect(container.querySelector("textarea")).toBeNull();
  fetch.mockResolvedValueOnce(json(401));
  await unlock();
  expect(container.textContent).toMatch(/key.*not accepted/i);
  fetch.mockResolvedValueOnce(json(503));
  await unlock();
  expect(container.textContent).toMatch(/not configured/i);
  await unlock();
  expect(container.querySelector('input[type="password"]')).toBeNull();
  expect(container.querySelector("textarea")).not.toBeNull();
  expect(storage).not.toHaveBeenCalled();
  storage.mockRestore();
});

test("logout clears private UI; late stream callbacks cannot repopulate a new session", async () => {
  let emit, finish;
  api.streamChat.mockImplementationOnce((_args, event) => {
    emit = event;
    return new Promise(resolve => { finish = resolve; });
  });
  await unlock();
  await type(container.querySelector("textarea"), "private message");
  await submit(container.querySelector("form.composer"));
  await act(async () => {
    emit({ type: "thread", thread_id: "private-thread" });
    emit({ type: "token", text: "private answer" });
    emit({ type: "route", route: "private-route", deployment: "test" });
    emit({ type: "done", stats: { cost_usd: 0, route: "private-stats" } });
    emit({ type: "approval_required", call_id: "secret", tool: "private-tool", arguments: {} });
  });
  expect(container.textContent).toContain("private answer");
  const button = [...container.querySelectorAll("button")].find(b => b.textContent === "Logout");
  expect(button).toBeDefined();
  await act(async () => button.click());
  expect(container.textContent).not.toContain("private-");
  expect(container.textContent).not.toContain("private answer");
  await unlock();
  await act(async () => {
    emit({ type: "token", text: "late private answer" });
    emit({ type: "approval_required", call_id: "late", tool: "late-private-tool" });
    finish();
  });
  expect(container.textContent).not.toContain("private");
  api.streamChat.mockResolvedValueOnce();
  await type(container.querySelector("textarea"), "fresh");
  await submit(container.querySelector("form.composer"));
  expect(api.streamChat.mock.calls.at(-1)[0].threadId).toBeNull();
  await act(async () => {
    fetch.mockResolvedValueOnce(json(401));
    await api.getRoutes().catch(() => {});
  });
  expect(container.querySelector('input[type="password"]')).not.toBeNull();
  expect(container.querySelector("textarea")).toBeNull();
});

test("approval failures are shown and logout remains reachable inside the dialog", async () => {
  let emit;
  api.streamChat.mockImplementationOnce((_args, event) => { emit = event; return Promise.resolve(); });
  await unlock();
  await type(container.querySelector("textarea"), "request approval");
  await submit(container.querySelector("form.composer"));
  await act(async () => emit({ type: "approval_required", call_id: "call", tool: "shell", arguments: {} }));
  const dialog = container.querySelector(".dialog");
  expect([...dialog.querySelectorAll("button")].find(b => b.textContent === "Logout")).toBeDefined();
  fetch.mockResolvedValueOnce(json(503));
  await act(async () => [...dialog.querySelectorAll("button")].find(b => b.textContent === "Approve").click());
  expect(dialog.textContent).toMatch(/not configured/i);
  await act(async () => [...dialog.querySelectorAll("button")].find(b => b.textContent === "Logout").click());
  expect(container.querySelector(".dialog")).toBeNull();
  expect(container.querySelector('input[type="password"]')).not.toBeNull();
});
