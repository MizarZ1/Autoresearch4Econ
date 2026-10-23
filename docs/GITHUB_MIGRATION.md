# GitHub 迁移说明

2026-10-23 起，后续开发使用 [MizarZ1/Autoresearch4Econ](https://github.com/MizarZ1/Autoresearch4Econ)。
本次迁移依据本地保存的 Git 历史和此前发布文件。旧 Gitea 在外网环境无法访问，研究者已要求暂不访问。

## 代码、分支与版本

迁移了本地保存的 46 个提交、10 个源分支，以及 `demoV0.1-test` 标签，保留原提交哈希、作者、时间和合并关系。
原 `main` 为 `5c502ed5ae66078a5152014f05a9847aea25b207`；标签仍指向 `8614cb33f21bfe891bb2de7f007a598fa34e0a9e`。
迁移配置另走 `codex/github-migration` 分支与新 PR，不改写既有提交或版本附件。

源分支为 `main`、`feat/cli-agent`、`feat/stage1-state`、`feat/stage2-stata`、
`feat/stage3-loop`、`feat/stage4-review`、`feat/stage5-cli`、`feat/stage6-report`、
`feat/stage7-methods`、`feat/demo-v01-test`。

本地 `origin` 改为 GitHub，旧地址保留在 `gitea` 远端。后续使用 GitHub 创建分支与 PR。
不要使用 `git push --mirror`，避免上传本机内部引用或删除远端独有分支。

## 历史 PR

原 PR #1–#9 的合并关系已经保留在 Git 提交图中；对应 GitHub Issue #1–#9 为关闭的历史档案。
每份档案包含原 PR 地址、原分支、合并前后提交，以及源提交中的本地 `docs/PR_DRAFT.md`。
草稿不保证等于远端最终正文。它们不是 GitHub 原生 PR，关闭状态也不代表新的审阅批准。
原提交仍包含历史 Gitea 链接，保留这些来源信息有助于追溯。

逐项映射见 [迁移证据](evidence/github_migration.json)。完整远端正文、评论、行内讨论、
审阅及平台事件尚未导出；Git 提交时间不冒充远端 PR 创建或合并时间。

## 发布与交付

[demoV0.1-test](https://github.com/MizarZ1/Autoresearch4Econ/releases/tag/demoV0.1-test)
保留预发布属性和同事版发布说明，迁移此前 7 个原始附件。
交付下载 `demoV0.1-test-delivery.zip` 与 `SHA256SUMS`；开始前阅读包内 `docs/QUICKSTART.md`。
附件与旧发布的已保存 SHA256 记录核对，并在上传后重新下载验证，不重新打包旧版本。
附件内版本清单和文档为原发布时的快照，旧链接及状态不因迁移而篡改。

## CI 与边界

`.github/workflows/ci.yml` 运行统一离线检查、CLI 检查及打包安装验证；不需要 Stata 许可或模型密钥。
旧 `.gitea/` 配置保留为历史。普通 CI 不运行真实研究、不启动外部服务，也不增加 Docker。
远端 CI、研究验收、独立审阅及合并分别记录，旧 runner 豁免不视为 GitHub 检查通过。

所有待推送历史已检查禁止路径、数据与二进制类型及常见凭证模式；没有发现阻断项。
未上传题目、密钥、原作者数据与程序、Stata 软件和许可、运行数据库或原始日志。
不迁移平台凭证、Webhook 密钥或旧 CI Secrets。

## 本次验证

本地 `.venv/bin/python scripts/check.py` 通过：格式、Ruff、严格 mypy、314 项离线测试与 CLI 版本。
原分支及标签应逐项与 GitHub 远端哈希核对；发布 7 个附件已重新下载校验。
GitHub Actions 的实际结果以迁移 PR 的 Checks 为准；本地通过不代表远端通过，独立审阅仍单列。

## 待补事项

恢复实验室网络并获准访问后，重新盘点 Gitea 的全部远端分支、标签、Issue、Wiki、发布与附件，
核对本地最近一次同步之后是否新增内容，再补充 PR 正文、评论、审阅和事件档案。
目前不能宣称 Gitea 全站内容或全部平台元数据已完成迁移。
