export default function ChatMessage({ message }) {
  const { role, content, tool, args, running, ok, error, pending } = message;

  if (role === "tool") {
    return (
      <div className="msg tool">
        <span className={`tool-dot ${running ? "running" : ok ? "ok" : "fail"}`} />
        <code>{tool}({JSON.stringify(args)})</code>
        {running ? <span className="muted"> running…</span> : null}
      </div>
    );
  }

  if (role === "system") {
    return <div className={`msg system ${error ? "error" : ""}`}>{content}</div>;
  }

  return (
    <div className={`msg ${role}`}>
      <div className="bubble">
        {content}
        {pending && <span className="cursor">▋</span>}
      </div>
    </div>
  );
}
