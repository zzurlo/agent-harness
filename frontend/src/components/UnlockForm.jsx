import { useEffect, useRef, useState } from "react";
import { unlock } from "../lib/api";

export default function UnlockForm({ reason }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  async function submit(event) {
    event.preventDefault();
    if (checking) return;
    const candidate = key;
    setKey("");
    setError("");
    setChecking(true);
    try {
      await unlock(candidate);
    } catch (err) {
      if (mounted.current && err.name !== "AbortError") {
        setError(err instanceof TypeError ? "Cannot reach the server. Please try again." : err.message);
      }
    } finally {
      if (mounted.current) setChecking(false);
    }
  }

  return (
    <form className="unlock" onSubmit={submit}>
      <h2>Private workspace</h2>
      <p>Enter the owner access key to unlock this single-owner conversation space.
        The key stays in memory and is cleared when you logout or reload.</p>
      <label htmlFor="access-key">Access key</label>
      <input id="access-key" type="password" value={key}
        onChange={event => { setKey(event.target.value); setError(""); }}
        autoComplete="off" autoCapitalize="none" spellCheck={false}
        required minLength={32} disabled={checking} autoFocus />
      {(error || reason) && <p className="auth-error" role="alert">{error || reason}</p>}
      <button type="submit" disabled={checking || !key}>
        {checking ? "Checking…" : "Unlock"}
      </button>
    </form>
  );
}
