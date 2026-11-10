# No target runs beside another under make -j: make test-container and make benchmark each start
# an engine of their own and build the same images, which side by side would only compete for
# the machine.
.NOTPARALLEL:

.PHONY: setup test test-container benchmark lint format frontend-setup frontend-test frontend-build screenshot check

setup: frontend-setup  ## install the development environment
	uv sync

# More workers than the machine has cores: a test spends much of its time on processes it
# started (tmux, git, the fake claude) and the kernel's work for them, which workers overlap.
# worksteal hands the tests still queued for a busy worker to an idle one, so a few long
# tests do not leave the rest waiting at the end.
TEST_WORKERS ?= 8
test:  ## every Python test that runs on the host, in parallel
	uv run pytest -n $(TEST_WORKERS) --dist worksteal -m "not container and not real_claude and not benchmark"

test-container: export RCMAN_REQUIRE_CONTAINER = 1
# In one process, as the container tests share the images their session fixtures build,
# which each pytest-xdist worker would build again.
test-container:  ## on demand: the Python tests that need a container; they fail rather than skip; when the working rules require them, and before the live reinstall
	uv run pytest -m "container and not real_claude and not benchmark"

# In one process, so that no other test runs beside one that measures; -s shows what each test
# reports. In no other target: it decides nothing.
benchmark:  ## on demand, not a check: the tests that measure and report; they fail only when they cannot measure
	uv run pytest -m "benchmark and not real_claude" -s

lint:  ## check formatting and lint: ruff for the Python code, Biome for the frontend
	uv run ruff format --check .
	uv run ruff check .
	cd frontend && npm run lint

format:  ## apply formatting and safe lint fixes, to the Python code and the frontend
	uv run ruff format .
	uv run ruff check --fix .
	cd frontend && npm run format

frontend-setup:  ## install the frontend's packages and the browser make screenshot drives
	cd frontend && npm ci && npx playwright install chromium

frontend-test:  ## type-check the frontend and run its component tests
	cd frontend && npm run typecheck && npm test

frontend-build:  ## build the frontend into frontend/dist
	cd frontend && npm run build

# Not a test: it asserts nothing (devtools/screenshots.py). PAGE is a page's path, STATE one of
# its states, or none; frontend/screenshots/capture.ts says which there are.
screenshot: frontend-build  ## not a test: PAGE, in STATE if given, at a phone's and a desktop's width, light and dark, into frontend/test-results/screenshots/
	uv run python -m devtools.screenshots "$(PAGE)" "$(STATE)"

check: export RCMAN_REQUIRE_REAL_CLAUDE = 1
check: lint test frontend-test frontend-build  ## the full check: lint, every test that runs on the host, the frontend's tests and its build; tests that would skip fail instead
