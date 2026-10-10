# 阶段 2 草稿 PR

[PR #3](http://10.13.66.18:3000/Kkkkaiyang/lab404test202610-Agent-AutoResearch4Econ/pulls/3)

分支：`feat/stage2-stata` → `main`。保持草稿，不自动合并。
阶段 0 的 PR #1 和阶段 1 的 PR #2 已合并，本 PR 只包含阶段 2 的增量。
作者已完成本地差异自检，独立审阅待完成。研究者已授权跳过离线的
`lab404Runner`，远端 CI 不计为通过。

本阶段接通生产 Stata 后端：通用 do-file 执行、数据检查、方法帮助与能力查询，
关联工具调用、分析尝试、实际代码、原始输出、日志、结果及核验。数据采用独立
备份、不可变快照和每次尝试的工作副本；超时及取消必须确认专用工作进程退出。
重试保留旧尝试，重新提取创建新结果和核验记录，不重跑统计命令。

AJR 基准 IV 及加入纬度控制的 IV 均通过真实 Stata 与独立参考的具名数值核对。
真实缺失变量错误、修复重试、通用命令、重新提取、CPU 循环超时与取消、
取消后的新会话和恢复读取均通过；原始数据 hash 未变。
使用本机既有 Stata，未下载官方更新包，未调用模型 API。
详细记录见 [阶段 2 验收](STAGE2_VALIDATION.md) 和
[脱敏真实证据](evidence/stage2_stata.json)。

按锁文件离线创建干净环境，格式、静态检查、严格类型检查、107 项测试及
安装入口检查全部通过，无新增依赖。统一检查命令为：

```sh
UV_PROJECT_ENVIRONMENT=tmp/stage2-clean-env uv sync --locked --offline --extra stata --group dev
tmp/stage2-clean-env/bin/python scripts/check.py
```

执行接口明确接收 `StataProgram`、返回 `AnalysisOutcome`，继续使用同一研究结果
类型；数据库仍为九表 v1。当前具名规范化与数值核验覆盖本阶段 IV 规格，
其他合法命令保留原始输出及实际提取状态。模型研究循环、完整研究 CLI、
人工介入和论文导出属于后续阶段。

Codex 桌面 PR 附件工具不支持该 Gitea URL；已尝试关联，使用上面的直接链接审阅。
