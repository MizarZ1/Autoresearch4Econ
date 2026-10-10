# econ-agent

通过 Stata MCP 开展实证经济研究的中文 Python 命令行项目。当前已完成 M1 的
阶段 0（环境与工程基线）和阶段 1（对象、接口及状态存储）。首版后端固定为
`stata`，研究循环、生产 Stata 执行器、CLI 人工交互、恢复命令和论文导出在后续
阶段实现。阶段 1 的存储 API 已可使用，`start`、`resume`、`status`、`export`
这些 CLI 命令尚未开放。

文档、代码说明、终端提示、模型交互与新增提交说明统一使用中文。JSON 字段名、
动作名、状态枚举、命令及软件包名称保留协议规定的拼写。

## 安装与检查

使用 Python 3.11+ 和提交的 `uv.lock`。本机系统 Python 3.9 不符合要求：

```sh
uv sync --locked --extra stata --group dev
uv run --locked --extra stata --group dev econ-agent --version
uv run --locked --extra stata --group dev econ-agent doctor --backend stata --json
uv run --locked --extra stata --group dev python scripts/check.py
```

`doctor` 只检查配置，不发送模型请求，也不启动 Stata。退出码 0 表示所需配置和
安装已找到；真实执行需要显式探测。缺少配置返回 1，参数或配置无效返回 2。
首版仅支持 `--backend stata`。`.env.example` 列出配置变量，程序目前不会自动
读取 `.env`；密钥继续使用本机环境变量，禁止提交、写入任务文件或数据库。

统一本地检查依次运行格式检查、Ruff、严格 mypy、离线单元与契约测试以及安装
入口。研究者已在 2026-10-10 授权暂时跳过离线 runner 的远端 CI，当前开发不
等待该服务。工作流配置保留，远端执行、独立审阅与合并状态分别记录。

## 阶段 1 的存储 API

`ResearchTask.from_file` 按任务文件所在目录解析相对路径。任务保存后，后端和
输入信息固定。每个 `runs/<run_id>/` 包含一个 SQLite v1 数据库及有 hash 的文件
产物；数据库恰有计划规定的九张表。

以下示例只创建状态和输入清单，不运行统计分析或模型：

```python
from pathlib import Path

from econ_agent.schemas import ResearchTask, RunStatus
from econ_agent.state import RunStore

任务 = ResearchTask.from_file(Path("examples/demo/task.json"))
with RunStore.create(Path("runs"), 任务) as 存储:
    存储.transition_run(RunStatus.RUNNING)
    研究目录 = 存储.directory
    print(存储.load_state().model_dump_json(indent=2))

# 只读打开不初始化模型、MCP 或 Stata，也不执行数据库迁移。
with RunStore.open(研究目录, writable=False) as 存储:
    print(存储.load_state().model_dump_json(indent=2))
```

设计通过 `create_design` 保存为不可变版本，`request_human` 持久化问题并进入
等待状态。`apply_answer` 必须匹配请求及具体设计 hash；同一答复重复应用是幂等
操作，过期答复不能确认新设计。设计变更保留历史，不覆盖旧规格。

一次统计分析对应一个 experiment，每次实际重跑对应新的 attempt 和工具调用。
登记和归档使用短事务，网络与 Stata 执行在事务之外。代码、原始返回、提取结果
和核验分别保存；选中结果及报告读取必须重新检查当前设计、执行与取消状态、
确切版本的核验和文件 hash。成功命令本身不能成为论文数值来源。

归档先写恢复意图与同目录临时文件，再原子发布且拒绝覆盖，最后登记数据库。
文件与数据库不具有跨系统原子性。恢复时可补登记证据匹配的文件索引，但不会仅
凭文件存在将任务标记为成功。未登记文件、缺失或改变的证据、未知调用和遗留
尝试均保留并报告；有未确认停止的旧任务时禁止重试。

写入者持有 POSIX 进程级排他锁，进程退出自动释放；只读查询可并存。当前支持
macOS/Linux，不宣称支持 Windows。对象格式、数据库格式、应用版本和设计版本
分别管理；遇到未来格式明确拒绝读写，不删库或隐式重建。

详见[阶段 1 验收](docs/STAGE1_VALIDATION.md)和[架构说明](docs/ARCHITECTURE.md)。

## 本机 Stata 与显式探测

优先复用已下载、配置好的本地 Stata。未经研究者明确要求，不从官网下载 Stata
更新包。本机已验证的副本在忽略目录 `local_reference/stata18/`，构建为 18.0.185：

```sh
export ECON_AGENT_STATA_HOME="$PWD/local_reference/stata18"
export STATA_PATH="$ECON_AGENT_STATA_HOME/StataMP.app/Contents/MacOS/stata-mp"
```

这些是本机配置，不随仓库分发。其他机器自行提供有许可的兼容安装并设置两个
变量。原安装 `/Applications/Stata` 保持 18.0.120。

```sh
uv run --locked --extra stata --group dev python scripts/fetch_ajr.py
uv run --locked --extra stata --group dev python scripts/reference_ajr.py
uv run --locked --extra stata --group dev python scripts/probe_stata.py
uv run --locked --extra stata --group dev python scripts/probe_model.py
```

下载 AJR 或调用模型需要网络；普通离线检查不执行这些命令。模型探测发送三次
小型中文请求，记录动作校验、模型标识、原始 usage 和实际并发区间。Stata
子进程不继承模型凭证，许可提示及原始日志保留在忽略目录，禁止推送。

当前 MCP 已验证的路径为保存 `.do` 文件、`set processors 1` 和专用会话。
内联多行 IV 或未限制 processors 的运行曾出现原生崩溃。取消任务后还要停止
专用会话并确认工作进程退出，再创建新会话。后续生产执行器需沿用这些约定。

AJR 参考脚本由开发者独立编写，在新进程中运行，不使用模型或 MCP。
`ivreg` 与 `ivregress 2sls, small` 的系数、标准误及自由度已核对；参考采用同方差
常规标准误和有限样本修正。任务只读取数据、项目字典和背景，参考值及作者原
代码保留给开发者和验收器，不注入模型任务材料。

## 依赖与证据

- `openai`：基础兼容接口客户端，显式探测禁用 SDK 隐式重试。
- `pydantic`：结构和动作校验，研究控制流由项目代码管理。
- `sqlite3`：标准库单 run 存储，九表、事务及必要索引。
- `mcp`：协议 SDK，为固定服务限定 1.x。
- `mcp-stata 3.3.0`：独立服务，来源为 [tmonk/mcp-stata](https://github.com/tmonk/mcp-stata/tree/v3.3.0)，许可 AGPL-3.0-or-later。仅接入通用执行和生命周期工具，不复制服务源码或委托其研究代理完成项目流程。
- NumPy 1.26.4、pandas 2.x：PyStata/MCP 连接依赖，不构成 M2 统计后端。
- Ruff、mypy、pytest、pytest-asyncio：开发检查；版本由锁文件固定。

POSIX 启动器 `econ_agent.stata_transport` 分离原生 stdout 和 MCP 协议，启动原样
第三方服务。[M0 验收记录](docs/M0_VALIDATION.md)、[AJR 验收卡](examples/demo/reference/acceptance.json)、
[独立参考](examples/demo/reference/independent_stata18.json)和[开发计划](docs/DEVELOPMENT_PLAN.md)
分别记录本地验证、真实环境及远端状态。交付通过草稿 PR 审阅，默认不自动合并。
