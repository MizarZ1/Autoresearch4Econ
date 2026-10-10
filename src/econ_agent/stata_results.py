"""轻量 2SLS 提取规则；推断数值由 Stata 计算，不猜测正态近似。"""

import csv
import io
import math
import re
from typing import Any

from econ_agent.inference import check_inference
from econ_agent.schemas import (
    CoefficientResult,
    Estimate,
    ExperimentSpec,
    StandardErrors,
    ValidationCheck,
)
from econ_agent.stata_contracts import IVSpecification, StataProgram


def canonical_command(command: str) -> str:
    # Stata 标识符区分大小写，不能把 x 与 X 或字符串条件合并。
    return re.sub(r"\s+", "", command)


def iv_extraction_code(iv: IVSpecification) -> str:
    """在同一锁及会话中读取真实列名、样本及 t 推断，再导出具名 CSV。"""
    if re.search(r"[^A-Za-z0-9_ .()=!<>|&+*/-]", iv.sample_expression):
        raise ValueError("已核验 IV 规则只接受单行数值型样本表达式")
    return coefficient_extraction_code(iv.sample_expression, iv.variables())


def coefficient_extraction_code(
    sample_expression: str,
    variable_names: tuple[str, ...],
    *,
    normal: bool = False,
    structure_code: str = "",
) -> str:
    """共同具名提取：线性模型使用 t，Logit 使用正态推断。"""
    if re.search(r"[^A-Za-z0-9_ .()=!<>|&+*/-]", sample_expression):
        raise ValueError("提取规则只接受单行数值型样本表达式")
    variables = ",".join(variable_names)
    p_expression = (
        "2*normal(-abs(_b[`econ_name']/econ_se))"
        if normal
        else "2*ttail(e(df_r),abs(_b[`econ_name']/econ_se))"
    )
    return f"""* 主估计的 e() 已先由 MCP 保存；这里读取具名参数及样本证据。
local econ_names : colnames e(b)
capture assert e(sample) == (({sample_expression}) & !missing({variables}))
local econ_sample_ok = (_rc == 0)
{structure_code}
tempname econ_sample_file
file open `econ_sample_file' using "$ECON_OUTPUT/sample_check.txt", write text
file write `econ_sample_file' "`econ_sample_ok'"
file close `econ_sample_file'
tempname econ_post
postfile `econ_post' str64 parameter ///
    double coefficient double standard_error double p_value ///
    double lower double upper double df double n ///
    using "$ECON_OUTPUT/coefficients.dta"
foreach econ_name of local econ_names {{
    scalar econ_se = _se[`econ_name']
    scalar econ_p = {p_expression}
    scalar econ_critical = {"invnormal(0.975)" if normal else "invttail(e(df_r),0.025)"}
    post `econ_post' ("`econ_name'") (_b[`econ_name']) (econ_se) (econ_p) ///
        (_b[`econ_name']-econ_critical*econ_se) ///
        (_b[`econ_name']+econ_critical*econ_se) ({"." if normal else "e(df_r)"}) (e(N))
}}
postclose `econ_post'
preserve
use "$ECON_OUTPUT/coefficients.dta", clear
format coefficient standard_error p_value lower upper df n %24.17g
export delimited using "$ECON_OUTPUT/coefficients.csv", datafmt
restore
"""


def extract_iv(
    raw: dict[str, Any], named_csv: str, spec: ExperimentSpec, iv: IVSpecification
) -> CoefficientResult:
    """具名返回必须与 e(b)/e(V) 对应，列名绝不由声明的变量列表替代。"""
    e = raw["data"]["e"]
    if e.get("cmd") != "ivregress" or e.get("estimator") != "2sls" or e.get("small") != "small":
        raise ValueError("实际命令、估计量或有限样本推断口径不属于当前提取规则")
    if (
        e.get("vce") != "unadjusted"
        or not isinstance(e.get("df_r"), (int, float))
        or e["df_r"] <= 0
    ):
        raise ValueError("当前规则要求常规协方差与有效残差自由度，不自动猜测推断方式")
    b, covariance = e["_matrices"]["b"], e["_matrices"]["V"]
    if b.get("truncated") or covariance.get("truncated"):
        raise ValueError("矩阵被截断，禁止用不完整输出构造结果")
    rows = list(csv.DictReader(io.StringIO(named_csv)))
    size = len(iv.controls) + 2
    names = {iv.endogenous, *iv.controls, "_cons"}
    if (
        len(rows) != size
        or {row["parameter"] for row in rows} != names
        or len(b["values"]) != 1
        or len(b["values"][0]) != size
        or len(covariance["values"]) != size
        or any(len(row) != size for row in covariance["values"])
        or e.get("rank") != size
    ):
        raise ValueError("具名参数、系数和协方差矩阵维度不一致")
    if not all(math.isfinite(float(x)) for row in covariance["values"] for x in row):
        raise ValueError("协方差矩阵含非有限值")
    for i in range(size):
        for j in range(size):
            if not math.isclose(
                covariance["values"][i][j], covariance["values"][j][i], abs_tol=1e-12
            ):
                raise ValueError("协方差矩阵不对称")
    n = float(e["N"])
    if not math.isfinite(n) or n <= size or n != int(n) or e["df_r"] != n - size:
        raise ValueError("IV 样本量或自由度与完整参数数量不一致")
    estimates = []
    for index, row in enumerate(rows):
        values = {
            k: float(row[k])
            for k in ("coefficient", "standard_error", "p_value", "lower", "upper", "df", "n")
        }
        if not all(math.isfinite(v) for v in values.values()):
            raise ValueError("具名推断返回含缺失或非有限数字")
        if len(covariance["values"][index]) != len(rows) or covariance["values"][index][index] < 0:
            raise ValueError("协方差矩阵不完整或方差为负")
        if not math.isclose(
            values["coefficient"], b["values"][0][index], rel_tol=1e-10, abs_tol=1e-12
        ) or not math.isclose(
            values["standard_error"],
            math.sqrt(covariance["values"][index][index]),
            rel_tol=1e-10,
            abs_tol=1e-12,
        ):
            raise ValueError("具名推断数值与 MCP 的原始矩阵不符")
        if values["df"] != e["df_r"] or values["n"] != e["N"]:
            raise ValueError("推断表的样本量或自由度与主估计不符")
        check_inference(values, values["df"])
        estimates.append(
            Estimate(
                name=row["parameter"],
                meaning="截距"
                if row["parameter"] == "_cons"
                else f"{row['parameter']} 的 2SLS 系数",
                value=values["coefficient"],
                standard_error=values["standard_error"],
                p_value=values["p_value"],
                confidence_interval=(values["lower"], values["upper"]),
                degrees_of_freedom=values["df"],
            )
        )
    if len({estimate.name for estimate in estimates}) != len(estimates) or float(e["N"]) != int(
        e["N"]
    ):
        raise ValueError("参数名重复或样本量不是整数")
    return CoefficientResult(
        sample_size=int(e["N"]),
        sample_note="Stata 实际估计样本；已与声明条件逐行比较",
        actual_command=e["cmdline"],
        estimator="2SLS，有限样本 t 推断",
        variables=spec.variables,
        sample_rule=iv.sample_expression,
        standard_errors=StandardErrors(
            treatment="常规同方差标准误",
            explanation="实际 e(vce)=unadjusted，small 修正，95% 区间及双侧 p 值由 Stata 计算",
        ),
        estimates=tuple(estimates),
    )


def iv_checks(
    raw: dict[str, Any],
    normalized: CoefficientResult,
    spec: ExperimentSpec,
    program: StataProgram,
    *,
    sample_match: bool,
    evidence_ids: tuple[str, ...],
) -> tuple[ValidationCheck, ...]:
    iv = program.iv
    if iv is None:
        raise ValueError("IV 核验需要明确的声明规格")
    e = raw["data"]["e"]
    expected_names = {iv.endogenous, *iv.controls, "_cons"}
    body = "\n".join(line for line in program.code.splitlines() if not line.strip().startswith("*"))
    body = re.sub(r"^\s*(quietly|qui)\s+", "", body)
    controls_after = (
        f"ivregress 2sls {iv.outcome} ({iv.endogenous}={' '.join(iv.instruments)}) "
        f"{' '.join(iv.controls)} if {iv.sample_expression}, small"
    )
    allowed_commands = {canonical_command(iv.command()), canonical_command(controls_after)}
    matches = (
        e.get("depvar") == iv.outcome
        and set(str(e.get("endog", "")).split()) == {iv.endogenous}
        and set(str(e.get("insts", "")).split()) == {*iv.instruments, *iv.controls}
        and {v.name for v in normalized.estimates} == expected_names
        and set(spec.variables) == set(iv.variables())
        and canonical_command(spec.sample_rule) == canonical_command(iv.sample_expression)
        and spec.method == "ivregress 2sls"
        and canonical_command(e["cmdline"]) in allowed_commands
        and canonical_command(body) == canonical_command(e["cmdline"])
        and sample_match
    )
    return (
        ValidationCheck(
            category="computation",
            name="具名返回与原始矩阵及输入代码一致",
            status="pass",
            assessor="program",
            explanation=(
                "核对实际参数名、矩阵维度、系数、方差、样本量及自由度；输入与代码由归档 hash 绑定"
            ),
            evidence_artifact_ids=evidence_ids,
        ),
        ValidationCheck(
            category="specification",
            name="实际 2SLS 与声明变量样本及当前设计一致",
            status="pass" if matches else "fail",
            assessor="program",
            explanation="核对主命令、因变量、内生变量、工具、控制、截距、样本、协方差和执行主体；当前设计及来源由存储层核对",
            evidence_artifact_ids=evidence_ids,
        ),
        ValidationCheck(
            category="method",
            name="工具变量识别的解释限制",
            status="pending",
            assessor="program",
            explanation=(
                "数值一致不能证明排除限制或工具外生性，须结合背景材料和诊断披露；"
                "阶段 2 不宣称识别已经证明"
            ),
            required=False,
            evidence_artifact_ids=evidence_ids,
        ),
    )
