# 阶段 7 与 M1 首版验收

2026-10-10，基线为已合并阶段六 PR #7 的主线 80c343b，功能分支
`feat/stage7-methods`。代码、离线检查、真实执行和首版工程交付已完成；
独立研究者审阅与独立 PR 审查尚未完成，远端 CI 按用户授权跳过，PR 不自动合并。
M2 未开始，没有新增依赖、数据库迁移或分析后端。

## 方法与非系数输出

本机已有 Stata 18.0.185、固定 mcp-stata 3.3.0；没有下载官方更新包。
AJR 用真实作者数据；其余优先组使用明确标注的 400 行自编合成数据、种子 2001。
逐命令范围见[方法清单](METHODS.md)，数值与文件指纹见
[真实方法证据](evidence/stage7_methods.json)。

OLS、两组两期重复截面 DiD、锐断点局部线性 RDD、Logit 和描述统计实际执行，
分别与独立原生 Stata 参考核对。各正例的包搬迁后在新原生会话重跑，
系数/se/p/95% CI 或标量按绝对 1e-7、相对 1e-6 容差，N/适用 df 精确匹配。
Logit 另核对闭式斜率 ln(3) 及信息矩阵标准误，DiD 另核对四格均值之差，
描述统计另核对标准库均值和样本标准差。没有将这些开发核对作为 Python 后端。

真实 RDD 负例将声明阈值故意设为 0.25（实际分配阈值为零）：估计成功，
但结构核验失败，选用被拒绝。它不进入当前数值结论。
本机 rdrobust 不可用；已验证的是预定带宽、均匀权重的局部线性 `regress`，
没有声称自动带宽、偏差校正、操纵检验或密度检验完成。

真实联调修复：

- Stata `postfile` 的 double 必须逐列声明，避免 sd/区间默认 float 精度损失。
- `regress` 常规 e(vce) 实际为 `ols`，不同于 IV 的 `unadjusted`。
- MCP 异步轮询后描述统计 r() 为空：主 do-file 即时保存 r_snapshot.json，
  原 MCP 返回仍独立保存；快照、CSV、样本、主体与提取文件共同绑定 hash。
- IV 控制放在工具括号前后均是合法等价命令。核验仍检查实际变量、工具、
  协方差、样本和整个执行主体，禁止附加命令或偷换条件。
- 必需核验失败作为循环错误反馈，避免计算成功后反复尝试不合格结果。
- 新的明确接受答复可恢复原设计的有界尝试，立即记录来源请求的检查点；
  普通 resume 不绕过错误上限，总预算、成功分析和单尝试重试上限不变。

## 真实模型加 Stata 的 AJR 首版演示

运行 `run_1af64417376540449cc9a9d17fa497a4`，最终设计 v4、状态 COMPLETED。
[脱敏证据](evidence/stage7_ajr.json)记录数值、并发区间、核验来源和代码指纹。

- 初始 baseco==1 基准 N=64，与冻结阶段零独立参考核对；原作者数据 hash 不变。
- 沿用用户真实原话“你自行选择最优”，程序选 `baseco==1 & lat_abst>0` 作
  最小工程变化，不声称统计最优；样本 N=63。
- 当前基准 avexpr 系数 0.9681260376258296、se 0.16379672065764586、df=61；
  当前纬度补充 avexpr 系数 0.997403173143447、se 0.21887683675603548、df=60。
  不显著的 lat_abst 系数及 p=0.7750251325378803 也保留，没有显著性筛选。
- 基准 `result_b84b62134e6541ccafd2514cb0ce5609`；补充
  `result_4d97e9009ad34092b604dffbddd0b794`，均在 v4，补充明确引用新样本基准。
- 模型两次遗漏依据 ID，被参数检查拒绝；随后发现控制位置的核验问题。
  四次实际补充计算保留，不假装只运行一次。修正规则后只重新提取最后一项
  既有原始输出，生成新 result/validation（stata-iv-2），没有重跑估计或增加调用。
  其余旧核验失败产物保持历史状态，没有自动转正。
- 模型实际进行了两批各两项并发审阅，四个请求均 SUCCEEDED/VALID，区间重叠
  见 review_batches。第一次 finish 使用不合格证据被拒绝，随后补充审阅并正确结束。
- 最终模型请求 27、工具调用 19，传输失败零、解析失败一、工具参数失败五、
  无重试或未终结调用；已知总 tokens=337326，缺失总用量零。
  requested=deepseek-chat、returned=deepseek-flash；未报告未知 reasoning tokens 为零。
- 论文 `report_2d76a19b735b4ffd944b1363c730e4cd` 为 complete。
  原样本与旧结果在历史披露，所有当前数字可追溯到确切 artifact/hash。
  弱工具、机制与排除限制诊断没有执行，论文明确写为未执行。

原始授权、程序选择、脚本化 /accept、参数纠正和规范化修复分别保存。
程序生成的新设计由开发脚本确认，不能计作研究者逐字提出规则并独立审阅。
这项人工独立验收及论文解释/正式书目审阅仍待完成；工程演示没有替代它们。

包移动到新目录后，用全新原生会话只执行包内 reproduce.do，全部当前估计及
样本检查重跑一致。错误模型 URL、缺失密钥和错误 Stata 路径不影响已有记录导出。
全新非 editable 核心依赖安装只有 17 包，无 MCP/PyStata/psutil，导出也通过，
调用计数不变。创建运行时 Git/dirty 记录保留，最终代码指纹不倒填更早调用版本。

## 首版逐项检查

| 要求 | 实际证据与状态 |
| --- | --- |
| 可安装、锁定依赖、CLI | uv.lock；全新 stage7-clean-env；统一 scripts/check.py；非 editable 核心安装与 wheel |
| 中文配置、doctor | 无密钥模板 .env.example；默认配置检查离线；`doctor --probe-stata --json` 真查询，模型请求零，见 stage7_doctor.json |
| AJR 来源、数据 hash、验收卡 | examples/demo/sources.json、reference/acceptance.json、独立参考；README 获取命令；数据留忽略目录 |
| 自有循环、动作与调度 | harness.py；test_harness；阶段三及本次真实模型 trace |
| 九表、对象/数据库版本、关联事务 | state.py/migrations.py/schemas.py；test_state、test_schemas；v1，无迁移 |
| 不可变产物、hash、崩溃与迟到保护 | test_state、test_stata_backend、阶段二真实生命周期记录 |
| 调用、重试、未知用量、来源 | model_calls/tool_calls、calls.jsonl/usage.json、模型/工具各自测试；未知保持 null |
| 真实 Stata MCP 与多形态结果 | backend/methods；真实方法脚本与 AJR；系数和标量实际映射，其余真实映射未验证 |
| IV 与 RDD 等优先组 | METHODS.md；stage7_methods.json、stage7_ajr.json；原生参考及迁移重跑 |
| 多轮、结果触发补充 | 本次 27 次模型请求与 6 项实际分析历史；补充 basis 指向新样本基准 |
| 真实并发 | 当前 run 两批实际 review_batches；阶段四屏障/局部失败测试 |
| 人工修改、等待退出、恢复 | 用户授权原文与脚本版本确认、本次 64→63；阶段五 CLI/硬中断记录；新的显式恢复请求 ID |
| 不重复成功分析的恢复 | test_session_cli、test_harness 显式恢复；规范化修复只读原产物不重新估计；本次历史重复计算单列披露 |
| 中文论文、表格与证据、历史 | 当前 complete 报告、精确事实引用、analysis_history.csv、code/validation；正式书目及解释独立审阅待补 |
| 复现、随机种子、环境 | 包内原主体/提取/输入及 hash、2001、新进程迁移重跑；本机命令来源文件 hash |
| 无 Stata 的测试/status/export | 普通检查不启动服务；非 editable 核心环境无 MCP/psutil 导出通过；status/export 只读 |
| 原件保护、密钥与排除规则 | 原件/备份/快照核对，子进程环境白名单；.gitignore；推送审计；不称严格沙箱 |
| 安装/格式/静态/类型/离线测试 | 全新环境统一检查通过：Ruff、严格 mypy 56 文件、256 tests、CLI version；新增 43 tests |
| 文档、贡献和兼容 | README、METHODS、ARCHITECTURE、CHANGELOG、CONTRIBUTING、阶段清单；新增提取字段默认 null |
| PR、独立审阅、CI、合并 | 草稿 PR 交付；作者自检完成；独立 PR/研究者审阅待完成；runner 授权跳过；不自动合并 |
| 后端与后续范围 | M1 只支持 Stata，小接口隔离；M2 Python 未开始，须另行授权 |

## 可重复命令

```sh
uv run --locked --extra stata --group dev python scripts/validate_stage7.py
uv run --locked --extra stata --group dev python scripts/validate_stage6.py --evidence-prefix stage7_ajr
uv run --locked --extra stata --group dev econ-agent doctor --backend stata --probe-stata --json
python scripts/check.py
```

已有本次 run 的最终导出与核对（不重新研究、不请求模型）：

```sh
python scripts/validate_stage6.py --evidence-prefix stage7_ajr \
  --run-id run_1af64417376540449cc9a9d17fa497a4 \
  --directory local_reference/stage7_ajr/20261010T115739012331 \
  --core-python tmp/stage7-wheel-core/bin/python
```

命令缺少实际环境时报告失败，不能当作通过。普通 CI 不需要真实数据、许可或 API。
lab404Runner 按原授权跳过，不部署新服务，不把远端 pending 当作通过。
独立审查、人工科学审阅及合并不由工程自检或模型结果审阅代替。
