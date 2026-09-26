import { useEffect, useRef, useState } from "react";
import { approve, getGeneration, logout } from "../lib/api";

export default function ApprovalDialog({ approval, onResolve }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  async function decide(ok) {
    if (pending) return;
    const epoch = getGeneration();
    const current = () => mounted.current && epoch === getGeneration();
    setPending(true);
    setError("");
    try {
      await approve(approval.callId, ok);
      if (current()) onResolve(ok);
    } catch (err) {
      if (current() && err.name !== "AbortError") setError(err.message);
    } finally {
      if (current()) setPending(false);
    }
  }

  return (
    <div className="overlay">
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="approval-title">
        <h3 id="approval-title">Approve tool call?</h3>
        <p>This tool is marked dangerous and needs confirmation.</p>
        <pre>{approval.tool}({JSON.stringify(approval.args, null, 2)})</pre>
        {error && <p role="alert">{error}</p>}
        <div className="dialog-actions">
          <button className="ghost" onClick={() => logout()}>Logout</button>
          <button className="ghost" disabled={pending} onClick={() => decide(false)}>Deny</button>
          <button disabled={pending} onClick={() => decide(true)}>Approve</button>
        </div>
      </div>
    </div>
  );
}
