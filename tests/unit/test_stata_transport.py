"""Verify actual file-descriptor isolation without requiring Stata or MCP."""

import json
import subprocess
import sys


def test_native_and_child_noise_cannot_contaminate_protocol() -> None:
    code = """
import os,sys,json,subprocess
from econ_agent.stata_transport import reserve_protocol_stdout
reserve_protocol_stdout()
os.write(1,b"native prompt. ")
subprocess.run([sys.executable,'-c','import os;os.write(1,b"child prompt. ")'],check=True)
print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'ok':True}}),flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert "native prompt" in result.stderr
    assert "child prompt" in result.stderr
    assert "prompt" not in result.stdout
