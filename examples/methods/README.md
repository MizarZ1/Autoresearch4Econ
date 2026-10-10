# 合成方法数值参考

本目录是开发验收材料，不是研究者的真实数据，也不作为模型的预期答案。
`synthetic_data()`（`scripts/validate_stage7.py`）用 Python 标准库、种子 2001
生成 400 行独立观测的 CSV，写入本地忽略目录；Python 仅生成夹具和独立核对
描述统计，不是分析后端。估计、推断与生产复现均由本机 Stata 执行。

`reference.do` 在全新原生 Stata 会话运行明确命令，使用 `lincom` 获取系数、
标准误、双侧 p 值、95% 区间与自由度；不调用生产提取脚本。
`stata18_reference.json` 保存真实 MCP 的 e() 白名单、具名提取 CSV、独立参考及
规范化结果。描述统计的 MCP r() 在异步轮询后为空，原样保留；`scalar_snapshot`
来自主 do-file 内即时冻结的 r() 文件，不能称作轮询后的 MCP 返回。
普通离线测试回放该参考，不启动 Stata 或请求模型。

数据结构：

- `x` 为五档连续解释变量；`ylinear` 按线性关系加扰动生成，首行故意缺失，
  核对实际估计 N=399。
- `group`、`post` 是两组两期重复截面，`did=group*post`；每个格子 100 行。
  `ydid` 包含交互项及扰动；无重复单位，不使用面板标准误。
- `running` 在负一到一之间均匀分布，阈值为零；`centered=running`，
  `treated=(running>=0)`，`interaction=treated*centered`；`yrdd` 含阈值跳跃
  与两侧不同斜率。预定带宽 0.5、均匀权重、局部线性、HC1，估计 N=200。
- `binary` 为各 x 档内固定比例的零一结果，避免完全预测；Logit 核对正态推断。
- `included=1` 是明确的全样本规则；描述统计核对 N、均值、样本标准差、极值。

运行：

```sh
uv run --locked --extra stata --group dev python scripts/validate_stage7.py
```

脚本还用错误阈值 0.25 实际运行一个 RDD 负例：估计成功但结构核验失败，
禁止选择为当前结果。所有正例分别导出并搬迁复现包，在新原生会话重跑；
样本检查、数字、N 和适用自由度必须匹配。
验收容差为绝对 1e-7、相对 1e-6，按二者较大值核对；N 和自由度精确一致。
不验证带宽选择、偏差校正、密度检验、平行趋势、分期处理或 Logit 边际效应。

本地原始 CSV、日志、数据库及包不上传；公开 JSON 中只保留自编合成数据
的数值参考和脱敏关联信息。AJR 来源与校验另见 `examples/demo/sources.json` 和根目录 README。
