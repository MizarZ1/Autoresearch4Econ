# M0 / 开发阶段 0 验证记录

核验日期：2026-10-09（Asia/Shanghai）。本记录区分工程基线、真实环境、
远端 CI 和独立审查。目前 Stata MCP 连续执行存在原生崩溃，不能将 M0
整体标为通过；已完成事项可支持后续不依赖真实工具的公共模块开发。

| 条件 | 当前结果 | 证据 |
| --- | --- | --- |
| Python 3.11+ | 通过，项目使用 3.11.15 | `.python-version`、`uv.lock`；未修改系统 Python |
| Stata 18 许可与最小计算 | 通过，`r(mean)=2.5`，`c(MP)=1` | 本地 `local_reference/m0/direct_probe.log`；不上传许可日志 |
| 独立 AJR 参考 | 通过 | `examples/demo/reference/independent_stata18.json`；`ivreg` 与 `ivregress, small` 系数、SE、df 对齐 |
| 数据及来源 | 通过 | 压缩包、数据 SHA256 与计划一致；163×10，主样本 64；原作者文件留在忽略目录 |
| 模型最小请求与动作 | 通过 | `docs/evidence/model_probe.json`，3 次真实请求；禁用 SDK 自动重试 |
| 模型并发 | 通过 | 两项独立 API 请求区间重叠；共已知 180 tokens；这不是 M1 研究审阅演示 |
| MCP 发现、简单结果与 IV 观察 | 部分通过 | `mcp-stata 3.3.0`、SDK 1.30.0；简单 r() 和一次真实 IV e(b)/e(V) 可读取 |
| MCP 稳定执行与取消 | 待解决 | Stata 原生库出现内存错误，取消 task 不足以证明 Stata 已停止 |
| 工程基线与离线检查 | 通过 | 清洁虚拟环境按锁文件安装；统一 gate；13 项测试通过 |
| Gitea Git 访问 | 通过 | 原仓库为空，默认 `main`；基线已推送，功能分支 `feat/cli-agent` |
| Gitea Actions / runner | 已核对，运行待完成 | 全局 `lab404Runner` 注册标签匹配，但状态 offline；仓库 runner 数 0 |
| PR / 独立审查 | 待完成 | 作者自检不等于独立 approval；交付草稿 PR 后单独记录 |

JSON 摘要在 `docs/evidence/m0_environment.json`。完整 raw 日志保留本机，不推送。

## 已处理的兼容问题

1. 最新 MCP SDK 2.x 移除了该服务使用的 FastMCP 接口；依赖限定 1.x。
2. PyStata 依赖使用 NumPy 1.26.4、pandas 2.x；实际版本均进入锁文件。
3. 原生 Stata 提示符污染 stdio，结果可能混入 stderr。项目 POSIX 启动器
   将协议 stdout 与原生输出分开，不修改或复制第三方服务源码；离线 FD
   隔离测试及实际 IV 返回证明该路径可用。
4. 独立参考保留作者旧命令，仅在自编参考脚本中采用 `ivregress 2sls, small`；
   不修改作者原件。结果以 double 保存，样本、SE、df 和容差显式固定。

## 尚未解决的运行问题

本机是 macOS 27.0.1，Stata 安装元数据是 18.0.120（2023 构建）。多次 MCP
运行出现 Python worker 原生退出；系统 crash report 指向 `libstata-mp.dylib`
解析代码，并报告 LIBMALLOC 内存错误。不能仅凭该栈断言操作系统或第三方
服务是哪一个的根因。普通独立 PyStata 参考脚本能够完成计算。

`stata_control cancel` 曾返回 `Operation cancelled`，但随后 worker 不响应；
Stata 18 的本地 SFI 没有服务尝试调用的 `breakIn`。后端必须确认底层执行
终止，必要时停止并回收专用会话；当前完整探测尚未通过这一项。

正在项目忽略目录里的独立 Stata 副本上验证[官方 Stata 18 更新](https://www.stata.com/support/updates/stata18.html)，
不改动 `/Applications/Stata`。更新效果尚未验证，不作为已解决记录。

## 复核命令

```sh
uv sync --locked --extra stata --group dev
uv run --locked --extra stata --group dev python scripts/check.py
uv run --locked --extra stata --group dev python scripts/fetch_ajr.py
uv run --locked --extra stata --group dev python scripts/reference_ajr.py
uv run --locked --extra stata --group dev python scripts/probe_stata.py
# 会调用真实模型；已完成验证后不必因无关文档修改重复调用
uv run --locked --extra stata --group dev python scripts/probe_model.py
```

参考 `.do` 和精确参考值是验收材料，不在模型任务的材料列表中。
`start`、研究循环、SQLite、人工恢复及论文输出仍属于 M1 后续阶段。
