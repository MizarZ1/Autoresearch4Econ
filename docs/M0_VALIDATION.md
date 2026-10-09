# M0 / 开发阶段 0 验证记录

核验日期：2026-10-09（Asia/Shanghai）。M0 的本地工程基线、案例准备和真实
环境实测已完成。远端 CI 与独立审查仍待完成，PR 保持草稿，不合并。
这不是 M1 首版研究闭环的整体验收。

| 条件 | 当前结果 | 证据 |
| --- | --- | --- |
| Python 3.11+ | 通过，项目使用 3.11.15 | `.python-version`、`uv.lock`；未修改系统 Python |
| Stata 18 许可与最小计算 | 通过，`r(mean)=2.5`，`c(MP)=1` | MCP 脱敏证据；许可日志仅保留本机 |
| 独立 AJR 参考 | 通过 | `examples/demo/reference/independent_stata18.json`；基准及纬度规格、OLS/IV/第一阶段；`ivreg` 与 `ivregress, small` 的系数、SE、df 对齐 |
| 数据及来源 | 通过 | 压缩包、数据 SHA256 与计划一致；163×10，主样本 64；原作者文件留在忽略目录 |
| 模型最小请求与动作 | 通过 | `docs/evidence/model_probe.json`，3 次真实 DeepSeek 请求；禁用 SDK 自动重试 |
| 模型并发 | 通过 | 两项独立 API 请求区间重叠；共已知 180 tokens；不是后续研究审阅演示 |
| MCP 发现、计算、结果与帮助 | 通过已冻结路径 | `mcp-stata 3.3.0`、SDK 1.30.0；保存 `.do` 文件、`processors 1`、专用会话、更新后的本地 Stata 副本 |
| MCP 取消与恢复 | 通过回收会话方案 | 真实计算开始后取消，停止专用会话，验证 PID 已不存在；新会话计算成功 |
| 工程基线与离线检查 | 通过 | 清洁虚拟环境按锁文件安装；统一 gate；13 项测试通过 |
| Gitea Git 访问 | 通过 | 原仓库为空，默认 `main`；基线已推送，功能分支 `feat/cli-agent` |
| Gitea Actions / runner | 已核对，运行待完成 | 全局 `lab404Runner` 标签匹配，状态 offline；仓库 runner 数 0 |
| PR / 独立审查 | 草稿 PR 已创建，独立审查待完成 | 作者自检不等于独立 review 或 approval |

脱敏摘要在 `docs/evidence/m0_environment.json` 和 `docs/evidence/stata_probe.json`。
完整 raw 日志、原生 crash report、许可和二进制均不推送。

## 已验证的 Stata 路径及限制

本机为 macOS 27.0.1 arm64。原安装 `/Applications/Stata` 的应用构建为
18.0.120；独立 PyStata 参考运行可完成，MCP 多行内联 IV 运行曾多次原生退出。
系统 crash report 指向 `libstata-mp.dylib`，并报告 LIBMALLOC 内存错误。
不能仅凭该栈判定操作系统、Stata 或服务的确切根因。

已从[官方 Stata 18 更新页](https://www.stata.com/support/updates/stata18.html)
下载 28Jul2026 Mac 更新包，在项目忽略目录里的独立副本更新应用、ado 与
utilities，保留有效本地许可。副本构建为 18.0.185；原安装未改动。
更新包 SHA256 为 `c07d3fadb52bbf1721f879eac02fe3d93e06f0e624da8be9a9431c930c70073c`。
运行使用的库 hash 进入 MCP 证据；不分发厂商文件。

更新后，多行内联 IV 仍出现过原生崩溃；`.do` 文件在未限制 processors 时
也有一次失败，不能将“更新”或 `.do` 文件单独当作修复。
本机已验证的路径为 `stata_run(code=<保存的 do-file>, is_file=true)`，
脚本执行 `set processors 1`，每项分析使用专用新会话。连续完整探测见
脱敏证据；这只是已测试的兼容配置，不是原生内存错误的根因证明。基准 IV 的
系数 `0.9442793851547995`、SE `0.1565254573295791`、N=64，与独立参考吻合。
独立参考也在更新副本重新运行，数值与原安装一致。

Stata 18 本地 SFI 不提供服务尝试调用的 `breakIn`。`stata_control cancel`
返回 `Operation cancelled` 不足以证明底层执行停止。探测先验证无限计算
的开始标记和 running 状态，再取消任务、停止会话并核对 worker 退出，最后
启动新会话计算。后续执行器必须沿用这一行为，并禁止接收取消后的迟到结果。
本阶段没有实现生产执行器，也不保证任意内联代码或所有统计方法可运行。

## 已处理的工程兼容问题

1. MCP SDK 2.x 移除了该服务使用的 FastMCP 接口；依赖限定并锁定 1.x。
2. PyStata 依赖固定 NumPy 1.26.4、pandas 2.x，实际版本均进入锁文件。
3. 原生 Stata 提示符可能污染 stdio。项目 POSIX 启动器将协议 stdout 与
   原生输出分开，启动原样第三方服务；FD 回归测试及真实返回均通过。
4. 独立参考保留作者旧代码原件，项目自编脚本核对 `ivreg` 和现代命令，
   double 保存结果，固定推断口径及容差。参考值不注入模型任务材料。

## 远端状态

[草稿 PR #1](http://10.13.66.18:3000/Kkkkaiyang/lab404test202610-Agent-AutoResearch4Econ/pulls/1)
为 `feat/cli-agent` → `main`。`main` 禁止直接推送及强推；runner 离线时，
未将尚未验证的 status context 设为必需检查。
[已检查的 PR CI](http://10.13.66.18:3000/Kkkkaiyang/lab404test202610-Agent-AutoResearch4Econ/actions/runs/766)
为 queued，不能记作通过；新提交的 CI 状态单独写入 JSON 摘要。
独立审查尚未执行，无平台 approval。桌面附件工具拒绝该 Gitea PR URL，
文档保留普通链接。

## 复核命令

从项目根目录运行，本机先显式选择已验证的副本：

```sh
export ECON_AGENT_STATA_HOME="$PWD/local_reference/stata18"
export STATA_PATH="$ECON_AGENT_STATA_HOME/StataMP.app/Contents/MacOS/stata-mp"
uv sync --locked --extra stata --group dev
uv run --locked --extra stata --group dev python scripts/check.py
uv run --locked --extra stata --group dev python scripts/fetch_ajr.py
uv run --locked --extra stata --group dev python scripts/reference_ajr.py
uv run --locked --extra stata --group dev python scripts/probe_stata.py
# 调用真实模型；已完成验证后不必因无关文档修改重复调用
uv run --locked --extra stata --group dev python scripts/probe_model.py
```

其他机器需自行提供有许可的兼容 Stata，并设置两个路径变量。
`start`、研究循环、SQLite、人工恢复及论文输出仍属于 M1 后续阶段。
