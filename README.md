<h1 align="center">Agent Decoder R.I.N.G.</h1>

<p align="center">
  <strong>Replay, Inspect, Navigate, Grep.</strong><br>
  <em>Decode your agents' secret messages. "Send 10¢ and a box top!"</em>
</p>

<p align="center">
  Browse, search, and analyze your AI coding assistant chat history — entirely local, entirely private.
</p>

<p align="center">
  <a href="https://github.com/Comradery64/Agent-Decoder/actions/workflows/ci.yml">
    <img alt="CI" src="https://img.shields.io/github/actions/workflow/status/Comradery64/Agent-Decoder/ci.yml?branch=main&label=CI&style=flat-square">
  </a>
  <a href="LICENSE">
    <img alt="License" src="https://img.shields.io/badge/license-FSL--1.1--Apache--2.0-blue?style=flat-square">
  </a>
  <a href="pyproject.toml">
    <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square">
  </a>
  <a href="https://github.com/Comradery64/Agent-Decoder/blob/main/CHANGELOG.md">
    <img alt="Changelog" src="https://img.shields.io/badge/changelog-keep--a--changelog-orange?style=flat-square">
  </a>
</p>

<p align="center">
  <video
    src="https://github.com/user-attachments/assets/6f5a9a09-fbab-467f-82b1-c2e642dc8726"
    poster="https://github.com/Comradery64/Agent-Decoder/raw/main/docs/demo-poster.jpg"
    controls
    muted
    playsinline
    width="820">
    <a href="https://github.com/user-attachments/assets/6f5a9a09-fbab-467f-82b1-c2e642dc8726">
      <img src="https://github.com/Comradery64/Agent-Decoder/raw/main/docs/demo-poster.jpg" alt="Agent Decoder demo — click to play" width="820">
    </a>
  </video>
</p>

---

## Why Agent Decoder?

Your AI coding assistant writes a small mountain of JSONL session files into
`~/.claude/projects/` — useful, but unreadable and impossible to search by hand.
Agent Decoder reads those files locally, indexes them into SQLite, and serves a
fast browser UI with full-text search, conversation rendering, analytics, and a
recap engine.

## Quickstart

```bash
# Install as an isolated tool — its own virtualenv, on your PATH, nothing
# touches your system Python. Use whichever you already have:
uv tool install git+https://github.com/Comradery64/Agent-Decoder.git
# or:
pipx install git+https://github.com/Comradery64/Agent-Decoder.git

ring                 # opens http://localhost:4242

# update later — re-pull the latest from main (git installs need --force;
# plain `upgrade`/`reinstall` won't fetch new commits):
uv tool install --force git+https://github.com/Comradery64/Agent-Decoder.git
# or:
pipx install --force git+https://github.com/Comradery64/Agent-Decoder.git
```

Requires Python 3.10+. The wheel ships the pre-built frontend, so **no Node.js is needed** — only for development. All data stays on your machine — no telemetry.

> Just trying it out? Run without installing: `uvx --from git+https://github.com/Comradery64/Agent-Decoder.git ring`
> Prefer not to install? Run it straight from a clone — see [Development](#development).
> Advanced: `pip install` works too, but only **inside an activated virtualenv** — never your system/Homebrew Python (PEP 668 will block it, and a global install can shadow other checkouts).

### Troubleshooting: existing clone won't pull

This repo's history has been rewritten. If an old clone fails to update (e.g. `fatal: refusing to merge unrelated histories`, or diverged-branch errors), reset it to the new history:

```bash
git fetch origin && git reset --hard origin/main
```

## Features

### Session browser
- Conversations grouped by project, with star and archive
- Sort by recent, oldest, or alphabetical
- Hover preview before opening a session

### Search
- `Cmd+K` global search across every session — content, tool use, file paths, thinking blocks
- Inline live-search bar on the home page for quick lookups
- Results show highlighted excerpts and jump directly to the matching message

### Conversation viewer
- Rendered markdown with code syntax highlighting
- Tool-use blocks showing files read, commands run, edits made
- Thinking blocks revealing the model's reasoning
- Sidechain branches for sub-agent conversations
- `Cmd+O` expand/collapse all tool + thinking blocks
- `Cmd+E` toggle full tool output without truncation

### Native terminal view
- Decoded / Native / Split modes — read the rendered conversation, watch the live `claude` terminal, or both side by side
- Live terminal powered by xterm.js with full scrollback — scroll all the way back to the first prompt, just like a real terminal
- Correct wide-character and emoji widths (Unicode 11), with the in-app font picker
- Intent-based spawn — a terminal starts only when you open Native/Split or focus the composer; browsing or switching sessions starts nothing
- The most-recent terminal is kept alive across navigation for instant return, and per-session view memory preserves your scroll position when flipping Decoded↔Native
- PTY ownership detection with a take-over banner when another `claude` already owns the session

### Home dashboard
- One headline insight banner when something surprising happens (a big week, a major model shift, a heavily-used tool)
- 30-day activity heatmap and 7-day sparklines next to top-line counts
- Featured "Pick up where you left off" card, with an "awaiting you" marker on threads waiting for your reply
- Most-touched files (click to open in the file viewer)
- Clickable project strip filters the sidebar

### File viewer
- Slides in as a resizable split pane on the right; the sidebar collapses automatically and restores when you close it
- Drag the left edge to resize — the width persists across sessions
- Word-wrapped source — no horizontal scrolling for long lines
- Markdown files render formatted by default, with a one-click toggle to raw source
- In-place editing with `Cmd+S` to save, dirty-state indicator, and confirm-on-discard
- Sandboxed to session-related directories; refuses binary or oversized writes

### Analytics
- Daily, weekly, and per-session token + cost breakdowns
- Cost estimation with live pricing data
- Model usage breakdown and trends
- Tool usage statistics
- File touch analysis
- Optimization tips (repeated reads, oversized results, cache hit rates)

### Live updates
- File watcher tails session files in real time
- UI auto-refreshes when new messages arrive
- Notification bell for unread updates

### Sending messages
- Send messages to any session directly from the web UI
- Drives the local `claude` CLI in interactive TUI mode through a hidden PTY,
  so messages use subscription-backed interactive behavior
- Lazy spawn on chat-input focus; idle PTYs auto-kill after 5 minutes
- Login required on the host (the web UI doesn't surface OAuth)

### Export
- Export any conversation as JSON or Markdown — includes token counts and cost estimates

### Multi-profile support
- Switch between separate config directories (e.g. multiple Claude installations or sandboxes)
- Each profile has its own data paths and color
- Click the avatar in the bottom-left to switch

### Themes
- Light, dark, and system theme
- Dark mode is tuned for long sessions — neutral surfaces, accent reserved for primary actions and live signals

## Usage

### CLI

| Flag | Description | Default |
|------|-------------|---------|
| `--path PATH` | Add a scan path (repeatable) | auto-detected |
| `--port PORT` | Override the listening port | `4242` |
| `--host HOST` | Bind host | `127.0.0.1` |
| `--expose` | Bind to `0.0.0.0` (accessible on the local network) | off |
| `--no-open` | Don't open the browser on startup | opens browser |
| `--enable-edit` | Enable message editing + deletion (creates a backup before every write) | off |
| `--force-refresh` | Clear the cache and force a full rescan | off |
| `--since YYYYMMDD` | Only include sessions on or after this date | all |
| `--version` | Print version and exit | |

> `--expose` makes your chat history visible to anyone on the same network. Use it on trusted networks only.

**Subcommands:**

| Command | Description |
|---------|-------------|
| `ring` | Launch the web UI (default) |
| `ring scan` | Rescan and print summary |
| `ring today` | Show today's token usage and cost |
| `ring stats` | Print statistical metrics |
| `ring tips` | Print optimization tips |

### Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| `Cmd+K` | Open global search |
| `Cmd+O` | Expand/collapse all tool + thinking blocks |
| `Cmd+E` | Toggle full tool results |
| `Cmd+S` | Save (in file editor) |
| `Cmd+I` | Toggle chat panel |
| `Cmd+B` | Toggle sidebar |
| `Cmd+,` / `Shift+Cmd+,` | Open settings |
| `Cmd+/` | Open keyboard-shortcuts menu |
| `Esc` | Close dialog / search |

## Configuration

Settings are saved to `~/.config/agent-decoder/config.json`. Edit them in the UI,
or directly:

```json
{
  "data_paths": ["~/.claude"],
  "theme": "system",
  "auto_open_browser": true,
  "port": 4242,
  "edit_enabled": false
}
```

The session index lives at `~/.local/share/agent-decoder/index.db` (durable, so it
survives cache clears). To force a full rescan, run `ring --force-refresh` —
prefer this over deleting the DB, which also discards stars, archives, and custom
titles.

## Architecture

Agent Decoder is a local-first FastAPI server that scans your AI coding
assistant's JSONL session files into a SQLite index, serves a React +
TypeScript SPA, and drives the Claude CLI through a hidden PTY for in-app
sessions.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for a system diagram and deeper notes.

## Development

```bash
git clone https://github.com/Comradery64/Agent-Decoder
cd Agent Decoder

# Install backend deps (into an isolated .venv) + build the frontend
make dev                      # = uv sync && build frontend

# Run the app from source — in the project venv, so it always runs THIS
# checkout (no global install, no shadowing):
uv run ring --no-open
# equivalently, as a module from the checkout root:
uv run python -m agent_decoder --no-open
# (make run wraps this)

# Run tests
make test

# Rebuild the frontend only
make frontend
```

Requires Python 3.10+, [`uv`](https://docs.astral.sh/uv/), and Node.js 20+ for
frontend development.

GitHub Actions runs lint, type-check, and the Python + frontend test suites on
every PR — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Contributing

Pull requests are welcome. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) for
the dev setup, code style, and commit conventions, and our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Security

If you discover a security issue, please follow the responsible disclosure
process in [`SECURITY.md`](SECURITY.md) — do **not** open a public issue.

## Related docs

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system diagram and component overview
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup and PR guidelines
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — community expectations
- [`SECURITY.md`](SECURITY.md) — responsible disclosure
- [`CHANGELOG.md`](CHANGELOG.md) — release notes

## Acknowledgements

Agent Decoder is **inspired by, and built around the file format of, [Claude](https://www.anthropic.com/claude) and Claude Code from [Anthropic](https://www.anthropic.com/)**. This project is not affiliated with, endorsed by, or sponsored by Anthropic, PBC. *Claude* and *Claude Code* are trademarks of Anthropic, PBC; all references in this project are nominative.

> *A personal note from the author* — I didn't write the code in this repository or build any of the underlying tools. I sat at my computer, typed into it, and kept asking it for more. The actual line-by-line authoring was done by AI coding assistants — Anthropic's [Claude](https://www.anthropic.com/claude) (Sonnet and Opus) and [Z.ai](https://z.ai/)'s GLM-5.1, with the latter doing a substantial share of the heavy lifting alongside them. They aren't in the lists below because they're AI models rather than libraries, but they deserve named credit here. The projects, products, and people below did the rest of the real work that made Agent Decoder possible:

**Backend (Python)**

- [FastAPI](https://fastapi.tiangolo.com/) — HTTP API framework
- [Uvicorn](https://www.uvicorn.org/) — ASGI server
- [Pydantic](https://docs.pydantic.dev/) — data validation and settings
- [aiosqlite](https://github.com/omnilib/aiosqlite) — async SQLite driver
- [SQLite](https://www.sqlite.org/) — embedded database, including the [FTS5](https://www.sqlite.org/fts5.html) full-text search extension
- [watchfiles](https://github.com/samuelcolvin/watchfiles) — filesystem change notifications
- [anyio](https://github.com/agronholm/anyio) and [httpx](https://www.python-httpx.org/) — async primitives and HTTP client
- [Hatch](https://hatch.pypa.io/) and [hatch-vcs](https://github.com/ofek/hatch-vcs) — packaging and version management
- [uv](https://docs.astral.sh/uv/) — Python project and tool runner
- [pytest](https://docs.pytest.org/), [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio), and [pytest-cov](https://github.com/pytest-dev/pytest-cov) — testing

**Frontend (Web)**

- [React](https://react.dev/) and [React DOM](https://react.dev/reference/react-dom) — UI runtime
- [TypeScript](https://www.typescriptlang.org/) — typed JavaScript
- [Vite](https://vitejs.dev/) — bundler / dev server
- [Vitest](https://vitest.dev/) and [@testing-library](https://testing-library.com/) — unit / component testing
- [Zustand](https://github.com/pmndrs/zustand) — state management
- [react-markdown](https://github.com/remarkjs/react-markdown), [remark-gfm](https://github.com/remarkjs/remark-gfm), and [rehype-highlight](https://github.com/rehypejs/rehype-highlight) — Markdown rendering and code highlighting
- [highlight.js](https://highlightjs.org/) — syntax highlighter behind rehype-highlight
- [Apache ECharts](https://echarts.apache.org/) — analytics charts
- [OverlayScrollbars](https://kingsora.github.io/OverlayScrollbars/) — custom scrollbars
- [xterm.js](https://xtermjs.org/) — browser terminal renderer for the Native view, with [`@xterm/addon-fit`](https://github.com/xtermjs/xterm.js/tree/master/addons/addon-fit) (viewport sizing) and [`@xterm/addon-unicode11`](https://github.com/xtermjs/xterm.js/tree/master/addons/addon-unicode11) (Unicode 11 wide-character widths)
- [clsx](https://github.com/lukeed/clsx) — conditional class names
- [Node.js](https://nodejs.org/) and [npm](https://www.npmjs.com/) — JS runtime and package manager

**Fonts (bundled — [SIL Open Font License 1.1](frontend/src/assets/fonts/LICENSES.md))**

The Native view ships these monospace fonts so the in-app font picker works on any machine (a browser terminal can only paint fonts the browser has). Each is © its respective authors and redistributed under OFL-1.1; the full copyright notices and license text live in [`frontend/src/assets/fonts/LICENSES.md`](frontend/src/assets/fonts/LICENSES.md).

- [Monaspace](https://github.com/githubnext/monaspace) (Argon) — GitHub
- [Source Code Pro](https://github.com/adobe-fonts/source-code-pro) — Adobe
- [Fira Code](https://github.com/tonsky/FiraCode) — Nikita Prokopov & the Fira Code authors
- [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) — JetBrains
- [Libertinus Mono](https://github.com/alerque/libertinus) — The Libertinus Project authors
- [Xanh Mono](https://github.com/yellow-type-foundry/xanhmono) — Yellow Type Foundry
- [JuliaMono](https://github.com/cormullion/juliamono) — cormullion
- [Spline Sans Mono](https://github.com/SorkinType/SplineSansMono) — Sorkin Type
- [Ioskeley Mono](https://github.com/ahatem/IoskeleyMono) — Ahmed Hatem (an Iosevka custom build)

**Demo reel pipeline**

- [VHS](https://github.com/charmbracelet/vhs) — terminal recording as code (with [ttyd](https://github.com/tsl0922/ttyd) under the hood)
- [chafa](https://hpjansson.org/chafa/) — image-to-ANSI rendering, used to embed pixel-art into the terminal welcome banner
- [ImageMagick](https://imagemagick.org/) — image autocrop and text-on-color rendering for the outro card
- [FFmpeg](https://ffmpeg.org/) — video concat, audio mixing, sidechain ducking, and final mux
- [testreel](https://github.com/greentfrapp/testreel) — programmatic Chromium recording for the web-app segment
- [Playwright](https://playwright.dev/) — browser automation underlying testreel
- [tmux](https://github.com/tmux/tmux) — terminal multiplexer (optional, for multi-pane VHS scenes)
- Methodology reference: [saas-product-demo-video](https://github.com/noamdorr/saas-product-demo-video) — the SaaS demo-reel skill that inspired our soundtrack-splice and beat-alignment approach

**Tooling and platforms**

- [Homebrew](https://brew.sh/) — package management for the demo-reel toolchain on macOS
- [GitHub Actions](https://github.com/features/actions) — CI / type-check / test runners
- [Ruff](https://docs.astral.sh/ruff/) and [pre-commit](https://pre-commit.com/) — code style and pre-commit hooks
- [Editorconfig](https://editorconfig.org/) — consistent indentation across editors

Every project listed above is independently licensed by its respective authors; check each project's repository for terms. If we've missed an attribution, please [open an issue](https://github.com/Comradery64/Agent-Decoder/issues).

## License

This project is licensed under the **Functional Source License, Version 1.1,
with the Apache 2.0 Future License**
([FSL-1.1-Apache-2.0](LICENSE)).

In short:
- Free for personal use, internal business use, modification, and forking.
- You may not use it to build a competing commercial product or service.
- Each release automatically converts to Apache 2.0 two years after publication.

See [`LICENSE`](LICENSE) for the full text and the
[FSL FAQ](https://fsl.software/) for context.
