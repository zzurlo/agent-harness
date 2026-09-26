import { useEffect, useRef, useState } from "react";
import ChatMessage from "./components/ChatMessage";
import Composer from "./components/Composer";
import StatsBar from "./components/StatsBar";
import ApprovalDialog from "./components/ApprovalDialog";
import { getGeneration, logout, streamChat, subscribeAuth, warmup } from "./lib/api";
import UnlockForm from "./components/UnlockForm";

export default function App() {
  const [messages, setMessages] = useState([]);
  const [busy, setBusy] = useState(false);
  const [threadId, setThreadId] = useState(null);
  const [stats, setStats] = useState(null);
  const [route, setRoute] = useState(null);
  const [approval, setApproval] = useState(null);
  const [warm, setWarm] = useState(false);
  const bottomRef = useRef(null);
  const [unlocked, setUnlocked] = useState(false);
  const [authError, setAuthError] = useState("");
  const mounted = useRef(true);
  const renderGeneration = getGeneration();

  useEffect(() => {
    mounted.current = true;
    const unsubscribe = subscribeAuth((open, reason) => {
      setUnlocked(open);
      setAuthError(reason);
      if (!open) {
        setMessages([]);
        setThreadId(null);
        setStats(null);
        setRoute(null);
        setApproval(null);
        setBusy(false);
      }
    });
    return () => { mounted.current = false; unsubscribe(); logout(); };
  }, []);

  // The backend runs at min-replicas 0 to cost ~$0 when idle, which means a
  // 3-10s cold start. Firing warmup on page load hides it behind the time the
  // user spends typing their first message.
  useEffect(() => {
    warmup()
      .then(() => setWarm(true))
      .catch(() => setWarm(false));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function send(text) {
    if (!unlocked || !text.trim() || busy) return;
    const epoch = getGeneration();
    const current = () => mounted.current && epoch === getGeneration();
    setBusy(true);
    setStats(null);
    setMessages((m) => [
      ...m,
      { role: "user", content: text },
      { role: "assistant", content: "", pending: true },
    ]);

    try {
      await streamChat({ message: text, threadId }, (evt) => {
        if (!current()) return;
        switch (evt.type) {
          case "thread":
            setThreadId(evt.thread_id);
            break;
          case "route":
            setRoute(evt);
            break;
          case "token":
            setMessages((m) => {
              const next = [...m];
              const last = next[next.length - 1];
              next[next.length - 1] = {
                ...last,
                content: last.content + evt.text,
                pending: false,
              };
              return next;
            });
            break;
          case "tool_start":
            setMessages((m) => [
              ...m,
              { role: "tool", tool: evt.tool, args: evt.arguments, running: true },
              { role: "assistant", content: "", pending: true },
            ]);
            break;
          case "tool_end":
            setMessages((m) =>
              m.map((msg) =>
                msg.role === "tool" && msg.running
                  ? { ...msg, running: false, ok: evt.ok }
                  : msg,
              ),
            );
            break;
          case "approval_required":
            setApproval({ callId: evt.call_id, tool: evt.tool, args: evt.arguments });
            break;
          case "compacted":
            setMessages((m) => [
              ...m,
              { role: "system", content: `Context compacted to ${evt.messages} messages` },
            ]);
            break;
          case "budget_exceeded":
            setMessages((m) => [
              ...m,
              { role: "system", content: `Stopped: budget exceeded (${evt.reason})` },
            ]);
            break;
          case "error":
            setMessages((m) => [
              ...m,
              { role: "system", content: `Error: ${evt.message}`, error: true },
            ]);
            break;
          case "done":
            setStats(evt.stats);
            break;
          default:
            break;
        }
      });
    } catch (err) {
      if (current() && err.name !== "AbortError") setMessages((m) => [
        ...m,
        { role: "system", content: `Request failed: ${err.message}`, error: true },
      ]);
    } finally {
      if (current()) {
        setBusy(false);
        setMessages((m) => m.filter((msg) => !(msg.pending && !msg.content)));
      }
    }
  }

  return (
    <div className="app">
      <header>
        <h1>agent-harness</h1>
        <div className="header-right">
          {unlocked && <button className="ghost" onClick={() => logout()}>Logout</button>}
          {route && <span className="route-chip">{route.route} · {route.deployment}</span>}
          <span className={`warm-dot ${warm ? "on" : "off"}`} title={warm ? "Backend warm" : "Cold start likely"} />
        </div>
      </header>

      {!unlocked ? <main><UnlockForm reason={authError} /></main> : <>
      <main>
        {messages.length === 0 && (
          <div className="empty">
            <h2>Ask anything</h2>
            <p>
              Turns are routed to the cheapest capable model. Say “think hard” to
              escalate to the reasoning tier.
            </p>
          </div>
        )}
        {messages.map((m, i) => (
          <ChatMessage key={i} message={m} />
        ))}
        <div ref={bottomRef} />
      </main>

      {stats && <StatsBar stats={stats} />}
      <Composer onSend={send} disabled={busy} />

      {approval && (
        <ApprovalDialog
          approval={approval}
          onResolve={() => {
            if (mounted.current && renderGeneration === getGeneration()) setApproval(null);
          }}
        />
      )}
      </>}
    </div>
  );
}
