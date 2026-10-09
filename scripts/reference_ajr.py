"""Run an independent Stata reference in a credential-free child process."""

import csv
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_HASH = "2b34cce21b58a3a424a804e97a134106ab7010ef936bb68b3a244d3e271af404"


def main() -> int:
    data = ROOT / "examples/demo/data/maketable4.dta"
    output = ROOT / "local_reference/m0/independent" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    output.mkdir(parents=True, exist_ok=True)
    if not data.exists() or hashlib.sha256(data.read_bytes()).hexdigest() != DATA_HASH:
        print("Missing or changed AJR dataset; obtain and verify it before reference execution.")
        return 1
    script = ROOT / "examples/demo/reference/ajr_reference.do"
    # Only launch locally authored reference code; this is not a production analysis backend.
    child_code = """
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]+'/utilities')
from pystata import config
config.init('mp',splash=False)
from pystata import stata
from sfi import Scalar,Macro
stata.run('scalar m0_mp=c(MP)\\nscalar m0_se=c(SE)',quietly=True)
metadata={'version':Macro.getGlobal('c(stata_version)'),
          'flavor':Macro.getGlobal('c(flavor)'),
          'MP':Scalar.getValue('m0_mp'),'SE':Scalar.getValue('m0_se')}
Path(sys.argv[4]+'/metadata.json').write_text(json.dumps(metadata))
stata.run('do "'+sys.argv[2]+'" "'+sys.argv[3]+'" "'+sys.argv[4]+'"',quietly=True)
Path(sys.argv[4]+'/completed').write_text('completed')
"""
    child_env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG"}}
    stata_home = os.getenv("ECON_AGENT_STATA_HOME", "/Applications/Stata")
    with (output / "driver.log").open("w") as log:
        result = subprocess.run(
            [sys.executable, "-c", child_code, stata_home, str(script), str(data), str(output)],
            env=child_env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
    if result.returncode:
        print("Stata reference failed; inspect local_reference/m0/independent/driver.log locally.")
        return result.returncode
    if not (output / "completed").exists():
        raise RuntimeError("Reference process did not complete")
    with (output / "results.csv").open() as stream:
        estimates = list(csv.DictReader(stream))
    with (output / "diagnostics.csv").open() as stream:
        diagnostics = list(csv.DictReader(stream))
    for row in estimates:
        for key in ("n", "coefficient", "standard_error", "df_r", "r2"):
            if not math.isfinite(float(row[key])):
                raise ValueError(f"Nonfinite reference {key}")
        if int(float(row["n"])) != 64:
            raise ValueError("Unexpected reference sample size")
        if row["estimator"] == "ivregress":
            legacy = next(
                r
                for r in estimates
                if r["estimator"] == "ivreg"
                and r["specification"] == row["specification"]
                and r["parameter"] == row["parameter"]
            )
            for key in ("coefficient", "standard_error", "df_r"):
                if not math.isclose(
                    float(row[key]), float(legacy[key]), rel_tol=1e-6, abs_tol=1e-7
                ):
                    raise ValueError(f"Legacy/modern inference mismatch: {key}")
    destination = ROOT / "examples/demo/reference/independent_stata18.json"
    record = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "provenance": "Independent developer-authored do-file, no model, no MCP",
        "data_sha256": DATA_HASH,
        "reference_code_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "stata": json.loads((output / "metadata.json").read_text()),
        "inference": "Homoskedastic conventional SE; finite-sample df correction; ivregress small",
        "legacy_modern_match": True,
        "estimates": estimates,
        "first_stage_diagnostics": diagnostics,
        "original_data_unchanged": hashlib.sha256(data.read_bytes()).hexdigest() == DATA_HASH,
    }
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {k: record[k] for k in ("stata", "legacy_modern_match", "original_data_unchanged")}
        )
    )
    print(f"Reference saved: {destination.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
