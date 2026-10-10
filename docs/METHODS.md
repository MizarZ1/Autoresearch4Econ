# M1 Stata 方法清单

验证针对本机已有 Stata 18.0.185、mcp-stata 3.3.0、实际原生库 hash 和
指定映射代码，不外推整个方法家族。`availability` 表示 `which` 实测，
`normalization` 表示已接入的明确规则，`validation` 表示匹配环境下的数值参考。
发布记录或环境、代码指纹不匹配时，数值验证状态为 `unverified`。

| 验证组 | 实际命令与口径 | 结果形态与数值核对 | 范围限制 |
| --- | --- | --- | --- |
| 线性 | `regress ylinear x if included==1`，OLS 常规协方差、t，N=399 | 具名系数、标准误、p、95% 区间、df；独立原生 `lincom` | 满秩、含截距、无权重；没有验证 xtreg 或高维固定效应 |
| IV | `ivregress 2sls logpgp95 (avexpr=logem4) if baseco==1, small`；新样本基准及 lat_abst 控制，常规协方差 | 阶段零 N=64 参考、新样本 N=63、具名系数及推断、迁移包原生重跑 | 单内生变量；控制可在工具括号前后；不证明排除限制，不把未执行诊断写成结果 |
| DiD | `regress ydid group post did if included==1, vce(robust)`，N=400，HC1/t | 四格样本及交互结构、独立原生参考；交互系数另核对四格均值之差 | 两组两期重复截面，非面板、非分期处理；平行趋势未验证 |
| RDD | `regress yrdd treated centered interaction if abs(running-0)<=0.5, vce(robust)`，N=200，HC1/t | 锐分配、阈值、中心化、交互项、两侧与带宽；独立原生参考和迁移重跑 | 预定阈值零、带宽 0.5、均匀权重、局部线性；没有数据驱动带宽或偏差校正 |
| 非线性 | `logit binary x if included==1`，N=400，观测信息矩阵/z | 零一结果、收敛、满秩、正态推断；独立原生参考；斜率 ln(3) 与信息矩阵标准误另核对 | 返回对数优势系数，无 t 自由度；未验证边际效应、其他非线性命令 |
| 非系数 | `summarize x if included==1`，N=400 | 即时 r() 快照与 double CSV、N/mean/sd/min/max；独立 Stata 和标准库；表格/证据/导出/搬迁重跑 | 单变量无权重描述统计；sd 是样本标准差，不是均值标准误 |

对应命令、版本或本机随附文件版本头与 hash、规范化数值、确切设计/attempt/
result/validation、输入和代码 hash 见：

- [方法真实证据](evidence/stage7_methods.json)
- [AJR 首版证据](evidence/stage7_ajr.json)
- [本机健康及能力实测](evidence/stage7_doctor.json)
- [自编数值参考](../examples/methods/stata18_reference.json)与[合成来源说明](../examples/methods/README.md)
- 安装包内 `econ_agent/method_validation.json`，记录数值验证范围及精确指纹。

规范化使用可选 `regression`（family/outcome/regressors/sample_expression/
covariance/did/rdd）、`summary`（variable/sample_expression）或既有 `iv`，
一次仅一种声明。DiD、RDD 结构变量须在输入中准备好并纳入确认设计。
规范化主体接受声明的单个估计命令；自定义预处理、扩展命令或其他返回形态
可通用执行和归档，尚无匹配规则的结果不强制解释成系数。
所有计算仍采用 Stata，Python 在开发脚本中生成夹具、核对参考和组织产物，
不是用户分析后端。

本机 `rdrobust`、reghdfe、csdid、synth 的可用性以最新健康记录为准，
`rdrobust` 当前不可用，未安装或下载。其他本机可用命令（如 xtreg、didregress、
probit、poisson、qreg、时间序列与生存模型）仍标为未验证；一个代表案例通过
不表示这些命令或整个家族通过。IV 第一阶段诊断、事件研究/效应路径、
边际效应、通用矩阵和表格已有结果契约，但本轮未完成各自真实映射验收。

错误 RDD 阈值的真实负例证明：估计执行成功且可提取，并不等于结果可以选用。
程序检查结构和数值不能证明阈值连续性、平行趋势或其他识别假设。

实现依据为本机 Stata 18 帮助及真实返回；对应官方说明：
[regress](https://www.stata.com/manuals/rregress.pdf)、
[logit](https://www.stata.com/manuals/rlogit.pdf)、
[summarize](https://www.stata.com/manuals/rsummarize.pdf)、
[e(b)/e(V)](https://www.stata.com/support/faqs/statistics/variance-covariance-matrix/)。
在线手册不替代本机具体构建的实测，不涉及 Stata 更新下载。
