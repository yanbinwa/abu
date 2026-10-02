# RomDataBu 数据目录说明

RomDataBu 保存随源码分发的行情样本和市场静态数据，用于教程、离线演示和基础回归。这里的数据是历史快照，不代表当前市场状态，也不应直接作为生产行情源。

## 内容索引

| 文件或目录 | 内容 |
| --- | --- |
| `csv` | 95 个股票、指数、期货和数字货币历史行情文件 |
| `csv.zip` | 内置行情压缩包，缺少解压目录时由示例环境使用 |
| `stock_code_CN.csv` | A 股代码与基础信息 |
| `stock_code_US.csv` | 美股代码与基础信息 |
| `stock_code_HK.csv` | 港股代码与基础信息 |
| `futures_cn.csv` | 国内期货元数据 |
| `futures_gb.csv` | 国际期货元数据 |
| `hk_unit.csv` | 港股每手股数 |
| `symbols_db.db` | 证券信息 SQLite 数据库 |
| `ml_test.csv` | 机器学习教学数据 |

## 行情格式

典型字段包括 `open`、`high`、`low`、`close`、`pre_close`、`volume`、`p_change`、`date`、`date_week`、`key`、`atr14` 和 `atr21`。文件名包含证券代码及覆盖日期，文件内容是 CSV，但多数没有 `.csv` 扩展名。

当前内置行情总体覆盖 2011 年 7 月 28 日至 2017 年 7 月 26 日，不同标的起止日期不同。测试必须选择实际覆盖的区间。

## 使用方式

调用 `ABuEnv.enable_example_env_ipython` 后，系统强制从本目录读取行情。该模式适合教程和可重复回归，不会自动补充最新数据。

## 更新规则

- 新数据必须保持标准字段、升序日期和唯一索引。
- 明确复权、时区、币种、交易单位和缺失值口径。
- 更新静态证券信息时同步检查代码解析和行业查询。
- 不覆盖已有基线数据而不记录来源、版本和变更原因。
- 大文件和授权受限数据不得直接提交仓库。

## 相关模块

[MarketBu](../MarketBu/README.md) 读取和解析数据，[CoreBu](../CoreBu/README.md) 控制示例环境，[CrawlBu](../CrawlBu/README.md) 可生成部分证券静态信息。
