import { useEffect, useState } from "react";
import { Database, RefreshCw } from "lucide-react";
import { Tools } from "./Tools";
import { Mcp } from "./Mcp";
import { Skills } from "./Skills";
import { Memory } from "./Memory";
import { Calls } from "./Calls";
import { Empty, ErrorLine, Fields, IconButton, ReadState, Status, navigate, route, tabs, text, useRead, when, type Item, type Listing } from "./shared";
import "../capabilities.css";

export function CapabilityCenter({ conversations, runs, onRun }: { conversations: Item[]; runs: Item[]; onRun: (id: string) => void }) {
  const [location, setLocation] = useState(route), [refresh, setRefresh] = useState(0);
  useEffect(() => { const listener = () => setLocation(route()); window.addEventListener("hashchange", listener); return () => window.removeEventListener("hashchange", listener); }, []);
  const { tab, id } = location;
  return <main className={`cap-center ${id ? "cap-has-detail" : ""}`}><div className="page-heading"><h1>能力中心</h1><IconButton label="刷新状态" onClick={() => setRefresh(v => v + 1)}><RefreshCw size={18} /></IconButton></div>
    <div className="cap-tabs" role="tablist" aria-label="能力中心">{tabs.map(([value, name]) => <button role="tab" key={value} aria-selected={tab === value} onClick={() => navigate(value, "", undefined, true)}>{name}</button>)}</div>
    <section role="tabpanel" aria-label={tabs.find(([value]) => value === tab)?.[1]}>
      {tab === "overview" && <Overview refresh={refresh} />}{tab === "tools" && <Tools id={id} refresh={refresh} />}{tab === "mcp" && <Mcp id={id} refresh={refresh} />}
      {tab === "skills" && <Skills id={id} refresh={refresh} />}{tab === "memory" && <Memory id={id} refresh={refresh} conversations={conversations} runs={runs} onRun={onRun} />}{tab === "calls" && <Calls id={id} refresh={refresh} onRun={onRun} />}
    </section></main>;
}

function Overview({ refresh }: { refresh: number }) {
  const catalog = useRead<Listing>("/api/capabilities?limit=50", refresh);
  const mcp = useRead<Listing>("/api/mcp/connections", refresh), skills = useRead<Listing>("/api/skills", refresh);
  const data = catalog.data;
  return <><ReadState {...catalog} /><ErrorLine error={data?.error} retry={catalog.reload} /><div className="cap-metrics">{[["工具", "tools", data], ["MCP", "mcp", mcp.data], ["SKILLS", "skills", skills.data]].map(([name, tab, result]) => {
    const listing = result as Listing | null;
    return <button key={String(tab)} onClick={() => navigate(String(tab))}><span>{String(name)}</span><strong>{listing?.read_only ? `未核验 / ${listing.total}` : listing ? `${listing.counts?.available ?? listing.rows.filter(row => row.enabled && (!row.health || row.health.status === "ready")).length} / ${listing.counts?.registered ?? listing.total ?? listing.rows.length}` : "未读取"}</strong>{listing?.next_cursor && !listing.counts && <small>当前页可用数 / 登记总数</small>}</button>;
  })}<div><span><Database size={14} />PostgreSQL</span><Status value={data?.database?.status} /></div></div>
    <ReadState {...mcp} /><ReadState {...skills} />{data?.database && <section className="cap-band"><Fields values={[["文档", data.database.documents], ["已索引", data.database.indexed_documents], ["空文本跳过", data.database.empty_documents ?? data.database.empty_text_documents], ["待索引", data.database.pending_documents], ["统计快照", when(data.database.observed_at || data.database.snapshot_at)], ["索引", data.database.index_ready === undefined ? undefined : data.database.index_ready ? "已就绪" : "未就绪"]]} /></section>}
    <section className="cap-band"><h2>待处理</h2>{data?.issues?.length ? [...data.issues].sort((a: Item, b: Item) => (a.priority ?? 99) - (b.priority ?? 99)).map((issue: Item, index: number) => <div className="cap-issue" key={issue.id || index}><span>{text(issue.message || issue.error || issue.description)}</span><button className="text-button" onClick={() => issue.kind === "database" ? catalog.reload() : navigate(issue.kind === "skill" ? "skills" : issue.resource_id?.startsWith("mcp_") ? "mcp" : "tools", issue.resource_id || "")}>{issue.kind === "database" ? "重试连接" : `查看${issue.kind === "skill" ? "技能" : issue.resource_id?.startsWith("mcp_") ? "连接" : "工具"}`}</button></div>) : data && <Empty>暂无待处理项</Empty>}{skills.data && !skills.data.rows.length && <Empty><span>尚未导入技能</span><button className="text-button" onClick={() => navigate("skills", "import")}>导入技能</button></Empty>}</section>
    <section className="cap-band"><h2>最近调用</h2>{data?.recent_calls?.length ? <table className="cap-table"><thead><tr><th>时间</th><th>能力</th><th>结果</th><th className="cap-secondary">总墙钟</th></tr></thead><tbody>{data.recent_calls.map((call: Item) => <tr key={call.id}><td>{when(call.started_at)}</td><td><button className="cap-row-link" onClick={() => navigate("calls", call.id!, { resource_id: call.resource_id || "" })}>{text(call.resource_name || call.resource_id)}</button></td><td><Status value={call.status} /></td><td className="cap-secondary">{text(call.wall_ms)} ms</td></tr>)}</tbody></table> : <Empty>{data?.recent_calls_available === false ? "数据库离线，调用记录暂不可读取" : "未记录调用"}</Empty>}</section>
    <details className="cap-band"><summary>运行环境</summary><Fields values={Object.entries(data?.environment || {}).map(([key, value]) => [key, value])} /></details>
  </>;
}
