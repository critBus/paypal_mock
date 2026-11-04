# Repository instructions

This FastAPI project mocks payment providers used by CrinnoPayments. Despite its
name, it includes Stripe, PayPal, Revolut, BMS and other integrations. Start with
[README.md](README.md) and [SCENARIOS.md](SCENARIOS.md). Keep fixes in the owning
provider and preserve unrelated working-tree changes.

## Safe local work

- Inspect `git status --short`, relevant source and tests before editing.
- Keep real payment providers, customer callbacks and shared databases out of tests.
  Intercept outgoing HTTP and use temporary provider storage; never reset `data/`
  or notify running consumers merely to run automated tests.
- Do not print or copy `.env`, API keys, webhook secrets, payment/customer data or
  raw delivery logs into prompts or documentation.
- Do not commit, push or change branches unless requested. Avoid new application
  dependencies; use the standard library and the existing stack.
- Add focused regression coverage for fixes, run the affected tests and review the
  complete task diff. Use English for identifiers and engineering documentation.

## CodeGraph

CodeGraph is an optional external developer tool, not an application dependency.
Setup and commands are documented in [README.md](README.md#codegraph).

- Run `codegraph status .` before broad source exploration. Initialize/synchronize
  with `uv run python -m tools.codegraph_hook --strict` when the CLI is available.
- If the CLI is outside PATH, use `--executable /absolute/path/to/codegraph` with
  that helper and expose its runtime on PATH. If unavailable or stale, report it
  and use `rg` and direct reads until it is ready.
- Find symbols with `codegraph query SYMBOL --json`, then inspect with
  `codegraph node SYMBOL --file provider/module.py`. Use focused `explore`,
  `callers`, `callees`, `impact` and `affected` queries for candidate relationships.
- Verify FastAPI route decorators, wildcard registration imports in `main.py`,
  background tasks and dynamic dispatch directly. Empty graph edges do not prove
  code is unused. The graph describes implementation, not payment policy or proof
  that a webhook was delivered or accepted.
- Every checkout owns its ignored `.codegraph/`. Never copy, commit or symlink an
  index from CrinnoPayments or another checkout. Synchronize after source edits
  before relying on impact analysis. Do not interrupt another session's indexing.

## Verification

Run `uv run pytest -q tests/<affected_test>.py` and
`uv run ruff check <changed_python_files>`. For tooling changes, exercise actual
CLI success and failure paths. No application tests are needed for documentation
alone. Report checks actually run and any remaining limitations.
