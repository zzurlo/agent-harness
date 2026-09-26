import { useState } from "react";

export default function Composer({ onSend, disabled }) {
  const [text, setText] = useState("");

  function submit(e) {
    e.preventDefault();
    if (!text.trim() || disabled) return;
    onSend(text);
    setText("");
  }

  return (
    <form className="composer" onSubmit={submit}>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) submit(e);
        }}
        placeholder="Send a message…  (Shift+Enter for newline)"
        rows={1}
        disabled={disabled}
      />
      <button type="submit" disabled={disabled || !text.trim()}>
        {disabled ? "…" : "Send"}
      </button>
    </form>
  );
}
