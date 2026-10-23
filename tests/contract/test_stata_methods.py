"""固定的真实合成数值参考；离线回放只检验映射，不声称重新运行 Stata。"""

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from econ_agent.exporting import export_run
from econ_agent.inputs import prepare_input
from econ_agent.reporting import result_tables
from econ_agent.schemas import (
    CoefficientResult,
    DesignContent,
    ExperimentSpec,
    ExtractionStatus,
    HumanAnswer,
    ResearchTask,
    RunStatus,
    ScalarResult,
    ValidationRecord,
    content_hash,
)
from econ_agent.stata_backend import StataBackend
from econ_agent.stata_contracts import StataProgram
from econ_agent.stata_methods import (
    extract_regression,
    extract_summary,
    method_checks,
    verified_scopes,
)
from econ_agent.state import RunStore, StateConflict, new_id
from scripts.validate_stage7 import compare
from tests.contract.test_stata_backend import FakeTransport, attempt

REFERENCE = Path(__file__).resolve().parents[2] / "examples/methods/stata18_reference.json"


def test_summary_case_sensitive_variable_is_not_verified() -> None:
    fixture = case("summary")
    program = StataProgram.model_validate(fixture["program"])
    assert program.summary
    program = program.model_copy(
        update={"code": program.code.replace("summarize x", "summarize X")}
    )
    assert program.summary is not None
    spec = specification(program)
    normalized = extract_summary(
        {"data": {"r": fixture["scalar_snapshot"]}},
        fixture["named_csv"],
        spec,
        program.summary,
        actual_command=program.code.strip(),
    )
    checks = method_checks(
        fixture["stored"], normalized, spec, program, sample_match=True, evidence_ids=("raw_test",)
    )
    assert next(c for c in checks if c.category == "specification").status == "fail"
    assert normalized.actual_command.startswith("summarize X")


@pytest.mark.parametrize("name", ["linear", "did", "rdd", "logit"])
@pytest.mark.parametrize("field,value", [("p_value", "0.8"), ("lower", "100"), ("upper", "200")])
def test_inconsistent_p_or_interval_is_rejected(name: str, field: str, value: str) -> None:
    import csv
    import io

    fixture = case(name)
    reader = csv.DictReader(io.StringIO(fixture["named_csv"]))
    rows = list(reader)
    rows[0][field] = value
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames or [])
    writer.writeheader()
    writer.writerows(rows)
    program = StataProgram.model_validate(fixture["program"])
    assert program.regression
    with pytest.raises(ValueError, match="推断口径"):
        extract_regression(
            fixture["stored"], output.getvalue(), specification(program), program.regression
        )


def case(name: str) -> dict[str, Any]:
    return dict(json.loads(REFERENCE.read_text())["cases"][name])


def specification(program: StataProgram, **ids: str) -> ExperimentSpec:
    rule = program.regression or program.summary
    assert rule is not None
    return ExperimentSpec(
        experiment_id=ids.get("experiment_id", "experiment_test"),
        run_id=ids.get("run_id", "run_test"),
        design_id=ids.get("design_id", "design_test"),
        input_artifact_id=ids.get("input_artifact_id", "input_test"),
        purpose="合成数值参考回放",
        initiation="planned",
        method=program.regression.method() if program.regression else "summarize",
        variables=rule.variables(),
        sample_rule=rule.sample_expression,
        expected_result_type="scalars" if program.summary else "coefficients",
    )


@pytest.mark.parametrize("name", ["linear", "did", "rdd", "logit", "summary"])
def test_real_fixture_maps_and_matches_independent_native_reference(name: str) -> None:
    fixture = case(name)
    program = StataProgram.model_validate(fixture["program"])
    spec = specification(program)
    normalized: CoefficientResult | ScalarResult
    if program.regression:
        normalized = extract_regression(
            fixture["stored"], fixture["named_csv"], spec, program.regression
        )
    else:
        assert program.summary
        normalized = extract_summary(
            {"data": {"r": fixture["scalar_snapshot"]}}, fixture["named_csv"], spec, program.summary
        )
    assert normalized.model_dump(mode="json") == fixture["normalized"]
    compare(normalized, fixture["native_csv"])
    checks = method_checks(
        fixture["stored"],
        normalized,
        spec,
        program,
        sample_match=True,
        evidence_ids=("actual_raw",),
    )
    assert all(c.status == "pass" for c in checks if c.required)
    if name == "logit":
        assert isinstance(normalized, CoefficientResult)
        assert all(e.degrees_of_freedom is None and e.missing_reasons for e in normalized.estimates)
    if name == "summary":
        assert isinstance(normalized, ScalarResult)
        assert normalized.standard_errors.treatment == "不适用"


@pytest.mark.parametrize(
    "fault",
    ["vce", "rank", "truncated", "matrix", "df", "names", "nonfinite", "convergence", "normal_p"],
)
def test_invalid_inference_and_raw_mappings_are_rejected(fault: str) -> None:
    fixture = case("logit" if fault in {"convergence", "normal_p"} else "linear")
    e = fixture["stored"]["data"]["e"]
    text = fixture["named_csv"]
    if fault == "vce":
        e["vce"] = "cluster"
    elif fault == "rank":
        e["rank"] = 1
    elif fault == "truncated":
        e["_matrices"]["V"]["truncated"] = True
    elif fault == "matrix":
        e["_matrices"]["b"]["values"][0][0] += 1
    elif fault == "df":
        e["df_r"] -= 1
    elif fault == "names":
        text = text.replace("x,", "invented,", 1)
    elif fault == "nonfinite":
        e["_matrices"]["V"]["values"][0][0] = float("nan")
    elif fault == "convergence":
        e["converged"] = 0
    elif fault == "normal_p":
        lines = text.splitlines()
        fields = lines[1].split(",")
        fields[3] = "0.8"
        lines[1] = ",".join(fields)
        text = "\n".join(lines)
    program = StataProgram.model_validate(fixture["program"])
    assert program.regression
    with pytest.raises(ValueError):
        extract_regression(fixture["stored"], text, specification(program), program.regression)


def test_summary_float_rounding_is_rejected() -> None:
    fixture = case("summary")
    fixture["named_csv"] = fixture["named_csv"].replace("1.415984", "1.415990")
    program = StataProgram.model_validate(fixture["program"])
    assert program.summary
    with pytest.raises(ValueError):
        extract_summary(
            {"data": {"r": fixture["scalar_snapshot"]}},
            fixture["named_csv"],
            specification(program),
            program.summary,
        )


@pytest.mark.parametrize("name", ["did", "rdd", "logit", "summary"])
def test_sample_or_structure_failure_is_blocking(name: str) -> None:
    fixture = case(name)
    program = StataProgram.model_validate(fixture["program"])
    spec = specification(program)
    normalized = (
        ScalarResult.model_validate(fixture["normalized"])
        if name == "summary"
        else CoefficientResult.model_validate(fixture["normalized"])
    )
    checks = method_checks(
        fixture["stored"], normalized, spec, program, sample_match=False, evidence_ids=("raw_test",)
    )
    assert next(c for c in checks if c.category == "specification").status == "fail"


class RecordedTransport(FakeTransport):
    """仅回放已经核对的协议边界；存储、提取和只读导出使用生产代码。"""

    def __init__(self, fixture: dict[str, Any], sample_ok: bool = True) -> None:
        super().__init__()
        self.fixture = fixture
        self.sample_ok = sample_ok

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "stata_get_results":
            self.calls.append((name, dict(arguments)))
            return {"success": True, **copy.deepcopy(self.fixture["stored"])}
        response = await super().call(name, arguments)
        if name == "stata_run":
            path = Path(arguments["code"]) if arguments.get("is_file") else None
            if path and (
                path.name == "extract.do"
                or (path.name == "analysis.do" and self.fixture["program"]["summary"])
            ):
                output = self.sessions[arguments["session_id"]]["output"]
                (
                    output
                    / ("scalars.csv" if self.fixture["program"]["summary"] else "coefficients.csv")
                ).write_text(self.fixture["named_csv"])
                (output / "sample_check.txt").write_text("1" if self.sample_ok else "0")
                if self.fixture["program"]["summary"]:
                    (output / "r_snapshot.json").write_text(
                        json.dumps(self.fixture["scalar_snapshot"])
                    )
        return response


@pytest.mark.parametrize("name", ["linear", "did", "rdd", "logit", "summary"])
@pytest.mark.parametrize("sample_ok", [True, False])
async def test_backend_selection_reextraction_and_noncoefficient_export(
    tmp_path: Path,
    name: str,
    sample_ok: bool,
) -> None:
    fixture = case(name)
    program = StataProgram.model_validate(fixture["program"])
    source = tmp_path / "synthetic.csv"
    # 离线回放使用冻结的数据字节，避免各平台浮点噪声生成的末位差异。
    # 原参考输入使用 CSV 标准 CRLF；仓库示例以 LF 存储。
    frozen = REFERENCE.parents[1] / "quickstart/synthetic.csv"
    source.write_bytes(frozen.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == json.loads(REFERENCE.read_text())["input_sha256"]
    )
    with RunStore.create(
        tmp_path / "runs", ResearchTask(data_path=source, research_goal="方法契约测试")
    ) as store:
        prepared = prepare_input(store, tmp_path / "backups")
        store.transition_run(RunStatus.RUNNING)
        base = specification(program)
        design = store.create_design(
            DesignContent(
                population="合成夹具",
                variables={v: "合成变量" for v in base.variables},
                sample_rule=base.sample_rule,
                estimand="仅测试映射",
                methods=(base.method,),
            ),
            change_reason="离线回放",
        )
        request = store.request_human(design.design_id, "夹具确认")
        store.apply_answer(
            request.request_id,
            design_sha256=content_hash(design),
            answer=HumanAnswer(decision="accept", explanation="脚本化测试"),
        )
        spec = specification(
            program,
            experiment_id=new_id("experiment"),
            run_id=store.run_id,
            design_id=design.design_id,
            input_artifact_id=prepared.input_artifact_id,
        )
        store.create_experiment(spec)
        transport = RecordedTransport(fixture, sample_ok)
        backend = StataBackend(store, transport, prepared, tmp_path / "work")
        outcome = await backend.run_analysis(spec, attempt_id=attempt(store, spec), program=program)
        assert outcome.error is None and outcome.result.result_id
        assert outcome.result.extraction_status == ExtractionStatus.SUCCEEDED
        assert outcome.validation_artifact_id
        validation = ValidationRecord.model_validate_json(
            store.read_artifact(outcome.validation_artifact_id)
        )
        assert validation.eligible() == sample_ok
        if not sample_ok:
            with pytest.raises(StateConflict):
                store.select_result(spec.experiment_id, outcome.result.result_id)
            return
        store.select_result(spec.experiment_id, outcome.result.result_id)
        call_count = len(transport.calls)
        reextracted = backend.reextract(outcome.result.attempt_id)
        assert len(transport.calls) == call_count
        assert reextracted.result.result_id != outcome.result.result_id
        assert reextracted.result.normalized == outcome.result.normalized
        assert reextracted.result.result_id
        store.select_result(spec.experiment_id, reextracted.result.result_id)
        tables = result_tables(store, store.selected_results())
        assert tables["facts"] and all(
            f["result_id"] == reextracted.result.result_id for f in tables["facts"].values()
        )
        if name == "summary":
            assert any(f["field"] == "normalized.values.2.value" for f in tables["facts"].values())
        store.transition_run(RunStatus.PAUSED, reason="离线导出")
        bundle = export_run(store, tmp_path / "output")
        manifest = json.loads((bundle / "script_manifest.json").read_text())
        assert any(entry["current"] and entry["portable"] for entry in manifest)
        assert len(transport.calls) == call_count


def test_conflicting_extraction_declarations_are_rejected() -> None:
    linear = case("linear")["program"]
    linear["summary"] = case("summary")["program"]["summary"]
    with pytest.raises(ValidationError):
        StataProgram.model_validate(linear)


def test_validation_is_limited_to_recorded_environment_and_code() -> None:
    environment = json.loads(REFERENCE.read_text())["environment"]
    verified = verified_scopes(environment)
    assert {"regress", "logit", "summarize"} <= set(verified)
    assert verified_scopes({**environment, "stata_build": "18.0.120"}) == {}
    assert verified_scopes({**environment, "mcp_version": "other"}) == {}
    assert verified_scopes({}) == {}


def test_changed_mapping_code_invalidates_published_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from econ_agent import stata_methods

    root = Path(stata_methods.__file__).parent
    proof = json.loads((root / "method_validation.json").read_text())
    for name in proof["mapping_sha256"]:
        (tmp_path / name).write_bytes((root / name).read_bytes())
    (tmp_path / "method_validation.json").write_text(json.dumps(proof))
    monkeypatch.setattr(stata_methods, "__file__", str(tmp_path / "stata_methods.py"))
    assert verified_scopes(proof["environment"])
    (tmp_path / "stata_results.py").write_text("# 测试中的不同提取版本\n")
    assert verified_scopes(proof["environment"]) == {}


@pytest.mark.parametrize("family", ["linear", "did", "rdd", "logit", "summary"])
def test_sample_expressions_cannot_inject_another_command(family: str) -> None:
    fixture = case(family)["program"]
    rule = fixture["summary"] if family == "summary" else fixture["regression"]
    rule["sample_expression"] = "included==1\nclear all"
    with pytest.raises(ValidationError):
        StataProgram.model_validate(fixture)


def test_iv_allows_equivalent_control_position_but_rejects_extra_commands() -> None:
    from econ_agent.schemas import IVSpecification
    from econ_agent.stata_results import iv_checks

    iv = IVSpecification(
        outcome="y",
        endogenous="x",
        instruments=("z",),
        controls=("w",),
        sample_expression="group==1",
    )
    normalized = CoefficientResult.model_validate(case("did")["normalized"])
    source_estimate = normalized.estimates[0]
    normalized = normalized.model_copy(
        update={
            "estimates": tuple(
                source_estimate.model_copy(update={"name": name}) for name in ("x", "w", "_cons")
            )
        }
    )
    command = "ivregress 2sls y (x=z) w if group==1, small"
    program = StataProgram(code=command, iv=iv)
    raw = {"data": {"e": {"depvar": "y", "endog": "x", "insts": "w z", "cmdline": command}}}
    spec = ExperimentSpec(
        experiment_id="experiment_iv",
        run_id="run_iv",
        design_id="design_iv",
        input_artifact_id="input_iv",
        purpose="等价命令的结构回归",
        initiation="planned",
        method="ivregress 2sls",
        variables=iv.variables(),
        sample_rule=iv.sample_expression,
        expected_result_type="coefficients",
    )
    checks = iv_checks(raw, normalized, spec, program, sample_match=True, evidence_ids=("raw_iv",))
    assert next(c for c in checks if c.category == "specification").status == "pass"
    for code in (command + "\nsummarize x", command.replace("group==1", "group==0")):
        checks = iv_checks(
            raw,
            normalized,
            spec,
            StataProgram(code=code, iv=iv),
            sample_match=True,
            evidence_ids=("raw_iv",),
        )
        assert next(c for c in checks if c.category == "specification").status == "fail"
