# AGENTS.md

Instructions for coding agents working in this repo.

## Tests

```bash
uv run pytest              # record/config tests always; transfer tests need redis
```

`tests/test_transfer.py::test_conservation_under_concurrency` is the important one — it's
the test that catches a read-modify-write implementation silently minting money.

## Dev tooling

Tooling config (dev deps, ruff, ty, pre-commit) follows the house setup from
`personal/expense_tracker`, so this project lints and type-checks the same way as the
rest of the repos.

```bash
uv sync --all-extras
uv run pre-commit install       # once
uv run pre-commit run --all-files
```

Hooks: `check-merge-conflict`, `end-of-file-fixer`, `check-toml`, `ruff-check --fix`,
`ruff-format`, `ty`.

```bash
uv run ruff check . && uv run ruff format .
uv run ty check
```
