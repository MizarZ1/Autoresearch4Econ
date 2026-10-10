# econ-agent

通过 Stata MCP 开展实证经济研究的中文 Python 命令行项目。当前完成 M1 阶段
0–6：环境、持久化对象、真实执行、自有研究循环、有界审阅、CLI 人工恢复及
有证据的论文与复现包。首版后端固定为 `stata`，开放 `start`、`resume`、`status`、
`report` 与 `export`。阶段 7 的方法扩展及首版交付尚未开始。

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

## 阶段 2 的真实执行闭环

`StataBackend` 实现能力、数据检查、帮助、完整分析及取消接口。`run_analysis`
接收不可变规格、已登记的 attempt 和 `StataProgram`，返回 `AnalysisOutcome`，
内含统一的 ExperimentResult、结构化错误及核验产物引用。执行前归档参数和
代码，后台任务 ID 绑定具体服务及专用会话；内部轮询不新增业务调用。

每份原数据建立独立本地备份与不可变 run 快照，每个 attempt 再复制工作文件。
主体代码默认通过 `$ECON_INPUT` 与 `$ECON_OUTPUT` 访问本次工作副本和输出目录。
所有尝试使用保存的 do-file、单 processor、新专用会话和记录的随机种子。
互斥锁覆盖执行、主返回读取、提取、工作进程停止核对及归档，避免返回值串用。
原件或快照改变时暂停，不自动覆盖用户文件。该措施不构成强文件访问沙箱。

通用 Stata 命令可执行并归档原始返回、日志及输出；未注册规则时不强行生成
规范化结果。已接入 2SLS（small、常规协方差）、具名满秩线性回归、两组两期
重复截面 DiD、锐断点局部线性 RDD、二元 Logit，以及单变量描述统计。
具体规格及验证边界见[方法清单](docs/METHODS.md)。可用性、规范化支持与
数值验证分别显示；`which` 成功不等于已验证。验证记录只对匹配的本机
Stata 构建、库 hash、MCP 版本与映射代码生效。

显式运行阶段 2 验收，不调用模型 API，也不下载 Stata 更新包：

```sh
uv run --locked --extra stata --group dev python scripts/validate_stage2.py
```

脚本使用本机已有 Stata 配置；未设置 STATA_PATH 时优先使用上述已验证的本地
副本。它验证 AJR 基准及纬度规格、失败修复与独立参考数值、重新提取、通用
命令、实际 CPU 循环超时和取消、取消后的新会话及关闭恢复。固定验收方案和
夹具确认不冒充模型自主决策或真实人工修改。数据库与原始日志保留在被忽略的
`runs/`、`backups/`、`tmp/` 和 `local_reference/stage2/`；脱敏证据见
[阶段 2 验收](docs/STAGE2_VALIDATION.md)。

`reextract` 只读取保存的输出，产生新结果和新核验，不连接 MCP 或重跑统计。
失败、未提取、规格不符、已取消或未确认停止的尝试不能混入当前数值。核验
通过表示计算及规格证据吻合，工具外生性和排除限制仍须作为识别假设披露。

## 阶段 3 的自有研究循环

`ResearchLoop` 使用小接口分发模型动作，传入数据摘要、本机方法能力、当前确认
设计、有效结果及最近工具反馈。模型生成主体代码；程序控制参数校验、业务调用、
尝试、核验选择和停止。补充分析必须引用本轮模型请求实际收到的结果 ID。
读取材料仅开放任务的文字快照，设计变更和实质问题保存后进入人工等待，循环
不会替研究者答复。配置阶段 4 审阅器后，工具列表开放 `review_results`。

`PersistentModelClient` 使用基础兼容接口，每次实际发送前登记 `model_calls` 并
归档中文请求，分别记录传输和解析状态、返回模型及原始/已知用量。SDK 隐式重试
关闭，显式重试逐次保存；修正动作后重新决策使用新操作。环境秘密、认证字段及
异常正文不进入调用产物。缓存和推理子项不重复计入总用量，缺失保持 null。

在已配置客户端和后端、持有写入存储的协程内，通过
`ResearchLoop(存储, 模型客户端, Stata后端, 输入快照ID).run()` 推进。未确认设计
仍可提出方案，但不能分析。新分析占用研究轮次；修复同一失败规格只增加尝试和
工具调用，默认最多修复两次；连续两次动作或执行错误暂停。模型请求、业务调用
和运行时间还有独立预算。检查点保存近期反馈与累计活动耗时，API 重新进入时不
重复初始化已保存的数据检查；遗留未终结调用必须先恢复核对。

`finish_research` 保存带证据引用的结论草稿，并将 run 置为 PAUSED，等待后续论文
输出阶段；预算耗尽同样保留部分结果及明确原因。它不把阶段 3 循环结束记为整项
研究 COMPLETED。草稿解释还需后续审阅，未经核验的数值不得作为结果来源。

显式真实验收会使用现有模型凭证并发送少量真实请求，须在项目根目录运行：

```sh
uv run --locked --extra stata --group dev python -m scripts.validate_stage3
```

该脚本只固定开发验收的设计范围，不提供预设动作列表、主体代码或参考数值给
模型。真实模型先取得基准 IV 结果，再生成引用基准结果的纬度补充 IV，最后保存
草稿；验收器在循环结束后单独核对参考。设计确认是开发夹具，不能算阶段 5 的
真实人工修改。原始请求、返回和 trace 在忽略目录，交付证据见
[阶段 3 验收](docs/STAGE3_VALIDATION.md)。

## 阶段 4 的有界并发审阅

`ResultReviewer` 对同一份当前有效结果并发进行设计一致性审阅和解释/后续分析
审阅。每项只有一次基础模型请求，没有独立研究循环或隐式重试。同一审阅器实例
同时最多两项请求，不同批次串行；每项默认超时 60 秒，还受主循环剩余时间限制。

在已经持有运行存储、配置模型和 Stata 后端的协程内接入：

```python
from econ_agent.harness import ResearchLoop
from econ_agent.review import ResultReviewer

审阅器 = ResultReviewer(存储, 模型客户端, timeout_seconds=60)
循环 = ResearchLoop(存储, 模型客户端, Stata后端, 输入快照ID, reviewer=审阅器)
await 循环.run()
```

独立调用可用 `await 审阅器.review(ReviewArguments(design_id=当前设计ID,
result_artifact_ids=当前结果ID元组))`。存储须处于 RUNNING，结果必须已选用且通过
程序核验。它计一次业务工具调用、两次模型请求，不新增统计尝试。两个请求分别
保存输入结果、父调用、中文请求、原始响应、意见和用量。请求预算不足两项时不
发请求，不自动提升预算；用量缺失保留 null。

传输失败、超时、格式或引用错误独立记录，局部失败批次仍返回成功审阅及每项
错误；取消保留已经完成的输出。审阅期间输入失效则暂停并拒绝作为有效批次。
完整意见反馈给主循环，但不改变程序核验、结果选择或人工决定。

显式验收复用本机阶段 3 最新成功 run 的两项真实结果，追加两项并发 DeepSeek
请求及一次接收审阅反馈的后续决策，保存后暂停；不启动 Stata 或下载更新包。
执行前要求剩余预算足够，已耗尽预算的 run 不能反复调用：

```sh
uv run --locked --extra stata --group dev python -m scripts.validate_stage4
```

后续决策在本脚本中只归档，不分发额外统计或论文输出动作。真实请求区间及
失败留痕见[阶段 4 验收](docs/STAGE4_VALIDATION.md)。CLI 与人工恢复用法见下节。

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
专用会话并确认工作进程退出，再创建新会话。生产执行器沿用这些约定。

AJR 参考脚本由开发者独立编写，在新进程中运行，不使用模型或 MCP。
`ivreg` 与 `ivregress 2sls, small` 的系数、标准误及自由度已核对；参考采用同方差
常规标准误和有限样本修正。任务只读取数据、项目字典和背景，参考值及作者原
代码保留给开发者和验收器，不注入模型任务材料。

## 阶段 5 的 CLI 与人工恢复

在项目根目录、已安装环境中运行，`RUN_ID` 换成 `start` 打印的运行 ID：

```sh
.venv/bin/econ-agent start --task examples/demo/task.json
.venv/bin/econ-agent resume RUN_ID
.venv/bin/econ-agent status RUN_ID --json
```

直接运行 `.venv/bin/econ-agent` 或 `start` 不带 `--task`，会询问数据、研究目标、
材料和变量字典路径。任务相对路径以任务文件目录为基准。`--workspace` 指定
`runs/`、`backups/`、`tmp/`、`output/` 的根目录，恢复及查询使用同一根目录。
后端优先取 `start --backend`，其次任务文件，默认 `stata`；其他后端在创建会话
前拒绝。`resume` 保留已保存的 Stata 后端，不接受后端覆盖。

首次方案展示后输入 `/accept` 才能执行。自然语言或 `/revise <说明>` 由模型
解释为完整新设计，展示变化后再次等待确认；模型不能自行批准。变量表使用
“实际数据列名 → 中文含义”，样本规则使用可执行的 Stata 条件表达式。
`/reject <理由>` 记录拒绝并交回研究循环；`/pause` 保留原问题；`/stop` 停止并
导出现有材料的复现包；没有当前有效论文时标记为部分交付，
保留分析历史，排除完整模型请求／响应、秘密配置、备份与历史报告。

Ctrl+C 先回收在途操作、保存后退出。非交互终端或 `--no-input` 在待答时退出并
保留原问题，不自动确认。也可从打印或 `status --json` 的请求中取 ID 与设计 hash：

```sh
.venv/bin/econ-agent resume RUN_ID --no-input --answer '/accept' \
  --request-id REQUEST_ID --design-sha256 DESIGN_HASH
```

答复绑定请求及设计版本；重复答复不重复推进，旧答复不能解锁新设计。没有待答
问题的暂停会话可用 `resume RUN_ID --feedback '修改意见' --no-input` 补充指令。
正常结束研究循环后的普通 `resume` 只显示保存状态，不重跑已完成分析。
`status` 只读数据库，环境密钥缺失、Stata/MCP 不可用时仍可查询，未知用量保持
`null`。恢复遗留任务时核对专用工作进程的 PID、出生时间及可执行文件；身份
不足或无法确认退出时保持暂停，不向新 MCP 服务查询旧任务 ID。

本阶段真实验证使用既有 DeepSeek 配置及本机 Stata 18.0.185，未下载更新包。
按研究者“你自行选择最优”的授权，为工程验收选择 `baseco==1 & lat_abst>0`，
样本从 64 改为 63，实际代码及独立 Stata 数值对照通过。规则输入和 `/accept`
为开发脚本，与原始授权分别保存；不宣称统计最优或研究者另行审阅确认。
具体失败留痕、额外同规格分析及验收命令见[阶段 5 验收](docs/STAGE5_VALIDATION.md)。

## 阶段 6 的中文论文与复现包

研究循环结束后，程序关闭统计连接，再用预留的一次模型调用写论文。数字由当前
已明确选用、执行/提取/必需核验通过的结果生成，不从原始日志或旧设计挑选。
写作失败、预算用尽、待答问题或证据失效会产生明确标记的部分交付，不计为完成。

```sh
.venv/bin/econ-agent export RUN_ID
.venv/bin/econ-agent export RUN_ID --output /绝对路径/交付目录
.venv/bin/econ-agent report RUN_ID
.venv/bin/econ-agent report RUN_ID --no-model
```

`export` 只读组织已有记录，不生成新论文、不调用模型或重跑统计；没有模型密钥、
Stata/MCP 或模型配置错误时仍可使用。`report` 只写一个论文新版本，使用原运行剩余
预算，不启动 Stata；`--no-model` 保存表格和未完成论文。报告和导出均创建新目录，
不覆盖旧版本。修改设计、选择、源文件或核验后，旧报告不能冒充当前论文。

每份 `output/<run_id>/<export_id>/` 包含：

- `paper.md`：八个中文章节，未执行的分析与未证明的识别前提明确列为局限。
- `tables.md`、`tables.csv`、`tables.json`：全部当前选中结果，包括负值或未显著结果。
- `evidence.json`：每项经验结论、结果 artifact ID/hash、字段路径及表格行/CSV 行位置。
- `analysis_history.csv`：每项实质分析及技术尝试、设计版本、计划/探索类型与排除原因。
- `calls.jsonl`、`usage.json`：逐次模型/工具状态、来源关联、重试、时间及用量缺失。
- `sources/`：已核验输入快照、历史 `.do`、日志、原始返回、规范化结果及核验记录。
- 设计、人工原始答复、事件、输入来源与实际环境记录。
- `reproduce.md`、`reproduce.do`、`rerun/`、`script_manifest.json` 与 `bundle_manifest.json`。

将整个包移到另一目录，在全新本机 Stata 会话进入包根目录后执行 `do reproduce.do`。
入口使用包内输入快照和原主体/提取脚本，输出到 `rerun_output/`；各次原脚本 hash、
派生入口 hash、种子和可迁移限制逐项保存。只重跑当前结果，历史入口保留在清单。
当前已验证的 IV 包固定种子 2001、Stata 18、一个 processor，无更新下载步骤。

未知用量保持 `null` 并计数；缓存及推理子项不再叠加到供应商总量。请求耗时按
UTC 区间并集合计，不将并发请求相加冒充整段研究墙钟时间。没有完整源文件或
可迁移入口时导出为部分交付，历史文件损坏也明确列出，不静默遗漏。

自动检查覆盖来源、当前设计和结果选择、执行/提取/必需核验、文件 hash、表格
数值及经验数字占位符。来源明确的资料年份、变量名和样本条件属于元数据。
程序无法证明每句自然语言的语义、工具外生性、排除限制或解释正确；研究者仍须
抽查关键结论。未提供经过书目核实的文献时只列项目材料并保留正式引用待补项。
模型肯定回答不能替代这些检查或人工审阅。

显式真实验收使用现有 DeepSeek 和本机已配置 Stata：

```sh
.venv/bin/python scripts/validate_stage6.py
```

首个完整案例包含基准后的授权样本修改、当前基准和纬度控制补充、论文与迁移后
原生 Stata 复现。用户原始授权、程序规则和脚本化 `/accept` 分别保存；不将开发
脚本当作研究者独立审阅。见[阶段 6 验收](docs/STAGE6_VALIDATION.md)。

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


## 阶段 7 与 M1 首版交付

优先方法组使用各自适合的数据：AJR 用于真实 IV 闭环，其余代表方法使用
自编合成夹具，均真实运行 Stata 并核对独立数值参考。RDD 使用内置 `regress`
估计预定带宽的锐断点局部线性模型；本机 `rdrobust` 不可用，未下载或冒称验证。
Logit 返回对数优势系数，不能称为概率边际效应。描述统计即时保存 r() 快照，
均值、样本标准差和极值可进入证据表与只读复现包。

```sh
uv run --locked --extra stata --group dev python scripts/validate_stage7.py
uv run --locked --extra stata --group dev python scripts/validate_stage6.py \
  --evidence-prefix stage7_ajr
econ-agent doctor --backend stata --probe-stata --json
python scripts/check.py
```

首个命令不调用模型，第二个使用现有 DeepSeek 配置并保存新的首版 AJR 记录，
不覆盖阶段六历史证据。复用用户原始授权“你自行选择最优”，程序选择最小
工程样本变化；版本确认仍是开发脚本操作，独立研究者审阅状态单列。
脚本按当前请求 ID/hash 确认设计，遇到错误保留记录、预算与成功分析。
普通 resume 不清空连续错误上限；新的明确接受答复可以开启下一段有界尝试，
并记录确切请求，累计预算和同一 attempt 重试上限不变。

[阶段 7 验收与首版逐项清单](docs/STAGE7_VALIDATION.md)汇总代码、离线检查、
真实运行、独立审查、CI 和合并状态。研究结果的并发模型审阅不等于独立 PR
审查。M1 交付可审阅草稿 PR；Python 分析模式属于另行授权的 M2。
