.PHONY: lint test e2e

# The gate is defined here rather than in .github/workflows/ci.yml, so the command
# CI runs is the one you can run before opening a pull request. Note that ruff
# honours git's global excludes file, which a runner does not have: a path you
# exclude globally is linted in CI and skipped locally.
lint:
	ruff check .
	ruff format --check .

test:
	python -m pytest -q

# The stack in e2e/: the image, a stubbed control plane and a stubbed upstream,
# asserting what reached the wire. Builds the image, so it is slower than `test`
# and answers a different question — whether the container runs at all.
e2e:
	docker compose -f e2e/compose.yml up --build \
		--abort-on-container-exit --exit-code-from driver
