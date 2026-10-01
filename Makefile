.PHONY: setup test lint format frontend-setup frontend-test frontend-build e2e check

setup: frontend-setup  ## install the development environment
	uv sync

test:  ## run the Python test suite
	uv run pytest

lint:  ## check formatting and lint
	uv run ruff format --check .
	uv run ruff check .

format:  ## apply formatting and safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

frontend-setup:  ## install the frontend's packages and Playwright's browser
	cd frontend && npm ci && npx playwright install chromium

frontend-test:  ## type-check the frontend and run its component tests
	cd frontend && npm run typecheck && npm test

frontend-build:  ## build the frontend into frontend/dist
	cd frontend && npm run build

e2e: frontend-build  ## the built frontend in a browser, against the real backend
	cd frontend && npx playwright test

check: lint test frontend-test e2e  ## everything CI runs
