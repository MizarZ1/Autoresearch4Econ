# demoV0.1-test 安装与运行

这是 M1 的本机 Stata 测试候选版，Python 包版本为 `0.1.0rc1`。目前真实验证环境为 macOS、Python 3.11、Stata/MP 18.0.185 和 `mcp-stata==3.3.0`。其他 Stata 构建显示未验证，不能沿用本机数值验证标记。Linux 尚无完整真实验收；Windows 不支持当前 POSIX 状态锁。暂不使用 Docker。

## 安装

准备 Python 3.11、uv（构建验证使用 0.12.3）、已有合法许可的 Stata 与 OpenAI-compatible 模型账号。首次安装依赖需要访问 Python 包源；交付包未携带第三方依赖 wheel，不能称为离线安装包。已有缓存时可以使用 `--offline`。

解压 `demoV0.1-test-delivery.zip`，进入 `demoV0.1-test/`。先用附件 `SHA256SUMS` 校验下载文件，例如 macOS 上运行 `shasum -a 256 -c SHA256SUMS`。

按锁文件安装源码（包含 Stata 连接；开发工具另加 `--group dev`）：

```sh
uv sync --locked --python 3.11 --extra stata --no-dev
uv run --locked --extra stata --no-dev econ-agent --version
```

也可非 editable 安装 wheel。单独安装 wheel 不会采用 `uv.lock`，因此先安装附带的精确依赖与 hash 清单：

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements-stata.txt
uv pip install --python .venv/bin/python --no-deps ./econ_agent-0.1.0rc1-py3-none-any.whl
.venv/bin/econ-agent --version
```

源码包 `.tar.gz` 包含锁文件和交付文档，wheel 包含应用和 `method_validation.json`。`demoV0.1-test-delivery.zip` 同时包含源码、wheel 与运行依赖清单。无需复制开发者的 `.venv`。

## 配置与检查

按照实际安装位置设置两个 Stata 路径。下面路径仅为 macOS 示例，不随包提供 Stata：

```sh
export ECON_AGENT_STATA_HOME=/Applications/Stata
export STATA_PATH=/Applications/Stata/StataMP.app/Contents/MacOS/stata-mp
export ECON_AGENT_MODEL_URL=https://api.deepseek.com
export ECON_AGENT_MODEL=deepseek-chat
export ECON_AGENT_KEY_VARIABLE=DEEPSEEK_API_KEY
```

把自己的密钥放入上面指定的进程环境变量 `DEEPSEEK_API_KEY`。不要写入 task.json、版本库或运行数据库。程序不自动读取 `.env`；`.env.example` 是配置说明。

```sh
.venv/bin/econ-agent doctor --backend stata --json
.venv/bin/econ-agent doctor --backend stata --probe-stata --workspace "$PWD" --json
```

第一个命令只检查配置和已安装依赖；退出码 0 不代表真实执行成功。第二个命令明确启动本机 MCP 和专用 Stata 会话进行探测，不调用模型；它会生成本地诊断记录。真实研究还需要模型账号能正常访问。

## 运行随包演示

```sh
.venv/bin/econ-agent start --backend stata --task examples/quickstart/task.json --workspace "$PWD"
```

程序自主推进，在关键处展示待确认设计并等待研究者。确认变量、完整观测处理与方法后输入 `/accept`；修改用 `/revise 修改说明`；也可以 `/reject 理由`、`/pause` 或 `/stop`。自然语言反馈消耗模型预算，显式命令可以在模型预算不足时处理待答问题。Ctrl+C 请求暂停并核对本次工作进程退出；遇到不明工作进程身份会保持暂停，不能盲目重试。

用输出中的实际 run_id 查询、恢复和导出：

```sh
.venv/bin/econ-agent status <run_id> --workspace "$PWD"
.venv/bin/econ-agent resume <run_id> --workspace "$PWD"
.venv/bin/econ-agent export <run_id> --workspace "$PWD"
```

`runs/` 保存本地状态；`output/` 保存导出。status 和 export 不需要模型凭证或 Stata。复现包会包含任务的数据副本，仅用于研究者本地复现；上传自己的复现包前应重新检查数据授权与敏感信息。本次公开候选交付只含自编合成数据，不上传 AJR 数据副本。

AJR 示例另需自行按 `examples/demo/sources.json` 获取数据并验证 hash，放入被忽略的 `examples/demo/data/`。原作者数据、代码、论文和题目材料均不在候选包中。

## 故障定位

- doctor 返回 1：核对密钥环境变量、Stata 路径和 `mcp-stata` 安装；不要自行替换为 Python 分析后端。
- Stata MCP 找不到库：同时核对 `ECON_AGENT_STATA_HOME` 的安装根目录和 `STATA_PATH` 的可执行文件，不下载厂商更新包解决路径问题。
- 方法显示 `available_unverified`：该构建或映射没有相同证据；命令可用不等于数值已验证。范围见 `docs/METHODS.md`。
- 模型动作无效、预算不足或网络失败：查看 status 的失败与暂停原因，保留已有分析；必要时确认设计后恢复。缺少证据时报告可能是明确标记的部分交付。
- 有未确认停止的旧任务：检查本地恢复记录及对应进程身份，不修改数据库强行标记成功。新版本没有数据库迁移；状态库仍为 v1。

仅在受信任本机和受信任数据目录运行。项目使用工作副本与完整性检查，尚不是强文件访问沙箱，也没有多用户服务、认证网关或云部署。
