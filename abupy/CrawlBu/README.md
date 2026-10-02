# CrawlBu 模块开发说明

CrawlBu 用于采集证券列表、行业导航和股票基础信息，并把结果转换为 MarketBu 可读取的静态文件。该模块不是回测主链路的一部分，运行依赖网页结构和浏览器驱动。

## 文件索引

| 文件 | 作用 |
| --- | --- |
| `ABuXqCrawl.py` | 更新、查询和批量采集入口 |
| `ABuXqCrawlImp.py` | Selenium 浏览器采集实现 |
| `ABuXqApi.py` | 页面地址和市场 URL 组织 |
| `ABuXqFile.py` | 股票列表与信息缓存的读写和清洗 |
| `ABuXqConsts.py` | 页面字段和采集常量 |

## 运行设置

浏览器驱动路径由 `CoreBu.ABuEnv.g_crawl_chrome_driver` 提供。相关功能还需要 Selenium、lxml、Chrome 及匹配的驱动版本，这些依赖不在核心回测环境中默认安装。

## 使用边界

网页结构、访问策略和字段名称可能变化。运行前应确认目标站点允许相应访问，并限制请求频率。采集结果必须经过人工抽样和字段校验后才能替换 `RomDataBu` 数据。

## 维护规则

- 页面选择器和字段映射分离，便于页面变化时定位。
- 原始响应和清洗后数据分开保存，失败标的进入错误清单。
- 更新操作先写临时结果并校验，再替换正式静态文件。
- 不把登录信息、Cookie、代理或密钥提交到仓库。
- 新字段同步更新 MarketBu 的查询接口和文档。

## 相关模块

[MarketBu](../MarketBu/README.md) 消费证券元数据，[RomDataBu](../RomDataBu/README.md) 保存静态结果，[CoreBu](../CoreBu/README.md) 提供驱动配置。
