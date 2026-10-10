# 自编合成数据字典

本文件由项目固定种子 2001 生成，400 个独立合成观测。没有原作者数据，不能解释为国家、个人或真实研究样本。生成程序是 `scripts/validate_stage7.py` 的 `synthetic_data`。

| 变量 | 定义 |
| --- | --- |
| x | 五档合成解释变量，取值 −2 至 2 |
| ylinear | 合成线性结果，首行缺失，其余为截距、x 项与随机扰动之和；OLS 需按完整观测筛选 |
| included | 全部为 1，表示预定的样本入口；不是结果筛选变量 |
| group、post、did、ydid | 两组两期独立重复截面演示；did 为 group×post，ydid 为合成结果 |
| running、centered、treated、interaction、yrdd | 锐断点演示，阈值为 0；centered=running，treated 表示非负侧，interaction=treated×centered；yrdd 为合成结果 |
| binary | 二元合成结果，仅用于 Logit 映射验收 |

快速任务只使用 x、ylinear 与 included。x 的描述统计有 400 个有效值，ylinear 的 OLS 有 399 个完整观测；不同分析的 N 不必相同，应披露缺失处理。
