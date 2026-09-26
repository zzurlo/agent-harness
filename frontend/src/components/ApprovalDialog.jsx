import { approve } from "../lib/api";

export default function ApprovalDialog({ approval, onResolve }) {
  async function decide(ok) {
    await approve(approval.callId, ok);
    onResolve(ok);
  }

  return (
    <div className="overlay">
      <div className="dialog">
        <h3>Approve tool call?</h3>
        <p>This tool is marked dangerous and needs confirmation.</p>
        <pre>{approval.tool}({JSON.stringify(approval.args, null, 2)})</pre>
        <div className="dialog-actions">
          <button className="ghost" onClick={() => decide(false)}>Deny</button>
          <button onClick={() => decide(true)}>Approve</button>
        </div>
      </div>
    </div>
  );
}
