"""以闭式分布和公开临界值核对推断校验，独立于提取实现。"""

import math

import pytest

from econ_agent.inference import t_critical_95, t_two_sided


@pytest.mark.parametrize("statistic", [0.0, 0.1, 1.0, 2.0, 20.0])
def test_t_one_df_matches_cauchy(statistic: float) -> None:
    assert t_two_sided(statistic, 1) == pytest.approx(
        1 - 2 * math.atan(statistic) / math.pi, abs=1e-12
    )


@pytest.mark.parametrize("statistic", [0.0, 0.1, 1.0, 2.0, 20.0])
def test_t_two_df_matches_closed_form(statistic: float) -> None:
    assert t_two_sided(statistic, 2) == pytest.approx(
        1 - statistic / math.sqrt(2 + statistic**2), abs=1e-12
    )


@pytest.mark.parametrize(
    "df,expected", [(1, 12.7062047364), (2, 4.3026527299), (10, 2.2281388520), (60, 2.0002978220)]
)
def test_95_percent_critical_values(df: float, expected: float) -> None:
    assert t_critical_95(df) == pytest.approx(expected, abs=1e-9)
