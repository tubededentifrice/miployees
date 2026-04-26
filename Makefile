.PHONY: lint fmt type test coverage schemathesis

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

type:
	uv run mypy app

test:
	uv run pytest

coverage:
	uv run pytest --cov=app --cov-report=term-missing

# API contract sweep (cd-3j25).
#
# Boots the FastAPI app under ``uvicorn``, mints a Bearer token via
# ``scripts/dev_login.py``, and runs ``schemathesis run`` with the
# custom checks under ``tests/contract/hooks.py`` registered through
# ``SCHEMATHESIS_HOOKS``. Spec: ``docs/specs/17-testing-quality.md``
# §"API contract" + §"Quality gates".
schemathesis:
	bash scripts/schemathesis_run.sh
