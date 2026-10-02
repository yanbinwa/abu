# MarketBu 模块开发说明

MarketBu 负责把市场标识、证券代码、在线数据源和本地缓存统一为标准 K 线表。上层模块应优先通过 `ABuSymbolPd` 获取行情，避免直接绑定某个数据源或缓存实现。

## 数据流

```text
Symbol
  └─ ABuDataSource 选择数据源
      └─ ABuDataFeed 请求原始数据
          └─ ABuDataParser 标准化字段
              └─ ABuDataCache 合并和缓存
                  └─ ABuSymbolPd 返回 K 线表
```

## 文件索引

| 文件 | 作用 |
| --- | --- |
| `ABuSymbolPd.py` | 单标的及批量行情对外入口 |
| `ABuSymbol.py` | `Symbol`、市场识别和指数常量 |
| `ABuSymbolStock.py` | A 股、美股、港股代码与基础信息 |
| `ABuSymbolFutures.py` | 国内和国际期货元数据 |
| `ABuDataSource.py` | 根据环境选择数据源 |
| `ABuDataFeed.py` | 百度、腾讯、网易、新浪及火币示例适配器 |
| `ABuDataParser.py` | 各数据源字段解析 |
| `ABuDataCache.py` | CSV 与 HDF5 行情缓存 |
| `ABuMarket.py` | 市场股票池和训练测试集切分 |
| `ABuIndustries.py` | 行业分类和行业匹配 |
| `ABuMarketDrawing.py` | K 线及多标的绘图 |

## 标准行情字段

核心流程依赖 `open`、`high`、`low`、`close`、`pre_close`、`volume`、`p_change`、`date`、`date_week` 和 `key`。`calc_atr` 会补充 `atr14` 与 `atr21`。索引应为有序交易日期，DataFrame 的 `name` 应保存规范化证券代码。

## 主要接口

- `make_kl_df` 获取单标的行情，并根据日期、年数、基准和数据模式切分。
- `kl_df_dict_parallel` 批量获取标的行情。
- `get_price` 返回指定日期范围的价格序列。
- `all_symbol` 获取目标市场标的集合。
- `code_to_symbol` 将字符串规范化为 `Symbol`。
- `query_stock_info` 查询股票静态信息。

## 关键设置

数据源、目标市场、获取模式和缓存类型位于 `CoreBu.ABuEnv`。`g_private_data_source` 可注册私有数据源；`g_use_env_market_set` 控制股票池是否受环境市场限制；绘图模块的 `g_only_draw_price` 控制是否只绘制价格。

## 新数据源接入

1. 继承对应市场基类并声明支持的市场。
2. 在解析器中将响应转换为标准字段。
3. 在 `ABuDataSource.kline_pd` 中接入选择逻辑，或注册 `g_private_data_source`。
4. 验证日期顺序、重复值、缺失值、复权口径和基准对齐。
5. 分别验证强制网络、强制本地和普通模式。

## AKShare A 股日线

`ABuDataFeedAkShare.AKShareCNApi` 提供沪深 A 股和四个内置基准指数的日线适配。默认使用前复权数据，结果仍由 `ABuDataCache` 保存到 `~/abu/data/csv`。适配器按东方财富、腾讯、新浪的顺序回退，特殊 CDR 使用 AKShare 专用接口，并统一将成交量保存为“股”。

注册 AKShare 后，`ABuMarket.all_symbol()` 和 `abu.run_kl_update()` 会使用 AKShare 缓存的动态沪深股票池，不再使用仓库内的旧静态 A 股列表。

代码中注册数据源：

```python
from abupy.MarketBu import use_akshare

use_akshare(adjust='qfq')
```

下载近一年沪深全市场数据：

```bash
.venv/bin/python scripts/download_akshare_cn.py
```

下载少量标的进行验证：

```bash
.venv/bin/python scripts/download_akshare_cn.py \
  --symbols sh600000,sz000001 --no-index --start 2024-01-01 --end 2024-12-31
```

脚本默认复用满足日期范围的本地缓存；使用 `--refresh` 可以强制重新请求。交易所股票池缓存在 `~/abu/data/cache/akshare_cn_stock_info.csv`，需要更新时使用 `--refresh-universe`。每轮完成后，下载口径和结果会写入 `~/abu/data/cache/akshare_download_manifest.json`。默认使用 4 个独立进程，隔离 AKShare 的复权运行时；`--executor thread` 只适合当前网络能够稳定访问东方财富接口、不需要新浪回退的环境。当前 ABU 市场枚举不包含北交所，因此股票池只包含上交所主板、科创板以及深交所 A 股。

内置在线数据源主要用于示例，服务可用性和字段稳定性没有保证。业务开发应优先接入有契约的数据服务。

## 相关模块

[CoreBu](../CoreBu/README.md) 保存市场设置，[RomDataBu](../RomDataBu/README.md) 提供样例数据，[TradeBu](../TradeBu/README.md) 通过 `AbuKLManager` 管理策略行情。
