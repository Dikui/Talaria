# Talaria 数据接入与处理模板：一手来源核查

核查日：**2026-11-16（Asia/Shanghai）**。定位：低频研究、历史回测、paper-first；空代码仓库阶段。本文只比较 **OpenBB、Qlib、FinRL-Meta、AKShare、Tushare** 五个项目，不扩展到交易执行系统。

方法：阅读既有研究后，检查官方仓库、固定提交源码、官方文档、许可证及发布元数据；**未安装、未运行外部项目，未登录、未调用收费 API、未交易**。路径存在与静态实现经过核查，但不等于在线接口已验通。版本信息是核查时快照，不以 stars 代替成熟度。

## 1. 核心结论

1. **有可复用的连接器/研究处理模板，没有一个是可以直接交付 Talaria 的完整数据中心。** 最接近接入脚手架的是 OpenBB 的官方 provider cookiecutter；最值得复用的研究表示法是 Qlib 的 collector、二进制导出及季度/年度 PIT。
2. 推荐先做一个薄的 **Provider Adapter → 原始响应/修订留存 → 规范化 Parquet → DuckDB 查询 + SQLite manifest** 闭环。该组合是 Talaria 的设计建议，不是这些项目原样提供的成品；原始数据留存也须取得相应授权。OpenBB 是可选接入层，Qlib 是下游研究导出目标，不应让任一 SDK 的数据模型成为唯一事实源。
3. **Qlib 能表达 PIT，不证明输入本身是真实 PIT。** 官方示例采集器甚至在公告日期缺失时使用季度 45 天、年度 90 天的日历日偏移；不能把这种估计伪装成真实可用时间。[Q3]
4. **软件开源 ≠ 数据免费 ≠ 可商用 ≠ 可重分发。** AKShare 官方文档明确限制接口和数据用于学术研究、不可商业使用；本次核查的 Tushare 个人数据服务协议限定非商业、个人查看使用。paper-first 并不会自动解除这些限制。[A3]、[T3]
5. OpenBB 的许可证必须按版本区分：**本次稳定 Python 版 `openbb-v5.0.0` 是 Apache-2.0；旧 `v4.7.0` 是 AGPL-3.0。** 不应把旧结论直接用于当前版本，也不能反向推定旧版本已变更许可。[O2]、[O3]

## 2. 对比表

| 项目 | 核查版本与软件许可 | 可以直接复用的部分 | 实际适用范围 / 成熟度证据 | 不提供的关键能力 | Talaria 建议 |
|---|---|---|---|---|---|
| **OpenBB / Open Data Platform** | 稳定 Python 版 `openbb-v5.0.0`；Apache-2.0。`develop` 与发行提交分开记录 | 官方 provider cookiecutter；查询/返回模型；`transform_query → aextract_data → transform_data`；provider 注册、凭证命名空间、测试脚手架 | 统一金融数据调用/模型/扩展。发行树中确实存在模板，不只是博客教程；模板自带静态检查/测试生成约定。示例提取函数返回固定假数据，真实接入仍须实现 | 不提供通用不可变原始档案、双时间版本库、历史标的主数据、授权治理或供应商数据质量保证 | **优先借鉴/复用 provider 模板**；如只接一个来源，不必先部署完整平台 |
| **Microsoft Qlib** | `v0.9.8`；本次与 `main` 同一 SHA；MIT | `BaseCollector/BaseNormalize/BaseRun`；日历/标的/特征文件布局；`dump_bin.py`、`dump_pit.py`；PIT 存储和 `P/PRef` 算子 | 面向量化研究、特征与回测；PIT 文档、实现和测试存在。PIT 明确面向季度/年度财务因子 | 不能修复缺失的原始修订记录或真实公告时间；不等于新闻/事件的通用双时间仓库；不自动交付无幸存者偏差的历史证券池 | **复用 PIT 设计与下游导出，不作为原始事实仓库** |
| **FinRL-Meta** | 当前 `master` 与最近 GitHub release `v0.3.6` 不同；MIT | `DataProcessor` 和各 provider processor；下载→清洗→技术指标→RL 数组的管线组织 | 面向强化学习训练数据。目录有多种来源，但“有适配器源码”不证明接口今天有效；当前 AKShare processor 仍调用已被上游更名且不保留别名的函数 | 时间切分不是 PIT；缓存不是数据版本；不解决历史标的、财报修订、合规、原始证据追溯 | **仅参考处理步骤；不作为首选数据中心底座** |
| **AKShare** | 稳定 `v1.20.65` 与当前 `main` 不同；软件 MIT | 面向 A 股等市场的接口函数与 DataFrame 字段转换；可作候选 A 股 adapter | 覆盖广，但底层来源和接口语义需逐项核查；官方记录 1.20.41–1.20.54 大规模更名，旧名不作兼容别名 | 不保证站点接口稳定、全历史修订或 PIT；MIT 不覆盖网站数据权利；官方另有学术/非商业限制 | **仅在用途和上游授权确认后用于研究原型**；不要默认可用于 Talaria 商业系统 |
| **Tushare / Tushare Pro** | 公共仓库 `master` BSD-3-Clause；PyPI `1.4.29` 与老 GitHub release `0.2.0` 不能混为一个版本 | Python `DataApi` 客户端、按名称调用 API/字段选择/DataFrame 转换；A 股财务接口字段定义 | 托管数据服务，不是自建数据仓库。官方 `income` 有 `ann_date/f_ann_date/end_date/report_type/update_flag`，但有 token/积分/接口权限门槛 | 不能从客户端许可证推导数据商用/团队共享/再分发权；公告日期字段不证明全历史修订完整；日级时间不足以直接判定盘中可用性 | **授权确认后的候选 A 股数据源**；本轮不注册、不申请、不调用 |

### 版本基线：不要混淆 current branch、tag 与包发行

| 项目 | 当前分支固定提交 | 稳定发行/包证据 | 注意事项 |
|---|---|---|---|
| OpenBB | `develop`：`fac2bd450fac5e0b69ca33f11c931f8b1b0b79fa` | Python `openbb-v5.0.0`，2026-09-30 发布，提交 `5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0` | 官方原地址 `OpenBB-finance/OpenBB` 本次重定向到 `openbq-org/OpenBB`；桌面 ODP release 与 Python `openbb` 包不同，不应直接用仓库 latest 标签判断 Python 版本。模板在该稳定提交中也已核实。[O1]、[O4] |
| Qlib | `main`：`54355232463878d2eebb91fe0ee5fa7fa1f5976c` | `v0.9.8`，2026-10-08 发布，指向同一提交 | 本文 Qlib 路径同时属于该发布版和本次 main。[Q1] |
| FinRL-Meta | `master`：`286fef6c828a02a2ccf807c05b292d8cb28f8e95` | GitHub 最近 release `v0.3.6`，2022-06-26；提交 `df4a021deb642e20c10c510c8b0f5e03c1724e32` | 本文 processor 行为来自 current master，未声称等同老 release；release 较老不等于主线完全未维护。[F1] |
| AKShare | `main`：`25acdfac7ddf3a0721a0ebdcc6a1d7ef3e76abc9` | `v1.20.65`，2026-11-15；tag 提交 `cf565ea3eca0a091576cfa8cff9b6621ddeb8603` | 本文路径和重命名说明按 current main 核查；使用时应固定包版本，而不是追随 main。[A1] |
| Tushare | `master`：`093856995af0811d3ebbe8c179b8febf4ae706f0` | PyPI `1.4.29`，2026-03-25；GitHub latest release `0.2.0` 为 2015 年旧发布 | 公共源码、当前分发包、托管 API 三者版本不能假定同步；本文客户端行为仅证明固定 GitHub 提交。[T1] |

## 3. 各项目究竟可拿走什么

### 3.1 OpenBB：最直接的 provider extension 起步模板

**准确入口，以下固定到稳定版提交：**

- [cookiecutter/README.md](https://github.com/openbq-org/OpenBB/blob/5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0/cookiecutter/README.md)：选择 provider/router/charting/obbject/on_command_output；生成独立可安装的 Python 扩展、entry points、测试约定。
- [cookiecutter/openbb_cookiecutter/template/cookiecutter.json](https://github.com/openbq-org/OpenBB/blob/5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0/cookiecutter/openbb_cookiecutter/template/cookiecutter.json)：生成参数。
- [cookiecutter/openbb_cookiecutter/template/{{cookiecutter.project_tag}}/{{cookiecutter.package_name}}/providers/{{cookiecutter.provider_name}}/models/equity_historical.py](https://github.com/openbq-org/OpenBB/blob/5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0/cookiecutter/openbb_cookiecutter/template/%7B%7Bcookiecutter.project_tag%7D%7D/%7B%7Bcookiecutter.package_name%7D%7D/providers/%7B%7Bcookiecutter.provider_name%7D%7D/models/equity_historical.py)：标准查询/数据模型、字段 alias、验证器、异步提取和转换三段式；**提取方法目前是两行固定示例行情，不是现成供应商调用**。
- [cookiecutter/openbb_cookiecutter/template/{{cookiecutter.project_tag}}/{{cookiecutter.package_name}}/providers/{{cookiecutter.provider_name}}/__init__.py](https://github.com/openbq-org/OpenBB/blob/5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0/cookiecutter/openbb_cookiecutter/template/%7B%7Bcookiecutter.project_tag%7D%7D/%7B%7Bcookiecutter.package_name%7D%7D/providers/%7B%7Bcookiecutter.provider_name%7D%7D/__init__.py)：provider 注册。
- [cookiecutter/openbb_cookiecutter/template/{{cookiecutter.project_tag}}/tests/test_provider.py](https://github.com/openbq-org/OpenBB/blob/5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0/cookiecutter/openbb_cookiecutter/template/%7B%7Bcookiecutter.project_tag%7D%7D/tests/test_provider.py)：起始测试，不是生产连接器验收的替代品。

**值得复用：** 接口分层、类型化查询与响应、供应商字段映射、隔离的凭证命名空间与插件注册。可把合法来源接入包装为自己的 provider；如果 Talaria 不需要 OpenBB 的公共 API/MCP 等外壳，先借鉴分层、保持 adapter 独立也合理。

**不要误读：** 当前 provider 目录存在 SEC/FRED/政府统计等实现，但不是所有供应商、所有资产或所有套餐均支持；旧教程中 yfinance/FMP 等 provider 不应被当作当前树内默认具备。每个数据集的覆盖、调整口径、频率、收费、缓存/重分发权限必须独立核查。模型标准化不能补出不存在的 `available_at`、历史版本或证券标识映射。[O5]

### 3.2 Qlib：数据组织、研究导出和 PIT 表示法

可复用入口：

- [scripts/data_collector/README.md](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/scripts/data_collector/README.md) 与 [scripts/data_collector/base.py](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/scripts/data_collector/base.py)：`BaseCollector / BaseNormalize / BaseRun` 扩展骨架，以及 features、calendar、instruments 组织。
- [scripts/dump_bin.py](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/scripts/dump_bin.py)：转成 Qlib 研究数据格式，不是原始响应存档。
- [scripts/dump_pit.py](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/scripts/dump_pit.py)、[docs/advanced/PIT.rst](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/docs/advanced/PIT.rst)、[qlib/data/pit.py](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/qlib/data/pit.py)、[tests/test_pit.py](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/tests/test_pit.py)：修订链接和按观察时间取数/计算的具体实现。

PIT 文件记录 `date`（公告日）、`period`（报告期）、`value`、`_next`（下一修订记录的字节位置），配合索引保留同一报告期的修订。`P/PRef` 把报告期特征投影到观察日期；实现拒绝引用未来报告期。这比“按交易日期划分 train/test”更接近真实历史可见性。[Q2]

**但边界非常明确：**

- 文档限定季度/年度财务因子，且承认 PIT 计算性能尚可优化；不要据此宣称通用实时 PIT 数据库已经具备。
- [PIT collector README](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/scripts/data_collector/pit/README.md) 警告 Baostock 数据可能不完善，建议有条件用户准备自己的高质量数据。缺失公告日期时用 **季度 45 天/年度 90 天日历日** 偏移，并不获取交易日历。
- `date` 是公告日，不包含 Talaria 实际抓取时间、供应商交付延迟、盘前/盘后时间、原始响应哈希和授权信息；都需要外围契约补足。
- 只有输入保留“当时发布的值、后续修订和各自可用时刻”，PIT 算子才能正确取历史版本；最新财报值加一个历史报告期，不能变成 PIT。
- 有 instruments 文件不等于有历史完整上市/退市证券池、代码更名/复用、历史指数成分和停复牌状态。输入是否含退市标的仍需单独验收。

**采用方式：** 先把 Talaria 的原始证据和修订管理做好，再从指定 manifest/snapshot 导出 Qlib；不要把 Qlib 二进制文件当唯一审计事实源。

### 3.3 FinRL-Meta：借管线，不借“即插即用数据平台”的承诺

- [meta/data_processor.py](https://github.com/AI4Finance-Foundation/FinRL-Meta/blob/286fef6c828a02a2ccf807c05b292d8cb28f8e95/meta/data_processor.py)：统一入口、processor 选择、下载/清洗/加技术指标/转数组。
- [meta/data_processors/_base.py](https://github.com/AI4Finance-Foundation/FinRL-Meta/blob/286fef6c828a02a2ccf807c05b292d8cb28f8e95/meta/data_processors/_base.py)：基础处理契约。
- [meta/data_processors/](https://github.com/AI4Finance-Foundation/FinRL-Meta/tree/286fef6c828a02a2ccf807c05b292d8cb28f8e95/meta/data_processors)：包括 AKShare、Tushare、Alpaca、Yahoo Finance 等各自实现；**目录中有名字 ≠ 今日服务可调用、许可已获准、功能相同**。

当前统一入口的缓存键按 ticker/source/起止时间/interval 组成，pickle 缓存并不构成来源版本或数据快照版本；时间拆分只筛选 `[start,end)`，不证明每条特征当时已可获得。面向 RL 的技术指标和数组转换也不是原始证据、新闻、财报修订的数据模型。

**一个可直接核实的兼容性风险：** 当前 [AKShare processor](https://github.com/AI4Finance-Foundation/FinRL-Meta/blob/286fef6c828a02a2ccf807c05b292d8cb28f8e95/meta/data_processors/akshare.py) 调用 `ak.stock_zh_a_hist(...)`，而 AKShare 官方 README 记录该函数在 1.20.46 更名为 `stock_zh_a_hist_em`，旧名不保留别名。这是静态可见的版本不匹配，不是本轮实测故障；足以说明不能不固定依赖便宣称“支持最新 AKShare”。[A2]

### 3.4 AKShare：A 股候选连接器，而不是数据权利包

已核查的 current main 路径包括 [akshare/stock/quote/stock_hist_em.py](https://github.com/akfamily/akshare/blob/25acdfac7ddf3a0721a0ebdcc6a1d7ef3e76abc9/akshare/stock/quote/stock_hist_em.py)、[akshare/stock/issue/stock_info.py](https://github.com/akfamily/akshare/blob/25acdfac7ddf3a0721a0ebdcc6a1d7ef3e76abc9/akshare/stock/issue/stock_info.py)。可复用获取网页/API 数据、字段清洗、输出 DataFrame 的实现，但每个接口都应保留上游来源和获取日期。

软件 [LICENSE](https://github.com/akfamily/akshare/blob/25acdfac7ddf3a0721a0ebdcc6a1d7ef3e76abc9/LICENSE) 为 MIT；与此同时，官方 [docs/acknowledgements.md](https://github.com/akfamily/akshare/blob/25acdfac7ddf3a0721a0ebdcc6a1d7ef3e76abc9/docs/acknowledgements.md) 明文：**“数据接口和数据仅用于学术研究，不可做任何商业用途”**。这不是可忽略的细则。MIT 许可不能替代上游站点的数据许可，也不能推导可复制到团队数据湖、对外展示或销售。

当前 [README](https://github.com/akfamily/akshare/blob/25acdfac7ddf3a0721a0ebdcc6a1d7ef3e76abc9/README.md) 记录约 320 个接口在 1.20.41–1.20.54 更名，提示应固定版本和做字段契约测试。对 A 股原型有价值，但不得把当前证券列表、当前复权值和当前财务数据自动当作历史 as-of 数据。

### 3.5 Tushare：开源客户端 + 受权限和条款约束的托管服务

- [tushare/pro/client.py](https://github.com/waditu/tushare/blob/093856995af0811d3ebbe8c179b8febf4ae706f0/tushare/pro/client.py)：`DataApi.query(api_name, fields='', **kwargs)` 携带 token 和参数请求服务，将返回字段/记录转成 DataFrame；可借鉴很薄的 provider 包装。
- [tushare/pro/data_pro.py](https://github.com/waditu/tushare/blob/093856995af0811d3ebbe8c179b8febf4ae706f0/tushare/pro/data_pro.py)：Pro API 接入相关代码。
- [官方利润表接口](https://tushare.pro/document/2?doc_id=33)：`income` 有公告日、实际公告日、报告期、报表类型、更新标记；文档包含调整前/调整后报表类型，有利于建立财务版本契约，但不能据此保证所有历史修订均完整可取。该页标明普通接口至少 2000 积分、全市场季度 `income_vip` 5000 积分；额度与规则未来可能变动。

所核查仓库客户端仍把服务 URL 写为 `http://api.tushare.pro`；这只证明该固定旧提交，不推断当前 PyPI 包实现相同。真正接入时需核对所采用版本的安全传输和官方端点，不能直接携 token 运行未经审计的旧代码。[T2]

软件是 BSD-3-Clause，但 [Tushare 数据服务协议](https://tushare.pro/document/1?doc_id=405) 的许可是**个人、不可转让、非商业、可撤销、有期限、非排他性**，并限定个人查看使用、禁止账号共享。Talaria 若涉及团队、商业研究、数据再分发，需要另行确认适用合同/取得明确授权；不能把付费积分视为不受限制的数据产权，也不能把开源 SDK 当免费离线数据库。

## 4. 推荐起步模板与最小补齐项

### 4.1 选择顺序

1. **首选 OpenBB 稳定版 provider 模板作为接入参考**：保留 `transform_query / extract / transform_data` 的职责隔离，先只接一个已合法授权的数据集。是否依赖 OpenBB 全栈由真实接入数量决定，当前无需为了“数据中心”引入全部平台。
2. **Qlib 做可选下游导出**：需要 Qlib 特征/回测时，按确定的数据快照生成其 features/calendar/instruments/PIT 文件；借用季度/年度修订表示，而不把示例采集器等同于 PIT 供应商。
3. **FinRL-Meta 延后到确实需要 RL 数据数组时**；不为低频研究预先引入其完整训练/数据处理栈。
4. **A 股来源先过授权门槛，再选 AKShare 或 Tushare**；本次证据不足以把任一默认列为 Talaria 商业数据源。

### 4.2 模板之外必须由 Talaria 定义的契约

| 缺口 | 最小要求 | 不能接受的替代品 |
|---|---|---|
| 原始证据与修订 | 在许可允许下保存原始响应；记录 source、endpoint/参数摘要、获取时间、内容 hash、parser 版本；追加修订而非覆盖 | “重新请求同一时间区间应该得到同样结果” |
| 时间可见性 | 分开 event/period、`published_at`、`available_at`、`ingested_at`；未知/估计显式标记；回测只取 cutoff 前可见版本 | 用报告期或季度末代替可用时间；把今天补采时间回填成当年可见时间 |
| 数据快照 | manifest 固定文件哈希、来源版本、代码版本、调整口径、时区/交易日历和质量状态；回测引用 snapshot ID | 下载日期相同、缓存命中、仅固定 Python 依赖 |
| 证券主数据 | 稳定 instrument ID；代码/交易所映射有效期；上市/退市和历史 universe；成分/停复牌/公司行动按时点管理 | 当前股票列表回测过去十年 |
| 数据质量 | 重复/缺口/排序、日历与时区、复权/币种/单位、OHLCV 约束、修订和未来信息泄漏测试 | provider 返回 DataFrame 就当作合格 |
| 权限 | 数据集级记录内部使用/商业使用/保存期限/衍生数据/再分发限制；限制查询和导出 | MIT/Apache/BSD 的软件 LICENSE |

上述契约可以落到轻量 Parquet + DuckDB + SQLite manifest 方案；**本文仅提出分析建议，没有创建数据中心、接入服务或验证该实现**。历史修订若供应商没有保留，今天开始保存快照只能保证未来可追踪，不能凭空恢复过去的真实可见状态。

## 5. 一手来源索引

以下 GitHub blob/tree 链接均为官方仓库；源码事实尽量固定 commit，动态协议和包元数据以核查日为准。

- **[O1] OpenBB 版本：** [官方 releases](https://github.com/openbq-org/OpenBB/releases)、[Python openbb-v5.0.0](https://github.com/openbq-org/OpenBB/releases/tag/openbb-v5.0.0)、[tag 对象](https://api.github.com/repos/openbq-org/OpenBB/git/ref/tags/openbb-v5.0.0)、[PyPI 包元数据](https://pypi.org/pypi/openbb/json)。
- **[O2] 当前稳定版许可：** [openbb-v5.0.0 LICENSE](https://github.com/openbq-org/OpenBB/blob/5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0/LICENSE)。
- **[O3] 旧版本许可对照：** [v4.7.0 LICENSE](https://github.com/openbq-org/OpenBB/blob/v4.7.0/LICENSE)。
- **[O4] 官方模板：** [稳定版 cookiecutter README](https://github.com/openbq-org/OpenBB/blob/5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0/cookiecutter/README.md)、[稳定版完整树 API](https://api.github.com/repos/openbq-org/OpenBB/git/trees/5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0?recursive=1)。
- **[O5] 当前范围：** [README](https://github.com/openbq-org/OpenBB/blob/fac2bd450fac5e0b69ca33f11c931f8b1b0b79fa/README.md)、[openbb_platform/pyproject.toml](https://github.com/openbq-org/OpenBB/blob/fac2bd450fac5e0b69ca33f11c931f8b1b0b79fa/openbb_platform/pyproject.toml)、[providers](https://github.com/openbq-org/OpenBB/tree/fac2bd450fac5e0b69ca33f11c931f8b1b0b79fa/openbb_platform/providers)。
- **[Q1] Qlib 版本与许可：** [v0.9.8 release](https://github.com/microsoft/qlib/releases/tag/v0.9.8)、[tag 对象](https://api.github.com/repos/microsoft/qlib/git/ref/tags/v0.9.8)、[LICENSE](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/LICENSE)。
- **[Q2] Qlib PIT 机制与限制：** [PIT 文档](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/docs/advanced/PIT.rst)、[P/PRef 实现](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/qlib/data/pit.py)。
- **[Q3] Qlib 数据质量与估算日期警告：** [PIT collector README](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/scripts/data_collector/pit/README.md)。
- **[F1] FinRL-Meta 版本与许可：** [release v0.3.6](https://github.com/AI4Finance-Foundation/FinRL-Meta/releases/tag/v0.3.6)、[tag 对象](https://api.github.com/repos/AI4Finance-Foundation/FinRL-Meta/git/ref/tags/v0.3.6)、[master LICENSE](https://github.com/AI4Finance-Foundation/FinRL-Meta/blob/286fef6c828a02a2ccf807c05b292d8cb28f8e95/LICENSE)。
- **[F2] FinRL-Meta 行为证据：** [DataProcessor](https://github.com/AI4Finance-Foundation/FinRL-Meta/blob/286fef6c828a02a2ccf807c05b292d8cb28f8e95/meta/data_processor.py)、[AKShare processor](https://github.com/AI4Finance-Foundation/FinRL-Meta/blob/286fef6c828a02a2ccf807c05b292d8cb28f8e95/meta/data_processors/akshare.py)。
- **[A1] AKShare 版本与许可：** [v1.20.65 release](https://github.com/akfamily/akshare/releases/tag/v1.20.65)、[PyPI 元数据](https://pypi.org/pypi/akshare/json)、[main LICENSE](https://github.com/akfamily/akshare/blob/25acdfac7ddf3a0721a0ebdcc6a1d7ef3e76abc9/LICENSE)。
- **[A2] AKShare 接口变更：** [官方 README](https://github.com/akfamily/akshare/blob/25acdfac7ddf3a0721a0ebdcc6a1d7ef3e76abc9/README.md)。
- **[A3] AKShare 数据用途限制：** [官方 acknowledgements](https://github.com/akfamily/akshare/blob/25acdfac7ddf3a0721a0ebdcc6a1d7ef3e76abc9/docs/acknowledgements.md)。
- **[T1] Tushare 版本与软件许可：** [官方仓库 releases](https://github.com/waditu/tushare/releases)、[PyPI 元数据](https://pypi.org/pypi/tushare/json)、[LICENSE](https://github.com/waditu/tushare/blob/093856995af0811d3ebbe8c179b8febf4ae706f0/LICENSE)。
- **[T2] Tushare 范围和字段：** [固定客户端源码](https://github.com/waditu/tushare/blob/093856995af0811d3ebbe8c179b8febf4ae706f0/tushare/pro/client.py)、[官方 income 文档](https://tushare.pro/document/2?doc_id=33)。
- **[T3] Tushare 数据权利边界：** [数据服务协议](https://tushare.pro/document/1?doc_id=405)，尤其二（二）5–6 项及第三、第五节。
