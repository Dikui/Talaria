## Agent skills

### Issue tracker

任务单与需求文档以本地 Markdown 保存在 `.scratch/<feature-slug>/`。See `docs/agents/issue-tracker.md`.

### Triage labels

使用标准分流标签：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。See `docs/agents/triage-labels.md`.

### Domain docs

单上下文布局（根目录 `GLOSSARY.md` + `docs/adr/` + `docs/architecture.md`）。涉及业务或架构设计时，参阅 `docs/agents/domain.md` 中的资料入口与按需读取规则。

### Document layout

文档放置约定见 `docs/agents/layout.md`。

### Testing

开发中只运行受影响的测试，按 `/local-test` 做：`pytest <path>`（或 `pytest -k <expr>`）。全量测试 `pytest` 由 pre-push 钩子在版本号（`VERSION`）前两位变化时运行。

### Commit rules

禁止 `--no-verify`、`git add -A` / `git add .` 和强推。
