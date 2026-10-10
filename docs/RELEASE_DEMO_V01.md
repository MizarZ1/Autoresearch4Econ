# demoV0.1-test 候选交付说明

发布名与 Git tag 为 `demoV0.1-test`，Python 包版本为 `0.1.0rc1`，Gitea 标为预发布。用户已授权修复、安装验证、打包与发布；不部署 Docker、额外服务或 M2 Python 分析后端。

## 本次修复

- Stata 命令比较保留变量大小写；描述统计保存实际主体命令，x 与 X 不会误认同一规格。
- 技术重试对照不可变的完整参数归档，禁止偷换协方差、变量角色或 DiD/RDD 结构；旧会话无需改数据库。
- 独立核对 t/正态推断的双侧 p 值与 95% 区间；IV 额外核对满秩标记、矩阵维度、有限值、对称性、样本量及残差自由度。
- 没有派发意图的遗留查询可确认为未启动；有派发证据而缺少进程身份时仍暂停。
- 自然语言反馈受剩余时间和调用预算约束，预留一次论文写作请求，反馈处理耗时计入恢复后的总预算。
- 论文对常见中文样本量和系数表达要求事实占位符。规则检查有边界，不等于完整语义核验或独立论文审阅。

## 交付物与边界

附件包括 wheel、源码 tar.gz、含合成演示的交付 zip、带 hash 的 Stata 运行依赖清单、发布清单、安装验收摘要及 `SHA256SUMS`。发布清单绑定准确 commit，列出包成员。依赖未随包打包，首次安装可能需要网络。

构建采用源码路径白名单与已提交文件双重约束，拒绝题目目录、本地参考、原作者 `.dta`、厂商二进制、运行数据库、原始日志、环境秘密文件及不安全路径。检查当前凭证值不会出现在包中。Gitea 附件只上传经过审计的明确清单；不把整个工作目录压缩上传。

启动、配置、恢复及故障处理见 [QUICKSTART.md](QUICKSTART.md)。已验证的方法范围见 [METHODS.md](METHODS.md)。合成演示是工程验收，AJR 作者数据需自行获取；公开数值摘要不能替代原数据。完整研究质量仍须判断识别假设、诊断和解释。

## 验证与审阅状态

统一离线检查、实际 Stata 方法与生命周期重验、仓库外 wheel 安装、源码包重建、安装资源与无凭证只读 CLI 分开验证。真实 Stata 使用本机既有 18.0.185、单 processor、专用会话；不下载更新。

真实 IV 数值及取消/超时验收摘要在 `docs/evidence/demo_v01_iv.json`；OLS、DiD、RDD、Logit、描述统计及迁移重跑在 `docs/evidence/demo_v01_methods.json`。这些脚本采用固定设计和脚本化确认，不冒充自主选择或独立研究者答复。过去真实模型闭环的记录另见阶段 3–7 文档，不能因本次固定验收而宣称任意研究问题已经通过。

| 项目 | 状态 |
| --- | --- |
| 作者自检与回归测试 | 本次修复后执行统一离线检查，准确结果见候选验收摘要 |
| 本机真实执行 | 本次重新验收，数值范围与证据单列 |
| 独立安装包验收 | 从实际附件独立安装并生成 installation-validation.json |
| 独立研究者审阅 | 待完成 |
| 独立 PR 审阅 | 待完成；前期问题审阅已用于修复，不等于最终候选提交获批准 |
| 远端 CI | lab404Runner 离线；按研究者授权暂跳过，不能记为通过 |
| 合并 | 候选分支待审查，不自动合并 main |

## 重建与验证

在干净已提交的候选源码中运行：

```sh
uv sync --locked --extra stata --group dev
uv run --locked --extra stata --group dev python scripts/check.py
uv run --locked --extra stata --group dev python scripts/build_release.py
uv run --locked --extra stata --group dev python scripts/validate_install.py --packages output/releases/demoV0.1-test
uv run --locked --extra stata --group dev python scripts/build_release.py --finalize
```

安装验证使用本机已缓存的锁定依赖（`--offline`）；缺缓存时先完成在线依赖安装。默认安装验证不会启动 Stata 或调用模型。明确增加 `--execute-stata` 才真实执行固定合成 OLS 和描述统计；需要配置本机路径，仍不调用模型。安装验证记录保留在本地临时目录，公开摘要不含数据库或日志。

第三方依赖许可仍适用：`mcp-stata` 上游采用 AGPL-3.0，Stata 是单独许可的商业软件。项目不携带其源码、安装程序或许可激活文件；本候选版不新增项目再分发许可证承诺。
