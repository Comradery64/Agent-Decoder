# Contributing to Agent Decoder

Thanks for your interest in Agent Decoder! This project is a local, privacy-focused tool for browsing AI coding assistant chat history. Contributions of all kinds are welcome.

## Ways to contribute

- **Report a bug** — open an issue using the bug report template.
- **Suggest a feature** — open an issue using the feature request template. Small, focused proposals are easiest to land.
- **Improve docs** — fixes to the README, inline help, or this file are always welcome.
- **Send a pull request** — bug fixes, new features, refactors, tests. See below.

If you're not sure whether something is in scope, open an issue first and we can talk it through.

## Development setup

Agent Decoder has a Python 3.10+ backend (FastAPI + SQLite, tested with pytest) and a TypeScript frontend (React 18 + Vite, tested with vitest). You'll need Python 3.10+, [`uv`](https://docs.astral.sh/uv/), and Node.js 20+.

```bash
git clone https://github.com/Comradery64/Agent-Decoder
cd Agent Decoder

# Install backend deps + build the frontend
make dev

# Run the app from source (opens http://localhost:4242)
make run

# Run the Python test suite
make test

# Rebuild the frontend only
make frontend
```

Frontend tests run from the `frontend/` directory:

```bash
cd frontend
npm test          # one-shot vitest run
npm run test:watch
```

## Code style

- **Python:** [ruff](https://docs.astral.sh/ruff/) for linting and formatting. Run `uv run ruff check --fix .` and `uv run ruff format .` before committing.
- **TypeScript:** [prettier](https://prettier.io/) for formatting. The TS compiler (`tsc`) is invoked as part of `npm run build` and must pass cleanly.
- **Pre-commit hooks** are recommended. After cloning, run `pre-commit install` to wire them up. The same checks run locally that run in CI.

## Branching and commits

- Create feature branches off `main` (e.g. `feat/search-filters`, `fix/file-viewer-resize`).
- Open a PR back into `main` and link the related issue (`Closes #123`).
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages:
  - `feat:` — new user-facing functionality
  - `fix:` — bug fix
  - `chore:` — tooling, deps, housekeeping
  - `docs:` — documentation only
  - `refactor:` — code change with no behavior change
  - `test:` — adding or fixing tests
  - `ci:` — CI/CD pipeline changes
- Keep commits focused. Squash noise locally before pushing.

## Before you submit a PR

Please confirm:

- [ ] Tests pass — `make test` and `cd frontend && npm test`
- [ ] ruff is clean — `uv run ruff check .` and `uv run ruff format --check .`
- [ ] TypeScript builds — `cd frontend && npm run build`
- [ ] `CHANGELOG.md` is updated under the `[Unreleased]` section if your change is user-visible
- [ ] Docs (README, inline help, doctrings) are updated if your change touches them
- [ ] No telemetry, no network calls to remote services, no data leaving the user's machine — this is a hard rule

## Licensing

Agent Decoder is released under the [Functional Source License, Version 1.1, with the Apache 2.0 Future License](LICENSE) (FSL-1.1-Apache-2.0). By submitting a contribution you agree that it is offered under those same terms. There is no CLA — your `git` authorship is sufficient attribution. Please don't paste in code you don't have the right to license under FSL-1.1-Apache-2.0.

## Questions

Open an issue or start a GitHub Discussion. Maintained by [@Comradery64](https://github.com/Comradery64).
