# One definition of what "checked" means. CI calls these same targets, so a
# green local run and a green pipeline cannot drift apart.
.DEFAULT_GOAL := help
.PHONY: help install lint format check-format typecheck test package check check-upstream

help:  ## Show the available targets
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install the project and its development dependencies
	uv sync

lint:  ## Report lint violations
	uv run ruff check .

format:  ## Reformat the code in place
	uv run ruff format .

check-format:  ## Verify formatting without changing anything
	uv run ruff format --check .

typecheck:  ## Run the type checker
	uv run mypy

test:  ## Run the test suite with coverage
	uv run pytest --cov

check-upstream:  ## Compare the cost tables with Google's discovery documents
	uv run python scripts/check_upstream_methods.py

package:  ## Build the distributions and check what they contain
	uv build
	uv run --no-sync python scripts/check_sdist.py

check: lint check-format typecheck test package  ## Everything the pipeline runs
