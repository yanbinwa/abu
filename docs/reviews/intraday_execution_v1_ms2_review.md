# 日线选股与分钟执行系统 v1 — MS2 Review

## 结果

实现不可变分钟批次、版本化 manifest、原子 `CURRENT` 指针、文件锁、逻辑去重、数据修订、`as_of` 读取和分区审计，并提供采集与审计 CLI。

## 并发与恢复契约

- 历史批次文件写入后不修改。
- 同一分区写入通过 advisory file lock 串行化。
- manifest 写完并校验后才原子更新 `CURRENT`。
- 进程在更新指针前中断最多留下未引用文件，读者不会看到半批数据。
- 相同行情内容的重复轮询不新增事件；OHLCV 变化生成新 revision。

## 格式说明

当前环境未安装 `pyarrow` 或 `fastparquet`，v1 使用规范 JSONL 不可变批次。存储接口和 manifest 不依赖具体列式格式，后续可在不改变事件契约的情况下增加 Parquet 编码。
