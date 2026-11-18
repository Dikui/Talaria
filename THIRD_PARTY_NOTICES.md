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
