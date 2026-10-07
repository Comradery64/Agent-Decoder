/**
 * API response types — mirrors the Python Pydantic models exactly.
 * This is the contract between frontend and backend.
 * Both Agent 3 and Agent 4 build against these types.
 */

// ---------------------------------------------------------------------------
// Content blocks
// ---------------------------------------------------------------------------

export interface TextBlock {
  type: "text";
  text: string;
}

export interface ToolUseBlock {
  type: "tool_use";
  id: string;
  name: string;
  input: Record<string, unknown>;
}

export interface ToolResultBlock {
  type: "tool_result";
  tool_use_id: string;
  content: string | Array<{ type: string; text?: string }> | null;
  is_error: boolean;
}

export interface ThinkingBlock {
  type: "thinking";
  thinking: string;
  signature?: string;
}

export interface ImageBlock {
  type: "image";
  source: Record<string, unknown>;
}

export type ContentBlock =
  | TextBlock
  | ToolUseBlock
  | ToolResultBlock
  | ThinkingBlock
  | ImageBlock;

// ---------------------------------------------------------------------------
// Message
// ---------------------------------------------------------------------------

export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
}

export interface Message {
  id: string;
  session_id: string;
  parent_id: string | null;
  role: "user" | "assistant" | "system";
  content_blocks: ContentBlock[];
  timestamp: string | null; // ISO 8601
  model: string | null;
  is_sidechain: boolean;
  is_meta: boolean;
  cwd: string | null;
  git_branch: string | null;
  source_tool_assistant_uuid: string | null;
  usage: TokenUsage | null;
  provider?: string; // "claude" | "codex" — drives provider skin
}

export interface MessageTree {
  message: Message;
  children: MessageTree[];
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

export interface Session {
  id: string;
  project_id: string;
  file_path: string;
  title: string | null;
  // Server-side rename override (issue #11). When set, this is what the user
  // typed in the sidebar. Frontend overlays it on top of `title`; both fields
  // are sent so original parse stays visible (tooltip, search debugging).
  custom_title: string | null;
  // Server-persisted flags (previously localStorage-only on the FE; bug fix
  // 2026-05-28). ISO-8601 string when set, null when unset.
  archived_at: string | null;
  starred_at: string | null;
  viewed_at: string | null;
  model: string | null;
  started_at: string | null;
  updated_at: string | null;
  message_count: number;
  user_message_count: number;
  cwd: string | null;
  git_branch: string | null;
  is_worktree: boolean;
  is_fork: boolean;
  permission_mode: string | null;
  last_message_role: "user" | "assistant" | "system" | null;
  provider?: string; // "claude" | "codex" — drives provider skin
}

// Provider capabilities + runtime drivability (GET /api/providers). The FE
// drives every interactive affordance off `effective` so a read-only or
// non-drivable provider never shows a send/Native/edit control that misfires.
export interface ProviderCaps {
  can_send: boolean;
  can_resume: boolean;
  can_fork: boolean;
  can_edit: boolean;
}

export interface DriverAvailabilityInfo {
  available: boolean;
  reason: string | null;
}

export interface ProviderInfo {
  name: string;
  caps: ProviderCaps; // static, declared by the adapter
  availability: DriverAvailabilityInfo; // runtime (tmux/codex present?)
  effective: ProviderCaps; // caps reconciled with availability
  driver_backed: boolean; // routed through the tmux DriverManager?
}

export interface SessionDetail extends Session {
  messages: Message[];
  total_message_count?: number;
  /** False when the session's project working directory no longer
   * exists on disk (resolved_path is null in the DB). Sending in such
   * a session would fail downstream when claude tries to spawn with a
   * nonexistent cwd, so the UI short-circuits with a clear error. */
  cwd_exists: boolean;
}

// ---------------------------------------------------------------------------
// Project
// ---------------------------------------------------------------------------

export interface Project {
  id: string;
  display_name: string;
  raw_path: string;
  resolved_path: string | null;
  data_source: string;
  session_count: number;
  last_activity_at: string | null;
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

export interface SearchHit {
  session_id: string;
  session_title: string | null;
  project_id: string;
  message_id: string;
  role: string;
  snippet: string;
  timestamp: string | null;
  source?: "message" | "ephemeral" | string;
  kind?: string | null;
  responds_to?: number | null;
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface Profile {
  id: string;
  name: string;
  data_paths: string[];
  color: string;
}

export type PermissionMode =
  | "dontAsk"
  | "acceptEdits"
  | "bypassPermissions"
  | "auto"
  | "plan"
  | "default";

export type ChatSendShortcut = "enter" | "modEnter";
export type NativePtyFontFamily =
  | "monaspace-argon"
  | "source-code-pro"
  | "fira-code"
  | "jetbrains-mono"
  | "ioskeley-mono"
  | "libertinus-mono"
  | "antithesis"
  | "thesansmono-condensed"
  | "xanh-mono"
  | "julia-mono"
  | "spline-sans-mono"
  | "system-monospace";

export interface AppConfig {
  data_paths: string[];
  profiles: Profile[];
  active_profile_id: string | null;
  theme: "light" | "dark" | "system";
  auto_open_browser: boolean;
  port: number;
  host: string;
  edit_enabled: boolean;
  show_provider_tag: boolean;
  claude_default_permission_mode: PermissionMode;
  chat_send_shortcut: ChatSendShortcut;
  native_pty_font_family: NativePtyFontFamily;
  native_pty_cols: number;
  claude_auto_stop_quiet_default_turns: boolean;
  claude_recap_enabled: boolean;
  claude_recap_idle_minutes: number;
}

export interface HostInfo {
  // True when the request did NOT originate from a loopback address.
  // Used to gate UI actions that run on the SERVER's host (osascript,
  // Finder/Terminal launches, etc.).
  is_remote_client: boolean;
  // Server's platform — informs whether host-side actions are even supported.
  platform: "darwin" | "linux" | "win32" | string;
  client_host: string | null;
  // App version, sourced from the backend's single source of truth
  // (agent_decoder.__version__). Shown in Settings ▸ About.
  version: string;
}

// Backend-cached PyPI version check (see update_check.py) backing the
// About panel's "update available" banner.
export interface UpdateCheck {
  current_version: string;
  latest_version: string | null;
  update_available: boolean;
}

// ---------------------------------------------------------------------------
// Recap
// ---------------------------------------------------------------------------

export interface Recap {
  id: number;
  session_id: string;
  text: string;
  created_at: string;
  covers_until_message_uuid: string | null;
  dismissed: boolean;
}

export interface ProfilesResponse {
  profiles: Profile[];
  active_profile_id: string | null;
}

// ---------------------------------------------------------------------------
// Runner status — batch shape served by /api/runner-status.
// Derived from PtyManager state on the server (Phase 6+); the only
// consumer is the sidebar busy-pulse.
// ---------------------------------------------------------------------------

export interface RunnerStatus {
  busy: boolean;
  last_error: string | null;
  permission_mode: PermissionMode | null;
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------

export interface StatsResponse {
  total_projects: number;
  total_sessions: number;
  total_messages: number;
  data_paths: string[];
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------

export interface TokenBreakdown {
  session_id?: string;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  total: number;
}

export interface PromptCostEntry {
  user_message_id: string;
  assistant_message_id: string;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  total: number;
}

export interface DailyBucket {
  day: string; // ISO date
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  total: number;
  prompt_count: number;
  session_count: number;
}

// ---------------------------------------------------------------------------
// Cost
// ---------------------------------------------------------------------------

export interface ModelCostEntry {
  model: string;
  input_usd: number;
  output_usd: number;
  cache_write_usd: number;
  cache_read_usd: number;
  total_usd: number;
  pricing_known: boolean;
}

export interface SessionCostResponse {
  session_id: string;
  models: ModelCostEntry[];
  total_usd: number;
  pricing_known: boolean;
  pricing_source: "live" | "hardcoded";
}

export interface ModelPricingEntry {
  model: string;
  input_per_mtok: number;
  output_per_mtok: number;
  cache_write_per_mtok: number;
  cache_read_per_mtok: number;
}

export interface PricingTableResponse {
  source: "live" | "hardcoded";
  models: ModelPricingEntry[];
}

// ---------------------------------------------------------------------------
// Phase 4 stats
// ---------------------------------------------------------------------------

export interface TokenDistribution {
  count: number;
  mean: number;
  median: number;
  p95: number;
  min: number;
  max: number;
}

export interface PromptStatsResponse {
  prompt_count: number;
  input_tokens: TokenDistribution | null;
  output_tokens: TokenDistribution | null;
  total_tokens: TokenDistribution | null;
}

export interface ModelUsageEntry {
  model: string;
  message_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface ToolUsageEntry {
  tool: string;
  count: number;
}

export interface FileTouchEntry {
  file: string;
  count: number;
}

// ---------------------------------------------------------------------------
// Phase 5 tips
// ---------------------------------------------------------------------------

export interface TipEntry {
  rule_id: string;
  severity: "info" | "warning" | "error";
  title: string;
  detail: string;
  evidence: string[];
}

// ---------------------------------------------------------------------------
// Phase 6 editing
// ---------------------------------------------------------------------------

export interface MessageContentPatch {
  content_blocks: ContentBlock[];
}

export interface MutationResult {
  ok: boolean;
  session_id?: string;
}

// ---------------------------------------------------------------------------
// File system browser
// ---------------------------------------------------------------------------

export interface DirEntry {
  name: string;
  type: "file" | "dir";
  size: number | null;
  modified: number;
}

export interface DirListing {
  path: string;
  entries: DirEntry[];
}

export interface FileContent {
  path: string;
  name: string;
  content: string;
  size: number;
  language: string | null;
}

// ---------------------------------------------------------------------------
// Phase 7 export
// ---------------------------------------------------------------------------

export type ExportFormat = "json" | "md";

// ---------------------------------------------------------------------------
// PTY runner (pty-runner-plan.md)
// ---------------------------------------------------------------------------

export interface PtyStatus {
  alive: boolean;
  last_activity_ms: number;
  last_input_ms: number;
  last_pty_output_ms: number;
  idle_kill_at_ms: number | null;
}

export type NativePtyState =
  | "booting"
  | "idle_chat_input"
  | "assistant_streaming"
  // codex's term for a turn in flight ("esc to interrupt") — the driver state
  // poller (driver_manager._STATE_TO_NATIVE) emits this for Codex where claude
  // emits "assistant_streaming". Same meaning: the agent is working, not blocked.
  | "running"
  | "slash_palette_open"
  | "ask_user_question"
  | "permission_prompt"
  | "login_required"
  | "trust_prompt"
  | "model_selector"
  | "btw_modal"
  | "native_input_required"
  | "unknown_interactive"
  | "dead";

export interface PtyNativeSnapshot {
  session_id: string;
  ring_b64: string;
  ring_complete?: boolean;
  rows: number;
  cols: number;
  alive: boolean;
  native_state: NativePtyState;
  decoded_input_safe: boolean;
}

export interface PtyOutputChunk {
  session_id: string;
  data_b64: string;
}

export interface PtyNativeStateEvent {
  session_id: string;
  state: NativePtyState;
  decoded_input_safe: boolean;
}

// Phase-0 ownership snapshot (pty-ownership-plan.md). Driven by the
// hybrid pgrep + lsof detector on the BE; reports who, if anyone, is
// already attached to the session id.
//   ours     — a live PtyChannel exists in THIS clau-decode instance.
//   terminal — at least one foreign claude process is attached.
//   idle     — no one is attached.
export type PtyOwnershipStatus = "ours" | "terminal" | "idle";

export interface PtyOwnership {
  status: PtyOwnershipStatus;
  foreign_pids: number[];
  jsonl_path: string | null;
  // Phase-1: structured metadata from the .lock sidecar when present.
  // null when no lock sidecar exists (e.g. an unwrapped terminal
  // claude that Phase-0's pgrep detector caught).
  foreign_owner: PtyForeignOwner | null;
}

export interface PtyForeignOwner {
  kind: string; // "clau-decode" | "claude-wrapper" | …
  pid: number;
  hostname: string;
  ui_endpoint: string | null;
  heartbeat_at: string; // ISO-8601
}

// Takeover responses share a shape on success; the 409 timeout body
// surfaces via the standard HTTPException detail and is read inline at
// the call site.
export interface PtyTakeoverResponse {
  ok: boolean;
  released_pids: number[];
  still_held_by: number[];
}

// ---------------------------------------------------------------------------
// Ephemeral messages (/btw capture — Phase 2, pty-runner-plan.md)
// ---------------------------------------------------------------------------

export interface EphemeralMessage {
  id: number;
  session_id: string;
  kind: string;           // "btw" for v1; extensible
  role: "user" | "assistant";
  content: string;
  responds_to: number | null;  // self-FK on the paired input row
  timestamp: string;           // ISO 8601
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

export interface DashboardSession {
  id: string;
  title: string | null;
  project_id: string;
  models: string[];
  message_count: number;
  total_usd: number;
  updated_at: string | null;
  last_message_role: "user" | "assistant" | "system" | null;
}

export interface DashboardProject {
  id: string;
  display_name: string;
  session_count: number;
  last_activity_at: string | null;
}

export interface DashboardTip {
  rule_id: string;
  severity: "info" | "warning" | "error";
  title: string;
  detail: string;
}

export interface DashboardData {
  recent_sessions: DashboardSession[];
  projects: DashboardProject[];
  model_usage: ModelUsageEntry[];
  total_cost_usd: number;
  total_sessions: number;
  total_messages: number;
  tips: DashboardTip[];
}
