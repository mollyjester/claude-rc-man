# No target runs beside another under make -j: check-all runs its parts one after the other,
# stopping at the first red one, and each on-demand run starts an engine of its own and builds
# the same images, which side by side would only compete for the machine.
.NOTPARALLEL:

.PHONY: setup test test-container check-ui lint format frontend-setup frontend-test frontend-build e2e check check-all

setup: frontend-setup  ## install the development environment
	uv sync

# More workers than the machine has cores: a test spends much of its time on processes it
# started (tmux, git, the fake claude) and the kernel's work for them, which workers overlap.
# worksteal hands the tests still queued for a busy worker to an idle one, so a few long
# tests do not leave the rest waiting at the end.
TEST_WORKERS ?= 8
test:  ## every Python test that runs on the host, in parallel
	uv run pytest -n $(TEST_WORKERS) --dist worksteal -m "not container and not real_claude"

test-container: export RCMAN_REQUIRE_CONTAINER = 1
# In one process, as the container tests share the images their session fixtures build,
# which each pytest-xdist worker would build again.
test-container:  ## on demand: the Python tests that need a container but not the whole stack; they fail rather than skip
	uv run pytest -m "container and not stack and not real_claude"

check-ui: export RCMAN_REQUIRE_CONTAINER = 1
# In one process, like test-container, whose reason holds here too.
check-ui:  ## on demand: the whole stack in a browser; needs make frontend-setup once, like e2e
	uv run pytest -m "stack and not real_claude"

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
check: lint test frontend-test e2e  ## lint and every test that runs on the host; tests that would skip fail instead

# One after the other (.NOTPARALLEL above). It stops at the first part that is red;
# make -k check-all runs every part and reports each red one, so that everything red is fixed
# together.
check-all: check test-container check-ui  ## check and both on-demand runs, stopping at the first red part (make -k: all parts): for the end of a change and before the live reinstall
