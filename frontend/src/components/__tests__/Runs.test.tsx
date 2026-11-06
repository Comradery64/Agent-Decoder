import { beforeEach, afterEach, describe, it, expect, vi } from "vitest";
import { render, screen, waitFor, fireEvent, cleanup, act } from "@testing-library/react";
import Runs from "../Runs";
import { api } from "../../api/client";
import type { AgentRun } from "../../api/types";
vi.mock("../../api/client", () => ({ api: { getRuns: vi.fn(), getRun: vi.fn() } }));
const row: AgentRun = { id: "run-1", native_session_id: "session-1", provider: "codex", model: "test-model", project_id: "project-1", cwd: "/tmp/project", git_branch: "feature", is_worktree: true, task_id: null, parent_run_id: null, state: "blocked", reason: "Transport detached; outcome unknown", provenance: "transport_unobserved", created_at: "2026-10-30T12:00:00Z", updated_at: "2026-10-30T12:00:00Z", last_observed_at: null, ended_at: null, source_available: false };
class Events { static latest: Events; onmessage: ((e: { data: string }) => void) | null = null; onopen: (() => void) | null = null; close = vi.fn(); constructor() { Events.latest = this; } }
beforeEach(() => { vi.stubGlobal("EventSource", Events); vi.mocked(api.getRuns).mockReset().mockResolvedValue([row]); vi.mocked(api.getRun).mockReset().mockResolvedValue(row); window.location.hash = "#/runs"; });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
describe("Runs", () => {
  it("shows loading, retained missing-source history and explicit uncertainty", async () => {
    render(<Runs />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading runs");
    expect(await screen.findByText("codex · test-model")).toBeInTheDocument();
    expect(screen.getByText(/Source chat unavailable/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Open decoded/ })).not.toBeInTheDocument();
    expect(screen.getByText(row.reason)).toBeInTheDocument();
  });
  it("loads deep-link detail outside the current page and follows browser navigation", async () => {
    window.location.hash = "#/runs/run-1";
    vi.mocked(api.getRuns).mockResolvedValue([]);
    render(<Runs />);
    expect(await screen.findByRole("region", { name: "Run details" })).toBeInTheDocument();
    expect(api.getRun).toHaveBeenCalledWith("run-1");
    act(() => { window.location.hash = "#/runs"; window.dispatchEvent(new HashChangeEvent("hashchange")); });
    await waitFor(() => expect(screen.queryByRole("region", { name: "Run details" })).not.toBeInTheDocument());
  });
  it("recovers from an API error using Retry and shows empty history", async () => {
    vi.mocked(api.getRuns).mockRejectedValueOnce(new Error("offline"));
    render(<Runs />);
    expect(await screen.findByRole("alert")).toHaveTextContent("offline");
    vi.mocked(api.getRuns).mockResolvedValue([]);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/No managed runs yet/)).toBeInTheDocument();
  });
  it("uses server filters, paging and confirmed-write SSE refresh", async () => {
    vi.mocked(api.getRuns).mockResolvedValue(Array.from({ length: 50 }, (_, i) => ({ ...row, id: `run-${i}` })));
    render(<Runs />);
    await screen.findByText("Page 1");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(vi.mocked(api.getRuns).mock.lastCall?.[0]).toContain("offset=50"));
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "claude" } });
    await waitFor(() => expect(vi.mocked(api.getRuns).mock.lastCall?.[0]).toContain("provider=claude"));
    expect(vi.mocked(api.getRuns).mock.lastCall?.[0]).toContain("offset=0");
    const count = vi.mocked(api.getRuns).mock.calls.length;
    act(() => Events.latest.onmessage?.({ data: JSON.stringify({ type: "runs_changed" }) }));
    await waitFor(() => expect(api.getRuns).toHaveBeenCalledTimes(count + 1));
  });
});
