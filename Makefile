.PHONY: setup test lint format check

setup:  ## install the development environment
	uv sync

test:  ## run the test suite
	uv run pytest

lint:  ## check formatting and lint
	uv run ruff format --check .
	uv run ruff check .

format:  ## apply formatting and safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

check: lint test  ## everything CI runs
