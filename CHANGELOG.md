# Changelog

All notable changes to Agent Decoder (formerly Clau-Decode) will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the project is in the 0.x series, breaking changes may land in any minor release; we'll call them out clearly.

## [Unreleased]

### Changed

- Renamed the project to **Agent Decoder R.I.N.G.** (Replay, Inspect, Navigate, Grep). The launcher is now `ring`; the `clau-decode` command is removed and the Python module is now `agent_decoder`.
- Config and index DB live in `agent-decoder/` dirs. Existing `clau-decode/` data is copied over on first run and left in place as a backup.

### Added

- Search query language: `"exact phrase"`, `-exclude`, `a OR b`, `prefix*`, and `role:` / `project:` / `session:` / `before:` / `after:` filters.

## [0.3.2] - 2026-09-07

### Added

- **`clau-decode migrate` subcommand** — merge and relocate Claude Code chat history
  and configs across machines. Folds one or more source config trees (e.g. a vanilla
  `~/.claude` plus mirrored profiles) into a destination `~/.claude`, rewriting a
  project-path prefix so chats stay viewable *and* resumable after the absolute project
  path changes. Carries top-level sessions and nested subagent transcripts, unions
  `.claude.json` project/trust entries and `history.jsonl` (deduped + time-sorted), and
  copies human configs non-destructively (a differing destination file is never
  overwritten — the incoming copy lands as a `.from-<source>` sidecar). Dry-run by
  default; `--apply` is gated behind `--i-have-a-backup`. Pure standard library.
- **Shift-click range select for session checkboxes.** In multi-select mode, click
  one checkbox then shift-click another to select every session between them
  (added to whatever's already selected) — the same convention as Finder/Gmail.

### Fixed

- **Post-migrate reindex guidance pointed at the wrong path and a destructive step.**
  The `migrate` runbook (and `README`/`ARCHITECTURE`/`config` docs) told users to
  `rm -f ~/.cache/clau-decode/index.db`, but the index moved to the durable
  `~/.local/share/clau-decode/index.db`, and deleting it discards non-regenerable
  state (stars, archives, custom titles). It also missed the real symptom: migrated
  files are copied verbatim and keep their old mtimes, so an incremental scan skips
  them. Guidance now points at `clau-decode --force-refresh`, which re-parses every
  session file without touching the DB.
- **A single deleted session file could silently break all scanning.** The
  background rescan and manual "Refresh" both iterate every configured provider
  in one pass; a file that vanished between discovery and the following `stat()`
  call threw unhandled, aborting the whole pass before later providers (e.g.
  Codex) ever ran. One stale reference meant no session — of any provider — got
  re-indexed until this was fixed.
- **Deleting a profile in Settings didn't stick.** Profile create/rename/delete
  mutate the server's config but never invalidated the browser's cached config
  snapshot, so reopening Settings (or even the next unrelated settings change)
  could silently replay the stale snapshot back to the server, resurrecting an
  already-deleted profile.
- **Profile management flow.** Fixed unsaved path text bleeding from one profile's
  "add path" field into another's when switching between them; every action
  (create/rename/recolor/delete/add path/remove path) now shows a saving state
  and surfaces errors instead of failing silently; delete now needs a second
  click within 3s to confirm instead of one accidental click; duplicate paths
  are rejected; rename has a visible pencil icon; and creating a profile now
  auto-expands it into the path field instead of leaving it silently pointed at
  `~/.claude`.
- **Codex-skin overlays (search, shortcuts, help) rendered as solid black**
  instead of a dim. The Codex dark skin pairs a near-black page background with
  a modal-overlay opacity *higher* than the standard dark theme uses on a much
  lighter background, leaving nothing to show through.

## [0.3.1.3] - 2026-06-15

### Fixed

- **Native terminal scrolling is no longer choppy/laggy in claude's live TUI.**
  The root cause was a backend hot path: on every PTY output chunk the server
  re-decoded and re-classified the *entire* output ring (up to 4 MB) on the
  event loop — ~290 ms per chunk on a long session — which starved PTY reads,
  SSE delivery, and input handling during a scroll. The classifier now reads
  only the last screenful (64 KB), making it constant-time (~4 ms) regardless of
  session length.

### Changed

- **PTY output is coalesced before broadcast.** claude emits a TUI repaint as
  many tiny (~1 KB) writes; each was becoming its own SSE event + reclassification
  + frontend repaint (~360 events per scroll flick). Reads are now batched and
  flushed once per ~12 ms frame as a single chunk, and `pty_native_state` is
  emitted only when the classified state actually changes (none during a scroll).
- **Native-view input is coalesced per animation frame** — a burst of wheel/mouse
  events becomes one ordered PTY write instead of one HTTP POST per event.
- Removed the `terminal.refresh()` scroll-settle workaround; it's unnecessary once
  the event loop is no longer blocked, and the default DOM renderer repaints
  faithfully on its own.

## [0.3.1.2] - 2026-06-15

### Fixed

- **Open session no longer looks empty/stale after the server restarts.** When
  the SSE event stream reconnects after a drop, the frontend now re-syncs —
  refetching the session list and the open conversation — instead of showing
  the view it had cached while disconnected.
- **Native terminal's first row is no longer clipped** by the conversation
  header's gradient fade — a small top inset on the terminal keeps claude's
  first prompt box fully visible.

### Changed

- **The index self-heals via a periodic rescan.** A lightweight 60s safety-net
  pass re-indexes any session files whose mtime moved while the live watcher was
  down or whose change events the OS dropped, across all profile data paths — so
  the Decoded view converges without requiring a restart.
- Refreshed the in-app app icon (Settings ▸ About).

## [0.3.1.1] - 2026-06-14

### Added

- **Settings ▸ About** — an Apple-style About panel (app icon, name, version,
  platform, and GitHub/Changelog/License links). The version is fetched from
  the backend, so the frontend holds no version string of its own.

### Changed

- **Version is single-sourced** from `src/clau_decode/__init__.py` (`__version__`).
  hatch packaging, `clau-decode --version`, the HTTP API (`/api/host-info`), and
  the About panel all derive from that one string — edit it in exactly one place
  per release. Also fixes `clau-decode --version`, which previously reported a
  stale `0.1.0`.

## [0.3.1] - 2026-06-14

### Added

- **`python -m clau_decode` entry point** for zero-install source runs, and a
  quickstart that recommends an isolated install (uv tool / pipx from git) to
  avoid PEP 668 "externally-managed" errors and PATH shadowing.

### Fixed

- **Native PTY: scrollback reaches the first message.** Re-opening an existing
  session in Native view now scrolls all the way to the top of the
  conversation. The captured terminal ring was being cleared on the
  post-spawn rows-only resize, discarding the oldest history; it is now
  preserved across height changes (only a width change resets it).
- **Native PTY: no cross-session bleed on the first switch.** Flipping from one
  session to another no longer briefly shows the previous session's terminal
  before the new one loads — a captured snapshot is now replayed only into the
  terminal for the session it belongs to.
- **Native PTY: the terminal fills the pane, with no bottom gap or smear.** The
  PTY now spawns at the fitted viewport height instead of a default 40 rows and
  then resizing, so claude's input/status footer sits at the bottom of the pane
  (no large empty area below it) and the grow no longer leaves stale "smeared"
  rows. Removes the one-shot Ctrl+L repaint that masked this.

## [0.3.0] - 2026-06-13

### Changed

- **PTY runner is the canonical send path.** Chat-input messages are now
  written through a hidden pseudo-terminal attached to the local `claude`
  CLI in interactive TUI mode, so they stay inside the user's
  subscription. The legacy `claude --print` (stream-json) runner has been
  removed alongside its three HTTP endpoints
  (`/api/sessions/{id}/send-message`, `/api/sessions/{id}/stop`,
  `/api/sessions/{id}/runner-status`). The sidebar busy badge keeps
  working through `/api/runner-status?ids=...` (batch), now derived
  purely from `PtyManager` state.
- **Recap runs through a forked PTY too.** `src/clau_decode/recap_runner.py`
  now spawns `claude --session-id <fork> --resume <source> --fork-session
  --permission-mode dontAsk` on a hidden PTY, writes the recap prompt
  one byte at a time (real claude's TUI fork drops bulk writes during
  bootstrap), polls the fork's JSONL for the assistant turn, and unlinks
  the fork JSONL on the way out. No `claude --print` spawn site remains
  in the tree — clau-decode stays inside the no-additional-cost envelope
  after the 2026-06-15 billing change.

### Removed

- Frontend: `api.sendMessage`, `api.stopMessage`, `api.getRunnerStatus`
  (single), and the `useQuietWarning` hook + `QuietWarningBanner`.
  Quiet-warning was a `default`-mode watchdog specific to the headless
  runner; the PTY's `pty_input_stalled` SSE event covers the equivalent
  failure mode with a deterministic signal.
- Backend: `claude_runner.py` is gone. `RunnerStatus` shed the
  `quiet_warning` and `quiet_age_seconds` fields.

## [0.2.0] - 2026-05-18

### Added

- **Message history in chat input** — Up/Down arrow keys cycle through all past user messages from the conversation (parsed from JSONL). Up at cursor position 0 recalls older messages; Down at end of text moves forward. Normal cursor movement is preserved everywhere else.
- **Model selector** — pick Auto / Opus / Sonnet / Haiku directly in the chat input bar, no trip to settings required.
- **File explorer** — `Cmd+Shift+E` toggles a resizable file explorer panel beside the conversation.
- **New Task** — `Cmd+Shift+O` or the "New Task" button starts a fresh, empty session without an auto-greeting.
- **Sidebar drag-resize & collapse** — drag the right edge to resize the sidebar; drag past the snap threshold to collapse it with a smooth animation. Width persists across changes (not just drag release).
- **Favicon set & webmanifest** — multi-size favicons and a PWA manifest for a proper browser tab identity.
- **Readline-style editing** — `Ctrl+E` jumps to end of line, `Ctrl+K` kills to end of line, in the chat textarea.
- **Active session marker** — sidebar shows a pulsing indicator on sessions that are currently streaming.
- **Real-time session rename sync** — renaming a session propagates to all connected browser tabs instantly.
- **Worktree support** — resume and open-terminal commands now understand git worktrees.
- **Remote viewer support** — host-only actions (edit, send) are disabled for remote viewers to prevent accidents.
- **Full-length session titles** — titles are no longer truncated at 80 characters; sidebar shows the full text with hover overflow.

### Changed

- **Dependency upgrades** — React 18 → 19, Vite 5 → 8, Zustand 4 → 5, Vitest 2 → 4.
- **Sidebar header** — no gradient fade; cleaner, flatter look. Collapsed state shows full-width toggle button.
- **Context menus** — submenus flip direction when near the viewport edge instead of overflowing off-screen.
- **Dark mode code blocks** — increased border alpha so the divider between header and body is visible.
- **Thought chain titles** — verbs are capitalized and always include a noun for multi-tool turns.

### Fixed

- **Search scroll-to-message** — uses `getBoundingClientRect` for accurate positioning; results ordered by timestamp.
- **Scroll position** — re-pins correctly across container resizes (file explorer open/close, sidebar toggle).
- **FTS5 search** — punctuation in queries no longer causes syntax errors; treated as plain text.
- **File preview editing** — separated from the global `edit_enabled` flag, fixing a 422 error on save.
- **File explorer breadcrumb** — stays on a single line instead of wrapping.
- **Sidebar collapse animation** — unified to 352 ms ease-out; auto-collapses on narrow windows.
- **Runner fallback** — falls back to plain `claude` when the inferred binary isn't on PATH.
- **Stale chunk imports** — gracefully handles changed content hashes on lazy-loaded bundles.
- **Title truncation** — removed the 80-char DB truncation; added a migration to restore full titles.
- **New Task auto-greeting** — new sessions start empty; no text injected on the user's behalf (#9).

### Security

## [0.1.0] - 2026-05-13

Initial public release.

### Added

- **Session browser** — conversations grouped by project, with star/archive, sort by recent/oldest/alphabetical, and hover previews.
- **Global search** — `Cmd+K` searches across every session including content, tool use, file paths, and thinking blocks. Inline live-search bar on the home page for quick lookups.
- **Conversation viewer** — rendered markdown with syntax-highlighted code blocks, tool-use blocks, thinking blocks, and sidechain branches for sub-agent conversations.
- **Analytics dashboard** — daily, weekly, and per-session token and cost breakdowns, model usage trends, top tools, most-touched files, and a 30-day activity heatmap.
- **File viewer** — resizable split pane with word-wrapped source, markdown preview, in-place editing (`Cmd+S`), and a sandbox limited to session-related directories.
- **Multi-profile support** — switch between separate config directories (multiple Claude installations, sandboxes, etc.), each with its own data paths and color.
- **Headless CLI runner** — send messages to sessions directly from the web UI; drives the CLI in stream-json mode with an auto-stop watchdog and slash command support.
- **Export** — export any conversation as JSON or Markdown, with token counts and cost estimates included.
- **Recap generation** — headline insights surfaced on the home page when something surprising happens (a big week, a major model shift, a heavily-used tool).
- **Live updates** — file watcher tails session files in real time and the UI auto-refreshes when new messages arrive.
- **Tips engine** — flags repeated file reads, oversized tool results, and low cache-hit rates.
- **Keyboard shortcuts** — `Cmd+K`, `Cmd+O`, `Cmd+E`, `Cmd+S`, `Cmd+I`, `Cmd+B`, `Shift+Cmd+,`.

### Changed

- License changed from MIT to [FSL-1.1-Apache-2.0](LICENSE). All prior unreleased history was developed under MIT; the 0.1.0 release and everything after it ship under FSL-1.1-Apache-2.0, which converts to Apache 2.0 two years after each release.

[0.3.2]: https://github.com/Comradery64/Clau-Decode/compare/v0.3.1.3...v0.3.2
[0.3.1.2]: https://github.com/Comradery64/Clau-Decode/compare/v0.3.1.1...v0.3.1.2
[0.3.1.1]: https://github.com/Comradery64/Clau-Decode/compare/v0.3.1...v0.3.1.1
[0.3.1]: https://github.com/Comradery64/Clau-Decode/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Comradery64/Clau-Decode/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Comradery64/Clau-Decode/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Comradery64/Clau-Decode/releases/tag/v0.1.0
