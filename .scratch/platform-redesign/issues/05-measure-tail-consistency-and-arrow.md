# 05 — 实测：补尾与直读的并发一致性、Arrow IPC 传输耗时

**What to build:** 两个原型实测，给 04 的实现定结论：① 服务持续写入和合并时，SDK 直读加补尾会不会漏柱、重柱；② 千万行经本机 HTTP 用 Arrow IPC 传输的耗时，判断范围接口是否需要 Arrow 格式。

**Blocked by:** 04 — Python 取数 SDK。

**Source of truth:** ADR-0016、ADR-0018。

**Status:** ready-for-agent

- [ ] 一致性：高频写入 + 合并期间反复读取，统计漏柱、重柱、读到旧文件被删等错误次数，给出复现脚本（原型代码不提交）。
- [ ] Arrow IPC：1M、10M 行在 JSON 与 Arrow IPC 下的传输与解析耗时。
- [ ] 结论写回 `.scratch/platform-redesign/spec.md` 的 D3“待实测”处；发现缺陷时新开任务单。
