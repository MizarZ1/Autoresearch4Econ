"""Explicit live MCP/Stata preflight, using only generic execution tools."""

import asyncio
import hashlib
import json
import math
import os
import plistlib
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "local_reference/m0/mcp_runs" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")


def envelope(result: CallToolResult) -> dict[str, Any]:
    payload = result.structuredContent
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise ValueError("Unexpected MCP envelope from pinned 3.3.0 server")
    return dict(payload["result"])


async def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "calls": [],
        "status": "running",
        "artifacts_directory": str(OUTPUT.relative_to(ROOT)),
    }
    child_env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG"}}
    child_env.update(
        STATA_PATH=os.getenv(
            "STATA_PATH", "/Applications/Stata/StataMP.app/Contents/MacOS/stata-mp"
        ),
        MCP_STATA_LOGLEVEL="WARNING",
        MCP_STATA_ALLOWED_ROOTS=str(ROOT),
    )
    executable = Path(child_env["STATA_PATH"])
    library = executable.with_name("libstata-mp.dylib")
    metadata = executable.parent.parent / "Info.plist"
    if library.is_file() and metadata.is_file():
        record["stata_runtime"] = {
            "bundle_version": plistlib.loads(metadata.read_bytes())["CFBundleVersion"],
            "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
        }
    record["probe_code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    record["launcher_code_sha256"] = hashlib.sha256(
        (ROOT / "src/econ_agent/stata_transport.py").read_bytes()
    ).hexdigest()
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "econ_agent.stata_transport"], env=child_env
    )
    with (OUTPUT / "mcp_server.log").open("w") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                record["server"] = initialized.serverInfo.model_dump()
                tools = await session.list_tools()
                (OUTPUT / "mcp_tools.json").write_text(
                    json.dumps([tool.model_dump() for tool in tools.tools], indent=2) + "\n"
                )
                record["tools"] = [tool.name for tool in tools.tools]

                active_session = "default"

                async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
                    if name not in {"stata_task_status", "stata_control"}:
                        args = {"session_id": active_session, **args}
                    index = len(record["calls"]) + 1
                    start = time.monotonic()
                    result = await asyncio.wait_for(session.call_tool(name, args), timeout=45)
                    end = time.monotonic()
                    (OUTPUT / f"mcp_call_{index:02d}.json").write_text(
                        result.model_dump_json(indent=2) + "\n"
                    )
                    payload = envelope(result)
                    record["calls"].append(
                        {
                            "index": index,
                            "tool": name,
                            "success": payload.get("success"),
                            "duration_seconds": end - start,
                        }
                    )
                    (OUTPUT / "probe.json").write_text(json.dumps(record, indent=2) + "\n")
                    return payload

                minimum = await call(
                    "stata_run",
                    {
                        "code": "set more off\nclear\nset obs 4\ngen x=_n\nquietly summarize x",
                        "echo": False,
                    },
                )
                results = await call("stata_get_results", {})
                record["minimal_mean"] = results["data"]["r"]["mean"]
                assert minimum["success"] and record["minimal_mean"] == 2.5
                await call("stata_manage_session", {"action": "stop"})
                active_session = "ajr"
                await call("stata_manage_session", {"action": "create"})
                data = ROOT / "examples/demo/data/maketable4.dta"
                before = hashlib.sha256(data.read_bytes()).hexdigest()
                analysis_file = OUTPUT / "analysis.do"
                analysis_file.write_text(
                    f'set more off\nset processors 1\nuse "{data}", clear\n'
                    "ivregress 2sls logpgp95 (avexpr=logem4) if baseco==1, small\n"
                    "display e(N)\n"
                )
                analysis = await call(
                    "stata_run",
                    {
                        "code": str(analysis_file),
                        "is_file": True,
                        "echo": False,
                    },
                )
                assert analysis["success"] and analysis["data"]["rc"] == 0
                estimated = await call("stata_get_results", {})
                (OUTPUT / "mcp_ajr_results.json").write_text(json.dumps(estimated, indent=2) + "\n")
                state = estimated["data"]["e"]
                reference = json.loads(
                    (ROOT / "examples/demo/reference/independent_stata18.json").read_text()
                )
                expected = next(
                    row
                    for row in reference["estimates"]
                    if row["specification"] == "baseline"
                    and row["estimator"] == "ivregress"
                    and row["parameter"] == "avexpr"
                )
                coefficient = state["_matrices"]["b"]["values"][0][0]
                standard_error = math.sqrt(state["_matrices"]["V"]["values"][0][0])
                assert state["cmd"] == "ivregress" and state["depvar"] == "logpgp95"
                assert state["endog"] == "avexpr" and state["insts"] == "logem4"
                assert state["df_r"] == float(expected["df_r"])
                assert math.isclose(
                    coefficient, float(expected["coefficient"]), rel_tol=1e-6, abs_tol=1e-7
                )
                assert math.isclose(
                    standard_error, float(expected["standard_error"]), rel_tol=1e-6, abs_tol=1e-7
                )
                record["ajr_reference_match"] = True
                record["ajr_coefficient"] = coefficient
                record["ajr_standard_error"] = standard_error
                record["ajr_sample_size"] = state["N"]
                assert record["ajr_sample_size"] == 64
                help_result = await call("stata_get_help", {"topic": "ivregress"})
                record["help_success"] = help_result["success"]
                assert record["help_success"]
                # A real CPU loop; cancellation must interrupt execution before its sentinel.
                await call("stata_manage_session", {"action": "stop"})
                active_session = "cancel"
                await call("stata_manage_session", {"action": "create"})
                listed_sessions = await call("stata_manage_session", {"action": "list"})
                worker_pid = next(
                    item["pid"]
                    for item in listed_sessions["data"]["sessions"]
                    if item["id"] == active_session
                )
                record["worker_pid"] = worker_pid
                started_marker = OUTPUT / "computation_started.txt"
                background = await call(
                    "stata_run",
                    {
                        "code": "scalar m0_completed=0\nscalar m0_counter=0\n"
                        f'file open m0mark using "{started_marker}", write text replace\n'
                        'file write m0mark "started"\nfile close m0mark\n'
                        "while 1 {\nscalar m0_counter=m0_counter+1\n}\nscalar m0_completed=1",
                        "background": True,
                        "echo": False,
                    },
                )
                task_id = background["data"]["task_id"]
                deadline = time.monotonic() + 10
                while not started_marker.exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("Background computation did not actually start")
                    await asyncio.sleep(0.05)
                record["computation_started_before_cancel"] = True
                before_cancel = await call("stata_task_status", {"task_id": task_id})
                assert before_cancel["data"]["status"] == "running"
                cancelled = await call("stata_control", {"action": "cancel", "id": task_id})
                assert cancelled["success"]
                after_cancel = await call(
                    "stata_task_status",
                    {"task_id": task_id, "wait": True, "timeout": 10, "poll_interval": 0.1},
                )
                record["cancel_status"] = after_cancel["data"]["status"]
                record["cancel_error"] = after_cancel["data"].get("error")
                assert record["cancel_status"] == "failed"
                assert record["cancel_error"] == "Operation cancelled"
                # Stata 18 lacks the sfi.breakIn hook expected by this server.
                # Task cancellation alone does not prove process termination.
                stopped = await call("stata_manage_session", {"action": "stop"})
                assert stopped["success"]
                try:
                    os.kill(worker_pid, 0)
                except ProcessLookupError:
                    record["worker_process_terminated"] = True
                else:
                    raise RuntimeError("Cancelled Stata worker is still alive")
                record["cancellation_strategy"] = (
                    "cancel task, then stop session and verify worker exit"
                )
                record["soft_cancel_supported"] = False
                active_session = "fresh"
                restarted = await call("stata_manage_session", {"action": "create"})
                assert restarted["success"]
                fresh = await call(
                    "stata_run", {"code": "clear\nset more off\ndisplay 2+2", "echo": False}
                )
                record["fresh_session_responds"] = fresh["success"]
                assert fresh["success"]
                record["input_unchanged"] = hashlib.sha256(data.read_bytes()).hexdigest() == before
                assert record["input_unchanged"]
                await call("stata_manage_session", {"action": "stop"})
    record["status"] = "passed"
    record["session_strategy"] = "fresh dedicated worker for each execution attempt"
    record["analysis_input"] = "saved do-file through stata_run is_file=true"
    record["finished_at"] = datetime.now(UTC).isoformat()
    (OUTPUT / "probe.json").write_text(json.dumps(record, indent=2) + "\n")
    (ROOT / "local_reference/m0/stata_probe.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except Exception as exc:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        failure_path = OUTPUT / "probe.json"
        failed = json.loads(failure_path.read_text()) if failure_path.exists() else {}
        failed.update(status="failed", error_type=type(exc).__name__)
        failure_path.write_text(json.dumps(failed, indent=2) + "\n")
        (ROOT / "local_reference/m0/stata_probe.json").write_text(
            json.dumps(failed, indent=2) + "\n"
        )
        (OUTPUT / "failure.log").write_text(traceback.format_exc())
        print(f"Stata/MCP probe failed; diagnostics: {OUTPUT.relative_to(ROOT)}")
        code = 1
    raise SystemExit(code)
