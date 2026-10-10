"""阶段七明确规格的 Stata 映射；通用执行及未知返回仍保留原始证据。"""

import csv
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any

from econ_agent.schemas import (
    CoefficientResult,
    Estimate,
    ExperimentSpec,
    RegressionSpecification,
    ScalarResult,
    ScalarValue,
    StandardErrors,
    SummarySpecification,
    ValidationCheck,
)
from econ_agent.stata_contracts import StataProgram
from econ_agent.stata_results import canonical_command, coefficient_extraction_code

SCOPES = {
    "ivregress": "仅 2SLS、small、常规协方差及完整具名参数",
    "regress": (
        "含截距、满秩、无权重 OLS；常规或 HC1；两组两期重复截面 DiD；"
        "锐断点局部线性 RDD（均匀权重、预定带宽、HC1）"
    ),
    "logit": "二元 Logit、满秩且收敛、观测信息矩阵、正态推断；系数不是边际效应",
    "summarize": "单变量、无权重的 N、均值、样本标准差、最小与最大值",
}


def verified_scopes(environment: dict[str, Any]) -> dict[str, str]:
    """发布记录仅适用于相同构建、连接版本和映射代码；其他环境保持未验证。"""
    path = Path(__file__).with_name("method_validation.json")
    try:
        proof = json.loads(path.read_text())
        if any(environment.get(k) != v for k, v in proof["environment"].items()):
            return {}
        for name, expected in proof["mapping_sha256"].items():
            if hashlib.sha256(path.with_name(name).read_bytes()).hexdigest() != expected:
                return {}
        return {str(k): str(v) for k, v in proof["validated_scopes"].items() if k in SCOPES}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def regression_extraction_code(rule: RegressionSpecification) -> str:
    """结构检查只读取估计样本，异常使必需规格核验失败。"""
    structure = []
    if rule.did:
        d = rule.did
        structure.extend(
            [
                f"capture assert inlist({d.treatment},0,1) & inlist({d.post},0,1) & "
                f"{d.interaction}=={d.treatment}*{d.post} if e(sample)",
                "local econ_sample_ok = `econ_sample_ok' & (_rc==0)",
            ]
        )
        for group in (0, 1):
            for period in (0, 1):
                structure.extend(
                    [
                        f"quietly count if e(sample) & {d.treatment}=={group} & {d.post}=={period}",
                        "local econ_sample_ok = `econ_sample_ok' & (r(N)>0)",
                    ]
                )
    if rule.rdd:
        r = rule.rdd
        structure.extend(
            [
                f"capture assert {r.treatment}==({r.running}>={r.cutoff}) & "
                f"abs({r.centered}-({r.running}-{r.cutoff}))<1e-12 & "
                f"abs({r.interaction}-{r.treatment}*{r.centered})<1e-12 & "
                f"abs({r.running}-{r.cutoff})<={r.bandwidth} if e(sample)",
                "local econ_sample_ok = `econ_sample_ok' & (_rc==0)",
            ]
        )
        for side in (0, 1):
            structure.extend(
                [
                    f"quietly count if e(sample) & {r.treatment}=={side}",
                    "local econ_sample_ok = `econ_sample_ok' & (r(N)>1)",
                ]
            )
    if rule.family == "logit":
        structure.extend(
            [
                f"capture assert inlist({rule.outcome},0,1) if e(sample)",
                "local econ_sample_ok = `econ_sample_ok' & (_rc==0)",
            ]
        )
    return coefficient_extraction_code(
        rule.sample_expression,
        rule.variables(),
        normal=rule.family == "logit",
        structure_code="\n".join(structure),
    )


def extract_regression(
    raw: dict[str, Any], named_csv: str, spec: ExperimentSpec, rule: RegressionSpecification
) -> CoefficientResult:
    """实际协方差、矩阵、收敛和自由度必须满足明确规则，不能猜测 z 或 t。"""
    e = raw["data"]["e"]
    normal = rule.family == "logit"
    expected_vce = "oim" if normal else ("ols" if rule.covariance == "unadjusted" else "robust")
    if e.get("cmd") != rule.method() or e.get("vce") != expected_vce:
        raise ValueError("实际估计命令或协方差不属于声明规则")
    if normal and e.get("converged") != 1:
        raise ValueError("Logit 未明确收敛")
    if not normal and (not isinstance(e.get("df_r"), (int, float)) or e["df_r"] <= 0):
        raise ValueError("线性估计缺少有效残差自由度")
    if e.get("wtype") or e.get("wexp"):
        raise ValueError("当前规则不接受权重")
    b, v = e["_matrices"]["b"], e["_matrices"]["V"]
    rows = list(csv.DictReader(io.StringIO(named_csv)))
    expected = (*rule.regressors, "_cons")
    size = len(expected)
    if (
        b.get("truncated")
        or v.get("truncated")
        or e.get("rank") != size
        or len(rows) != size
        or tuple(row["parameter"] for row in rows) != expected
        or len(b["values"]) != 1
        or len(b["values"][0]) != size
        or len(v["values"]) != size
        or any(len(row) != size for row in v["values"])
    ):
        raise ValueError("截断、遗漏参数、非满秩或矩阵维度不一致")
    if not all(math.isfinite(float(x)) for row in v["values"] for x in row):
        raise ValueError("协方差矩阵含非有限值")
    for i in range(size):
        for j in range(size):
            if not math.isclose(v["values"][i][j], v["values"][j][i], abs_tol=1e-12):
                raise ValueError("协方差矩阵不对称")
    n = float(e["N"])
    if not math.isfinite(n) or n <= size or n != int(n):
        raise ValueError("样本量无效")
    estimates = []
    for i, row in enumerate(rows):
        values = {
            k: float(row[k])
            for k in ("coefficient", "standard_error", "p_value", "lower", "upper", "n")
        }
        if not all(math.isfinite(x) for x in values.values()):
            raise ValueError("推断表含缺失或非有限数字")
        if v["values"][i][i] <= 0 or values["standard_error"] <= 0:
            raise ValueError("参数方差为零或负数，不能作为完整具名估计")
        if (
            not math.isclose(values["coefficient"], b["values"][0][i], rel_tol=1e-10, abs_tol=1e-12)
            or not math.isclose(
                values["standard_error"], math.sqrt(v["values"][i][i]), rel_tol=1e-10, abs_tol=1e-12
            )
            or values["n"] != n
        ):
            raise ValueError("推断表与原始矩阵或实际样本不一致")
        df = None if normal else float(row["df"])
        if (normal and row["df"] not in {"", "."}) or (
            not normal and (df != e["df_r"] or df != n - size)
        ):
            raise ValueError("推断自由度与实际估计不符")
        if normal and (
            not math.isclose(
                values["p_value"],
                math.erfc(abs(values["coefficient"] / values["standard_error"]) / math.sqrt(2)),
                abs_tol=1e-12,
            )
            or not math.isclose(
                values["lower"],
                values["coefficient"] - 1.959963984540054 * values["standard_error"],
                rel_tol=1e-10,
                abs_tol=1e-12,
            )
            or not math.isclose(
                values["upper"],
                values["coefficient"] + 1.959963984540054 * values["standard_error"],
                rel_tol=1e-10,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("Logit 推断与正态口径不一致")
        estimates.append(
            Estimate(
                name=row["parameter"],
                meaning="截距"
                if row["parameter"] == "_cons"
                else f"{row['parameter']} 的 {rule.family} 系数",
                value=values["coefficient"],
                standard_error=values["standard_error"],
                p_value=values["p_value"],
                confidence_interval=(values["lower"], values["upper"]),
                degrees_of_freedom=df,
                missing_reasons={"degrees_of_freedom": "Logit 使用渐近正态推断，无 t 自由度"}
                if normal
                else {},
            )
        )
    return CoefficientResult(
        sample_size=int(n),
        sample_note="实际估计样本与声明规则逐行比较；结构条件另行核验",
        actual_command=e["cmdline"],
        estimator={
            "linear": "OLS",
            "did": "两组两期重复截面 DiD",
            "rdd": "锐断点局部线性 RDD，均匀权重，预先指定带宽",
            "logit": "二元 Logit 对数优势系数",
        }[rule.family],
        variables=spec.variables,
        sample_rule=rule.sample_expression,
        standard_errors=StandardErrors(
            treatment="观测信息矩阵"
            if normal
            else ("HC1 稳健标准误" if rule.covariance == "robust" else "常规同方差标准误"),
            explanation="95% 区间与双侧 p 值由 Stata 计算；"
            + ("正态推断" if normal else "残差自由度的 t 推断"),
        ),
        estimates=tuple(estimates),
    )


def summary_extraction_code(rule: SummarySpecification) -> str:
    """在其他 r-class 命令覆盖 r() 之前保存真实描述统计。"""
    # 复用单行表达式检查，不能通过样本表达式注入其他命令。
    coefficient_extraction_code(rule.sample_expression, rule.variables())
    saved = "\n".join(
        f"scalar econ_saved_{name} = r({name})" for name in ("N", "mean", "sd", "min", "max")
    )
    snapshot = "\n".join(
        f'''file write `econ_snapshot' _char(34) "{name}" _char(34) ": " '''
        f"%24.17g (econ_saved_{name})" + (' "," _n' if name != "max" else " _n")
        for name in ("N", "mean", "sd", "min", "max")
    )
    return f"""* 在主 do-file 内即时冻结 r()，不把轮询后的空 r() 伪装为结果。
{saved}
tempname econ_snapshot
file open `econ_snapshot' using "$ECON_OUTPUT/r_snapshot.json", write text
file write `econ_snapshot' "{{" _n
{snapshot}
file write `econ_snapshot' "}}" _n
file close `econ_snapshot'
tempname econ_post
postfile `econ_post' double N double mean double sd double min double max ///
    using "$ECON_OUTPUT/scalars.dta"
post `econ_post' (econ_saved_N) (econ_saved_mean) (econ_saved_sd) (econ_saved_min) (econ_saved_max)
postclose `econ_post'
scalar econ_n = econ_saved_N
quietly count if ({rule.sample_expression}) & !missing({rule.variable})
local econ_sample_ok = (r(N)==econ_n)
tempname econ_file
file open `econ_file' using "$ECON_OUTPUT/sample_check.txt", write text
file write `econ_file' "`econ_sample_ok'"
file close `econ_file'
preserve
use "$ECON_OUTPUT/scalars.dta", clear
format N mean sd min max %24.17g
export delimited using "$ECON_OUTPUT/scalars.csv", datafmt
restore
"""


def extract_summary(
    raw: dict[str, Any], named_csv: str, spec: ExperimentSpec, rule: SummarySpecification
) -> ScalarResult:
    r = raw["data"]["r"]
    rows = list(csv.DictReader(io.StringIO(named_csv)))
    if len(rows) != 1 or set(rows[0]) != {"N", "mean", "sd", "min", "max"}:
        raise ValueError("描述统计表字段或行数不符")
    values = []
    for name, meaning in (
        ("N", "非缺失样本量"),
        ("mean", "均值"),
        ("sd", "样本标准差"),
        ("min", "最小值"),
        ("max", "最大值"),
    ):
        value = float(rows[0][name])
        if not math.isfinite(value) or not math.isclose(
            value, float(r[name]), abs_tol=1e-12, rel_tol=1e-10
        ):
            raise ValueError("描述统计表与原始 r() 不符")
        values.append(ScalarValue(name=name, value=value, meaning=meaning))
    n = float(r["N"])
    if n < 2 or n != int(n) or r["sd"] < 0 or not r["min"] <= r["mean"] <= r["max"]:
        raise ValueError("描述统计样本不足或取值无效")
    return ScalarResult(
        sample_size=int(n),
        sample_note="声明条件下单变量非缺失观测",
        actual_command=rule.command(),
        estimator="未加权描述统计",
        variables=spec.variables,
        sample_rule=rule.sample_expression,
        standard_errors=StandardErrors(
            treatment="不适用", explanation="样本标准差不是均值标准误；当前结果不提供推断"
        ),
        values=tuple(values),
    )


def method_checks(
    raw: dict[str, Any],
    normalized: CoefficientResult | ScalarResult,
    spec: ExperimentSpec,
    program: StataProgram,
    *,
    sample_match: bool,
    evidence_ids: tuple[str, ...],
) -> tuple[ValidationCheck, ...]:
    rule = program.regression or program.summary
    assert rule is not None
    body = "\n".join(line for line in program.code.splitlines() if not line.strip().startswith("*"))
    expected_method = rule.method() if isinstance(rule, RegressionSpecification) else "summarize"
    matches = (
        canonical_command(body) == canonical_command(rule.command())
        and spec.method == expected_method
        and spec.variables == rule.variables()
        and canonical_command(spec.sample_rule) == canonical_command(rule.sample_expression)
        and sample_match
    )
    if isinstance(rule, RegressionSpecification):
        e = raw["data"]["e"]
        matches = (
            matches
            and e.get("depvar") == rule.outcome
            and canonical_command(e["cmdline"]) == canonical_command(rule.command())
        )
    return (
        ValidationCheck(
            category="computation",
            name="具名输出与实际返回及归档一致",
            status="pass",
            assessor="program",
            explanation=(
                "核对有限数值、参数和矩阵、样本量、推断口径或原始描述统计；文件 hash 绑定确切尝试"
            ),
            evidence_artifact_ids=evidence_ids,
        ),
        ValidationCheck(
            category="specification",
            name="实际命令变量样本及方法结构一致",
            status="pass" if matches else "fail",
            assessor="program",
            explanation="核对实际命令与执行主体、声明变量和样本，以及二元、交互项或锐断点结构条件",
            evidence_artifact_ids=evidence_ids,
        ),
        ValidationCheck(
            category="method",
            name="方法识别与解释限制",
            status="pending",
            assessor="program",
            required=False,
            explanation=(
                "数值及结构检查不证明平行趋势、阈值连续性或因果识别；"
                "Logit 系数不是概率边际效应；描述统计不是效应估计"
            ),
            evidence_artifact_ids=evidence_ids,
        ),
    )


def extraction_code(program: StataProgram) -> str | None:
    if program.regression:
        return regression_extraction_code(program.regression)
    if program.summary:
        return summary_extraction_code(program.summary)
    return None
