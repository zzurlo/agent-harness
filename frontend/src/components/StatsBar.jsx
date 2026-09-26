export default function StatsBar({ stats }) {
  return (
    <div className="stats">
      <span><b>{stats.route}</b> · {stats.deployment}</span>
      <span>{stats.prompt_tokens} in / {stats.completion_tokens} out</span>
      {stats.tool_calls > 0 && <span>{stats.tool_calls} tool calls</span>}
      {stats.compactions > 0 && <span>{stats.compactions} compactions</span>}
      <span>${stats.cost_usd.toFixed(6)}</span>
      <span>{stats.elapsed}s</span>
    </div>
  );
}
