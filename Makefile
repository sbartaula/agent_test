.PHONY: install test eval demo serve lint
install:
	uv sync
test:
	PYTHONPATH= PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
eval:
	uv run issuepilot eval
demo:
	rm -rf /tmp/bookshelf && cp -r examples/bookshelf /tmp/bookshelf
	uv run issuepilot fix "GET /books?page=1 returns books 11-20 instead of 1-10" --repo /tmp/bookshelf --mode develop
serve:
	ISSUEPILOT_REPOS_ROOT=$${ISSUEPILOT_REPOS_ROOT:-/tmp} uv run uvicorn issuepilot.api.app:app_factory --factory --host 127.0.0.1 --port 8765
lint:
	uv run ruff check . && uv run mypy src
