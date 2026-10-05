.PHONY: init fmt lint test e2e e2e-standalone e2e-envoy-grpc

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

# Every e2e stack in turn; each is an image, a stubbed control plane and a
# stubbed upstream, and asserts what reached the wire.
e2e: e2e-standalone e2e-envoy-grpc

e2e-standalone:
	docker compose -f e2e/standalone/compose.yml up --build \
		--abort-on-container-exit --exit-code-from driver

e2e-envoy-grpc:
	docker compose -f e2e/envoy-grpc/compose.yml up --build \
		--abort-on-container-exit --exit-code-from driver
