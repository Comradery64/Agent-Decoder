import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { AgentRun } from "../api/types";
import { navigateTo, useRoute } from "../router";
import { useDebounce } from "../utils/useDebounce";
import "./Runs.css";

const states = ["queued", "starting", "running", "awaiting_input", "blocked", "completed", "failed", "canceled"];
const time = (value: string | null) => value ? new Date(value).toLocaleString() : "Not yet observed";
const label = (value: string) => value.replace(/_/g, " ");
const observation = (run: AgentRun) => !run.last_observed_at ? "—" : `${time(run.last_observed_at)}${!run.ended_at && Date.now() - Date.parse(run.last_observed_at) > 30000 ? " (stale)" : ""}`;

export default function Runs() {
  const route = useRoute();
  const runId = route.startsWith("/runs/") ? route.slice(6) : null;
  const [detail, setDetail] = useState<AgentRun | null>(null);
  const [detailMissing, setDetailMissing] = useState(false);
  const [page, setPage] = useState(0);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const query = useDebounce(search, 200);
  const [provider, setProvider] = useState("");
  const [state, setState] = useState("");
  const [project, setProject] = useState("");
  const projectQuery = useDebounce(project, 200);
  const generation = useRef(0);
  const refresh = useCallback(async () => {
    const request = ++generation.current;
    const params = new URLSearchParams({ provider, state, project: projectQuery, q: query, limit: "50", offset: String(page * 50) });
    try {
      const rows = await api.getRuns(`?${params}`);
      let selected: AgentRun | null = null;
      let missing = false;
      if (runId) {
        try { selected = await api.getRun(runId); }
        catch (e) { if (e instanceof Error && e.message.includes("404")) missing = true; else throw e; }
      }
      if (request !== generation.current) return;
      setRuns(rows); setDetail(selected); setDetailMissing(missing); setError("");
    } catch (e) {
      if (request === generation.current) setError(e instanceof Error ? e.message : "Could not load runs");
    } finally { if (request === generation.current) setLoading(false); }
  }, [provider, state, projectQuery, query, page, runId]);
  useEffect(() => {
    setLoading(true);
    void refresh();
    const events = new EventSource("/api/events");
    events.onmessage = (event) => {
      try { if (["runs_changed", "refresh"].includes(JSON.parse(event.data).type)) void refresh(); } catch { /* ignore malformed event */ }
    };
    events.onopen = () => void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => { ++generation.current; events.close(); window.clearInterval(timer); };
  }, [refresh]);
  const filtered = !!(provider || state || project || search || page);
  return <main className="runs-page">
    <header><h1>Runs</h1><p>Execution history across Claude and Codex.</p></header>
    <div className="runs-filters">
      <label>Search<input value={search} onChange={(e) => { setPage(0); setSearch(e.target.value); }} placeholder="Branch, model, session…" /></label>
      <label>Provider<select value={provider} onChange={(e) => { setPage(0); setProvider(e.target.value); }}><option value="">All providers</option><option>claude</option><option>codex</option></select></label>
      <label>State<select value={state} onChange={(e) => { setPage(0); setState(e.target.value); }}><option value="">All states</option>{states.map((s) => <option key={s} value={s}>{label(s)}</option>)}</select></label>
      <label>Project<input value={project} onChange={(e) => { setPage(0); setProject(e.target.value); }} placeholder="Project ID (exact)" /></label>
      <button onClick={() => void refresh()}>Refresh</button>
    </div>
    {error && <p role="alert">{error} <button onClick={() => void refresh()}>Retry</button></p>}
    {loading ? <p role="status">Loading runs…</p> : <>
      {runId && (detail ? <RunDetail run={detail} /> : detailMissing ? <p role="status">Run not found. <a href="#/runs">View all runs</a></p> : null)}
      {!error && !runs.length && <p role="status">{filtered ? "No runs match these filters." : "No managed runs yet. Open a session and attach its managed terminal to create a run. Historical chats are not automatically live runs."}</p>}
      <div className="runs-list">{runs.map((run) => <article key={run.id} className="run-card">
        <div className="run-card-title"><a href={`#/runs/${run.id}`}><strong>{run.provider} · {run.model || "Model unknown"}</strong></a><span className={`run-state run-state-${run.state}`}>{label(run.state)}</span></div>
        <p>{run.project_id || run.cwd || "Project unknown"} · {run.git_branch || "Branch unknown"}{run.is_worktree ? " · Worktree" : ""}</p>
        <p>{run.reason}</p>
        <small>Started {time(run.created_at)} · Last observed {observation(run)}</small>
        <p><SourceLink run={run} /></p>
      </article>)}</div>
      <nav className="runs-pagination" aria-label="Run pages"><button disabled={!page || loading} onClick={() => setPage(page - 1)}>Previous</button><span>Page {page + 1}</span><button disabled={runs.length < 50 || loading} onClick={() => setPage(page + 1)}>Next</button></nav>
    </>}
  </main>;
}

function SourceLink({ run }: { run: AgentRun }) {
  return run.source_available ? <a href={`#/chat/${run.native_session_id}`}>Open decoded chat / native terminal</a> : <span className="run-muted">Source chat unavailable; run history retained</span>;
}

function RunDetail({ run }: { run: AgentRun }) {
  const values = { "Run ID": run.id, "Native session": run.native_session_id, State: label(run.state), Reason: run.reason,
    "State evidence": run.provenance, Provider: run.provider, Model: run.model || "Unknown", Project: run.project_id || "Unknown", Directory: run.cwd || "Unknown", Branch: run.git_branch || "Unknown",
    Worktree: run.is_worktree ? "Yes" : "No", Created: time(run.created_at), Updated: time(run.updated_at), "Last observed": observation(run), Ended: run.ended_at ? time(run.ended_at) : "No ending recorded", Task: run.task_id || "Not linked", "Parent run": run.parent_run_id || "Not linked" };
  return <section aria-label="Run details" className="run-card run-detail">
    <div className="run-card-title"><h2>Run details</h2><button onClick={() => navigateTo("/runs")}>Close details</button></div>
    <dl>{Object.entries(values).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
    <p><SourceLink run={run} /></p>
    {!run.ended_at && <p className="run-muted">An idle prompt or process exit does not establish task success. Live state is best-effort. Check the last observation time; restart or transport loss may leave this attempt detached or unobserved.</p>}
  </section>;
}
