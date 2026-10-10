"""独立核对已提取的推断数字；不执行统计估计，也不替代 Stata 后端。"""

import math
from functools import lru_cache


def _beta_fraction(a: float, b: float, x: float) -> float:
    """用连分式计算不完全 beta 的因子；不收敛时拒绝核验。"""
    minimum = 1e-300
    c = 1.0
    d = 1 - (a + b) * x / (a + 1)
    if abs(d) < minimum:
        d = minimum
    d = 1 / d
    value = d
    for m in range(1, 501):
        even = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        odd = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))
        for term in (even, odd):
            d = 1 + term * d
            c = 1 + term / c
            if abs(d) < minimum:
                d = minimum
            if abs(c) < minimum:
                c = minimum
            d = 1 / d
            delta = d * c
            value *= delta
        if abs(delta - 1) < 1e-14:
            return value
    raise ValueError("t 推断独立核对未收敛")


def t_two_sided(statistic: float, df: float) -> float:
    """通过正则不完全 beta 计算双侧 t 尾概率。"""
    if not math.isfinite(df) or df <= 0 or math.isnan(statistic):
        raise ValueError("t 推断需要有效统计量和正自由度")
    x = df / (df + statistic * statistic)
    if x == 0:
        return 0.0
    if x == 1:
        return 1.0
    a, b = df / 2, 0.5
    factor = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1) / (a + b + 2):
        result = factor * _beta_fraction(a, b, x) / a
    else:
        result = 1 - factor * _beta_fraction(b, a, 1 - x) / b
    return min(1.0, max(0.0, result))


@lru_cache(maxsize=128)
def t_critical_95(df: float) -> float:
    """二分求双侧百分之九十五区间的 t 临界值。"""
    left, right = 0.0, 2.0
    while t_two_sided(right, df) > 0.05:
        right *= 2
        if right > 1e12:
            raise ValueError("t 临界值超出核验范围")
    for _ in range(100):
        middle = (left + right) / 2
        if t_two_sided(middle, df) > 0.05:
            left = middle
        else:
            right = middle
    return (left + right) / 2


def check_inference(values: dict[str, float], df: float | None) -> None:
    """复核 p 值和两端区间，包含估计值并保持正确的推断口径。"""
    coefficient, se = values["coefficient"], values["standard_error"]
    if se <= 0:
        raise ValueError("完整推断需要正标准误")
    statistic = abs(coefficient / se)
    p_value = math.erfc(statistic / math.sqrt(2)) if df is None else t_two_sided(statistic, df)
    critical = 1.959963984540054 if df is None else t_critical_95(df)
    expected = {
        "p_value": p_value,
        "lower": coefficient - critical * se,
        "upper": coefficient + critical * se,
    }
    if any(
        not math.isclose(values[k], value, rel_tol=1e-8, abs_tol=1e-10)
        for k, value in expected.items()
    ):
        raise ValueError("p 值或置信区间与系数、标准误及推断口径不一致")
