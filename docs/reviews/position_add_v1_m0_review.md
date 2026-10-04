# 加仓策略插件 v1 M0 Review

## 结论

M0 通过。当前 Alpha158、VCP、配置、预测、代码提交、脏工作树补丁、未跟踪文件和 Python 运行环境已经冻结，可以进入 M1 schema 开发。

## 基线

- 基线目录：`/Users/wjy/abu/backtests/position_add_v1_baseline_20261004`
- `baseline_id`：`152a780f6594c0742e430a7f9b467ebd6fa673e3bb8cd75aaf14e6218290aa4b`
- Git HEAD：`39fce61de9318fa24058063d9e9c7a44f25794b8`
- Python：项目 `.venv/bin/python`
- 执行模式：`pit_corrected`
- 滑点：25 bp
- 动态止损同步：关闭的正式 NoAdd 基线；开启版本保留为独立研究情景

工作树在冻结时存在未提交修改，manifest 同时保存 `tracked_patch_sha256`、全部未跟踪文件哈希和总 `working_tree_sha256`，没有把 HEAD 错误描述为完整代码状态。

## 冻结输入

- VCP NoAdd 基线；
- Alpha158 NoAdd 基线；
- Alpha158 OOS 预测；
- execution、risk、Alpha158 policy、VCP core 和 residual 配置。

## 实现

- `scripts/freeze_position_add_baseline.py`
- `scripts/compare_position_add_golden.py`
- `tests/test_position_add_research.py`

## 自测

```text
test_path_hash_is_stable_and_changes_with_content ... ok
test_comparator_sorts_by_business_key_and_uses_tolerance ... ok
test_comparator_reports_first_material_difference ... ok
Ran 3 tests ... OK
```

全项目开发前基线：

```text
Ran 239 tests ... OK
```

比较器对冻结 VCP fills 自比较：284 行、20 个公共字段、0 个差异。

## 环境说明

系统 `/usr/bin/python3` 和 Codex bundled Python 缺少项目测试依赖。项目 `.venv` 包含 NumPy、pandas、matplotlib 和 sklearn；当前仓库测试使用 `unittest`，后续统一使用：

```bash
.venv/bin/python -m unittest ...
```

## 门禁

- 基线身份完整：通过；
- 输入哈希稳定：通过；
- 比较器能识别经济差异：通过；
- 当前测试基线可复现：通过；
- 允许进入 M1：是。
