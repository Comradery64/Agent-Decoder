.PHONY: install dev frontend build test clean run

# One-command install from source
install: build
	uv tool install .

# Development setup — install backend deps + build frontend
dev:
	uv sync --extra dev
	cd frontend && npm install && npm run build

# Build frontend only
frontend:
	cd frontend && npm install && npm run build

# Build everything (frontend bundles into Python package)
build: frontend

# Run tests
test:
	uv run pytest

# Run the app from source
run:
	uv run clau-decode

# Clean build artifacts
clean:
	rm -rf frontend/node_modules frontend/dist
	rm -rf src/agent_decoder/static/assets src/agent_decoder/static/index.html
	rm -rf .pytest_cache
	find . -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
