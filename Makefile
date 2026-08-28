.PHONY: lint test

# The gate is defined here rather than in .github/workflows/ci.yml, so the command
# CI runs is the one you can run before opening a pull request. Note that ruff
# honours git's global excludes file, which a runner does not have: a path you
# exclude globally is linted in CI and skipped locally.
lint:
	ruff check .
	ruff format --check .

test:
	python -m pytest -q
