# econ-agent

Python CLI for empirical economic research through Stata MCP. This repository
currently implements **stage 0 of M1**: environment probes, an installable entry
point, a locked development environment and an independent AJR reference. The
research loop, SQLite persistence, human interaction, resume and paper export
are scheduled for stages 1–7 and are not yet implemented. M2 Python statistics
execution is outside this baseline.

## Install and inspect

Use Python 3.11+ (the macOS system Python 3.9 is unsupported):

```sh
uv sync --locked --extra stata --group dev
uv run --locked --extra stata --group dev econ-agent --version
uv run --locked --extra stata --group dev econ-agent doctor --backend stata --json
uv run --locked --extra stata --group dev python scripts/check.py
```

`doctor` inspects configuration without model requests or Stata initialization.
Exit 0 means required configuration/installations were found; it **does not**
mean live analysis passed. Exit 1 means an item is missing, and exit 2 indicates
invalid configuration or arguments. `--backend python` is rejected. `.env.example`
documents environment variable names; this version does not automatically load
`.env`. Set credentials in your shell without committing them.

## Explicit live verification

```sh
uv run --locked --extra stata --group dev python scripts/fetch_ajr.py
uv run --locked --extra stata --group dev python scripts/reference_ajr.py
uv run --locked --extra stata --group dev python scripts/probe_stata.py
uv run --locked --extra stata --group dev python scripts/probe_model.py
```

These commands require network access when downloading or calling the model;
Stata probes require a valid local Stata 18 license. The model probe makes three
small API requests and records structured-action validation, returned model IDs,
usage and overlap of two actual requests. Stata runs in a child environment that
does not inherit model credentials. Raw logs, including any Stata license splash,
remain under ignored `local_reference/` and are not exported or pushed.

`reference_ajr.py` runs locally authored Stata code in a fresh process, without a
model or MCP, to provide an independent numerical reference. It compares legacy
`ivreg` with `ivregress 2sls, small`, checks sample size and input hash, and writes
sanitized numerical output to `examples/demo/reference/`. Homoskedastic standard
errors and finite-sample degrees of freedom are deliberate replication choices.

The demo task reads only the dataset, the project-authored variable dictionary
and background. The `reference/` directory and authors' original code are for
developers/verifiers; never automatically feed their exact answers to the model.
`examples/demo/task.json` is a stage 0 fixture; `econ-agent start` is not available.

## Dependency boundaries

- `openai`: ordinary compatible API client; SDK retries disabled in live probes.
- `pydantic`: validates data shapes; does not control the research loop.
- `mcp`: protocol client/server SDK, restricted to 1.x for the pinned server.
- `mcp-stata 3.3.0`: separate Stata service, AGPL-3.0-or-later, from
  [tmonk/mcp-stata](https://github.com/tmonk/mcp-stata/tree/v3.3.0). This project
  calls generic execution, result and session tools; it does not delegate research
  to the third party's research-audit/estimation-agent tools or copy its source.
- NumPy 1.26.4 / pandas 2.x: PyStata/MCP integration dependencies, not an M2
  analysis executor. Other transport/server dependencies are locked in `uv.lock`.
- Ruff, mypy, pytest and pytest-asyncio: development quality checks.

The local POSIX launcher `econ_agent.stata_transport` reserves a separate MCP
stdout stream so native Stata prompts cannot contaminate JSON-RPC. It starts the
unchanged third-party server. This compatibility path is verified on macOS; it is
not claimed to support Windows. Use the project launcher for the pinned service.

## Evidence and collaboration

See [stage 0 validation](docs/M0_VALIDATION.md),
[acceptance card](examples/demo/reference/acceptance.json),
[independent results](examples/demo/reference/independent_stata18.json), and
[development plan](docs/DEVELOPMENT_PLAN.md).

Local and CI checks use `python scripts/check.py`, which runs formatting, lint,
strict typing, offline tests and the module entry point. CI needs no Stata license,
model key, private assignment files or downloaded AJR data. Gitea runner and review
status are reported separately from local success. No automatic merge is requested.
