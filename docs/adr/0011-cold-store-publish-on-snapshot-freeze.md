---
status: accepted
---

# 冷库发布由快照冻结触发，使用 append-only 游标而非独立后台采集循环

06 要求“把 02 的简易落盘升级为冷热一体”：快照冻结时把热端未归档切片物化，同时把已可见批次原子发布到冷库。实现时发现，v1 没有独立的实时采集循环和清晰的批次边界；若再引入一个后台 collector，它会与快照构建并发读写 `data_manifest.sqlite`，并需要自己定义“一批完成”的判据。因此 v1 选择：以快照构建为唯一发布触发点，冷库游标只向前推进。

## Considered Options

- **独立后台 collector 持续发布**：能缩短冷库延迟，但需要新增生命周期、批次边界、故障恢复与单写者协调；在 M1/M2 没有实时交易闭环的情况下，收益不足。否决。
- **每次快照全量重发该标的历史**：实现简单，但会反复写同一 generation，破坏 append-only 与 manifest 的原子切换语义，且成本随历史增长。否决。
- **快照构建同步等待冷库发布成功**：能保证每次快照都完整落冷库，但冷库故障会阻塞用户看盘，违背快通道零阻塞。否决。

## Consequences

- `ColdPublisher.publish_snapshot_bars` 按 `ts_event` 排序、同 timestamp 后出现者胜，只保留 `ts_event > cursor.last_ts` 的柱；无新柱时返回 `None`。
- `ColdStore.publish` 拒绝首柱不晚于游标的批次；冷库发布失败只记 warning，快照继续返回。冷库延迟因此取决于快照构建频率。
- 已发布 timestamp 的迟到 revision 不会回填；M3 若需要 `OBSERVED_REPLAY` 的严格修订重放，必须新增 revision-aware 的追加路径，而不是放宽当前游标。
- 快照仍只读 manifest 显式列出的 generation 文件；冷库与热端快照通过同一清单协调，不改变 06 的原子发布与重启逐字节一致约束。
