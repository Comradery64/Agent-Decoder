/**
 * Typed API client — all fetch calls go through here.
 * Base URL is always relative (same origin), so no env config needed.
 */

import type {
  AppConfig,
  ContentBlock,
  DailyBucket,
  DashboardData,
  DirListing,
  EphemeralMessage,
  ExportFormat,
  FileContent,
  HostInfo,
  FileTouchEntry,
  MutationResult,
  PermissionMode,
  PricingTableResponse,
  ProfilesResponse,
  Profile,
  ProviderInfo,
  Project,
  PtyNativeStateEvent,
  PtyNativeSnapshot,
  PtyOwnership,
  PtyOutputChunk,
  PtyStatus,
  PtyTakeoverResponse,
  Recap,
  RunnerStatus,
  SearchHit,
  Session,
  SessionDetail,
  ToolUsageEntry,
  UpdateCheck,
} from "./types";
import { emit } from "../utils/events";

// Re-export EphemeralMessage so callers can import from this module directly.
export type { EphemeralMessage };

// In-memory cache for AppConfig — shared between App's boot-time theme
// fetch and SettingsModal's first open so Settings paints instantly. Kept
// fresh by updateConfig and getConfigCached below.
let _cachedConfig: AppConfig | null = null;
let _configInflight: Promise<AppConfig> | null = null;

export function getCachedConfig(): AppConfig | null {
  return _cachedConfig;
}

export function getConfigCached(): Promise<AppConfig> {
  if (_configInflight) return _configInflight;
  _configInflight = fetch("/api/config")
    .then((r) => {
      if (!r.ok) throw new Error(`GET /api/config → ${r.status}`);
      return r.json() as Promise<AppConfig>;
    })
    .then((cfg) => { _cachedConfig = cfg; return cfg; })
    .finally(() => { _configInflight = null; });
  return _configInflight;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
  return res.json() as Promise<T>;
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`PUT ${path} → ${res.status}`);
  return res.json() as Promise<T>;
}

// FastAPI errors carry a JSON {detail: "..."} body. Surface that message so
// callers (and toasts) can show why a request failed, not just the status.
async function _errorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const data = await res.json();
    if (data && typeof data.detail === "string") return data.detail;
  } catch {
    // non-JSON body — fall through to the status line
  }
  return fallback;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const opts: RequestInit = { method: "POST" };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(await _errorMessage(res, `POST ${path} → ${res.status}`));
  return res.json() as Promise<T>;
}

function postKeepalive(path: string, body: unknown): boolean {
  const payload = JSON.stringify(body);
  if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
    return navigator.sendBeacon(
      path,
      new Blob([payload], { type: "application/json" }),
    );
  }
  void fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
    keepalive: true,
  }).catch(() => {});
  return true;
}

function del<T>(path: string): Promise<T> {
  return fetch(path, { method: "DELETE" }).then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json() as Promise<T>;
  });
}

function patch<T>(path: string, body: unknown): Promise<T> {
  return fetch(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json() as Promise<T>;
  });
}

export const api = {
  getRuns: (params = "") => get<import("./types").AgentRun[]>(`/api/runs${params}`),
  getRun: (id: string) => get<import("./types").AgentRun>(`/api/runs/${encodeURIComponent(id)}`),
  getProjects: () => get<Project[]>("/api/projects"),
  getProjectSessions: (projectId: string) =>
    get<Session[]>(`/api/projects/${encodeURIComponent(projectId)}/sessions`),
  getAllSessions: () => get<Session[]>("/api/sessions"),
  getSession: (sessionId: string) =>
    get<SessionDetail>(`/api/sessions/${encodeURIComponent(sessionId)}`),
  search: (q: string, projectId?: string, limit = 50, offset = 0) => {
    const params = new URLSearchParams({ q, limit: String(limit), offset: String(offset) });
    if (projectId) params.set("project", projectId);
    return get<SearchHit[]>(`/api/search?${params}`);
  },
  getDashboard: () => get<DashboardData>("/api/dashboard"),
  getHostInfo: () => get<HostInfo>("/api/host-info"),
  getUpdateCheck: () => get<UpdateCheck>("/api/update-check"),
  getProviders: () => get<ProviderInfo[]>("/api/providers"),
  getConfig: () => get<AppConfig>("/api/config"),
  updateConfig: (config: AppConfig) => {
    _cachedConfig = config;
    emit("config-updated", config);
    return put<AppConfig>("/api/config", config);
  },
  refresh: () => post<{ ok: boolean }>("/api/refresh"),
  revealSession: (sessionId: string) =>
    post<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(sessionId)}/reveal`),
  openTerminal: (sessionId: string) =>
    post<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(sessionId)}/open-terminal`),
  getDailyAnalytics: () => get<DailyBucket[]>("/api/analytics/daily"),
  getPricingTable: () => get<PricingTableResponse>("/api/pricing"),
  getToolUsage: (): Promise<ToolUsageEntry[]> =>
    get("/api/analytics/tools"),
  getFileTouches: (): Promise<FileTouchEntry[]> =>
    get("/api/analytics/files"),
  deleteMessage: (messageId: string): Promise<MutationResult> =>
    del(`/api/messages/${messageId}`),
  patchMessage: (messageId: string, content_blocks: ContentBlock[]): Promise<MutationResult> =>
    patch(`/api/messages/${messageId}`, { content_blocks }),

  // Profiles
  // These endpoints mutate config.profiles/active_profile_id server-side but
  // bypass the full-config PUT path, so _cachedConfig drifts out of sync with
  // the server unless invalidated here. A stale _cachedConfig gets reseeded
  // into SettingsModal on its next mount, and the next unrelated setting
  // change (theme, shortcut, ...) replays it via api.updateConfig's full PUT
  // — silently resurrecting a just-deleted profile. Nulling the cache forces
  // the next getConfigCached() to refetch the true server state instead.
  getProfiles: () => get<ProfilesResponse>("/api/profiles"),
  createProfile: (name: string, data_paths?: string[], color?: string) => {
    _cachedConfig = null;
    return post<Profile>("/api/profiles", { name, data_paths: data_paths || ["~/.claude"], color: color || "#b8956a" });
  },
  updateProfile: (id: string, updates: Partial<Pick<Profile, "name" | "data_paths" | "color">>) => {
    _cachedConfig = null;
    return put<Profile>(`/api/profiles/${encodeURIComponent(id)}`, updates);
  },
  deleteProfile: (id: string) => {
    _cachedConfig = null;
    return del<MutationResult>(`/api/profiles/${encodeURIComponent(id)}`);
  },
  setActiveProfile: (active_profile_id: string | null) => {
    _cachedConfig = null;
    return put<{ active_profile_id: string | null }>("/api/profiles/active", { active_profile_id });
  },

  // File system browser
  listDir: (path: string, showHidden = false) => {
    const params = new URLSearchParams({ path });
    if (showHidden) params.set("show_hidden", "true");
    return get<DirListing>(`/api/fs/list?${params}`);
  },
  readFile: (path: string) =>
    get<FileContent>(`/api/fs/read?${new URLSearchParams({ path })}`),
  writeFile: (path: string, content: string) =>
    put<{ ok: boolean; path: string; size: number }>("/api/fs/write", { path, content }),

  // Export (Phase 7)
  exportSession: (sessionId: string, format: ExportFormat) =>
    fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/export?format=${format}`
    ).then((r) => {
      if (!r.ok) throw new Error(`Export failed: ${r.status}`);
      const disposition = r.headers.get("content-disposition") || "";
      const match = disposition.match(/filename="([^"]+)"/);
      const filename = match ? match[1] : `session.${format}`;
      return r.blob().then((blob) => ({ blob, filename }));
    }),

  // Batch busy-snapshot for the sidebar pulse. Derived from PtyManager
  // state on the server; one request for all visible sessions instead of
  // one per session per poll tick.
  getRunnerStatuses: (sessionIds: string[]): Promise<Record<string, RunnerStatus>> =>
    get<Record<string, RunnerStatus>>(
      `/api/runner-status?ids=${sessionIds.map(encodeURIComponent).join(",")}`,
    ),

  // Recaps
  generateRecap: (sessionId: string) =>
    post<Recap>(`/api/sessions/${encodeURIComponent(sessionId)}/recap`),
  listRecaps: (sessionId: string, opts?: { includeDismissed?: boolean }) => {
    const params = new URLSearchParams();
    if (opts?.includeDismissed) params.set("include_dismissed", "true");
    const qs = params.toString();
    return get<Recap[]>(
      `/api/sessions/${encodeURIComponent(sessionId)}/recaps${qs ? `?${qs}` : ""}`,
    );
  },
  dismissRecap: (sessionId: string, recapId: number) =>
    post<{ ok: boolean; dismissed: boolean }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/recaps/${recapId}/dismiss`,
    ),

  // Session rename (issue #11). Pass null to clear the override.
  setSessionTitle: (sessionId: string, title: string | null) =>
    put<{ ok: boolean; id: string; custom_title: string | null }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/title`,
      { title },
    ),

  // Session flags — server-backed replacements for the old LS.ARCHIVED /
  // LS.STARRED / LS.VIEWED_AT (2026-05-28 bug fix: localStorage-only state
  // wasn't visible across browsers).
  setArchived: (sessionId: string, archived: boolean) =>
    put<{ ok: boolean; id: string; archived_at: string | null }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/archived`,
      { archived },
    ),
  setStarred: (sessionId: string, starred: boolean) =>
    put<{ ok: boolean; id: string; starred_at: string | null }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/starred`,
      { starred },
    ),
  setViewedAt: (sessionId: string, viewedAt: string | null) =>
    put<{ ok: boolean; id: string; viewed_at: string | null }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/viewed`,
      { viewed_at: viewedAt },
    ),
  // One-time migration: upload existing localStorage flags to the server.
  // FE clears the corresponding LS keys only after a 200 response.
  migrateLocalStorage: (payload: {
    archived: string[];
    starred: string[];
    viewed_at: Record<string, string>;
  }) =>
    post<{ ok: boolean; applied: { archived: number; starred: number; viewed_at: number } }>(
      "/api/sessions/migrate-localstorage",
      payload,
    ),

  // Bulk-delete sessions (destructive: removes .jsonl + DB rows).
  deleteSessions: (sessionIds: string[]): Promise<{ ok: boolean; deleted: string[]; failed: { id: string; error: string }[] }> =>
    post("/api/sessions/delete", { session_ids: sessionIds }),

  // Mint metadata for a brand-new Claude Code session (issue #9 — "New Task").
  // The backend returns a fresh uuid + cwd + permission_mode but does NOT spawn
  // the CLI. The session materialises on disk only when the user submits their
  // first message via ptySubmit; the watcher → SSE pipeline indexes it as
  // soon as the JSONL appears. Caller navigates to /chat/<session_id>.
  newSession: (opts?: { cwd?: string; permission_mode?: PermissionMode; provider?: string }) =>
    post<{ session_id: string; cwd: string; permission_mode: PermissionMode }>(
      "/api/sessions/new",
      opts ?? {},
    ),

  // PTY runner endpoints (pty-runner-plan.md)
  ptyFocus: (sessionId: string, model?: string, rows?: number) =>
    post<{ ok: boolean }>("/api/pty/focus", {
      session_id: sessionId,
      ...(model ? { model } : {}),
      // Native view passes the fitted row count so the PTY spawns at its final
      // height (no spawn-at-40-then-resize that smears/strands the footer).
      ...(rows ? { rows } : {}),
    }),
  ptyBlur: (sessionId: string) =>
    post<{ ok: boolean }>("/api/pty/blur", { session_id: sessionId }),
  ptySubmit: (sessionId: string, content: string, model?: string) =>
    post<{ ok: boolean }>("/api/pty/submit", {
      session_id: sessionId,
      content,
      ...(model ? { model } : {}),
    }),
  ptyKill: (sessionId: string) =>
    post<{ ok: boolean }>("/api/pty/kill", { session_id: sessionId }),
  ptyKillKeepalive: (sessionId: string) =>
    postKeepalive("/api/pty/kill", { session_id: sessionId }),
  ptyStatus: (sessionId: string) =>
    get<PtyStatus>(`/api/pty/status?${new URLSearchParams({ session_id: sessionId })}`),
  ptyNativeSnapshot: (sessionId: string) =>
    get<PtyNativeSnapshot>(
      `/api/pty/native-snapshot?${new URLSearchParams({ session_id: sessionId })}`,
    ),
  ptyInput: (sessionId: string, data: string) =>
    post<{ ok: boolean }>("/api/pty/input", { session_id: sessionId, data }),
  ptyResize: (sessionId: string, rows: number, cols: number) =>
    post<{ ok: boolean }>("/api/pty/resize", {
      session_id: sessionId,
      rows,
      cols,
    }),
  // Phase-0 ownership (pty-ownership-plan.md). Pure read — does not
  // spawn. Drives the ConversationHeader badge + the take-over banner
  // in ChatInputBar.
  ptyOwnership: (sessionId: string) =>
    get<PtyOwnership>(`/api/pty/ownership/${encodeURIComponent(sessionId)}`),
  // SIGINTs each foreign claude on the session and polls for fd
  // release for ~3 s. Resolves on success; rejects on 409 (timeout)
  // or 403 (cross-user pid).
  ptyTakeover: (sessionId: string) =>
    post<PtyTakeoverResponse>(`/api/pty/takeover/${encodeURIComponent(sessionId)}`, {}),

  // Phase 2 — ephemeral messages (/btw capture, pty-runner-plan.md).
  // Returns all ephemeral rows for the given session ordered by (timestamp, id).
  ptyEphemerals: (sessionId: string): Promise<EphemeralMessage[]> =>
    get<EphemeralMessage[]>(`/api/sessions/${encodeURIComponent(sessionId)}/ephemerals`),
};

/** Handlers for the SSE event types we know about (issue #11). */
export interface EventSourceHandlers {
  onRefresh: () => void;
  // Fired when the SSE stream RE-connects after a drop (e.g. the server was
  // restarted). The browser's EventSource auto-reconnects, but while it was
  // down the tab missed every refresh/meta event, so its session list and the
  // open conversation can be stale. Use this to re-sync (refetch list + open
  // session). NOT fired on the initial connect.
  onReconnect?: () => void;
  // Fired when another client mutates session metadata on the server (or
  // our own PUT echoes back). Each event carries the id plus only the
  // fields that changed (the rest are absent from the payload, NOT null).
  // - `title` for rename (issue #11)
  // - `archived_at` / `starred_at` / `viewed_at` for the server-backed
  //   flag mutations (2026-05-28 bug fix). ISO timestamp or null.
  onSessionMeta?: (payload: {
    id: string;
    title?: string | null;
    archived_at?: string | null;
    starred_at?: string | null;
    viewed_at?: string | null;
  }) => void;
  // Fired once after a localStorage migration completes (POST
  // /api/sessions/migrate-localstorage). Other tabs use this as a signal
  // to refetch their session list so the migrated flags appear.
  onSessionMetaBulkMigration?: (payload: {
    applied: { archived: number; starred: number; viewed_at: number };
  }) => void;
  // Fired when a brand-new driver chat (Codex) adopts its real rollout id: the
  // placeholder session the user is viewing should navigate to `new`. The live
  // driver was re-keyed in place, so the new id is already attachable.
  onSessionAdopted?: (payload: { old: string; new: string }) => void;
  // PTY runner events (pty-runner-plan.md)
  // Emitted at ~1 minute remaining before idle kill.
  onPtyIdleWarn?: (e: { session_id: string; kill_in_seconds: number }) => void;
  // Phase 3: claude is prompting for API key / login.
  onAuthRequired?: (e: { session_id: string }) => void;
  // Phase 4: claude is prompting for trust (new cwd).
  onTrustRequired?: (e: { session_id: string; cwd: string }) => void;
  // Phase 4: PTY output stalled — possible hang.
  onStuckSession?: (e: { session_id: string }) => void;
  // The TUI has started emitting output in response to our last submit.
  // Backend fires ~500ms after the write. Frontend uses this purely as a
  // diagnostic / liveness signal (the JSONL-driven isActive still drives
  // the indicator's actual hide).
  onPtyInputAcknowledged?: (e: { session_id: string }) => void;
  // The TUI did NOT react to our last submit within the stall window
  // (~5s). The submit likely never reached the model. Frontend should
  // hide the "Thinking" indicator and surface an error.
  onPtyInputStalled?: (e: { session_id: string; elapsed_ms: number }) => void;
  // Terminal submit lifecycle for command-like submits that may not produce
  // a normal JSONL end_turn.
  onPtySubmitCompleted?: (e: {
    session_id: string;
    kind: string;
    status: string;
    input_id?: number | null;
    response_id?: number | null;
  }) => void;
  // Native terminal output chunks for the browser terminal renderer.
  onPtyOutputChunk?: (e: PtyOutputChunk) => void;
  // Conservative PTY screen-state classification.
  onPtyNativeState?: (e: PtyNativeStateEvent) => void;
  // Phase 2: a /btw input row has been persisted; response can still be pending.
  onEphemeralInputPersisted?: (e: {
    session_id: string;
    input_id: number;
    kind: string;
  }) => void;
  // Phase 2: a /btw exchange has been fully captured and persisted.
  // Frontend should refetch ephemerals for the session and re-render inline.
  onEphemeralPairPersisted?: (e: {
    session_id: string;
    input_id: number;
    response_id: number;
    kind: string;
  }) => void;
}

/** SSE event source — dispatches events by their `type` field. */
export function createEventSource(
  handlersOrRefresh: EventSourceHandlers | (() => void),
): EventSource {
  // Back-compat: callers passing just `onRefresh` still work — preserves the
  // existing App.tsx call site contract and the older test fixtures.
  const handlers: EventSourceHandlers =
    typeof handlersOrRefresh === "function"
      ? { onRefresh: handlersOrRefresh }
      : handlersOrRefresh;

  const es = new EventSource("/api/events");
  es.addEventListener("message", (e) => {
    try {
      const data = JSON.parse(e.data) as { type: string };
      if (data.type === "refresh") {
        handlers.onRefresh();
      } else if (data.type === "session_adopted") {
        const ev = data as { type: "session_adopted"; old: string; new: string };
        if (handlers.onSessionAdopted) {
          handlers.onSessionAdopted({ old: ev.old, new: ev.new });
        } else {
          console.debug("[SSE] session_adopted (no handler)", ev);
        }
      } else if (data.type === "session-meta") {
        // Payload only carries the fields that changed (rest are absent
        // from the dict, NOT null) — forward as-is so handlers can branch.
        const meta = data as {
          type: "session-meta";
          id: string;
          title?: string | null;
          archived_at?: string | null;
          starred_at?: string | null;
          viewed_at?: string | null;
        };
        handlers.onSessionMeta?.({
          id: meta.id,
          ...(("title" in meta) ? { title: meta.title } : {}),
          ...(("archived_at" in meta) ? { archived_at: meta.archived_at } : {}),
          ...(("starred_at" in meta) ? { starred_at: meta.starred_at } : {}),
          ...(("viewed_at" in meta) ? { viewed_at: meta.viewed_at } : {}),
        });
      } else if (data.type === "session-meta-bulk-migration") {
        const ev = data as {
          type: "session-meta-bulk-migration";
          applied: { archived: number; starred: number; viewed_at: number };
        };
        handlers.onSessionMetaBulkMigration?.({ applied: ev.applied });
      } else if (data.type === "pty_idle_warn") {
        const ev = data as { type: "pty_idle_warn"; session_id: string; kill_in_seconds: number };
        if (handlers.onPtyIdleWarn) {
          handlers.onPtyIdleWarn({ session_id: ev.session_id, kill_in_seconds: ev.kill_in_seconds });
        } else {
          console.debug("[SSE] pty_idle_warn (no handler)", ev);
        }
      } else if (data.type === "auth_required") {
        const ev = data as { type: "auth_required"; session_id: string };
        if (handlers.onAuthRequired) {
          handlers.onAuthRequired({ session_id: ev.session_id });
        } else {
          console.debug("[SSE] auth_required (no handler)", ev);
        }
      } else if (data.type === "trust_required") {
        const ev = data as { type: "trust_required"; session_id: string; cwd: string };
        if (handlers.onTrustRequired) {
          handlers.onTrustRequired({ session_id: ev.session_id, cwd: ev.cwd });
        } else {
          console.debug("[SSE] trust_required (no handler)", ev);
        }
      } else if (data.type === "stuck_session") {
        const ev = data as { type: "stuck_session"; session_id: string };
        if (handlers.onStuckSession) {
          handlers.onStuckSession({ session_id: ev.session_id });
        } else {
          console.debug("[SSE] stuck_session (no handler)", ev);
        }
      } else if (data.type === "pty_input_acknowledged") {
        const ev = data as { type: "pty_input_acknowledged"; session_id: string };
        if (handlers.onPtyInputAcknowledged) {
          handlers.onPtyInputAcknowledged({ session_id: ev.session_id });
        } else {
          console.debug("[SSE] pty_input_acknowledged (no handler)", ev);
        }
      } else if (data.type === "pty_input_stalled") {
        const ev = data as {
          type: "pty_input_stalled";
          session_id: string;
          elapsed_ms: number;
        };
        if (handlers.onPtyInputStalled) {
          handlers.onPtyInputStalled({
            session_id: ev.session_id,
            elapsed_ms: ev.elapsed_ms,
          });
        } else {
          console.debug("[SSE] pty_input_stalled (no handler)", ev);
        }
      } else if (data.type === "pty_submit_completed") {
        const ev = data as {
          type: "pty_submit_completed";
          session_id: string;
          kind: string;
          status: string;
          input_id?: number | null;
          response_id?: number | null;
        };
        if (handlers.onPtySubmitCompleted) {
          handlers.onPtySubmitCompleted({
            session_id: ev.session_id,
            kind: ev.kind,
            status: ev.status,
            input_id: ev.input_id,
            response_id: ev.response_id,
          });
        } else {
          console.debug("[SSE] pty_submit_completed (no handler)", ev);
        }
      } else if (data.type === "pty_output_chunk") {
        const ev = data as {
          type: "pty_output_chunk";
          session_id: string;
          data_b64: string;
        };
        if (handlers.onPtyOutputChunk) {
          handlers.onPtyOutputChunk({
            session_id: ev.session_id,
            data_b64: ev.data_b64,
          });
        } else {
          console.debug("[SSE] pty_output_chunk (no handler)", ev);
        }
      } else if (data.type === "pty_native_state") {
        const ev = data as PtyNativeStateEvent & { type: "pty_native_state" };
        if (handlers.onPtyNativeState) {
          handlers.onPtyNativeState({
            session_id: ev.session_id,
            state: ev.state,
            decoded_input_safe: ev.decoded_input_safe,
          });
        } else {
          console.debug("[SSE] pty_native_state (no handler)", ev);
        }
      } else if (data.type === "ephemeral_input_persisted") {
        const ev = data as {
          type: "ephemeral_input_persisted";
          session_id: string;
          input_id: number;
          kind: string;
        };
        if (handlers.onEphemeralInputPersisted) {
          handlers.onEphemeralInputPersisted({
            session_id: ev.session_id,
            input_id: ev.input_id,
            kind: ev.kind,
          });
        } else {
          console.debug("[SSE] ephemeral_input_persisted (no handler)", ev);
        }
      } else if (data.type === "ephemeral_pair_persisted") {
        const ev = data as {
          type: "ephemeral_pair_persisted";
          session_id: string;
          input_id: number;
          response_id: number;
          kind: string;
        };
        if (handlers.onEphemeralPairPersisted) {
          handlers.onEphemeralPairPersisted({
            session_id: ev.session_id,
            input_id: ev.input_id,
            response_id: ev.response_id,
            kind: ev.kind,
          });
        } else {
          console.debug("[SSE] ephemeral_pair_persisted (no handler)", ev);
        }
      }
    } catch {
      // ignore malformed events
    }
  });

  // Reconnect self-heal. EventSource fires "open" on the first connect AND on
  // every auto-reconnect after a drop (e.g. the server restarted). On a
  // reconnect — but not the first open — fire onReconnect so the app can
  // re-sync the events it missed while disconnected.
  let hasOpened = false;
  es.addEventListener("open", () => {
    if (hasOpened) {
      handlers.onReconnect?.();
    }
    hasOpened = true;
  });

  return es;
}
