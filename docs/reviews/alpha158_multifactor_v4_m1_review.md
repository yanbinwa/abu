# Alpha158 多因子 v4 M1 证据链审查

## 目标

M1 防止预测缓存、来源报告、数据快照和配置被静默混用。该层不判断收益，只验证研究产物能否追溯到唯一输入。

当前状态：`M1_VERIFIED / RESEARCH_ONLY`。

## 实现

- `ABuArtifactManifest` 使用规范化 JSON 计算内容哈希；
- manifest 绑定 Git 状态、数据快照、八类配置、训练/校准/测试区间、环境、输入、输出和父 manifest；
- 预测文件默认绑定同名 `.manifest.json`；
- 文件内容变化一个字节、数据快照不符、配置不符或父产物不符都会拒绝；
- 文件移动后只有通过显式 `ROLE=PATH` 重定位且内容哈希一致才允许读取；
- manifest 采用新建写入，拒绝覆盖已有文件；
- 试验登记支持 `manifest_sha256` 和 `parent_trial_id`，父试验必须先登记；
- 正式接口不存在 `allow_unverified_input` 绕过参数。

## 自测

```bash
.venv/bin/python -m unittest tests.test_artifact_manifest tests.test_multifactor_v4 tests.test_walk_forward -v
.venv/bin/python -m compileall -q abupy scripts tests
git diff --check
```

专项测试与全量回归均通过后，本阶段才保持 `M1_VERIFIED` 状态。
