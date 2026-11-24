# Third-Party Notices

本仓库前端在构建时打包以下第三方组件。分发构建产物时必须保留本文件及对应许可证。

## KLineChart

- 版本：`klinecharts@10.1.1`
- 许可证：Apache License 2.0
- 项目主页：<https://github.com/klinecharts/KLineChart>
- 许可证全文：<https://github.com/klinecharts/KLineChart/blob/main/LICENSE>

NOTICE 原文：

```text
KLineChart
Copyright (c) 2019 lihu

TradingView Lightweight Charts
Copyright (с) 2019 TradingView, Inc. https://www.tradingview.com
```

KLineChart 的 `licenses/` 目录还包含其内嵌/参考组件的许可证；分发时请一并保留 `node_modules/klinecharts/licenses/` 中的内容。

## websockets

- 版本：`websockets@15.0.1`
- 用途：调用 Bybit V5 Linear 公共 WebSocket 行情，以及本地 fake 服务器测试（仅后端，不进入前端构建产物）
- 许可证：BSD 3-Clause
- 项目主页：<https://github.com/python-websockets/websockets>
- 许可证全文：<https://github.com/python-websockets/websockets/blob/main/LICENSE>

## TA-Lib

- 版本：`ta-lib==0.8.3`（Python 封装，PyPI 包名 `TA-Lib`）
- 用途：技术指标冷库批量的可选交叉校验基准；运行时以仓库内纯 Python 算子核与 `contracts/operator_vectors/` 黄金向量为准
- 许可证：底层 C 库 BSD 3-Clause；Python 封装 BSD 2-Clause
- 项目主页：<https://github.com/TA-Lib/ta-lib-python>
- C 库许可证全文：<https://github.com/TA-Lib/ta-lib/blob/main/LICENSE>
- Python 封装许可证全文：<https://github.com/TA-Lib/ta-lib-python/blob/master/LICENSE>
- 说明：已锁定 `TA-Lib==0.8.3`。若目标平台没有可用 wheel 且本机缺少 C 库，允许不安装该依赖；纯 Python 算子核仍提供完整结果，不影响指标功能。

## talipp

- 版本：`talipp==2.7.0`
- 用途：技术指标热流增量的可选交叉校验基准；运行时以仓库内纯 Python 算子核的 `update_series` 为准
- 许可证：MIT
- 项目主页：<https://github.com/nardew/talipp>
- 许可证全文：<https://github.com/nardew/talipp/blob/main/LICENSE>

## Vela (`@luxalgo/vela`)

- 版本：`@luxalgo/vela@0.10.17`（仅安装无头核心与 `@luxalgo/vela/plugin`；**未安装** AGPL-3.0 的 `@luxalgo/vela-pinets`）
- 用途：独立看盘工作台（`workbench/packages/talaria-desk-ui/`）主力 K 线与原生 WebGL2/Canvas2D 图表渲染核
- 许可证：Apache License 2.0
- 项目主页：<https://velacharts.dev>
- 代码仓库：<https://github.com/LuxAlgo/Vela>
- 许可证全文：<https://github.com/LuxAlgo/Vela/blob/main/LICENSE>

NOTICE 原文（Apache-2.0 §4(d)，须在所有展示 Vela 图表的页面保持可见署名 `"Vela"` 与主页链接 `https://velacharts.dev`）：

```text
Vela
Copyright (c) 2026 LuxAlgo
https://velacharts.dev

This product includes the Vela charting library, developed by LuxAlgo.

ATTRIBUTION REQUIREMENT

Any product, website, or application that displays charts rendered by this
software must show a visible attribution to the Vela project on every page or
screen where such a chart is displayed.

The library satisfies this requirement automatically: it renders an attribution
mark (the project logomark linking to the Vela project page) on the chart
itself, enabled by default. You may restyle or reposition this mark to fit your
design.

You may disable the built-in mark (renderer feature `attribution`) ONLY if you
display an equivalent visible attribution — the name "Vela" together with a
link to the project page above — elsewhere on the same page or screen, in a
place available to your users.

Removing, hiding, or obscuring the attribution without providing the
equivalent notice described above is not permitted.

This file constitutes the NOTICE file described in Section 4(d) of the Apache
License, Version 2.0: redistributions of this software, in source or binary
form, must include a readable copy of these attribution notices.
```

## dockview-react

- 版本：`dockview-react@8.9.1`（含 `dockview@8.9.1`、`dockview-core@8.9.1`）
- 用途：多图分屏、标签组、可折叠停靠面板、布局序列化与弹窗工作台布局引擎
- 许可证：MIT
- 项目主页：<https://github.com/dockview/dockview>
- 许可证全文：<https://github.com/dockview/dockview/blob/master/LICENCE.md>

## Tauri v2

- 版本：`tauri@2.x` / `tauri-build@2.x`（`workbench/src-tauri/`）
- 用途：独立看盘桌面薄壳、多窗口原生管理与 `talaria-data` 进程生命周期监管
- 许可证：Apache License 2.0 OR MIT
- 项目主页：<https://v2.tauri.app>
- 代码仓库：<https://github.com/tauri-apps/tauri>
- 许可证全文：<https://github.com/tauri-apps/tauri/blob/dev/LICENSE_APACHE-2.0> / <https://github.com/tauri-apps/tauri/blob/dev/LICENSE_MIT>

## lucide-react

- 版本：`lucide-react@1.78.0`
- 用途：工作台轻量矢量图标组件
- 许可证：ISC
- 项目主页：<https://lucide.dev>
- 许可证全文：<https://github.com/lucide-icons/lucide/blob/main/LICENSE>
