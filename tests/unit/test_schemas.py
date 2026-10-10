"""中文动作、多结果形态和格式版本的离线校验。"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from econ_agent.schemas import (
    ACTION_ADAPTER,
    MODEL_SYSTEM_PROMPT_ZH,
    Artifact,
    CoefficientResult,
    EffectPathResult,
    Estimate,
    ExperimentResult,
    ExtractionStatus,
    MatrixResult,
    MatrixValue,
    MethodCapability,
    ModelCall,
    ResearchTask,
    ScalarResult,
    ScalarValue,
    StandardErrors,
    TableResult,
    ValidationCheck,
    ValidationRecord,
)


def estimate() -> Estimate:
    return Estimate(
        name="x",
        meaning="合成系数",
        value=1.0,
        standard_error=0.1,
        missing_reasons={
            "p_value": "测试未计算",
            "confidence_interval": "测试未计算",
            "degrees_of_freedom": "测试未计算",
        },
    )


def context() -> dict[str, object]:
    return {
        "sample_size": 2,
        "sample_note": "合成测试样本",
        "actual_command": "测试命令",
        "estimator": "测试估计量",
        "variables": ["x"],
        "sample_rule": "全部测试行",
        "standard_errors": StandardErrors(
            treatment="不适用", explanation="仅测试结构"
        ).model_dump(),
    }


def test_task_paths_resolve_from_task_directory(tmp_path: Path) -> None:
    directory = tmp_path / "materials"
    directory.mkdir()
    source = directory / "task.json"
    source.write_text(
        json.dumps(
            {
                "data_path": "../data.csv",
                "research_goal": "测试目标",
                "material_paths": ["notes.md"],
                "max_research_rounds": 4,
            }
        )
    )
    task = ResearchTask.from_file(source)
    assert task.data_path == tmp_path / "data.csv"
    assert task.material_paths == (directory / "notes.md",)
    assert task.budget.max_research_rounds == 4
    assert task.output_language == "zh-CN"


@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_future_or_invalid_object_version_rejected(version: object) -> None:
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python(
            {
                "schema_version": version,
                "action": "get_method_help",
                "reason": "测试版本",
                "arguments": {"method": "iv"},
            }
        )


@pytest.mark.parametrize(
    "change",
    [
        {"action": "unknown"},
        {"arguments": {"method": "iv", "api_key": "禁止字段"}},
        {"arguments": {}},
        {"reason": ""},
    ],
)
def test_unknown_actions_and_arguments_are_rejected(change: dict[str, object]) -> None:
    raw = {"action": "get_method_help", "reason": "查看方法前提", "arguments": {"method": "iv"}}
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python(raw | change)


def test_chinese_interaction_contract() -> None:
    assert "所有解释、问题、方案和结论使用中文" in MODEL_SYSTEM_PROMPT_ZH
    action = ACTION_ADAPTER.validate_json(
        json.dumps(
            {
                "action": "finish_research",
                "reason": "预算已耗尽，保留部分证据",
                "arguments": {
                    "stop_reason": "预算耗尽",
                    "conclusion_draft": "尚不能作出完整研究结论",
                },
            }
        )
    )
    assert action.reason == "预算已耗尽，保留部分证据"


def test_all_result_shapes_have_roundtrip() -> None:
    models = [
        CoefficientResult.model_validate(context() | {"estimates": [estimate().model_dump()]}),
        ScalarResult.model_validate(
            context()
            | {
                "values": [
                    ScalarValue(
                        name="mean",
                        value=None,
                        meaning="未取得的测试均值",
                        missing_reason="执行器未输出",
                    ).model_dump()
                ]
            }
        ),
        MatrixResult.model_validate(
            context()
            | {
                "matrices": [
                    MatrixValue(
                        name="V",
                        row_names=("x",),
                        column_names=("x",),
                        values=((1.0,),),
                        meaning="测试矩阵",
                    ).model_dump()
                ]
            }
        ),
        EffectPathResult.model_validate(
            context() | {"periods": ["0"], "effects": [estimate().model_dump()]}
        ),
        TableResult.model_validate(
            context()
            | {"columns": ["名称", "值"], "rows": [["测试", None]], "notes": ["缺失值不替换为零"]}
        ),
    ]
    for result in models:
        assert type(result).model_validate_json(result.model_dump_json()) == result


def test_nonfinite_and_dimension_errors_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ScalarValue(name="test", value=float("nan"), meaning="测试")
    with pytest.raises(ValidationError):
        MatrixValue(
            name="V", row_names=("x",), column_names=("x",), values=((1, 2),), meaning="测试"
        )
    with pytest.raises(ValidationError):
        EffectPathResult.model_validate(
            context() | {"periods": ["0", "1"], "effects": [estimate().model_dump()]}
        )
    with pytest.raises(ValidationError):
        TableResult.model_validate(context() | {"columns": ["值"], "rows": [[1, 2]]})
    with pytest.raises(ValidationError):
        Estimate(name="x", meaning="测试", value=0)


def test_execution_success_is_not_extraction_success() -> None:
    fields = {
        "experiment_id": "exp",
        "attempt_id": "attempt",
        "design_id": "design",
        "execution_status": "SUCCEEDED",
        "input_sha256": "a" * 64,
        "code_sha256": "b" * 64,
    }
    raw_only = ExperimentResult.model_validate(fields)
    assert raw_only.normalized is None
    assert raw_only.extraction_status == ExtractionStatus.PENDING
    with pytest.raises(ValidationError):
        ExperimentResult.model_validate(fields | {"extraction_status": "SUCCEEDED"})


def test_capability_validation_is_not_assumed() -> None:
    capability = MethodCapability(method="iv", commands=("ivregress",))
    assert capability.validation == "unverified"
    assert capability.availability == "unknown"
    with pytest.raises(ValidationError):
        MethodCapability(method="iv", commands=("ivregress",), validation="validated")


def test_usage_negative_or_non_utc_time_rejected() -> None:
    fields: dict[str, object] = {
        "model_call_id": "model",
        "run_id": "run",
        "operation_id": "operation",
        "purpose": "测试",
        "provider": "测试",
        "requested_model": "测试",
        "prompt_version": "1",
        "config_version": "1",
    }
    assert ModelCall.model_validate(fields).total_tokens is None
    invalid_records: list[dict[str, object]] = [
        {"total_tokens": -1},
        {"retry_no": -1},
        {"started_at": "2026-10-10T12:00:00"},
        {"started_at": "2026-10-10T12:00:00+08:00"},
        {"interaction_language": "en"},
    ]
    for invalid in invalid_records:
        with pytest.raises(ValidationError):
            ModelCall.model_validate(fields | invalid)


def test_future_artifact_format_rejected() -> None:
    with pytest.raises(ValidationError):
        Artifact.model_validate(
            {
                "artifact_id": "art",
                "run_id": "run",
                "kind": "input",
                "relative_path": "inputs/data.csv",
                "sha256": "a" * 64,
                "byte_size": 1,
                "format_version": 2,
            }
        )


def test_validation_requires_program_evidence_and_both_categories() -> None:
    base = {
        "result_artifact_id": "result",
        "result_sha256": "a" * 64,
        "design_id": "design",
        "design_sha256": "b" * 64,
        "rules_version": "1",
    }
    check = ValidationCheck(
        category="computation",
        name="数值",
        status="pass",
        assessor="program",
        explanation="仅测试规则",
        evidence_artifact_ids=("raw",),
    )
    record = ValidationRecord.model_validate(base | {"checks": [check.model_dump()]})
    assert not record.eligible()
