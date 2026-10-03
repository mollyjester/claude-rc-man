.PHONY: setup test test-container lint format frontend-setup frontend-test frontend-build e2e check

setup: frontend-setup  ## install the development environment
	uv sync

test:  ## every Python test that runs on the host, in parallel
	uv run pytest -n auto -m "not container and not real_claude"

test-container: export RCMAN_REQUIRE_CONTAINER = 1
# In one process, as the container tests share the toolchain the installer unpacks into
# a host cache, which nothing locks.
test-container:  ## the Python tests that need a container; they fail rather than skip
	uv run pytest -m "container and not real_claude"

lint:  ## check formatting and lint: ruff for the Python code, Biome for the frontend
	uv run ruff format --check .
	uv run ruff check .
	cd frontend && npm run lint

format:  ## apply formatting and safe lint fixes, to the Python code and the frontend
	uv run ruff format .
	uv run ruff check --fix .
	cd frontend && npm run format

frontend-setup:  ## install the frontend's packages and Playwright's browser
	cd frontend && npm ci && npx playwright install chromium

frontend-test:  ## type-check the frontend and run its component tests
	cd frontend && npm run typecheck && npm test

frontend-build:  ## build the frontend into frontend/dist
	cd frontend && npm run build

e2e: frontend-build  ## the built frontend in a browser, against the real backend
	cd frontend && npx playwright test

check: export RCMAN_REQUIRE_REAL_CLAUDE = 1
check: lint test test-container frontend-test e2e  ## everything; tests that would skip fail instead
