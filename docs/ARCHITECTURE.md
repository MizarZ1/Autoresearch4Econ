# Stage 0 boundaries

`cli.py` currently parses arguments and inspects configuration. `config.py` stores
endpoint/model/key-variable names but never copies the key into an artifact.
`stata_transport.py` isolates native stdout from protocol stdout on POSIX and
starts the fixed third-party server; it owns no research control flow.

`scripts/` contains explicit developer commands for checks, data acquisition,
independent Stata reference execution and live model/MCP probes. Imports and the
quality gate do not run these external probes. Independent reference execution
is an acceptance tool, not a second production backend.

Stages 1–7 will introduce the small ModelClient, AnalysisBackend and RunStore
interfaces, nine SQLite tables and the project-owned research loop. No empty
placeholder implementation or Python statistics backend is included in stage 0.
Task/reference/evidence records use `schema_version: 1`; no database format exists
yet. Application version 0.1.0 is separate from future schema/design versions.

The pinned MCP server offers broad tools. Future adapters must expose only the
required generic execution, inspection, help, result and lifecycle tools to the
project loop. The third-party research-agent tools are not used. Model outputs
must never determine reference answers or claim mock computations were real.

Cancellation must include evidence that the underlying Stata execution ended.
The Stata 18 runtime lacks the `sfi.breakIn` hook expected by the pinned server;
a cancelled background task alone is insufficient. Stage 0 probes verified the
hard-stop strategy: cancel the task, terminate its MCP session, confirm the worker
has exited, then start a fresh session. The accepted analysis path uses saved
do-files, `set processors 1`, and the updated local Stata copy. Inline multiline
analysis and unrestricted processors can still crash. See M0_VALIDATION.md.
Stage 2 must carry the verified behavior into the
AnalysisBackend cancellation contract and reject late/cancelled results.
