# demoV0.1-test 候选验收摘要

2026-10-10，本次候选以已合并阶段 7 的 main 为基线。运行后端始终为 Stata；没有新增 Python 分析后端、Docker 或服务部署。

## 离线工程与真实统计

统一入口 `scripts/check.py` 通过格式、Ruff、严格 mypy、314 项离线测试与版本入口。测试使用替身的部分只证明契约和控制流；真实统计另列。

本机既有 Stata/MP 18.0.185、`mcp-stata==3.3.0` 完成 IV 基准和纬度控制与独立参考对照（N=64），同时验证错误后修复、重新提取、实际 CPU 循环超时、主动取消和取消后新会话。取消与超时均核对工作进程退出。原作者数据 hash 未改变，仅上传数值摘要：[IV 证据](evidence/demo_v01_iv.json)。

OLS、两组两期重复截面 DiD、预定带宽锐断点 RDD、二元 Logit 和单变量描述统计使用自编合成数据，与独立原生 Stata 参考核对，并在迁移复现包中重跑；错误阈值 RDD 不能选用：[方法证据](evidence/demo_v01_methods.json)。这些数值验收采用固定设计和脚本化确认。

## 实际安装附件

在仓库外新建 Python 3.11 环境，按导出的精确依赖与 hash 安装实际 wheel，确认非 editable、版本入口、`method_validation.json` 和无凭证 status/export 可用。源码包在独立目录重建的 wheel 所有成员字节与发布 wheel 一致。安装环境实际通过 MCP 执行合成 OLS（N=399）与描述统计（N=400），与独立数值参考一致。

初次缺少 numpy/pandas 缓存，按锁定版本和 hash 补齐后重验；没有下载 Stata。验收中发现并修复了脚本自身的预算声明不一致和私钥扫描误报。最终确切附件 hash 与结果见 Gitea 附件 `installation-validation.json` 和 `SHA256SUMS`，不上传本地虚拟环境、数据库与原始日志。

## 已安装 CLI 的真实模型闭环

使用随包 `examples/quickstart/task.json`，在独立安装环境启动现有模型账号和本机 Stata。工程任务预设 OLS 与相关描述统计的范围，设计确认由开发脚本完成，不冒充独立研究者决定。

- run `run_c66bee969c9b468faeac8921cf05a511` 完成两轮实际分析，状态 `COMPLETED`。
- 主体为 `regress ylinear x if included==1`（N=399）与 `summarize x if included==1`（N=400）；补充分析引用已观察的基准结果。
- 两项审阅模型请求均有效，实际在途区间重叠 4.70552 秒。
- 共 9 次模型请求、8 次工具调用；没有传输或解析失败，1 次超出两轮预算的新分析被程序拒绝，模型随后基于已有结果结束；没有未终结调用；已知总 tokens 为 99669。
- 论文 `report_c6ebab6217c44f2bb90bc2f452d23f07` 为 `complete`，表格、事实与结果证据绑定，导出 manifest 中全部文件 hash 核对通过。

脱敏结果、调用摘要与代码指纹见 [模型闭环证据](evidence/demo_v01_loop.json)。该记录对应与最终候选一致的应用代码及 wheel 内容；最终源码包另按准确 tag commit 重建并验收。没有以合成工程演示证明任意现实研究的有效性，完整论文仍需研究者审阅识别和语义。

## 交付状态

源码与包采用白名单和 Git 跟踪文件双重核对；没有题目、密钥、原作者数据与代码、Stata 软件、许可激活文件、运行数据库或原始日志。包内只提供项目自编数据和项目分析/参考代码。

最终独立研究者与 PR 审阅待完成；lab404Runner 离线，远端 CI 按研究者授权暂跳过；候选 PR 不自动合并。发布为 `demoV0.1-test` 测试预发布，Python 包版本 `0.1.0rc1`。
