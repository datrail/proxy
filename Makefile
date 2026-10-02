.PHONY: init fmt lint test e2e

# CI runs these same targets.

# Every member, editable, plus the pinned dev tools. Run once, and after pulling.
init:
	uv sync

# Format, and fix what ruff can.
fmt:
	uv run ruff format .
	uv run ruff check --fix .

# Check only. Ruff honours git's global excludes, which CI does not have.
lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest -q

# The image, a stubbed control plane and a stubbed upstream; asserts what reached the wire.
e2e:
	docker compose -f e2e/compose.yml up --build \
		--abort-on-container-exit --exit-code-from driver
