# 最坏案例逐条审计

按 abs(log1p(预测秒数) − log1p(实际秒数)) 降序。分类是直接证据支持的主因，允许次因；不是全数据错误比例。

每个子句的完整合法候选、历史原始耗时、特征、父边及权重见同名 `results/*-worst30.json.gz`。下列输出片段只用于事后归因，没有输入优化模型。

## lattice_shrinkage

### 1. swe-rebench:tobymao__sqlglot-3630:36

trace `sqlglot-reserved100__tobymao__sqlglot-3630.trace.jsonl:227`；action `tool_36_call_ycOOURkl7V8ZR7XoPEcWZOo8`。

```sh
python -m venv --system-site-packages .venv && .venv/bin/python -m pip install 'maturin>=1.4,<2.0' && PATH=/testbed/.venv/bin:$PATH VIRTUAL_ENV=/testbed/.venv make install-dev-rs && PATH=/testbed/.venv/bin:$PATH VIRTUAL_ENV=/testbed/.venv make test-rs
```

实际 0.096502s；预测 142.568661s；绝对误差 142.472159s；log 误差 4.874689；桶 0→4（差 4）。

**主因 B / conditional_stage_composition：** venv 创建失败：ensurepip 不可用；实际只执行首阶段，后续 pip/make 未执行。 研究执行成功概率与条件路径合成；不能事后用真实退出状态选择预测分支。

次因/边界：D: 前序成功与否未观测；C: 部分子句规则/证据不足，但不足以解释未执行阶段被相加。

子句 0：parse=partial，diagnostics=["missing_rule:venv"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "opaque:[\"L1/venv\",[\"--system-site-packages\",\".venv\"]]", "target:[\"venv\"]", "tool=python"], "core": ["target:[\"venv\"]", "tool=python"], "parse_digest": "31ce9177c47af96f00d37fb45b45d78f8f0eee3b4547ac69b7011e0384ea8e07", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 4 个；选中样本 n=2，min/median/max(ms)=3923.54297/4327.285127/4731.027284。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"venv\"]", "tool=python"]
```

Edge node=03eb91fdf59f4ee038312573db0c349f397baa4b178b839cd63cb4b7361dc354，exact=False，覆盖证据 n=0，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 7579b54bae63e90b6f85e7ef643dae84700973f786e96d9a83bdd03b0aaab624 | 12 | 0.5 | 0.4950495 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "opaque:[\"L1/venv\",[\"--system-site-packages\",\".venv\"]]"]` |
| 8ed404ae37b03f3d7ac75f552ee10a53193c41d10c53a28f920a45aa0af5c577 | 2 | 0.5 | 0.4950495 | `["mode:[\"module\"]", "opaque:[\"L1/venv\",[\"--system-site-packages\",\".venv\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 1：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pip\",0,\"maturin>=1.4,<2.0\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pip\",\"install\"]", "tool=python"], "core": ["target:[\"pip\",\"install\"]", "tool=python"], "parse_digest": "db653c18aa8e1a123dd9d15fd463394d7acc2eaa6b3506d23a5a22d750b43ae9", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 4 个；选中样本 n=265，min/median/max(ms)=18.753362/10655.063956/474168.642057。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pip\",\"install\"]", "tool=python"]
```

Edge node=bd1576b68f1e7a5a7f152f2e45cbc9208570865072ad7a8633dcfc7f32038a1d，exact=False，覆盖证据 n=0，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 4584ccbce5f84d9b39bc4e3c9d4c6bb0ec5db6ca673602768dd6abe739075313 | 657 | 0.5 | 0.4950495 | `["arg:[\"L1/pip\",0,\"maturin>=1.4,<2.0\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |
| d916bf659c948517a5be22d337e3d6074b2a6dfd7bb8a3942c26b33c3fba3516 | 265 | 0.5 | 0.4950495 | `["arg:[\"L1/pip\",0,\"maturin>=1.4,<2.0\"]", "mode:[\"module\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 2：parse=complete，diagnostics=[]。

```json
{"tool": "make", "features": ["arg:[\"L0/make\",0,\"install-dev-rs\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "tool=make"], "core": ["tool=make"], "parse_digest": "01917b6d15bd856d34eedf17e6298c65e940e947c967a2dfc7c3978ae8377d1f", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 2 个；选中样本 n=90，min/median/max(ms)=2.471471/7143.396446/189977.527289。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "tool=make"]
```

Edge node=9ea708cd8a9cf00f3b1e320e1e3928db7725421f2fb4d1b3c130caf5e2126434，exact=False，覆盖证据 n=0，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 7acdd054b7fa250be875da1c6d932d5636112219a08b9251f839e0c2292fb577 | 90 | 1 | 0.99009901 | `["arg:[\"L0/make\",0,\"install-dev-rs\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 3：parse=complete，diagnostics=[]。

```json
{"tool": "make", "features": ["arg:[\"L0/make\",0,\"test-rs\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "tool=make"], "core": ["tool=make"], "parse_digest": "c35a96945dc7d1abb62c9d181754fea3116c66209f38248fa8dd58425a50ec78", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=2，min/median/max(ms)=109070.056983/112271.72012750001/115473.383272。选中特征：

```json
["arg:[\"L0/make\",0,\"test-rs\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "tool=make"]
```

Edge node=4da34aad0211e2579209c9699c89c9c35fbf82057b5e105c1846833619775717，exact=True，覆盖证据 n=2，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 7acdd054b7fa250be875da1c6d932d5636112219a08b9251f839e0c2292fb577 | 88 | 0.5 | 0.19920319 | `["arg:[\"L0/make\",0,\"test-rs\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
The virtual environment was not created successfully because ensurepip is not
available.  On Debian/Ubuntu systems, you need to install the python3-venv
package using the following command.

    apt install python3.11-venv

You may need to use sudo with that command.  After installing the python3-venv
package, recreate your virtual environment.

Failing command: /testbed/.venv/bin/python



Exit code: 1
```

### 2. swe-rebench:PennyLaneAI__pennylane-6939:58

trace `pennylane__PennyLaneAI__pennylane-6939.trace.jsonl:359`；action `tool_58_call_ru4FYOEEu3e7PTFKm4iY1Mua`。

```sh
/opt/conda/bin/python -m black --check pennylane/measurements/expval.py pennylane/devices/qubit/measure.py pennylane/devices/qubit_mixed/measure.py pennylane/devices/qutrit_mixed/measure.py pennylane/devices/_qubit_device.py tests/measurements/test_expval.py && /opt/conda/bin/python -m pytest tests/measurements -q
```

实际 0.852566s；预测 220.925690s；绝对误差 220.073124s；log 误差 4.785771；桶 2→4（差 2）。

**主因 B / conditional_stage_composition：** black --check 报告 4 个文件需重排并退出；后续 pytest 未执行。 研究执行成功概率与条件路径合成；不能事后用真实退出状态选择预测分支。

次因/边界：D: 前序成功与否未观测；C: 部分子句规则/证据不足，但不足以解释未执行阶段被相加。

子句 0：parse=partial，diagnostics=["missing_rule:black"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opaque:[\"L1/black\",[\"--check\",\"pennylane/measurements/expval.py\",\"pennylane/devices/qubit/measure.py\",\"pennylane/devices/qubit_mixed/measure.py\",\"pennylane/devices/qutrit_mixed/measure.py\",\"pennylane/devices/_qubit_device.py\",\"tests/measurements/test_expval.py\"]]", "target:[\"black\"]", "tool=python"], "core": ["target:[\"black\"]", "tool=python"], "parse_digest": "53772d81585a6b25553581d8e783cdd83082cc08882e1bb7b4a4912a2d2becdb", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=1，min/median/max(ms)=852.566051/852.566051/852.566051。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opaque:[\"L1/black\",[\"--check\",\"pennylane/measurements/expval.py\",\"pennylane/devices/qubit/measure.py\",\"pennylane/devices/qubit_mixed/measure.py\",\"pennylane/devices/qutrit_mixed/measure.py\",\"pennylane/devices/_qubit_device.py\",\"tests/measurements/test_expval.py\"]]", "target:[\"black\"]", "tool=python"]
```

Edge node=cb1d37720dbe70977b54ebe693ba7f3cf3110fc3f82968fa1bd09472661ba65b，exact=True，覆盖证据 n=1，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| c80d458945e5e7814ce4b34a36c29b7db2f02e4d8bb506fb0d574dda470bcef3 | 35 | 0.33333333 | 0.19880716 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "opaque:[\"L1/black\",[\"--check\",\"pennylane/measurements/expval.py\",\"pennylane/devices/qubit/measure.py\",\"pennylane/devices/qubit_mixed/measure.py\",\"pennylane/devices/qutrit_mixed/measure.py\",\"pennylane/devices/_qubit_device.py\",\"tests/measurements/test_expval.py\"]]"]` |
| d98d70823a72c5d3f0439b19ec78d5b516272e98aa397e1b5dd19d801f835c0c | 35 | 0.33333333 | 0.19880716 | `["mode:[\"module\"]", "opaque:[\"L1/black\",[\"--check\",\"pennylane/measurements/expval.py\",\"pennylane/devices/qubit/measure.py\",\"pennylane/devices/qubit_mixed/measure.py\",\"pennylane/devices/qutrit_mixed/measure.py\",\"pennylane/devices/_qubit_device.py\",\"tests/measurements/test_expval.py\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 1：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests/measurements\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "523b5de51d0fcf6f0e86f833d03a8c05ef780ec279965539de5617b893094d6f", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=1，min/median/max(ms)=220073.124279/220073.124279/220073.124279。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests/measurements\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=7292a8ba8ee3bfab256483578d574fadf15cda5d7ab271e026c75a73c351b41b，exact=True，覆盖证据 n=2，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 7f69b153d3605504aa0d2cd83578b4e8aa444dd8c8b48767643b9367411b2342 | 3 | 0.5 | 0.16611296 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]"]` |
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 640 | 0.5 | 0.16611296 | `["arg:[\"L1/pytest\",0,\"tests/measurements\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
would reformat pennylane/measurements/expval.py
would reformat pennylane/devices/qubit/measure.py
would reformat tests/measurements/test_expval.py
would reformat pennylane/devices/_qubit_device.py

Oh no! 💥 💔 💥
4 files would be reformatted, 2 files would be left unchanged.


Exit code: 1
```

### 3. swe-rebench:PennyLaneAI__pennylane-3483:54

trace `pennylane__PennyLaneAI__pennylane-3483.trace.jsonl:335`；action `tool_54_call_9uPpBhKQ8n2OpVFE4NcAmJQd`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest -q -p no:asyncio tests/returntypes
```

实际 481.231065s；预测 3.391401s；绝对误差 477.839664s；log 误差 4.698775；桶 4→4（差 0）。

**主因 C / unseen_scope_fallback：** 未见 tests/returntypes 与插件组合；回退丢失路径/插件，实际输出 4139 passed、1119 skipped。 保留范围/模式语义并学习可迁移的工作量表示；补充规则本身未必得到范围大小。

次因/边界：B: 粗粒度证据混合；D: 输入规模/缓存未充分观测；具体因果贡献未分离。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests/returntypes\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "sequence:[\"L1/pytest\",[[\"-p\",[\"no:asyncio\"]]]]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "df39d2d1f7610961339ab4b44d6dd2d4dc25f5b62729dceb85d79a19a18b2152", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 8 个；选中样本 n=642，min/median/max(ms)=12.174701/3391.4005875/598046.750194。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=31f5bae619c05657129199f1e344f6dcac16e1d3a7f71e26877ae29e27a80569，exact=False，覆盖证据 n=0，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 642 | 1 | 0.99009901 | `["arg:[\"L1/pytest\",0,\"tests/returntypes\"]", "sequence:[\"L1/pytest\",[[\"-p\",[\"no:asyncio\"]]]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
y Test Report===
=========================== short test summary info ============================
FAILED tests/returntypes/transforms/test_qcut_new.py::TestMCPostprocessing::test_sample_postprocess[torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestMCPostprocessing::test_mc_sample_postprocess[torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitMCTransform::test_all_interfaces_samples[torch-torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitMCTransform::test_all_interfaces_mc[torch-torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestQCutProcessingFn::test_process_tensor[1-torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestQCutProcessingFn::test_process_tensor[2-torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestQCutProcessingFn::test_qcut_processing_fn_torch[True]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestQCutProcessingFn::test_qcut_processing_fn_torch[False]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_torch[True]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_torch[False]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_jax[True]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_jax[False]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_torch_trace[True]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_torch_trace[False]
14 failed, 4139 passed, 1119 skipped, 100 xfailed, 18 xpassed, 87 warnings in 478.86s (0:07:58)


Exit code: 1
```

### 4. swe-rebench:PennyLaneAI__pennylane-3579:45

trace `pennylane__PennyLaneAI__pennylane-3579.trace.jsonl:281`；action `tool_45_call_Ik8uqhboL7HBe5Xe8ooXHdp3`。

```sh
PYTHONPATH=/testbed /opt/conda/envs/testbed/bin/pytest -q tests/returntypes tests/tape tests/transforms
```

实际 597.912368s；预测 5.332746s；绝对误差 592.579621s；log 误差 4.549381；桶 4→4（差 0）。

**主因 C / unseen_scope_fallback：** 三目录组合未命中；回退丢失测试路径，实际接近 598 秒。 保留范围/模式语义并学习可迁移的工作量表示；补充规则本身未必得到范围大小。

次因/边界：B: 粗粒度证据混合；D: 输入规模/缓存未充分观测；具体因果贡献未分离。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "pytest", "features": ["arg:[\"L0/pytest\",0,\"tests/returntypes\"]", "arg:[\"L0/pytest\",1,\"tests/tape\"]", "arg:[\"L0/pytest\",2,\"tests/transforms\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L0/pytest\",\"--quiet\"]", "tool=pytest"], "core": ["tool=pytest"], "parse_digest": "202b342ceff44589d8c9113c6d8424860d4b18e48640f56a1aa24fc366a6c991", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 4 个；选中样本 n=94，min/median/max(ms)=149.544062/5332.746488/274456.763758。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L0/pytest\",\"--quiet\"]", "tool=pytest"]
```

Edge node=88601906ac2d69484cdf46daba1ec88206228e91a63f41218cd6df2222ab578f，exact=False，覆盖证据 n=0，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| ba137b385e55c3d2027cbdf0bab03349555f38be558c3395a06861c30afc50c0 | 94 | 1 | 0.99009901 | `["arg:[\"L0/pytest\",0,\"tests/returntypes\"]", "arg:[\"L0/pytest\",1,\"tests/tape\"]", "arg:[\"L0/pytest\",2,\"tests/transforms\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
rd_circuit[10000000-False] passed 1 out of the required 1 times. Success!
test_cut_circuit_mc_expval passed 1 out of the required 1 times. Success!
test_simple_cut_circuit[None-True] passed 1 out of the required 1 times. Success!
test_simple_cut_circuit[None-False] passed 1 out of the required 1 times. Success!
test_simple_cut_circuit[10000000-True] passed 1 out of the required 1 times. Success!
test_simple_cut_circuit[10000000-False] passed 1 out of the required 1 times. Success!
test_standard_circuit[None-True] passed 1 out of the required 1 times. Success!
test_standard_circuit[None-False] passed 1 out of the required 1 times. Success!
test_standard_circuit[10000000-True] passed 1 out of the required 1 times. Success!
test_standard_circuit[10000000-False] passed 1 out of the required 1 times. Success!

===End Flaky Test Report===
=========================== short test summary info ============================
FAILED tests/returntypes/core/test_new_return_types_qnode.py::TestIntegrationSingleReturn::test_probs[op3-None-lightning.qubit]
1 failed, 5695 passed, 2202 skipped, 100 xfailed, 18 xpassed, 85 warnings in 595.47s (0:09:55)
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 1
```

### 5. swe-rebench:PennyLaneAI__pennylane-2964:66

trace `pennylane__PennyLaneAI__pennylane-2964.trace.jsonl:407`；action `tool_66_call_yS8gOa1KA5HV9mPm5t34MYjM`。

```sh
/tmp/pl310/bin/python -m pytest tests -q -n 8 --tb=short --no-flaky-report
```

实际 0.342041s；预测 115.514810s；绝对误差 115.172769s；log 误差 4.463827；桶 1→4（差 3）。

**主因 D / dependency_or_collection_failure：** conftest 导入失败，缺少 pkg_resources。 在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。

次因/边界：B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验。

子句 0：parse=partial，diagnostics=["unknown_option:--no-flaky-report"]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opaque:[\"L1/pytest\",[\"--no-flaky-report\"]]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"8\"]]", "opt:[\"L1/pytest\",\"--tb\",[\"short\"]]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "a700431156800a0c1f4e9081e7bdde1b07a83541b24188db61adcab5e7fe5865", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=3，min/median/max(ms)=342.041246/115514.810338/121930.64924。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opaque:[\"L1/pytest\",[\"--no-flaky-report\"]]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"8\"]]", "opt:[\"L1/pytest\",\"--tb\",[\"short\"]]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=d204f6233ab739447bc4100398aef3a7a981e2c9cf90faf8cc33420c99cee9e5，exact=True，覆盖证据 n=5，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 107e184f937da86a49f84e76146717d4f309532f7c97bae4d598129bf5bb6115 | 57 | 0.5 | 0.083194676 | `["arg:[\"L1/pytest\",0,\"tests\"]", "opaque:[\"L1/pytest\",[\"--no-flaky-report\"]]", "opt:[\"L1/pytest\",\"--tb\",[\"short\"]]"]` |
| 4480e859e50565c24d58da8cd997a5b58437e39639cb4da00136b550ea63e701 | 9 | 0.5 | 0.083194676 | `["opt:[\"L1/pytest\",\"--numprocesses\",[\"8\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
/tmp/pl310/lib/python3.10/site-packages/_pytest/config/__init__.py:324: PluggyTeardownRaisedWarning: A plugin raised an exception during an old-style hookwrapper teardown.
Plugin: helpconfig, Hook: pytest_cmdline_parse
ConftestImportFailure: ModuleNotFoundError: No module named 'pkg_resources' (from /testbed/tests/conftest.py)
For more information see https://pluggy.readthedocs.io/en/stable/api_reference.html#pluggy.PluggyTeardownRaisedWarning
  config = pluginmanager.hook.pytest_cmdline_parse(
ImportError while loading conftest '/testbed/tests/conftest.py'.
tests/conftest.py:23: in <module>
    import pennylane as qml
pennylane/__init__.py:20: in <module>
    import pkg_resources
E   ModuleNotFoundError: No module named 'pkg_resources'


Exit code: 4
```

### 6. swe-rebench:PennyLaneAI__pennylane-4161:69

trace `pennylane__PennyLaneAI__pennylane-4161.trace.jsonl:425`；action `tool_69_call_PnjFMh11rMN4Y8bqbiTFuhJR`。

```sh
python3 -m pytest --lf -x -vv --tb=long
```

实际 218.490558s；预测 1.851097s；绝对误差 216.639461s；log 误差 4.343605；桶 4→2（差 2）。

**主因 C / unseen_scope_fallback：** --lf/-x/-vv/--tb=long 组合未命中；last-failed 的运行集合还依赖未观测缓存。 保留范围/模式语义并学习可迁移的工作量表示；补充规则本身未必得到范围大小。

次因/边界：B: 粗粒度证据混合；D: 输入规模/缓存未充分观测；具体因果贡献未分离。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "51316ee58afa3ee2f4dc9c1f4c0f30a3ad2d5e0a70f85b052d1a52467aeedf02", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 48 个；选中样本 n=4，min/median/max(ms)=1766.135618/1851.096928/2309.788816。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=b358937ea4c4037148f7020f5c1475091230f89603e61e942ed348285f0b7ebe，exact=False，覆盖证据 n=0，父边 6 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 190515f4090088fcdef68c0a184840f53a7bdfd7e7b953443ec791fef8844fb7 | 875 | 0.16666667 | 0.1650165 | `["flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]"]` |
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 1461 | 0.16666667 | 0.1650165 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]"]` |
| 21c283e9199e3713d8a0a254ca1ba2ff301ef51669228b7a01d73c17c9c0dfc1 | 7 | 0.16666667 | 0.1650165 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]"]` |
| 8f560fbef17d11b2bbe3793bd5766652e80075e864233d7ec2d741c3da68f998 | 44 | 0.16666667 | 0.1650165 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]"]` |
| a5c5e22ed3c4392ba26d52611b7b97aec02d0a50ccc0f0f9f43dff973f81738d | 90 | 0.16666667 | 0.1650165 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]"]` |
| fc8ebc944560c5b69d1607a601818806c9b62f580eded40a30b5e703f7bb70fe | 46 | 0.16666667 | 0.1650165 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
tory_variance_single_param failed (6 runs remaining out of 8).
	<class 'AssertionError'>
	assert array(1.30210091) == 0 ± 1.0e+00
  comparison failed
  Obtained: 1.302100912102162
  Expected: 0 ± 1.0e+00
	[<TracebackEntry /testbed/tests/gradients/parameter_shift/test_parameter_shift_shot_vec.py:1549>]
test_involutory_and_noninvolutory_variance_single_param passed 1 out of the required 1 times. Success!
test_involutory_and_noninvolutory_variance_multi_param failed (7 runs remaining out of 8).
	<class 'AssertionError'>
	assert False
 +  where False = <function primitive.<locals>.f_wrapped at 0x7bf85190de40>(array(-37.66896295), -36.524463611058046, atol=1)
 +    where <function primitive.<locals>.f_wrapped at 0x7bf85190de40> = np.allclose
	[<TracebackEntry /testbed/tests/gradients/parameter_shift/test_parameter_shift_shot_vec.py:1625>]
test_involutory_and_noninvolutory_variance_multi_param failed (6 runs remaining out of 8).
	<class 'AssertionError'>
	assert False
 +  where False = <function primitive.<locals>.f_wrapped at 0x7bf85190de40>(array(-37.64386525), -36.524463611058046, atol=1)
 +    where <function primitive.<locals>.f_wrapped at 0x7bf85190de40> = np.allclose
	[<TracebackEntry /testbed/tests/gradients/parameter_shift/test_parameter_shift_shot_vec.py:1625>]
test_involutory_and_noninvolutory_variance_multi_param passed 1 out of the required 1 times. Success!

===End Flaky Test Report===
=========================== short test summary info ============================
ERROR tests/kernels/test_kernels.py::TestRegularization::test_closest_psd_matrix[input0-False-expected_output0]
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
= 631 passed, 3 skipped, 96 deselected, 9 xfailed, 94 warnings, 1 error in 216.93s (0:03:36) =


Exit code: 1
```

### 7. swe-rebench:PennyLaneAI__pennylane-5063:63

trace `pennylane__PennyLaneAI__pennylane-5063.trace.jsonl:389`；action `tool_63_call_IaaedzEWlRfnSMFCeHrVXzCC`。

```sh
python3 -m pip uninstall -y cvxopt && python3 -m pytest tests/kernels -q --disable-warnings -n auto && python3 -m pytest tests/gradients tests/interfaces tests/kernels tests/logging tests/math tests/measurements tests/numpy tests/optimize -q --disable-warnings -n auto
```

实际 0.322239s；预测 94.435222s；绝对误差 94.112983s；log 误差 4.279121；桶 1→4（差 3）。

**主因 B / conditional_stage_composition：** pip uninstall 被 externally-managed-environment 拒绝；后续两段 pytest 未执行。 研究执行成功概率与条件路径合成；不能事后用真实退出状态选择预测分支。

次因/边界：D: 前序成功与否未观测；C: 部分子句规则/证据不足，但不足以解释未执行阶段被相加。

子句 0：parse=partial，diagnostics=["unknown_option:-y"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opaque:[\"L1/pip\",[\"-y\",\"cvxopt\"]]", "target:[\"pip\",\"uninstall\"]", "tool=python"], "core": ["target:[\"pip\",\"uninstall\"]", "tool=python"], "parse_digest": "ecc049f0253afc5271e97c14ea1d4976c6875b9b8e8c5df08f49e20ed89ee971", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=2，min/median/max(ms)=322.239177/334.0192295/345.799282。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opaque:[\"L1/pip\",[\"-y\",\"cvxopt\"]]", "target:[\"pip\",\"uninstall\"]", "tool=python"]
```

Edge node=3e743f70942ac162180c5117386e6ffc64bc1ba2f2ece266d24a141cc6eb530a，exact=True，覆盖证据 n=2，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1920f35179c774132b6314b58ad2196853e176305595e296dadc202a9ec8a461 | 17 | 0.33333333 | 0.124533 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "opaque:[\"L1/pip\",[\"-y\",\"cvxopt\"]]"]` |
| ad1f4eb088605a9638553146ed4b9d9a90027cfeb881a5b0db14a34c72a31e83 | 17 | 0.33333333 | 0.124533 | `["mode:[\"module\"]", "opaque:[\"L1/pip\",[\"-y\",\"cvxopt\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 1：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests/kernels\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "98e5b73faf00f3202e5201a70dc1ed7db57d505341812b0e34b84ff36ac3d7dc", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=4，min/median/max(ms)=387.568296/6330.4977245/6576.099279。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests/kernels\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=0aff8bb45a44d0f983f4dce25c433985dca816781b1c121084787ed49e708dd1，exact=True，覆盖证据 n=4，父边 3 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 377f98307e640658cbf9769bc56e5a308d583460a6cab313bd2ab8dbbfe2818e | 37 | 0.33333333 | 0.066533599 | `["arg:[\"L1/pytest\",0,\"tests/kernels\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]"]` |
| 5b8cb3e64ab8a5e5ce6270629683f61f76c840001563c0d84686b27a7d1c61d2 | 41 | 0.33333333 | 0.066533599 | `["arg:[\"L1/pytest\",0,\"tests/kernels\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| 807af1dccf64c0ab6a809f3cb43b9c88d2f1338a7badc59df5835d1247e830ee | 2 | 0.33333333 | 0.066533599 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 2：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "6d4d1d0f9e35968a77ae73dd5b39312dbe083246e5637aaaf87db47bce851310", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=2，min/median/max(ms)=87640.280897/90406.544572/93172.808247。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=2d175bacc004f51a663d5d6c4eab86f428a9ae7460bdc35edd0a608ce64f5735，exact=True，覆盖证据 n=2，父边 7 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 3553ae23052258226a887cc4e1f70def9daecfe4f3bae683f9ee0fa92b845d8f | 1 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| 377f98307e640658cbf9769bc56e5a308d583460a6cab313bd2ab8dbbfe2818e | 39 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]"]` |
| 46644b15b622484b153cd380265f9b14dd51165dfeee27e13c6e0dbfac2b335a | 2 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| 5b8cb3e64ab8a5e5ce6270629683f61f76c840001563c0d84686b27a7d1c61d2 | 43 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| b83e057ea7c8d98be79b91bf1e376a8bec5957691375674c9d7dcf47316691a4 | 7 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| c335df336001eac1fa2ae8b4d926c8ee01b937f43bdafd21ca8345eb26963411 | 8 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| c8810c02b785f9016ecc91d2f9a50b8eea4ba18206a1d5a573803330fa3222fa | 3 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
error: externally-managed-environment

× This environment is externally managed
╰─> To install Python packages system-wide, try apt install
    python3-xyz, where xyz is the package you are trying to
    install.
    
    If you wish to install a non-Debian-packaged Python package,
    create a virtual environment using python3 -m venv path/to/venv.
    Then use path/to/venv/bin/python and path/to/venv/bin/pip. Make
    sure you have python3-full installed.
    
    If you wish to install a non-Debian packaged Python application,
    it may be easiest to use pipx install xyz, which will manage a
    virtual environment for you. Make sure you have pipx installed.
    
    See /usr/share/doc/python3.11/README.venv for more information.

note: If you believe this is a mistake, please contact your Python installation or OS distribution provider. You can override this, at the risk of breaking your Python installation or OS, by passing --break-system-packages.
hint: See PEP 668 for the detailed specification.


Exit code: 1
```

### 8. swe-rebench:tobymao__sqlglot-3290:30

trace `sqlglot-reserved100__tobymao__sqlglot-3290.trace.jsonl:191`；action `tool_30_call_ab7mxlE4V9BzeLtLorRMkLHl`。

```sh
SQLGLOTRS_TOKENIZER=0 python3 -m pytest -q tests/dialects/test_presto.py && SQLGLOTRS_TOKENIZER=0 python3 -m pytest -q tests
```

实际 0.673039s；预测 100.174644s；绝对误差 99.501604s；log 误差 4.102206；桶 2→4（差 2）。

**主因 B / conditional_stage_composition：** 首段 Presto 测试失败；&& 后的全量测试未执行。 研究执行成功概率与条件路径合成；不能事后用真实退出状态选择预测分支。

次因/边界：D: 前序成功与否未观测；C: 部分子句规则/证据不足，但不足以解释未执行阶段被相加。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests/dialects/test_presto.py\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "7a0114b78b63a865cf340fd126a111899f6c15c20d0c8bf9d29c3f9d8f61a2d9", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=5，min/median/max(ms)=28.770275/829.907187/1468.797311。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests/dialects/test_presto.py\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5fa1add3866cb178e0f852e45e6543c75d139e5e4790658bacf835b9a6a51536，exact=True，覆盖证据 n=5，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 113978e3334b282efaab29572d05a1663a98699cb1a2ef751845f877201a22a6 | 208 | 0.5 | 0.083194676 | `["arg:[\"L1/pytest\",0,\"tests/dialects/test_presto.py\"]"]` |
| 423806c0d1bc767e95b7b8d757098feb6e3f7808526fa2a43fdab098f83affb4 | 5 | 0.5 | 0.083194676 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 1：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "03f9a29ba4d1c2260a54b433135f9f3ba06d17e8dd3597bf0907c04fbf4e942a", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=4，min/median/max(ms)=1251.049907/116564.637323/140801.083208。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=9b743075ed85cc82b441120bd707dbc36308b39402a621c16613c8ba5c6527e2，exact=True，覆盖证据 n=4，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 113978e3334b282efaab29572d05a1663a98699cb1a2ef751845f877201a22a6 | 209 | 0.5 | 0.099800399 | `["arg:[\"L1/pytest\",0,\"tests\"]"]` |
| 1d63ff3e0ccf15b6351fed758b8aa2181c80a2d33c946b2e5475a0d6c7be5a23 | 8 | 0.5 | 0.099800399 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
============
______________________ TestPresto.test_group_by_distinct _______________________

self = <tests.dialects.test_presto.TestPresto testMethod=test_group_by_distinct>

    def test_group_by_distinct(self):
>       self.validate_identity(
            "SELECT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'), COUNT(*) "
            "FROM table_a "
            "GROUP BY DISTINCT(JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'))",
            "SELECT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'), COUNT(*) "
            "FROM table_a "
            "GROUP BY DISTINCT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b')",
        )

tests/dialects/test_presto.py:578: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
tests/dialects/test_dialect.py:31: in validate_identity
    self.assertEqual(write_sql or sql, expression.sql(dialect=self.dialect, pretty=pretty))
E   AssertionError: "SELE[91 chars]STINCT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b')" != "SELE[91 chars]STINCT (JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'))"
E   - SELECT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'), COUNT(*) FROM table_a GROUP BY DISTINCT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b')
E   + SELECT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'), COUNT(*) FROM table_a GROUP BY DISTINCT (JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'))
E   ?                                                                                                       +                                                     +
=========================== short test summary info ============================
FAILED tests/dialects/test_presto.py::TestPresto::test_group_by_distinct - As...
1 failed, 15 passed in 0.33s


Exit code: 1
```

### 9. swe-rebench:tobymao__sqlglot-4096:32

trace `sqlglot-dev100__tobymao__sqlglot-4096.trace.jsonl:203`；action `tool_32_call_UMXnPf2FOHT2ohNgRwmQqrcy`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python3.9 -m pytest
```

实际 144.138322s；预测 1.732315s；绝对误差 142.406006s；log 误差 3.972538；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "cce49b17dd9899820dda2768a9aec64d8b366fbc041bddf3ae7339185fd561ba", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 68%]
tests/test_helper.py ...                                                 [ 69%]
tests/test_jsonpath.py ...                                               [ 69%]
tests/test_lineage.py ..................                                 [ 72%]
tests/test_optimizer.py ................................................ [ 80%]
.......                                                                  [ 81%]
tests/test_parser.py ................................................... [ 90%]
....                                                                     [ 90%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 596 passed, 2 skipped in 143.26s (0:02:23) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 10. swe-rebench:tobymao__sqlglot-3290:24

trace `sqlglot-reserved100__tobymao__sqlglot-3290.trace.jsonl:155`；action `tool_24_call_Xe5XbEvVV0ceUmfWvJ9DXqDO`。

```sh
SQLGLOTRS_TOKENIZER=0 python3 -m pytest -q tests
```

实际 1.251050s；预测 116.564637s；绝对误差 115.313587s；log 误差 3.955592；桶 2→4（差 2）。

**主因 D / dependency_or_collection_failure：** pytest 在收集阶段因 5 个 collection errors 中止。 在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。

次因/边界：B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "03f9a29ba4d1c2260a54b433135f9f3ba06d17e8dd3597bf0907c04fbf4e942a", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=4，min/median/max(ms)=1251.049907/116564.637323/140801.083208。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=9b743075ed85cc82b441120bd707dbc36308b39402a621c16613c8ba5c6527e2，exact=True，覆盖证据 n=4，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 113978e3334b282efaab29572d05a1663a98699cb1a2ef751845f877201a22a6 | 209 | 0.5 | 0.099800399 | `["arg:[\"L1/pytest\",0,\"tests\"]"]` |
| 1d63ff3e0ccf15b6351fed758b8aa2181c80a2d33c946b2e5475a0d6c7be5a23 | 8 | 0.5 | 0.099800399 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
_grouped_data.py _______
ImportError while importing test module '/testbed/tests/dataframe/integration/test_grouped_data.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/usr/lib/python3.11/importlib/__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests/dataframe/integration/test_grouped_data.py:1: in <module>
    from pyspark.sql import functions as F
E   ModuleNotFoundError: No module named 'pyspark'
_________ ERROR collecting tests/dataframe/integration/test_session.py _________
ImportError while importing test module '/testbed/tests/dataframe/integration/test_session.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/usr/lib/python3.11/importlib/__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests/dataframe/integration/test_session.py:1: in <module>
    from pyspark.sql import functions as F
E   ModuleNotFoundError: No module named 'pyspark'
=============================== warnings summary ===============================
tests/test_expressions.py:1090
  /testbed/tests/test_expressions.py:1090: DeprecationWarning: invalid escape sequence '\.'
    AssertionError, "x is not <class 'sqlglot.expressions.Identifier'>\."

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
ERROR tests/test_executor.py
ERROR tests/test_optimizer.py
ERROR tests/dataframe/integration/test_dataframe.py
ERROR tests/dataframe/integration/test_grouped_data.py
ERROR tests/dataframe/integration/test_session.py
!!!!!!!!!!!!!!!!!!! Interrupted: 5 errors during collection !!!!!!!!!!!!!!!!!!!!
1 warning, 5 errors in 0.88s


Exit code: 2
```

### 11. swe-rebench:tobymao__sqlglot-4148:19

trace `sqlglot-dev100__tobymao__sqlglot-4148.trace.jsonl:125`；action `tool_19_call_0ZbpRXfwTYTPDLniwYtX8yPn`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest
```

实际 141.639273s；预测 1.732315s；绝对误差 139.906958s；log 误差 3.955170；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 68%]
tests/test_helper.py ...                                                 [ 69%]
tests/test_jsonpath.py ...                                               [ 69%]
tests/test_lineage.py ..................                                 [ 72%]
tests/test_optimizer.py ................................................ [ 80%]
.......                                                                  [ 81%]
tests/test_parser.py ................................................... [ 90%]
....                                                                     [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 599 passed, 2 skipped in 140.68s (0:02:20) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 12. swe-rebench:PennyLaneAI__pennylane-3921:53

trace `pennylane__PennyLaneAI__pennylane-3921.trace.jsonl:329`；action `tool_53_call_OxQ0UFIovJZKLaH9IMbCiYP9`。

```sh
/tmp/pl310/bin/python -m pytest tests/test_*.py tests/circuit_graph tests/collections tests/data tests/docs tests/drawer tests/fourier tests/gpu tests/grouping tests/kernels tests/math tests/measurements tests/numpy tests/optimize tests/pauli tests/pulse tests/qinfo tests/qnn tests/resource tests/shadow tests/tape -q -n 4
```

实际 212.804305s；预测 3.145300s；绝对误差 209.659005s；log 误差 3.943086；桶 4→4（差 0）。

**主因 C / unseen_scope_fallback：** 未展开的 glob 使参数保守保留为 opaque；回退丢失测试范围，实际 8814 passed、3140 skipped。 保留范围/模式语义并学习可迁移的工作量表示；补充规则本身未必得到范围大小。

次因/边界：B: 粗粒度证据混合；D: 输入规模/缓存未充分观测；具体因果贡献未分离。

子句 0：parse=partial，diagnostics=["unresolved_expansion"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opaque:[\"L1/pytest\",[\"tests/test_*.py\",\"tests/circuit_graph\",\"tests/collections\",\"tests/data\",\"tests/docs\",\"tests/drawer\",\"tests/fourier\",\"tests/gpu\",\"tests/grouping\",\"tests/kernels\",\"tests/math\",\"tests/measurements\",\"tests/numpy\",\"tests/optimize\",\"tests/pauli\",\"tests/pulse\",\"tests/qinfo\",\"tests/qnn\",\"tests/resource\",\"tests/shadow\",\"tests/tape\",\"-q\",\"-n\",\"4\"]]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "aa8f2dfb3222204f873a2984ee4bb0838b3bfce5508cdc4e7f01e076a36fac34", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 4 个；选中样本 n=875，min/median/max(ms)=0.0/3145.299989/598046.750194。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=a5707c3439aadb842b5ca727f100e3c2eb60498954a9be55c594be79baa3190d，exact=False，覆盖证据 n=0，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 190515f4090088fcdef68c0a184840f53a7bdfd7e7b953443ec791fef8844fb7 | 875 | 0.5 | 0.4950495 | `["mode:[\"module\"]", "opaque:[\"L1/pytest\",[\"tests/test_*.py\",\"tests/circuit_graph\",\"tests/collections\",\"tests/data\",\"tests/docs\",\"tests/drawer\",\"tests/fourier\",\"tests/gpu\",\"tests/grouping\",\"tests/kernels\",\"tests/math\",\"tests/measurements\",\"tests/numpy\",\"tests/optimize\",\"tests/pauli\",\"tests/pulse\",\"tests/qinfo\",\"tests/qnn\",\"tests/resource\",\"tests/shadow\",\"tests/tape\",\"-q\",\"-n\",\"4\"]]"]` |
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 1461 | 0.5 | 0.4950495 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "opaque:[\"L1/pytest\",[\"tests/test_*.py\",\"tests/circuit_graph\",\"tests/collections\",\"tests/data\",\"tests/docs\",\"tests/drawer\",\"tests/fourier\",\"tests/gpu\",\"tests/grouping\",\"tests/kernels\",\"tests/math\",\"tests/measurements\",\"tests/numpy\",\"tests/optimize\",\"tests/pauli\",\"tests/pulse\",\"tests/qinfo\",\"tests/qnn\",\"tests/resource\",\"tests/shadow\",\"tests/tape\",\"-q\",\"-n\",\"4\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
nfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires1-True-param6-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires1-True-param7-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires1-True-param8-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires1-True-param9-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param0-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param1-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param2-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param3-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param4-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param5-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param6-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param7-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param8-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param9-lightning.qubit]
136 failed, 8814 passed, 3140 skipped, 10 xfailed, 2 xpassed, 1904 warnings in 211.00s (0:03:30)


Exit code: 1
```

### 13. swe-rebench:tobymao__sqlglot-4523:19

trace `sqlglot-dev100__tobymao__sqlglot-4523.trace.jsonl:125`；action `tool_19_call_22ty15nwaJqScwf1cr3TeVTS`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest
```

实际 139.442794s；预测 1.732315s；绝对误差 137.710479s；log 误差 3.939651；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 70%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 71%]
tests/test_lineage.py ..................                                 [ 74%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
.....                                                                    [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 94%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 638 passed, 2 skipped in 138.32s (0:02:18) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 14. swe-rebench:tobymao__sqlglot-4331:23

trace `sqlglot-dev100__tobymao__sqlglot-4331.trace.jsonl:149`；action `tool_23_call_HmyUW9g4wBgwiuIuEad3iHn3`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 138.676089s；预测 1.732315s；绝对误差 136.943773s；log 误差 3.934177；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 69%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 70%]
tests/test_lineage.py ..................                                 [ 73%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
....                                                                     [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 93%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 622 passed, 2 skipped in 137.75s (0:02:17) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 15. swe-rebench:tobymao__sqlglot-4087:20

trace `sqlglot-dev100__tobymao__sqlglot-4087.trace.jsonl:131`；action `tool_20_call_r1IL1PeMXCyOmFuaAn8YYZY9`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 137.617322s；预测 1.732315s；绝对误差 135.885006s；log 误差 3.926568；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 68%]
tests/test_helper.py ...                                                 [ 69%]
tests/test_jsonpath.py ...                                               [ 69%]
tests/test_lineage.py ..................                                 [ 72%]
tests/test_optimizer.py ................................................ [ 80%]
.......                                                                  [ 81%]
tests/test_parser.py ................................................... [ 90%]
....                                                                     [ 90%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 596 passed, 2 skipped in 136.75s (0:02:16) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 16. swe-rebench:PennyLaneAI__pennylane-1072:31

trace `pennylane__PennyLaneAI__pennylane-1072.trace.jsonl:197`；action `tool_31_call_9HWYGs4CfTqn7qiOn9QDbdMz`。

```sh
/tmp/plenv/bin/python -m pytest -q tests
```

实际 0.273888s；预测 62.647874s；绝对误差 62.373986s；log 误差 3.911293；桶 1→4（差 3）。

**主因 D / dependency_or_collection_failure：** conftest 导入失败，缺少 pkg_resources。 在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。

次因/边界：B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "ea7e4470f2dbb2d96f714b21860b89db3e25071e58a7ab5f50ae9563c92817a1", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=25，min/median/max(ms)=273.887685/62647.874033/518569.221292。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=692554a5e50c2c71b88d46897dbea65f4183e56c3bf0335c13ab78dae9e76c67，exact=True，覆盖证据 n=57，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 395b817eac7f1643b730121329a00276d9476d5f0acd98589dedeb7915488ac0 | 62 | 0.5 | 0.0086192036 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 585 | 0.5 | 0.0086192036 | `["arg:[\"L1/pytest\",0,\"tests\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
/tmp/plenv/lib/python3.11/site-packages/_pytest/config/__init__.py:331: PluggyTeardownRaisedWarning: A plugin raised an exception during an old-style hookwrapper teardown.
Plugin: helpconfig, Hook: pytest_cmdline_parse
ConftestImportFailure: ModuleNotFoundError: No module named 'pkg_resources' (from /testbed/tests/conftest.py)
For more information see https://pluggy.readthedocs.io/en/stable/api_reference.html#pluggy.PluggyTeardownRaisedWarning
  config = pluginmanager.hook.pytest_cmdline_parse(
ImportError while loading conftest '/testbed/tests/conftest.py'.
tests/conftest.py:22: in <module>
    import pennylane as qml
pennylane/__init__.py:19: in <module>
    import pkg_resources
E   ModuleNotFoundError: No module named 'pkg_resources'


Exit code: 4
```

### 17. swe-rebench:tobymao__sqlglot-4506:21

trace `sqlglot-dev100__tobymao__sqlglot-4506.trace.jsonl:137`；action `tool_21_call_jTkirtR5pSCQC7jEQh2kZu48`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest
```

实际 134.801276s；预测 1.732315s；绝对误差 133.068961s；log 误差 3.906043；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                 [ 48%]
tests/dialects/test_tsql.py ............................................ [ 55%]
...                                                                      [ 55%]
tests/test_build.py .                                                    [ 55%]
tests/test_diff.py ...........                                           [ 57%]
tests/test_executor.py ....................                              [ 60%]
tests/test_expressions.py .............................................. [ 67%]
.............                                                            [ 69%]
tests/test_generator.py ....                                             [ 70%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 71%]
tests/test_lineage.py ..................                                 [ 74%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
.....                                                                    [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 94%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 637 passed, 2 skipped in 133.91s (0:02:13) ==================


Exit code: 0
```

### 18. swe-rebench:tobymao__sqlglot-4505:22

trace `sqlglot-dev100__tobymao__sqlglot-4505.trace.jsonl:143`；action `tool_22_call_LJsmzX1wNDsernKZObr3NdHs`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest
```

实际 134.736486s；预测 1.732315s；绝对误差 133.004171s；log 误差 3.905566；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 70%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 71%]
tests/test_lineage.py ..................                                 [ 74%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
.....                                                                    [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 94%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 637 passed, 2 skipped in 133.91s (0:02:13) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 19. swe-rebench:PennyLaneAI__pennylane-1325:40

trace `pennylane__PennyLaneAI__pennylane-1325.trace.jsonl:251`；action `tool_40_call_THppHkuXbYATeJAkLEbXINTo`。

```sh
/opt/conda/envs/pltest/bin/python -m pytest -q tests
```

实际 0.290326s；预测 62.647874s；绝对误差 62.357548s；log 误差 3.898471；桶 1→4（差 3）。

**主因 D / dependency_or_collection_failure：** conftest 导入失败，缺少 pkg_resources。 在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。

次因/边界：B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "e9d2f3aa849ae0735d07038d87b72a2dee73ec9a90355d372fed2812c6404d4b", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=25，min/median/max(ms)=273.887685/62647.874033/518569.221292。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=692554a5e50c2c71b88d46897dbea65f4183e56c3bf0335c13ab78dae9e76c67，exact=True，覆盖证据 n=57，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 395b817eac7f1643b730121329a00276d9476d5f0acd98589dedeb7915488ac0 | 62 | 0.5 | 0.0086192036 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 585 | 0.5 | 0.0086192036 | `["arg:[\"L1/pytest\",0,\"tests\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
/opt/conda/envs/pltest/lib/python3.10/site-packages/_pytest/config/__init__.py:331: PluggyTeardownRaisedWarning: A plugin raised an exception during an old-style hookwrapper teardown.
Plugin: helpconfig, Hook: pytest_cmdline_parse
ConftestImportFailure: ModuleNotFoundError: No module named 'pkg_resources' (from /testbed/tests/conftest.py)
For more information see https://pluggy.readthedocs.io/en/stable/api_reference.html#pluggy.PluggyTeardownRaisedWarning
  config = pluginmanager.hook.pytest_cmdline_parse(
ImportError while loading conftest '/testbed/tests/conftest.py'.
tests/conftest.py:22: in <module>
    import pennylane as qml
pennylane/__init__.py:21: in <module>
    import pkg_resources
E   ModuleNotFoundError: No module named 'pkg_resources'


Exit code: 4
```

### 20. swe-rebench:tobymao__sqlglot-4457:17

trace `sqlglot-dev100__tobymao__sqlglot-4457.trace.jsonl:113`；action `tool_17_call_M8bah0RWl7U9jg5YlloxeMcw`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 133.623090s；预测 1.732315s；绝对误差 131.890775s；log 误差 3.897330；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                 [ 48%]
tests/dialects/test_tsql.py ............................................ [ 55%]
..                                                                       [ 55%]
tests/test_build.py .                                                    [ 55%]
tests/test_diff.py ...........                                           [ 57%]
tests/test_executor.py ....................                              [ 60%]
tests/test_expressions.py .............................................. [ 67%]
.............                                                            [ 69%]
tests/test_generator.py ....                                             [ 70%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 71%]
tests/test_lineage.py ..................                                 [ 74%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
.....                                                                    [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 94%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 635 passed, 2 skipped in 131.46s (0:02:11) ==================


Exit code: 0
```

### 21. swe-rebench:tobymao__sqlglot-4488:14

trace `sqlglot-dev100__tobymao__sqlglot-4488.trace.jsonl:95`；action `tool_14_call_D8oHlpU1q9YCNGqDSnYTS84H`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 132.770223s；预测 1.732315s；绝对误差 131.037907s；log 误差 3.890974；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 70%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 71%]
tests/test_lineage.py ..................                                 [ 74%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
.....                                                                    [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 94%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 637 passed, 2 skipped in 131.88s (0:02:11) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 22. swe-rebench:tobymao__sqlglot-3870:20

trace `sqlglot-dev100__tobymao__sqlglot-3870.trace.jsonl:131`；action `tool_20_call_5aFZLw2VRVu705tmk1vBdLCY`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest
```

实际 131.263971s；预测 1.732315s；绝对误差 129.531656s；log 误差 3.879650；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 67%]
tests/test_helper.py ...                                                 [ 67%]
tests/test_jsonpath.py ...                                               [ 68%]
tests/test_lineage.py ..................                                 [ 71%]
tests/test_optimizer.py ................................................ [ 80%]
.......                                                                  [ 81%]
tests/test_parser.py ................................................... [ 90%]
...                                                                      [ 90%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py .                                                     [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

======================= 567 passed in 130.37s (0:02:10) ========================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 23. swe-rebench:tobymao__sqlglot-4172:18

trace `sqlglot-dev100__tobymao__sqlglot-4172.trace.jsonl:119`；action `tool_18_call_XSA8jw2lxgaso682RkWSQHGh`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 130.025104s；预测 1.732315s；绝对误差 128.292789s；log 误差 3.870240；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 69%]
tests/test_helper.py ...                                                 [ 69%]
tests/test_jsonpath.py ...                                               [ 70%]
tests/test_lineage.py ..................                                 [ 73%]
tests/test_optimizer.py ................................................ [ 80%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
....                                                                     [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 608 passed, 2 skipped in 129.18s (0:02:09) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 24. swe-rebench:tobymao__sqlglot-4067:15

trace `sqlglot-dev100__tobymao__sqlglot-4067.trace.jsonl:101`；action `tool_15_call_HahSUEZat1n3ownHf01gV2kA`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 129.687982s；预测 1.732315s；绝对误差 127.955667s；log 误差 3.867663；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                 [ 45%]
tests/dialects/test_tsql.py ............................................ [ 52%]
                                                                         [ 52%]
tests/test_build.py .                                                    [ 52%]
tests/test_diff.py .........                                             [ 54%]
tests/test_executor.py ....................                              [ 57%]
tests/test_expressions.py .............................................. [ 65%]
.............                                                            [ 67%]
tests/test_generator.py ....                                             [ 68%]
tests/test_helper.py ...                                                 [ 68%]
tests/test_jsonpath.py ...                                               [ 69%]
tests/test_lineage.py ..................                                 [ 72%]
tests/test_optimizer.py ................................................ [ 80%]
.......                                                                  [ 81%]
tests/test_parser.py ................................................... [ 90%]
....                                                                     [ 90%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 592 passed, 2 skipped in 129.02s (0:02:09) ==================


Exit code: 0
```

### 25. swe-rebench:PennyLaneAI__pennylane-2601:24

trace `pennylane__PennyLaneAI__pennylane-2601.trace.jsonl:155`；action `tool_24_call_vi33vF4z5MxEfAN46ofeytPG`。

```sh
python3 -m pytest -q tests
```

实际 0.337258s；预测 62.647874s；绝对误差 62.310616s；log 误差 3.862745；桶 1→4（差 3）。

**主因 D / dependency_or_collection_failure：** autograd 导入 inspect.getargspec 失败，依赖与解释器不兼容。 在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。

次因/边界：B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "03f9a29ba4d1c2260a54b433135f9f3ba06d17e8dd3597bf0907c04fbf4e942a", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=25，min/median/max(ms)=273.887685/62647.874033/518569.221292。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=692554a5e50c2c71b88d46897dbea65f4183e56c3bf0335c13ab78dae9e76c67，exact=True，覆盖证据 n=57，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 395b817eac7f1643b730121329a00276d9476d5f0acd98589dedeb7915488ac0 | 62 | 0.5 | 0.0086192036 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 585 | 0.5 | 0.0086192036 | `["arg:[\"L1/pytest\",0,\"tests\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
ImportError while loading conftest '/testbed/tests/conftest.py'.
tests/conftest.py:23: in <module>
    import pennylane as qml
pennylane/__init__.py:29: in <module>
    import pennylane.kernels
pennylane/kernels/__init__.py:18: in <module>
    from .cost_functions import (
pennylane/kernels/cost_functions.py:18: in <module>
    from pennylane import numpy as np
pennylane/numpy/__init__.py:84: in <module>
    from autograd import numpy as _np
/usr/local/lib/python3.11/dist-packages/autograd/__init__.py:2: in <module>
    from .differential_operators import (
/usr/local/lib/python3.11/dist-packages/autograd/differential_operators.py:5: in <module>
    from inspect import getargspec
E   ImportError: cannot import name 'getargspec' from 'inspect' (/usr/lib/python3.11/inspect.py)


Exit code: 4
```

### 26. swe-rebench:tobymao__sqlglot-3652:25

trace `sqlglot-reserved100__tobymao__sqlglot-3652.trace.jsonl:161`；action `tool_25_call_mMKg4yRpCqIbql6XE7Gxiz0J`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 125.668262s；预测 1.732315s；绝对误差 123.935946s；log 误差 3.836422；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                 [ 43%]
tests/dialects/test_trino.py .                                           [ 43%]
tests/dialects/test_tsql.py ..........................................   [ 51%]
tests/test_build.py .                                                    [ 51%]
tests/test_diff.py .........                                             [ 52%]
tests/test_executor.py ...................                               [ 56%]
tests/test_expressions.py .............................................. [ 64%]
.........                                                                [ 66%]
tests/test_generator.py ...                                              [ 66%]
tests/test_helper.py ...                                                 [ 67%]
tests/test_jsonpath.py ...                                               [ 67%]
tests/test_lineage.py ..................                                 [ 71%]
tests/test_optimizer.py ................................................ [ 79%]
.......                                                                  [ 80%]
tests/test_parser.py ................................................... [ 90%]
...                                                                      [ 90%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 92%]
tests/test_time.py .                                                     [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

======================= 556 passed in 124.99s (0:02:04) ========================


Exit code: 0
```

### 27. swe-rebench:tobymao__sqlglot-3696:27

trace `sqlglot-reserved100__tobymao__sqlglot-3696.trace.jsonl:173`；action `tool_27_call_FRc3QgKDcSlp3W0tTNer4abt`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 125.000509s；预测 1.732315s；绝对误差 123.268194s；log 误差 3.831137；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 66%]
tests/test_helper.py ...                                                 [ 67%]
tests/test_jsonpath.py ...                                               [ 67%]
tests/test_lineage.py ..................                                 [ 71%]
tests/test_optimizer.py ................................................ [ 79%]
.......                                                                  [ 80%]
tests/test_parser.py ................................................... [ 90%]
...                                                                      [ 90%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 92%]
tests/test_time.py .                                                     [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

======================= 557 passed in 123.52s (0:02:03) ========================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 28. swe-rebench:PennyLaneAI__pennylane-3381:44

trace `pennylane__PennyLaneAI__pennylane-3381.trace.jsonl:275`；action `tool_44_call_AmwE4ehhhnD3vesw0N9v6eY7`。

```sh
python3 -m pytest -q tests
```

实际 0.408704s；预测 62.647874s；绝对误差 62.239170s；log 误差 3.810696；桶 1→4（差 3）。

**主因 D / dependency_or_collection_failure：** autograd 导入 inspect.getargspec 失败，依赖与解释器不兼容。 在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。

次因/边界：B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "03f9a29ba4d1c2260a54b433135f9f3ba06d17e8dd3597bf0907c04fbf4e942a", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=25，min/median/max(ms)=273.887685/62647.874033/518569.221292。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=692554a5e50c2c71b88d46897dbea65f4183e56c3bf0335c13ab78dae9e76c67，exact=True，覆盖证据 n=57，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 395b817eac7f1643b730121329a00276d9476d5f0acd98589dedeb7915488ac0 | 62 | 0.5 | 0.0086192036 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 585 | 0.5 | 0.0086192036 | `["arg:[\"L1/pytest\",0,\"tests\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
ImportError while loading conftest '/testbed/tests/conftest.py'.
tests/conftest.py:23: in <module>
    import pennylane as qml
pennylane/__init__.py:31: in <module>
    import pennylane.kernels
pennylane/kernels/__init__.py:18: in <module>
    from .cost_functions import (
pennylane/kernels/cost_functions.py:18: in <module>
    from pennylane import numpy as np
pennylane/numpy/__init__.py:84: in <module>
    from autograd import numpy as _np
/usr/local/lib/python3.11/dist-packages/autograd/__init__.py:2: in <module>
    from .differential_operators import (
/usr/local/lib/python3.11/dist-packages/autograd/differential_operators.py:5: in <module>
    from inspect import getargspec
E   ImportError: cannot import name 'getargspec' from 'inspect' (/usr/lib/python3.11/inspect.py)


Exit code: 4
```

### 29. swe-rebench:tobymao__sqlglot-3620:15

trace `sqlglot-reserved100__tobymao__sqlglot-3620.trace.jsonl:101`；action `tool_15_call_9VQEDLnkjIaQv7QEGPpk3UHU`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest
```

实际 120.825898s；预测 1.732315s；绝对误差 119.093583s；log 误差 3.797444；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 66%]
tests/test_helper.py ...                                                 [ 67%]
tests/test_jsonpath.py ...                                               [ 68%]
tests/test_lineage.py ..................                                 [ 71%]
tests/test_optimizer.py ................................................ [ 79%]
.......                                                                  [ 81%]
tests/test_parser.py ................................................... [ 90%]
..                                                                       [ 90%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py .                                                     [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py ....                                            [ 95%]
tests/test_transpile.py ..........................                       [100%]

======================= 554 passed in 120.05s (0:02:00) ========================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 30. swe-rebench:tobymao__sqlglot-4430:28

trace `sqlglot-dev100__tobymao__sqlglot-4430.trace.jsonl:179`；action `tool_28_call_NmzNwsSwpDt11ChXbsQjg4lu`。

```sh
SQLGLOTRS_TOKENIZER=0 pytest -q
```

实际 1.132290s；预测 92.873632s；绝对误差 91.741342s；log 误差 3.784753；桶 2→4（差 2）。

**主因 D / dependency_or_collection_failure：** pytest 收集阶段缺少 pandas，提前中止。 在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。

次因/边界：B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "pytest", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L0/pytest\",\"--quiet\"]", "tool=pytest"], "core": ["tool=pytest"], "parse_digest": "e2a56c1f10156821cf2d0aa2d54acf6d28e4d2717860e48db021b5edd88e0665", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=26，min/median/max(ms)=1132.289925/92873.632323/143890.051664。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L0/pytest\",\"--quiet\"]", "tool=pytest"]
```

Edge node=be36649bad15a2d11167b22670bb1695e2e3bc86c003aa016feb8bd7907c7e4d，exact=True，覆盖证据 n=85，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 0c02205d4bfe7b1bc11dbe602572f6d74c1dd2d1a53517e55ef1838c4ac92547 | 94 | 0.51917499 | 0.0060348713 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |
| 5707bc7bc6696193a08936039361ee4899933f2012f8411efafa60a8b804e42b | 40 | 0.5 | 0.0058119818 | `["flag:[\"L0/pytest\",\"--quiet\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
==================================== ERRORS ====================================
___________________ ERROR collecting tests/test_executor.py ____________________
ImportError while importing test module '/testbed/tests/test_executor.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/usr/lib/python3.11/importlib/__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests/test_executor.py:8: in <module>
    import numpy as np
E   ModuleNotFoundError: No module named 'numpy'
___________________ ERROR collecting tests/test_optimizer.py ___________________
ImportError while importing test module '/testbed/tests/test_optimizer.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/usr/lib/python3.11/importlib/__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests/test_optimizer.py:7: in <module>
    from pandas.testing import assert_frame_equal
E   ModuleNotFoundError: No module named 'pandas'
=========================== short test summary info ============================
ERROR tests/test_executor.py
ERROR tests/test_optimizer.py
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
2 errors in 0.59s


Exit code: 2
```

## edge_native

### 1. swe-rebench:tobymao__sqlglot-3630:36

trace `sqlglot-reserved100__tobymao__sqlglot-3630.trace.jsonl:227`；action `tool_36_call_ycOOURkl7V8ZR7XoPEcWZOo8`。

```sh
python -m venv --system-site-packages .venv && .venv/bin/python -m pip install 'maturin>=1.4,<2.0' && PATH=/testbed/.venv/bin:$PATH VIRTUAL_ENV=/testbed/.venv make install-dev-rs && PATH=/testbed/.venv/bin:$PATH VIRTUAL_ENV=/testbed/.venv make test-rs
```

实际 0.096502s；预测 133.248936s；绝对误差 133.152434s；log 误差 4.807571；桶 0→4（差 4）。

**主因 B / conditional_stage_composition：** venv 创建失败：ensurepip 不可用；实际只执行首阶段，后续 pip/make 未执行。 研究执行成功概率与条件路径合成；不能事后用真实退出状态选择预测分支。

次因/边界：D: 前序成功与否未观测；C: 部分子句规则/证据不足，但不足以解释未执行阶段被相加。

子句 0：parse=partial，diagnostics=["missing_rule:venv"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "opaque:[\"L1/venv\",[\"--system-site-packages\",\".venv\"]]", "target:[\"venv\"]", "tool=python"], "core": ["target:[\"venv\"]", "tool=python"], "parse_digest": "31ce9177c47af96f00d37fb45b45d78f8f0eee3b4547ac69b7011e0384ea8e07", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 4 个；选中样本 n=2，min/median/max(ms)=3923.54297/4327.285127/4731.027284。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"venv\"]", "tool=python"]
```

Edge node=03eb91fdf59f4ee038312573db0c349f397baa4b178b839cd63cb4b7361dc354，exact=False，覆盖证据 n=0，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 7579b54bae63e90b6f85e7ef643dae84700973f786e96d9a83bdd03b0aaab624 | 12 | 0.5 | 0.4950495 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "opaque:[\"L1/venv\",[\"--system-site-packages\",\".venv\"]]"]` |
| 8ed404ae37b03f3d7ac75f552ee10a53193c41d10c53a28f920a45aa0af5c577 | 2 | 0.5 | 0.4950495 | `["mode:[\"module\"]", "opaque:[\"L1/venv\",[\"--system-site-packages\",\".venv\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 1：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pip\",0,\"maturin>=1.4,<2.0\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pip\",\"install\"]", "tool=python"], "core": ["target:[\"pip\",\"install\"]", "tool=python"], "parse_digest": "db653c18aa8e1a123dd9d15fd463394d7acc2eaa6b3506d23a5a22d750b43ae9", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 4 个；选中样本 n=265，min/median/max(ms)=18.753362/10655.063956/474168.642057。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pip\",\"install\"]", "tool=python"]
```

Edge node=bd1576b68f1e7a5a7f152f2e45cbc9208570865072ad7a8633dcfc7f32038a1d，exact=False，覆盖证据 n=0，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 4584ccbce5f84d9b39bc4e3c9d4c6bb0ec5db6ca673602768dd6abe739075313 | 657 | 0.5 | 0.4950495 | `["arg:[\"L1/pip\",0,\"maturin>=1.4,<2.0\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |
| d916bf659c948517a5be22d337e3d6074b2a6dfd7bb8a3942c26b33c3fba3516 | 265 | 0.5 | 0.4950495 | `["arg:[\"L1/pip\",0,\"maturin>=1.4,<2.0\"]", "mode:[\"module\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 2：parse=complete，diagnostics=[]。

```json
{"tool": "make", "features": ["arg:[\"L0/make\",0,\"install-dev-rs\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "tool=make"], "core": ["tool=make"], "parse_digest": "01917b6d15bd856d34eedf17e6298c65e940e947c967a2dfc7c3978ae8377d1f", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 2 个；选中样本 n=90，min/median/max(ms)=2.471471/7143.396446/189977.527289。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "tool=make"]
```

Edge node=9ea708cd8a9cf00f3b1e320e1e3928db7725421f2fb4d1b3c130caf5e2126434，exact=False，覆盖证据 n=0，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 7acdd054b7fa250be875da1c6d932d5636112219a08b9251f839e0c2292fb577 | 90 | 1 | 0.99009901 | `["arg:[\"L0/make\",0,\"install-dev-rs\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 3：parse=complete，diagnostics=[]。

```json
{"tool": "make", "features": ["arg:[\"L0/make\",0,\"test-rs\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "tool=make"], "core": ["tool=make"], "parse_digest": "c35a96945dc7d1abb62c9d181754fea3116c66209f38248fa8dd58425a50ec78", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=2，min/median/max(ms)=109070.056983/112271.72012750001/115473.383272。选中特征：

```json
["arg:[\"L0/make\",0,\"test-rs\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "tool=make"]
```

Edge node=4da34aad0211e2579209c9699c89c9c35fbf82057b5e105c1846833619775717，exact=True，覆盖证据 n=2，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 7acdd054b7fa250be875da1c6d932d5636112219a08b9251f839e0c2292fb577 | 88 | 0.5 | 0.19920319 | `["arg:[\"L0/make\",0,\"test-rs\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
The virtual environment was not created successfully because ensurepip is not
available.  On Debian/Ubuntu systems, you need to install the python3-venv
package using the following command.

    apt install python3.11-venv

You may need to use sudo with that command.  After installing the python3-venv
package, recreate your virtual environment.

Failing command: /testbed/.venv/bin/python



Exit code: 1
```

### 2. swe-rebench:PennyLaneAI__pennylane-3483:54

trace `pennylane__PennyLaneAI__pennylane-3483.trace.jsonl:335`；action `tool_54_call_9uPpBhKQ8n2OpVFE4NcAmJQd`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest -q -p no:asyncio tests/returntypes
```

实际 481.231065s；预测 3.391401s；绝对误差 477.839664s；log 误差 4.698775；桶 4→4（差 0）。

**主因 C / unseen_scope_fallback：** 未见 tests/returntypes 与插件组合；回退丢失路径/插件，实际输出 4139 passed、1119 skipped。 保留范围/模式语义并学习可迁移的工作量表示；补充规则本身未必得到范围大小。

次因/边界：B: 粗粒度证据混合；D: 输入规模/缓存未充分观测；具体因果贡献未分离。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests/returntypes\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "sequence:[\"L1/pytest\",[[\"-p\",[\"no:asyncio\"]]]]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "df39d2d1f7610961339ab4b44d6dd2d4dc25f5b62729dceb85d79a19a18b2152", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 8 个；选中样本 n=642，min/median/max(ms)=12.174701/3391.4005875/598046.750194。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=31f5bae619c05657129199f1e344f6dcac16e1d3a7f71e26877ae29e27a80569，exact=False，覆盖证据 n=0，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 642 | 1 | 0.99009901 | `["arg:[\"L1/pytest\",0,\"tests/returntypes\"]", "sequence:[\"L1/pytest\",[[\"-p\",[\"no:asyncio\"]]]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
y Test Report===
=========================== short test summary info ============================
FAILED tests/returntypes/transforms/test_qcut_new.py::TestMCPostprocessing::test_sample_postprocess[torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestMCPostprocessing::test_mc_sample_postprocess[torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitMCTransform::test_all_interfaces_samples[torch-torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitMCTransform::test_all_interfaces_mc[torch-torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestQCutProcessingFn::test_process_tensor[1-torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestQCutProcessingFn::test_process_tensor[2-torch]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestQCutProcessingFn::test_qcut_processing_fn_torch[True]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestQCutProcessingFn::test_qcut_processing_fn_torch[False]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_torch[True]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_torch[False]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_jax[True]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_jax[False]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_torch_trace[True]
FAILED tests/returntypes/transforms/test_qcut_new.py::TestCutCircuitTransform::test_simple_cut_circuit_torch_trace[False]
14 failed, 4139 passed, 1119 skipped, 100 xfailed, 18 xpassed, 87 warnings in 478.86s (0:07:58)


Exit code: 1
```

### 3. swe-rebench:PennyLaneAI__pennylane-3182:39

trace `pennylane__PennyLaneAI__pennylane-3182.trace.jsonl:245`；action `tool_39_call_gRgW70OxfJQbGZg15ghF3ZLx`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest pennylane/devices/tests -q
```

实际 598.046750s；预测 5.072047s；绝对误差 592.974703s；log 误差 4.591644；桶 4→3（差 1）。

**主因 C / unseen_scope_fallback：** pennylane/devices/tests 范围未命中；回退到粗粒度样本，实际 722 passed、756 failed。 保留范围/模式语义并学习可迁移的工作量表示；补充规则本身未必得到范围大小。

次因/边界：B: 粗粒度证据混合；D: 输入规模/缓存未充分观测；具体因果贡献未分离。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"pennylane/devices/tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "64f1903cf9c4e07d63a8fd563002bd2836ae341c7bdc3d119b4a815c2179e7b7", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=1，min/median/max(ms)=598046.750194/598046.750194/598046.750194。选中特征：

```json
["arg:[\"L1/pytest\",0,\"pennylane/devices/tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=fda1ff56cccc1b455cc36c553161d5ade47f449f6a0de4835d32f662b031bc06，exact=True，覆盖证据 n=10，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 33849cb38dfc69e20bcfcebe619b129f48172491b3e46b9c6088deb37da317b3 | 4 | 0.5 | 0.045413261 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]"]` |
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 632 | 0.5 | 0.045413261 | `["arg:[\"L1/pytest\",0,\"pennylane/devices/tests\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
r::test_tracker_updated_in_execution_mode[device_kwargs2]
FAILED pennylane/devices/tests/test_tracker.py::TestTracker::test_tracker_batch_execute[device_kwargs0]
FAILED pennylane/devices/tests/test_tracker.py::TestTracker::test_tracker_batch_execute[device_kwargs2]
FAILED pennylane/devices/tests/test_wires.py::TestWiresIntegration::test_wires_expval[device_kwargs0-make_simple_circuit_expval-wires10-wires20]
FAILED pennylane/devices/tests/test_wires.py::TestWiresIntegration::test_wires_expval[device_kwargs0-make_simple_circuit_expval-wires11-wires21]
FAILED pennylane/devices/tests/test_wires.py::TestWiresIntegration::test_wires_expval[device_kwargs0-make_simple_circuit_expval-wires12-wires22]
FAILED pennylane/devices/tests/test_wires.py::TestWiresIntegration::test_wires_expval[device_kwargs0-make_simple_circuit_expval-wires13-wires23]
FAILED pennylane/devices/tests/test_wires.py::TestWiresIntegration::test_wires_expval[device_kwargs0-make_simple_circuit_expval-wires14-wires24]
FAILED pennylane/devices/tests/test_wires.py::TestWiresIntegration::test_wires_expval[device_kwargs2-make_simple_circuit_expval-wires10-wires20]
FAILED pennylane/devices/tests/test_wires.py::TestWiresIntegration::test_wires_expval[device_kwargs2-make_simple_circuit_expval-wires11-wires21]
FAILED pennylane/devices/tests/test_wires.py::TestWiresIntegration::test_wires_expval[device_kwargs2-make_simple_circuit_expval-wires12-wires22]
FAILED pennylane/devices/tests/test_wires.py::TestWiresIntegration::test_wires_expval[device_kwargs2-make_simple_circuit_expval-wires13-wires23]
FAILED pennylane/devices/tests/test_wires.py::TestWiresIntegration::test_wires_expval[device_kwargs2-make_simple_circuit_expval-wires14-wires24]
756 failed, 722 passed, 34 skipped, 233 warnings in 593.56s (0:09:53)


Exit code: 1
```

### 4. swe-rebench:PennyLaneAI__pennylane-3579:45

trace `pennylane__PennyLaneAI__pennylane-3579.trace.jsonl:281`；action `tool_45_call_Ik8uqhboL7HBe5Xe8ooXHdp3`。

```sh
PYTHONPATH=/testbed /opt/conda/envs/testbed/bin/pytest -q tests/returntypes tests/tape tests/transforms
```

实际 597.912368s；预测 5.332746s；绝对误差 592.579621s；log 误差 4.549381；桶 4→4（差 0）。

**主因 C / unseen_scope_fallback：** 三目录组合未命中；回退丢失测试路径，实际接近 598 秒。 保留范围/模式语义并学习可迁移的工作量表示；补充规则本身未必得到范围大小。

次因/边界：B: 粗粒度证据混合；D: 输入规模/缓存未充分观测；具体因果贡献未分离。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "pytest", "features": ["arg:[\"L0/pytest\",0,\"tests/returntypes\"]", "arg:[\"L0/pytest\",1,\"tests/tape\"]", "arg:[\"L0/pytest\",2,\"tests/transforms\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L0/pytest\",\"--quiet\"]", "tool=pytest"], "core": ["tool=pytest"], "parse_digest": "202b342ceff44589d8c9113c6d8424860d4b18e48640f56a1aa24fc366a6c991", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 4 个；选中样本 n=94，min/median/max(ms)=149.544062/5332.746488/274456.763758。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L0/pytest\",\"--quiet\"]", "tool=pytest"]
```

Edge node=88601906ac2d69484cdf46daba1ec88206228e91a63f41218cd6df2222ab578f，exact=False，覆盖证据 n=0，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| ba137b385e55c3d2027cbdf0bab03349555f38be558c3395a06861c30afc50c0 | 94 | 1 | 0.99009901 | `["arg:[\"L0/pytest\",0,\"tests/returntypes\"]", "arg:[\"L0/pytest\",1,\"tests/tape\"]", "arg:[\"L0/pytest\",2,\"tests/transforms\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
rd_circuit[10000000-False] passed 1 out of the required 1 times. Success!
test_cut_circuit_mc_expval passed 1 out of the required 1 times. Success!
test_simple_cut_circuit[None-True] passed 1 out of the required 1 times. Success!
test_simple_cut_circuit[None-False] passed 1 out of the required 1 times. Success!
test_simple_cut_circuit[10000000-True] passed 1 out of the required 1 times. Success!
test_simple_cut_circuit[10000000-False] passed 1 out of the required 1 times. Success!
test_standard_circuit[None-True] passed 1 out of the required 1 times. Success!
test_standard_circuit[None-False] passed 1 out of the required 1 times. Success!
test_standard_circuit[10000000-True] passed 1 out of the required 1 times. Success!
test_standard_circuit[10000000-False] passed 1 out of the required 1 times. Success!

===End Flaky Test Report===
=========================== short test summary info ============================
FAILED tests/returntypes/core/test_new_return_types_qnode.py::TestIntegrationSingleReturn::test_probs[op3-None-lightning.qubit]
1 failed, 5695 passed, 2202 skipped, 100 xfailed, 18 xpassed, 85 warnings in 595.47s (0:09:55)
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 1
```

### 5. swe-rebench:PennyLaneAI__pennylane-6939:58

trace `pennylane__PennyLaneAI__pennylane-6939.trace.jsonl:359`；action `tool_58_call_ru4FYOEEu3e7PTFKm4iY1Mua`。

```sh
/opt/conda/bin/python -m black --check pennylane/measurements/expval.py pennylane/devices/qubit/measure.py pennylane/devices/qubit_mixed/measure.py pennylane/devices/qutrit_mixed/measure.py pennylane/devices/_qubit_device.py tests/measurements/test_expval.py && /opt/conda/bin/python -m pytest tests/measurements -q
```

实际 0.852566s；预测 173.455884s；绝对误差 172.603318s；log 误差 4.545100；桶 2→4（差 2）。

**主因 B / conditional_stage_composition：** black --check 报告 4 个文件需重排并退出；后续 pytest 未执行。 研究执行成功概率与条件路径合成；不能事后用真实退出状态选择预测分支。

次因/边界：D: 前序成功与否未观测；C: 部分子句规则/证据不足，但不足以解释未执行阶段被相加。

子句 0：parse=partial，diagnostics=["missing_rule:black"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opaque:[\"L1/black\",[\"--check\",\"pennylane/measurements/expval.py\",\"pennylane/devices/qubit/measure.py\",\"pennylane/devices/qubit_mixed/measure.py\",\"pennylane/devices/qutrit_mixed/measure.py\",\"pennylane/devices/_qubit_device.py\",\"tests/measurements/test_expval.py\"]]", "target:[\"black\"]", "tool=python"], "core": ["target:[\"black\"]", "tool=python"], "parse_digest": "53772d81585a6b25553581d8e783cdd83082cc08882e1bb7b4a4912a2d2becdb", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=1，min/median/max(ms)=852.566051/852.566051/852.566051。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opaque:[\"L1/black\",[\"--check\",\"pennylane/measurements/expval.py\",\"pennylane/devices/qubit/measure.py\",\"pennylane/devices/qubit_mixed/measure.py\",\"pennylane/devices/qutrit_mixed/measure.py\",\"pennylane/devices/_qubit_device.py\",\"tests/measurements/test_expval.py\"]]", "target:[\"black\"]", "tool=python"]
```

Edge node=cb1d37720dbe70977b54ebe693ba7f3cf3110fc3f82968fa1bd09472661ba65b，exact=True，覆盖证据 n=1，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| c80d458945e5e7814ce4b34a36c29b7db2f02e4d8bb506fb0d574dda470bcef3 | 35 | 0.33333333 | 0.19880716 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "opaque:[\"L1/black\",[\"--check\",\"pennylane/measurements/expval.py\",\"pennylane/devices/qubit/measure.py\",\"pennylane/devices/qubit_mixed/measure.py\",\"pennylane/devices/qutrit_mixed/measure.py\",\"pennylane/devices/_qubit_device.py\",\"tests/measurements/test_expval.py\"]]"]` |
| d98d70823a72c5d3f0439b19ec78d5b516272e98aa397e1b5dd19d801f835c0c | 35 | 0.33333333 | 0.19880716 | `["mode:[\"module\"]", "opaque:[\"L1/black\",[\"--check\",\"pennylane/measurements/expval.py\",\"pennylane/devices/qubit/measure.py\",\"pennylane/devices/qubit_mixed/measure.py\",\"pennylane/devices/qutrit_mixed/measure.py\",\"pennylane/devices/_qubit_device.py\",\"tests/measurements/test_expval.py\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 1：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests/measurements\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "523b5de51d0fcf6f0e86f833d03a8c05ef780ec279965539de5617b893094d6f", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=1，min/median/max(ms)=220073.124279/220073.124279/220073.124279。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests/measurements\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=7292a8ba8ee3bfab256483578d574fadf15cda5d7ab271e026c75a73c351b41b，exact=True，覆盖证据 n=2，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 7f69b153d3605504aa0d2cd83578b4e8aa444dd8c8b48767643b9367411b2342 | 3 | 0.5 | 0.16611296 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]"]` |
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 640 | 0.5 | 0.16611296 | `["arg:[\"L1/pytest\",0,\"tests/measurements\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
would reformat pennylane/measurements/expval.py
would reformat pennylane/devices/qubit/measure.py
would reformat tests/measurements/test_expval.py
would reformat pennylane/devices/_qubit_device.py

Oh no! 💥 💔 💥
4 files would be reformatted, 2 files would be left unchanged.


Exit code: 1
```

### 6. swe-rebench:tobymao__sqlglot-3045:17

trace `sqlglot-reserved100__tobymao__sqlglot-3045.trace.jsonl:113`；action `tool_17_call_iMzYOrtT6J625CauveqYB8LQ`。

```sh
/opt/conda/bin/python -m pytest -q
```

实际 202.778860s；预测 1.465911s；绝对误差 201.312949s；log 误差 4.414474；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "ae951bd3ab9af2a990a0b57570b2cce2d27ad8e240b531f2858b094314de9112", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=73，min/median/max(ms)=849.187226/4841.651968/202778.860343。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=113978e3334b282efaab29572d05a1663a98699cb1a2ef751845f877201a22a6，exact=True，覆盖证据 n=213，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426 | 373 | 0.5 | 0.0023383325 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |
| ba056ba4b4902bca002074a48a53ee0b9be4c1c414153d0bf3677b757e87e9dc | 642 | 0.31758536 | 0.0014852404 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
frameFunc::test_where_many_and
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_where_many_or
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_where_mixed_and_or
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_where_multiple_chained
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_with_column
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_with_column_existing_name
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_with_column_renamed
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_with_column_renamed_double
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_avg
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_count
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_group_by
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_group_by_where_aggregate_like_having
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_group_by_where_non_aggregate
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_max
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_mean
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_min
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_sum
FAILED tests/dataframe/integration/test_session.py::TestSessionFunc::test_sql_simple_select
FAILED tests/dataframe/integration/test_session.py::TestSessionFunc::test_sql_with_join
88 failed, 855 passed, 185 warnings in 201.63s (0:03:21)


Exit code: 1
```

### 7. swe-rebench:tobymao__sqlglot-3515:28

trace `sqlglot-reserved100__tobymao__sqlglot-3515.trace.jsonl:179`；action `tool_28_call_upmMtAO8Mne5OtacZJZNRg96`。

```sh
/opt/conda/bin/python -m pytest -q
```

实际 194.666172s；预测 1.465911s；绝对误差 193.200261s；log 误差 4.373849；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "ae951bd3ab9af2a990a0b57570b2cce2d27ad8e240b531f2858b094314de9112", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=73，min/median/max(ms)=849.187226/4841.651968/202778.860343。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=113978e3334b282efaab29572d05a1663a98699cb1a2ef751845f877201a22a6，exact=True，覆盖证据 n=213，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426 | 373 | 0.5 | 0.0023383325 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |
| ba056ba4b4902bca002074a48a53ee0b9be4c1c414153d0bf3677b757e87e9dc | 642 | 0.31758536 | 0.0014852404 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
re_many_or
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_where_mixed_and_or
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_where_multiple_chained
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_with_column
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_with_column_existing_name
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_with_column_renamed
FAILED tests/dataframe/integration/test_dataframe.py::TestDataframeFunc::test_with_column_renamed_double
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_avg
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_count
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_group_by
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_group_by_where_aggregate_like_having
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_group_by_where_non_aggregate
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_max
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_mean
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_min
FAILED tests/dataframe/integration/test_grouped_data.py::TestDataframeFunc::test_sum
FAILED tests/dataframe/integration/test_session.py::TestSessionFunc::test_nameless_column
FAILED tests/dataframe/integration/test_session.py::TestSessionFunc::test_sql_simple_select
FAILED tests/dataframe/integration/test_session.py::TestSessionFunc::test_sql_with_join
89 failed, 881 passed, 185 warnings, 9463 subtests passed in 192.97s (0:03:12)


Exit code: 1
```

### 8. swe-rebench:PennyLaneAI__pennylane-5063:63

trace `pennylane__PennyLaneAI__pennylane-5063.trace.jsonl:389`；action `tool_63_call_IaaedzEWlRfnSMFCeHrVXzCC`。

```sh
python3 -m pip uninstall -y cvxopt && python3 -m pytest tests/kernels -q --disable-warnings -n auto && python3 -m pytest tests/gradients tests/interfaces tests/kernels tests/logging tests/math tests/measurements tests/numpy tests/optimize -q --disable-warnings -n auto
```

实际 0.322239s；预测 99.706901s；绝对误差 99.384661s；log 误差 4.332888；桶 1→4（差 3）。

**主因 B / conditional_stage_composition：** pip uninstall 被 externally-managed-environment 拒绝；后续两段 pytest 未执行。 研究执行成功概率与条件路径合成；不能事后用真实退出状态选择预测分支。

次因/边界：D: 前序成功与否未观测；C: 部分子句规则/证据不足，但不足以解释未执行阶段被相加。

子句 0：parse=partial，diagnostics=["unknown_option:-y"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opaque:[\"L1/pip\",[\"-y\",\"cvxopt\"]]", "target:[\"pip\",\"uninstall\"]", "tool=python"], "core": ["target:[\"pip\",\"uninstall\"]", "tool=python"], "parse_digest": "ecc049f0253afc5271e97c14ea1d4976c6875b9b8e8c5df08f49e20ed89ee971", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=2，min/median/max(ms)=322.239177/334.0192295/345.799282。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opaque:[\"L1/pip\",[\"-y\",\"cvxopt\"]]", "target:[\"pip\",\"uninstall\"]", "tool=python"]
```

Edge node=3e743f70942ac162180c5117386e6ffc64bc1ba2f2ece266d24a141cc6eb530a，exact=True，覆盖证据 n=2，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1920f35179c774132b6314b58ad2196853e176305595e296dadc202a9ec8a461 | 17 | 0.33333333 | 0.124533 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "opaque:[\"L1/pip\",[\"-y\",\"cvxopt\"]]"]` |
| ad1f4eb088605a9638553146ed4b9d9a90027cfeb881a5b0db14a34c72a31e83 | 17 | 0.33333333 | 0.124533 | `["mode:[\"module\"]", "opaque:[\"L1/pip\",[\"-y\",\"cvxopt\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 1：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests/kernels\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "98e5b73faf00f3202e5201a70dc1ed7db57d505341812b0e34b84ff36ac3d7dc", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=4，min/median/max(ms)=387.568296/6330.4977245/6576.099279。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests/kernels\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=0aff8bb45a44d0f983f4dce25c433985dca816781b1c121084787ed49e708dd1，exact=True，覆盖证据 n=4，父边 3 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 377f98307e640658cbf9769bc56e5a308d583460a6cab313bd2ab8dbbfe2818e | 37 | 0.33333333 | 0.066533599 | `["arg:[\"L1/pytest\",0,\"tests/kernels\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]"]` |
| 5b8cb3e64ab8a5e5ce6270629683f61f76c840001563c0d84686b27a7d1c61d2 | 41 | 0.33333333 | 0.066533599 | `["arg:[\"L1/pytest\",0,\"tests/kernels\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| 807af1dccf64c0ab6a809f3cb43b9c88d2f1338a7badc59df5835d1247e830ee | 2 | 0.33333333 | 0.066533599 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 2：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "6d4d1d0f9e35968a77ae73dd5b39312dbe083246e5637aaaf87db47bce851310", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=2，min/median/max(ms)=87640.280897/90406.544572/93172.808247。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=2d175bacc004f51a663d5d6c4eab86f428a9ae7460bdc35edd0a608ce64f5735，exact=True，覆盖证据 n=2，父边 7 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 3553ae23052258226a887cc4e1f70def9daecfe4f3bae683f9ee0fa92b845d8f | 1 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| 377f98307e640658cbf9769bc56e5a308d583460a6cab313bd2ab8dbbfe2818e | 39 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]"]` |
| 46644b15b622484b153cd380265f9b14dd51165dfeee27e13c6e0dbfac2b335a | 2 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| 5b8cb3e64ab8a5e5ce6270629683f61f76c840001563c0d84686b27a7d1c61d2 | 43 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| b83e057ea7c8d98be79b91bf1e376a8bec5957691375674c9d7dcf47316691a4 | 7 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| c335df336001eac1fa2ae8b4d926c8ee01b937f43bdafd21ca8345eb26963411 | 8 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",5,\"tests/measurements\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |
| c8810c02b785f9016ecc91d2f9a50b8eea4ba18206a1d5a573803330fa3222fa | 3 | 0.1 | 0.036900369 | `["arg:[\"L1/pytest\",0,\"tests/gradients\"]", "arg:[\"L1/pytest\",1,\"tests/interfaces\"]", "arg:[\"L1/pytest\",2,\"tests/kernels\"]", "arg:[\"L1/pytest\",3,\"tests/logging\"]", "arg:[\"L1/pytest\",4,\"tests/math\"]", "arg:[\"L1/pytest\",6,\"tests/numpy\"]", "arg:[\"L1/pytest\",7,\"tests/optimize\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--disable-pytest-warnings\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--numprocesses\",[\"auto\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
error: externally-managed-environment

× This environment is externally managed
╰─> To install Python packages system-wide, try apt install
    python3-xyz, where xyz is the package you are trying to
    install.
    
    If you wish to install a non-Debian-packaged Python package,
    create a virtual environment using python3 -m venv path/to/venv.
    Then use path/to/venv/bin/python and path/to/venv/bin/pip. Make
    sure you have python3-full installed.
    
    If you wish to install a non-Debian packaged Python application,
    it may be easiest to use pipx install xyz, which will manage a
    virtual environment for you. Make sure you have pipx installed.
    
    See /usr/share/doc/python3.11/README.venv for more information.

note: If you believe this is a mistake, please contact your Python installation or OS distribution provider. You can override this, at the risk of breaking your Python installation or OS, by passing --break-system-packages.
hint: See PEP 668 for the detailed specification.


Exit code: 1
```

### 9. swe-rebench:PennyLaneAI__pennylane-1072:31

trace `pennylane__PennyLaneAI__pennylane-1072.trace.jsonl:197`；action `tool_31_call_9HWYGs4CfTqn7qiOn9QDbdMz`。

```sh
/tmp/plenv/bin/python -m pytest -q tests
```

实际 0.273888s；预测 95.851070s；绝对误差 95.577182s；log 误差 4.331101；桶 1→4（差 3）。

**主因 D / dependency_or_collection_failure：** conftest 导入失败，缺少 pkg_resources。 在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。

次因/边界：B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "ea7e4470f2dbb2d96f714b21860b89db3e25071e58a7ab5f50ae9563c92817a1", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=25，min/median/max(ms)=273.887685/62647.874033/518569.221292。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=692554a5e50c2c71b88d46897dbea65f4183e56c3bf0335c13ab78dae9e76c67，exact=True，覆盖证据 n=57，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 395b817eac7f1643b730121329a00276d9476d5f0acd98589dedeb7915488ac0 | 62 | 0.5 | 0.0086192036 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 585 | 0.5 | 0.0086192036 | `["arg:[\"L1/pytest\",0,\"tests\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
/tmp/plenv/lib/python3.11/site-packages/_pytest/config/__init__.py:331: PluggyTeardownRaisedWarning: A plugin raised an exception during an old-style hookwrapper teardown.
Plugin: helpconfig, Hook: pytest_cmdline_parse
ConftestImportFailure: ModuleNotFoundError: No module named 'pkg_resources' (from /testbed/tests/conftest.py)
For more information see https://pluggy.readthedocs.io/en/stable/api_reference.html#pluggy.PluggyTeardownRaisedWarning
  config = pluginmanager.hook.pytest_cmdline_parse(
ImportError while loading conftest '/testbed/tests/conftest.py'.
tests/conftest.py:22: in <module>
    import pennylane as qml
pennylane/__init__.py:19: in <module>
    import pkg_resources
E   ModuleNotFoundError: No module named 'pkg_resources'


Exit code: 4
```

### 10. swe-rebench:PennyLaneAI__pennylane-1325:40

trace `pennylane__PennyLaneAI__pennylane-1325.trace.jsonl:251`；action `tool_40_call_THppHkuXbYATeJAkLEbXINTo`。

```sh
/opt/conda/envs/pltest/bin/python -m pytest -q tests
```

实际 0.290326s；预测 95.851070s；绝对误差 95.560744s；log 误差 4.318279；桶 1→4（差 3）。

**主因 D / dependency_or_collection_failure：** conftest 导入失败，缺少 pkg_resources。 在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。

次因/边界：B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "e9d2f3aa849ae0735d07038d87b72a2dee73ec9a90355d372fed2812c6404d4b", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=25，min/median/max(ms)=273.887685/62647.874033/518569.221292。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=692554a5e50c2c71b88d46897dbea65f4183e56c3bf0335c13ab78dae9e76c67，exact=True，覆盖证据 n=57，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 395b817eac7f1643b730121329a00276d9476d5f0acd98589dedeb7915488ac0 | 62 | 0.5 | 0.0086192036 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 585 | 0.5 | 0.0086192036 | `["arg:[\"L1/pytest\",0,\"tests\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
/opt/conda/envs/pltest/lib/python3.10/site-packages/_pytest/config/__init__.py:331: PluggyTeardownRaisedWarning: A plugin raised an exception during an old-style hookwrapper teardown.
Plugin: helpconfig, Hook: pytest_cmdline_parse
ConftestImportFailure: ModuleNotFoundError: No module named 'pkg_resources' (from /testbed/tests/conftest.py)
For more information see https://pluggy.readthedocs.io/en/stable/api_reference.html#pluggy.PluggyTeardownRaisedWarning
  config = pluginmanager.hook.pytest_cmdline_parse(
ImportError while loading conftest '/testbed/tests/conftest.py'.
tests/conftest.py:22: in <module>
    import pennylane as qml
pennylane/__init__.py:21: in <module>
    import pkg_resources
E   ModuleNotFoundError: No module named 'pkg_resources'


Exit code: 4
```

### 11. swe-rebench:PennyLaneAI__pennylane-2601:24

trace `pennylane__PennyLaneAI__pennylane-2601.trace.jsonl:155`；action `tool_24_call_vi33vF4z5MxEfAN46ofeytPG`。

```sh
python3 -m pytest -q tests
```

实际 0.337258s；预测 95.851070s；绝对误差 95.513812s；log 误差 4.282553；桶 1→4（差 3）。

**主因 D / dependency_or_collection_failure：** autograd 导入 inspect.getargspec 失败，依赖与解释器不兼容。 在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。

次因/边界：B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "03f9a29ba4d1c2260a54b433135f9f3ba06d17e8dd3597bf0907c04fbf4e942a", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=25，min/median/max(ms)=273.887685/62647.874033/518569.221292。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=692554a5e50c2c71b88d46897dbea65f4183e56c3bf0335c13ab78dae9e76c67，exact=True，覆盖证据 n=57，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 395b817eac7f1643b730121329a00276d9476d5f0acd98589dedeb7915488ac0 | 62 | 0.5 | 0.0086192036 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 585 | 0.5 | 0.0086192036 | `["arg:[\"L1/pytest\",0,\"tests\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
ImportError while loading conftest '/testbed/tests/conftest.py'.
tests/conftest.py:23: in <module>
    import pennylane as qml
pennylane/__init__.py:29: in <module>
    import pennylane.kernels
pennylane/kernels/__init__.py:18: in <module>
    from .cost_functions import (
pennylane/kernels/cost_functions.py:18: in <module>
    from pennylane import numpy as np
pennylane/numpy/__init__.py:84: in <module>
    from autograd import numpy as _np
/usr/local/lib/python3.11/dist-packages/autograd/__init__.py:2: in <module>
    from .differential_operators import (
/usr/local/lib/python3.11/dist-packages/autograd/differential_operators.py:5: in <module>
    from inspect import getargspec
E   ImportError: cannot import name 'getargspec' from 'inspect' (/usr/lib/python3.11/inspect.py)


Exit code: 4
```

### 12. swe-rebench:PennyLaneAI__pennylane-3381:44

trace `pennylane__PennyLaneAI__pennylane-3381.trace.jsonl:275`；action `tool_44_call_AmwE4ehhhnD3vesw0N9v6eY7`。

```sh
python3 -m pytest -q tests
```

实际 0.408704s；预测 95.851070s；绝对误差 95.442366s；log 误差 4.230505；桶 1→4（差 3）。

**主因 D / dependency_or_collection_failure：** autograd 导入 inspect.getargspec 失败，依赖与解释器不兼容。 在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。

次因/边界：B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "03f9a29ba4d1c2260a54b433135f9f3ba06d17e8dd3597bf0907c04fbf4e942a", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=25，min/median/max(ms)=273.887685/62647.874033/518569.221292。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=692554a5e50c2c71b88d46897dbea65f4183e56c3bf0335c13ab78dae9e76c67，exact=True，覆盖证据 n=57，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 395b817eac7f1643b730121329a00276d9476d5f0acd98589dedeb7915488ac0 | 62 | 0.5 | 0.0086192036 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |
| fa44e22660b8f062e924b1ae71926887c30e20ffd1c3edf6c99afe84c3c10cbb | 585 | 0.5 | 0.0086192036 | `["arg:[\"L1/pytest\",0,\"tests\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
ImportError while loading conftest '/testbed/tests/conftest.py'.
tests/conftest.py:23: in <module>
    import pennylane as qml
pennylane/__init__.py:31: in <module>
    import pennylane.kernels
pennylane/kernels/__init__.py:18: in <module>
    from .cost_functions import (
pennylane/kernels/cost_functions.py:18: in <module>
    from pennylane import numpy as np
pennylane/numpy/__init__.py:84: in <module>
    from autograd import numpy as _np
/usr/local/lib/python3.11/dist-packages/autograd/__init__.py:2: in <module>
    from .differential_operators import (
/usr/local/lib/python3.11/dist-packages/autograd/differential_operators.py:5: in <module>
    from inspect import getargspec
E   ImportError: cannot import name 'getargspec' from 'inspect' (/usr/lib/python3.11/inspect.py)


Exit code: 4
```

### 13. swe-rebench:PennyLaneAI__pennylane-4161:69

trace `pennylane__PennyLaneAI__pennylane-4161.trace.jsonl:425`；action `tool_69_call_PnjFMh11rMN4Y8bqbiTFuhJR`。

```sh
python3 -m pytest --lf -x -vv --tb=long
```

实际 218.490558s；预测 2.199741s；绝对误差 216.290817s；log 误差 4.228239；桶 4→2（差 2）。

**主因 C / unseen_scope_fallback：** --lf/-x/-vv/--tb=long 组合未命中；last-failed 的运行集合还依赖未观测缓存。 保留范围/模式语义并学习可迁移的工作量表示；补充规则本身未必得到范围大小。

次因/边界：B: 粗粒度证据混合；D: 输入规模/缓存未充分观测；具体因果贡献未分离。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "51316ee58afa3ee2f4dc9c1f4c0f30a3ad2d5e0a70f85b052d1a52467aeedf02", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 48 个；选中样本 n=4，min/median/max(ms)=1766.135618/1851.096928/2309.788816。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=b358937ea4c4037148f7020f5c1475091230f89603e61e942ed348285f0b7ebe，exact=False，覆盖证据 n=0，父边 6 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 190515f4090088fcdef68c0a184840f53a7bdfd7e7b953443ec791fef8844fb7 | 875 | 0.16666667 | 0.1650165 | `["flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]"]` |
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 1461 | 0.16666667 | 0.1650165 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]"]` |
| 21c283e9199e3713d8a0a254ca1ba2ff301ef51669228b7a01d73c17c9c0dfc1 | 7 | 0.16666667 | 0.1650165 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]"]` |
| 8f560fbef17d11b2bbe3793bd5766652e80075e864233d7ec2d741c3da68f998 | 44 | 0.16666667 | 0.1650165 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]"]` |
| a5c5e22ed3c4392ba26d52611b7b97aec02d0a50ccc0f0f9f43dff973f81738d | 90 | 0.16666667 | 0.1650165 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "flag:[\"L1/pytest\",\"--exitfirst\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]"]` |
| fc8ebc944560c5b69d1607a601818806c9b62f580eded40a30b5e703f7bb70fe | 46 | 0.16666667 | 0.1650165 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opt:[\"L1/pytest\",\"--tb\",[\"long\"]]", "repeat:[\"L1/pytest\",\"--verbose\",[[],[]]]", "sequence:[\"L1/pytest\",[[\"--last-failed\",[]]]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
tory_variance_single_param failed (6 runs remaining out of 8).
	<class 'AssertionError'>
	assert array(1.30210091) == 0 ± 1.0e+00
  comparison failed
  Obtained: 1.302100912102162
  Expected: 0 ± 1.0e+00
	[<TracebackEntry /testbed/tests/gradients/parameter_shift/test_parameter_shift_shot_vec.py:1549>]
test_involutory_and_noninvolutory_variance_single_param passed 1 out of the required 1 times. Success!
test_involutory_and_noninvolutory_variance_multi_param failed (7 runs remaining out of 8).
	<class 'AssertionError'>
	assert False
 +  where False = <function primitive.<locals>.f_wrapped at 0x7bf85190de40>(array(-37.66896295), -36.524463611058046, atol=1)
 +    where <function primitive.<locals>.f_wrapped at 0x7bf85190de40> = np.allclose
	[<TracebackEntry /testbed/tests/gradients/parameter_shift/test_parameter_shift_shot_vec.py:1625>]
test_involutory_and_noninvolutory_variance_multi_param failed (6 runs remaining out of 8).
	<class 'AssertionError'>
	assert False
 +  where False = <function primitive.<locals>.f_wrapped at 0x7bf85190de40>(array(-37.64386525), -36.524463611058046, atol=1)
 +    where <function primitive.<locals>.f_wrapped at 0x7bf85190de40> = np.allclose
	[<TracebackEntry /testbed/tests/gradients/parameter_shift/test_parameter_shift_shot_vec.py:1625>]
test_involutory_and_noninvolutory_variance_multi_param passed 1 out of the required 1 times. Success!

===End Flaky Test Report===
=========================== short test summary info ============================
ERROR tests/kernels/test_kernels.py::TestRegularization::test_closest_psd_matrix[input0-False-expected_output0]
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
= 631 passed, 3 skipped, 96 deselected, 9 xfailed, 94 warnings, 1 error in 216.93s (0:03:36) =


Exit code: 1
```

### 14. swe-rebench:PennyLaneAI__pennylane-3921:53

trace `pennylane__PennyLaneAI__pennylane-3921.trace.jsonl:329`；action `tool_53_call_OxQ0UFIovJZKLaH9IMbCiYP9`。

```sh
/tmp/pl310/bin/python -m pytest tests/test_*.py tests/circuit_graph tests/collections tests/data tests/docs tests/drawer tests/fourier tests/gpu tests/grouping tests/kernels tests/math tests/measurements tests/numpy tests/optimize tests/pauli tests/pulse tests/qinfo tests/qnn tests/resource tests/shadow tests/tape -q -n 4
```

实际 212.804305s；预测 2.287143s；绝对误差 210.517163s；log 误差 4.175042；桶 4→4（差 0）。

**主因 C / unseen_scope_fallback：** 未展开的 glob 使参数保守保留为 opaque；回退丢失测试范围，实际 8814 passed、3140 skipped。 保留范围/模式语义并学习可迁移的工作量表示；补充规则本身未必得到范围大小。

次因/边界：B: 粗粒度证据混合；D: 输入规模/缓存未充分观测；具体因果贡献未分离。

子句 0：parse=partial，diagnostics=["unresolved_expansion"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "opaque:[\"L1/pytest\",[\"tests/test_*.py\",\"tests/circuit_graph\",\"tests/collections\",\"tests/data\",\"tests/docs\",\"tests/drawer\",\"tests/fourier\",\"tests/gpu\",\"tests/grouping\",\"tests/kernels\",\"tests/math\",\"tests/measurements\",\"tests/numpy\",\"tests/optimize\",\"tests/pauli\",\"tests/pulse\",\"tests/qinfo\",\"tests/qnn\",\"tests/resource\",\"tests/shadow\",\"tests/tape\",\"-q\",\"-n\",\"4\"]]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "aa8f2dfb3222204f873a2984ee4bb0838b3bfce5508cdc4e7f01e076a36fac34", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=False，候选 4 个；选中样本 n=875，min/median/max(ms)=0.0/3145.299989/598046.750194。选中特征：

```json
["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=a5707c3439aadb842b5ca727f100e3c2eb60498954a9be55c594be79baa3190d，exact=False，覆盖证据 n=0，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 190515f4090088fcdef68c0a184840f53a7bdfd7e7b953443ec791fef8844fb7 | 875 | 0.5 | 0.4950495 | `["mode:[\"module\"]", "opaque:[\"L1/pytest\",[\"tests/test_*.py\",\"tests/circuit_graph\",\"tests/collections\",\"tests/data\",\"tests/docs\",\"tests/drawer\",\"tests/fourier\",\"tests/gpu\",\"tests/grouping\",\"tests/kernels\",\"tests/math\",\"tests/measurements\",\"tests/numpy\",\"tests/optimize\",\"tests/pauli\",\"tests/pulse\",\"tests/qinfo\",\"tests/qnn\",\"tests/resource\",\"tests/shadow\",\"tests/tape\",\"-q\",\"-n\",\"4\"]]"]` |
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 1461 | 0.5 | 0.4950495 | `["context:[\"repo\",\"swe-rebench:PennyLaneAI/pennylane\"]", "opaque:[\"L1/pytest\",[\"tests/test_*.py\",\"tests/circuit_graph\",\"tests/collections\",\"tests/data\",\"tests/docs\",\"tests/drawer\",\"tests/fourier\",\"tests/gpu\",\"tests/grouping\",\"tests/kernels\",\"tests/math\",\"tests/measurements\",\"tests/numpy\",\"tests/optimize\",\"tests/pauli\",\"tests/pulse\",\"tests/qinfo\",\"tests/qnn\",\"tests/resource\",\"tests/shadow\",\"tests/tape\",\"-q\",\"-n\",\"4\"]]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
nfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires1-True-param6-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires1-True-param7-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires1-True-param8-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires1-True-param9-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param0-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param1-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param2-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param3-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param4-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param5-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param6-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param7-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param8-lightning.qubit]
FAILED tests/qinfo/test_purity.py::TestPurity::test_IsingXX_qnode_purity_jax_jit[jax-wires2-False-param9-lightning.qubit]
136 failed, 8814 passed, 3140 skipped, 10 xfailed, 2 xpassed, 1904 warnings in 211.00s (0:03:30)


Exit code: 1
```

### 15. swe-rebench:tobymao__sqlglot-4096:32

trace `sqlglot-dev100__tobymao__sqlglot-4096.trace.jsonl:203`；action `tool_32_call_UMXnPf2FOHT2ohNgRwmQqrcy`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python3.9 -m pytest
```

实际 144.138322s；预测 1.290415s；绝对误差 142.847907s；log 误差 4.148954；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "cce49b17dd9899820dda2768a9aec64d8b366fbc041bddf3ae7339185fd561ba", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 68%]
tests/test_helper.py ...                                                 [ 69%]
tests/test_jsonpath.py ...                                               [ 69%]
tests/test_lineage.py ..................                                 [ 72%]
tests/test_optimizer.py ................................................ [ 80%]
.......                                                                  [ 81%]
tests/test_parser.py ................................................... [ 90%]
....                                                                     [ 90%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 596 passed, 2 skipped in 143.26s (0:02:23) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 16. swe-rebench:tobymao__sqlglot-4148:19

trace `sqlglot-dev100__tobymao__sqlglot-4148.trace.jsonl:125`；action `tool_19_call_0ZbpRXfwTYTPDLniwYtX8yPn`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest
```

实际 141.639273s；预测 1.290415s；绝对误差 140.348858s；log 误差 4.131586；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 68%]
tests/test_helper.py ...                                                 [ 69%]
tests/test_jsonpath.py ...                                               [ 69%]
tests/test_lineage.py ..................                                 [ 72%]
tests/test_optimizer.py ................................................ [ 80%]
.......                                                                  [ 81%]
tests/test_parser.py ................................................... [ 90%]
....                                                                     [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 599 passed, 2 skipped in 140.68s (0:02:20) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 17. swe-rebench:tobymao__sqlglot-4523:19

trace `sqlglot-dev100__tobymao__sqlglot-4523.trace.jsonl:125`；action `tool_19_call_22ty15nwaJqScwf1cr3TeVTS`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest
```

实际 139.442794s；预测 1.290415s；绝对误差 138.152379s；log 误差 4.116067；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 70%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 71%]
tests/test_lineage.py ..................                                 [ 74%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
.....                                                                    [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 94%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 638 passed, 2 skipped in 138.32s (0:02:18) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 18. swe-rebench:tobymao__sqlglot-4331:23

trace `sqlglot-dev100__tobymao__sqlglot-4331.trace.jsonl:149`；action `tool_23_call_HmyUW9g4wBgwiuIuEad3iHn3`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 138.676089s；预测 1.290415s；绝对误差 137.385673s；log 误差 4.110593；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 69%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 70%]
tests/test_lineage.py ..................                                 [ 73%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
....                                                                     [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 93%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 622 passed, 2 skipped in 137.75s (0:02:17) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 19. swe-rebench:tobymao__sqlglot-4087:20

trace `sqlglot-dev100__tobymao__sqlglot-4087.trace.jsonl:131`；action `tool_20_call_r1IL1PeMXCyOmFuaAn8YYZY9`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 137.617322s；预测 1.290415s；绝对误差 136.326907s；log 误差 4.102984；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 68%]
tests/test_helper.py ...                                                 [ 69%]
tests/test_jsonpath.py ...                                               [ 69%]
tests/test_lineage.py ..................                                 [ 72%]
tests/test_optimizer.py ................................................ [ 80%]
.......                                                                  [ 81%]
tests/test_parser.py ................................................... [ 90%]
....                                                                     [ 90%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 596 passed, 2 skipped in 136.75s (0:02:16) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 20. swe-rebench:tobymao__sqlglot-3290:30

trace `sqlglot-reserved100__tobymao__sqlglot-3290.trace.jsonl:191`；action `tool_30_call_ab7mxlE4V9BzeLtLorRMkLHl`。

```sh
SQLGLOTRS_TOKENIZER=0 python3 -m pytest -q tests/dialects/test_presto.py && SQLGLOTRS_TOKENIZER=0 python3 -m pytest -q tests
```

实际 0.673039s；预测 99.563940s；绝对误差 98.890900s；log 误差 4.096152；桶 2→4（差 2）。

**主因 B / conditional_stage_composition：** 首段 Presto 测试失败；&& 后的全量测试未执行。 研究执行成功概率与条件路径合成；不能事后用真实退出状态选择预测分支。

次因/边界：D: 前序成功与否未观测；C: 部分子句规则/证据不足，但不足以解释未执行阶段被相加。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests/dialects/test_presto.py\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "7a0114b78b63a865cf340fd126a111899f6c15c20d0c8bf9d29c3f9d8f61a2d9", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=5，min/median/max(ms)=28.770275/829.907187/1468.797311。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests/dialects/test_presto.py\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5fa1add3866cb178e0f852e45e6543c75d139e5e4790658bacf835b9a6a51536，exact=True，覆盖证据 n=5，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 113978e3334b282efaab29572d05a1663a98699cb1a2ef751845f877201a22a6 | 208 | 0.5 | 0.083194676 | `["arg:[\"L1/pytest\",0,\"tests/dialects/test_presto.py\"]"]` |
| 423806c0d1bc767e95b7b8d757098feb6e3f7808526fa2a43fdab098f83affb4 | 5 | 0.5 | 0.083194676 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

子句 1：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "03f9a29ba4d1c2260a54b433135f9f3ba06d17e8dd3597bf0907c04fbf4e942a", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=4，min/median/max(ms)=1251.049907/116564.637323/140801.083208。选中特征：

```json
["arg:[\"L1/pytest\",0,\"tests\"]", "context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L1/pytest\",\"--quiet\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=9b743075ed85cc82b441120bd707dbc36308b39402a621c16613c8ba5c6527e2，exact=True，覆盖证据 n=4，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 113978e3334b282efaab29572d05a1663a98699cb1a2ef751845f877201a22a6 | 209 | 0.5 | 0.099800399 | `["arg:[\"L1/pytest\",0,\"tests\"]"]` |
| 1d63ff3e0ccf15b6351fed758b8aa2181c80a2d33c946b2e5475a0d6c7be5a23 | 8 | 0.5 | 0.099800399 | `["flag:[\"L1/pytest\",\"--quiet\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
============
______________________ TestPresto.test_group_by_distinct _______________________

self = <tests.dialects.test_presto.TestPresto testMethod=test_group_by_distinct>

    def test_group_by_distinct(self):
>       self.validate_identity(
            "SELECT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'), COUNT(*) "
            "FROM table_a "
            "GROUP BY DISTINCT(JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'))",
            "SELECT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'), COUNT(*) "
            "FROM table_a "
            "GROUP BY DISTINCT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b')",
        )

tests/dialects/test_presto.py:578: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
tests/dialects/test_dialect.py:31: in validate_identity
    self.assertEqual(write_sql or sql, expression.sql(dialect=self.dialect, pretty=pretty))
E   AssertionError: "SELE[91 chars]STINCT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b')" != "SELE[91 chars]STINCT (JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'))"
E   - SELECT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'), COUNT(*) FROM table_a GROUP BY DISTINCT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b')
E   + SELECT JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'), COUNT(*) FROM table_a GROUP BY DISTINCT (JSON_EXTRACT_SCALAR(CAST(extra AS JSON), '$.value_b'))
E   ?                                                                                                       +                                                     +
=========================== short test summary info ============================
FAILED tests/dialects/test_presto.py::TestPresto::test_group_by_distinct - As...
1 failed, 15 passed in 0.33s


Exit code: 1
```

### 21. swe-rebench:tobymao__sqlglot-3425:34

trace `sqlglot-reserved100__tobymao__sqlglot-3425.trace.jsonl:215`；action `tool_34_call_GUZX1nggUaSDVcUI6wK0zOQK`。

```sh
export PYSPARK_PYTHON=/opt/conda/envs/testbed/bin/python PYSPARK_DRIVER_PYTHON=/opt/conda/envs/testbed/bin/python SQLGLOTRS_TOKENIZER=0; /opt/conda/envs/testbed/bin/python -m unittest
```

实际 183.478088s；预测 2.103147s；绝对误差 181.374940s；log 误差 4.085114；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=partial，diagnostics=["missing_rule:unittest"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"unittest\"]", "tool=python"], "core": ["target:[\"unittest\"]", "tool=python"], "parse_digest": "6848c4082cc0723b9d13beaf3a0eda81bcc4040ab21d3449879c8a248817f74d", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=64，min/median/max(ms)=4724.593922/86408.306641/183478.087699。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"unittest\"]", "tool=python"]
```

Edge node=0ad529c7f5a608a722114958cb692de682ee713e7d4fbbf28372f1701e93cbbf，exact=True，覆盖证据 n=146，父边 0 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
I' could not bind on port 4040. Attempting port 4041.

[Stage 0:>                                                          (0 + 8) / 8]

                                                                                
.....
[Stage 30:=======>      (113 + 9) / 200][Stage 31:>                 (0 + 0) / 8]

[Stage 30:============> (172 + 8) / 200][Stage 31:>                 (0 + 0) / 8]

                                                                                
...............
[Stage 77:=============================================>          (26 + 6) / 32]

                                                                                
...........................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................
----------------------------------------------------------------------
Ran 1055 tests in 180.789s

OK


Exit code: 0
```

### 22. swe-rebench:tobymao__sqlglot-3339:25

trace `sqlglot-reserved100__tobymao__sqlglot-3339.trace.jsonl:161`；action `tool_25_call_4tcsOnkIxsbiVXs07Hge6NNg`。

```sh
export PYSPARK_PYTHON=/opt/conda/envs/testbed/bin/python
export PYSPARK_DRIVER_PYTHON=/opt/conda/envs/testbed/bin/python
export SQLGLOTRS_TOKENIZER=0
/opt/conda/envs/testbed/bin/python -m unittest
```

实际 183.048276s；预测 2.103147s；绝对误差 180.945129s；log 误差 4.082781；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=partial，diagnostics=["missing_rule:unittest"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"unittest\"]", "tool=python"], "core": ["target:[\"unittest\"]", "tool=python"], "parse_digest": "6848c4082cc0723b9d13beaf3a0eda81bcc4040ab21d3449879c8a248817f74d", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=64，min/median/max(ms)=4724.593922/86408.306641/183478.087699。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"unittest\"]", "tool=python"]
```

Edge node=0ad529c7f5a608a722114958cb692de682ee713e7d4fbbf28372f1701e93cbbf，exact=True，覆盖证据 n=146，父边 0 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                                                             
.....
[Stage 30:======>        (88 + 9) / 200][Stage 31:>                 (0 + 0) / 8]

[Stage 30:========>     (125 + 9) / 200][Stage 31:>                 (0 + 0) / 8]

[Stage 30:==========>   (154 + 8) / 200][Stage 31:>                 (0 + 0) / 8]

[Stage 31:>                                                         (0 + 8) / 8]

                                                                                
...............
[Stage 77:=================================================>      (28 + 4) / 32]

                                                                                
...................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................
----------------------------------------------------------------------
Ran 1047 tests in 180.302s

OK


Exit code: 0
```

### 23. swe-rebench:tobymao__sqlglot-4506:21

trace `sqlglot-dev100__tobymao__sqlglot-4506.trace.jsonl:137`；action `tool_21_call_jTkirtR5pSCQC7jEQh2kZu48`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest
```

实际 134.801276s；预测 1.290415s；绝对误差 133.510861s；log 误差 4.082460；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                 [ 48%]
tests/dialects/test_tsql.py ............................................ [ 55%]
...                                                                      [ 55%]
tests/test_build.py .                                                    [ 55%]
tests/test_diff.py ...........                                           [ 57%]
tests/test_executor.py ....................                              [ 60%]
tests/test_expressions.py .............................................. [ 67%]
.............                                                            [ 69%]
tests/test_generator.py ....                                             [ 70%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 71%]
tests/test_lineage.py ..................                                 [ 74%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
.....                                                                    [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 94%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 637 passed, 2 skipped in 133.91s (0:02:13) ==================


Exit code: 0
```

### 24. swe-rebench:tobymao__sqlglot-4505:22

trace `sqlglot-dev100__tobymao__sqlglot-4505.trace.jsonl:143`；action `tool_22_call_LJsmzX1wNDsernKZObr3NdHs`。

```sh
/opt/conda/envs/testbed/bin/python -m pytest
```

实际 134.736486s；预测 1.290415s；绝对误差 133.446071s；log 误差 4.081982；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 70%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 71%]
tests/test_lineage.py ..................                                 [ 74%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
.....                                                                    [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 94%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 637 passed, 2 skipped in 133.91s (0:02:13) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 25. swe-rebench:tobymao__sqlglot-3440:34

trace `sqlglot-reserved100__tobymao__sqlglot-3440.trace.jsonl:215`；action `tool_34_call_Zbvtjvd3vgGuErcwm9jJ3b6f`。

```sh
SQLGLOTRS_TOKENIZER=0 python3 -m unittest
```

实际 182.777896s；预测 2.103147s；绝对误差 180.674749s；log 误差 4.081311；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=partial，diagnostics=["missing_rule:unittest"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"unittest\"]", "tool=python"], "core": ["target:[\"unittest\"]", "tool=python"], "parse_digest": "aecb9ac590a9f9b16765c447f2e1d4ad0e8a68727afc6004319bda21df8e7069", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=64，min/median/max(ms)=4724.593922/86408.306641/183478.087699。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"unittest\"]", "tool=python"]
```

Edge node=0ad529c7f5a608a722114958cb692de682ee713e7d4fbbf28372f1701e93cbbf，exact=True，覆盖证据 n=146，父边 0 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
=====================>                    (121 + 10) / 200]

[Stage 37:=======================================>              (145 + 8) / 200]

[Stage 37:=================================================>   (186 + 11) / 200]

                                                                                
........................................
[Stage 296:===============================>                     (120 + 8) / 200]

[Stage 296:=============================================>       (170 + 8) / 200]

[Stage 299:============================================>        (169 + 9) / 200]

                                                                                
.................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................
----------------------------------------------------------------------
Ran 1054 tests in 179.861s

OK


Exit code: 0
```

### 26. swe-rebench:tobymao__sqlglot-3425:33

trace `sqlglot-reserved100__tobymao__sqlglot-3425.trace.jsonl:209`；action `tool_33_call_qZ5gTZLUSINl5pyh6IBeTnj6`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m unittest
```

实际 181.552477s；预测 2.103147s；绝对误差 179.449330s；log 误差 4.074621；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=partial，diagnostics=["missing_rule:unittest"]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"unittest\"]", "tool=python"], "core": ["target:[\"unittest\"]", "tool=python"], "parse_digest": "6848c4082cc0723b9d13beaf3a0eda81bcc4040ab21d3449879c8a248817f74d", "status": "partial", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=64，min/median/max(ms)=4724.593922/86408.306641/183478.087699。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"unittest\"]", "tool=python"]
```

Edge node=0ad529c7f5a608a722114958cb692de682ee713e7d4fbbf28372f1701e93cbbf，exact=True，覆盖证据 n=146，父边 0 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
(Iterator.scala:460)
	at scala.collection.Iterator$$anon$10.hasNext(Iterator.scala:460)
	at org.apache.spark.sql.catalyst.expressions.GeneratedClass$GeneratedIteratorForCodegenStage1.processNext(Unknown Source)
	at org.apache.spark.sql.execution.BufferedRowIterator.hasNext(BufferedRowIterator.java:43)
	at org.apache.spark.sql.execution.WholeStageCodegenEvaluatorFactory$WholeStageCodegenPartitionEvaluator$$anon$1.hasNext(WholeStageCodegenEvaluatorFactory.scala:43)
	at scala.collection.Iterator$$anon$10.hasNext(Iterator.scala:460)
	at org.apache.spark.shuffle.sort.BypassMergeSortShuffleWriter.write(BypassMergeSortShuffleWriter.java:140)
	at org.apache.spark.shuffle.ShuffleWriteProcessor.write(ShuffleWriteProcessor.scala:59)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:104)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:166)
	at org.apache.spark.scheduler.Task.run(Task.scala:141)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$4(Executor.scala:620)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:64)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:61)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:94)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:623)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:840)


----------------------------------------------------------------------
Ran 1055 tests in 175.555s

FAILED (errors=89)


Exit code: 1
```

### 27. swe-rebench:tobymao__sqlglot-4457:17

trace `sqlglot-dev100__tobymao__sqlglot-4457.trace.jsonl:113`；action `tool_17_call_M8bah0RWl7U9jg5YlloxeMcw`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 133.623090s；预测 1.290415s；绝对误差 132.332675s；log 误差 4.073746；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                 [ 48%]
tests/dialects/test_tsql.py ............................................ [ 55%]
..                                                                       [ 55%]
tests/test_build.py .                                                    [ 55%]
tests/test_diff.py ...........                                           [ 57%]
tests/test_executor.py ....................                              [ 60%]
tests/test_expressions.py .............................................. [ 67%]
.............                                                            [ 69%]
tests/test_generator.py ....                                             [ 70%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 71%]
tests/test_lineage.py ..................                                 [ 74%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
.....                                                                    [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 94%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 635 passed, 2 skipped in 131.46s (0:02:11) ==================


Exit code: 0
```

### 28. swe-rebench:tobymao__sqlglot-4488:14

trace `sqlglot-dev100__tobymao__sqlglot-4488.trace.jsonl:95`；action `tool_14_call_D8oHlpU1q9YCNGqDSnYTS84H`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/python -m pytest
```

实际 132.770223s；预测 1.290415s；绝对误差 131.479808s；log 误差 4.067391；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "python", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"], "core": ["target:[\"pytest\"]", "tool=python"], "parse_digest": "491d93e238dcb6572d62e30121a5ee1feb10b4f767ef74a143340d01a1f9db7e", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=87，min/median/max(ms)=816.306253/1732.31537/144138.321663。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "mode:[\"module\"]", "target:[\"pytest\"]", "tool=python"]
```

Edge node=5f4319b054cbf41f81f68835657b13e127c7c922c01e34bc41bff8311e94c426，exact=True，覆盖证据 n=586，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 1d00431cb296a1bd997b9d19fb91114d229346dbd09337f9a8814ccf37bb48d7 | 875 | 0.3709922 | 0.00063268115 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 70%]
tests/test_helper.py ...                                                 [ 70%]
tests/test_jsonpath.py ...                                               [ 71%]
tests/test_lineage.py ..................                                 [ 74%]
tests/test_optimizer.py ................................................ [ 81%]
.......                                                                  [ 82%]
tests/test_parser.py ................................................... [ 90%]
.....                                                                    [ 91%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py ..s                                                   [ 94%]
tests/test_tokens.py .......                                             [ 95%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

================== 637 passed, 2 skipped in 131.88s (0:02:11) ==================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```

### 29. swe-rebench:tobymao__sqlglot-5015:15

trace `sqlglot-dev100__tobymao__sqlglot-5015.trace.jsonl:101`；action `tool_15_call_y71ITdfkCdyB4SVhK3PBorSd`。

```sh
SQLGLOTRS_TOKENIZER=0 /opt/conda/envs/testbed/bin/pytest -q
```

实际 143.890052s；预测 1.481545s；绝对误差 142.408506s；log 误差 4.067094；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "pytest", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L0/pytest\",\"--quiet\"]", "tool=pytest"], "core": ["tool=pytest"], "parse_digest": "e50cc1b9dcf0a6922b2daab267705e921112f64e1ecd4858ff58f49e351fe8de", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=26，min/median/max(ms)=1132.289925/92873.632323/143890.051664。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "flag:[\"L0/pytest\",\"--quiet\"]", "tool=pytest"]
```

Edge node=be36649bad15a2d11167b22670bb1695e2e3bc86c003aa016feb8bd7907c7e4d，exact=True，覆盖证据 n=85，父边 2 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 0c02205d4bfe7b1bc11dbe602572f6d74c1dd2d1a53517e55ef1838c4ac92547 | 94 | 0.51917499 | 0.0060348713 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |
| 5707bc7bc6696193a08936039361ee4899933f2012f8411efafa60a8b804e42b | 40 | 0.5 | 0.0058119818 | `["flag:[\"L0/pytest\",\"--quiet\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
          self.returncode = sys.maxsize
            except ChildProcessError:
                pass
    
            try:
                exception_name, hex_errno, err_msg = (
                        errpipe_data.split(b':', 2))
                # The encoding here should match the encoding
                # written in by the subprocess implementations
                # like _posixsubprocess
                err_msg = err_msg.decode()
            except ValueError:
                exception_name = b'SubprocessError'
                hex_errno = b'0'
                err_msg = 'Bad exception data from child: {!r}'.format(
                              bytes(errpipe_data))
            child_exception_type = getattr(
                    builtins, exception_name.decode('ascii'),
                    SubprocessError)
            if issubclass(child_exception_type, OSError) and hex_errno:
                errno_num = int(hex_errno, 16)
                child_exec_never_called = (err_msg == "noexec")
                if child_exec_never_called:
                    err_msg = ""
                    # The error must be from chdir(cwd).
                    err_filename = cwd
                else:
                    err_filename = orig_executable
                if errno_num != 0:
                    err_msg = os.strerror(errno_num)
>               raise child_exception_type(errno_num, err_msg, err_filename)
E               FileNotFoundError: [Errno 2] No such file or directory: 'python'

/opt/conda/envs/testbed/lib/python3.9/subprocess.py:1837: FileNotFoundError
=========================== short test summary info ============================
FAILED tests/dialects/test_dialect.py::TestDialect::test_lazy_load - FileNotF...
1 failed, 696 passed, 2 skipped in 143.12s (0:02:23)


Exit code: 1
```

### 30. swe-rebench:tobymao__sqlglot-3806:18

trace `sqlglot-dev100__tobymao__sqlglot-3806.trace.jsonl:119`；action `tool_18_call_6NRjgS10RjxAOSc8Joc2EESf`。

```sh
/opt/conda/envs/testbed/bin/pytest
```

实际 147.366012s；预测 1.544771s；绝对误差 145.821241s；log 误差 4.065642；桶 4→2（差 2）。

**主因 B / heterogeneous_exact_signature：** 真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。 区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。

次因/边界：D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载。

子句 0：parse=complete，diagnostics=[]。

```json
{"tool": "pytest", "features": ["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "tool=pytest"], "core": ["tool=pytest"], "parse_digest": "9f90dc024c589b02b3ce5bf1680ebb94dbf8bdecb024d3441528e4fc3dc456bb", "status": "complete", "omitted_features": 0, "format": "command-features.v1"}
```

Lattice exact=True，候选 1 个；选中样本 n=18，min/median/max(ms)=1064.136583/49513.6637785/147366.011702。选中特征：

```json
["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]", "tool=pytest"]
```

Edge node=5707bc7bc6696193a08936039361ee4899933f2012f8411efafa60a8b804e42b，exact=True，覆盖证据 n=125，父边 1 条。

| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |
|---|---:|---:|---:|---|
| 566470f64f11c5e60a01696e83a957aacba2d774a258f712dcb3514bc264d896 | 97 | 1.0351309 | 0.0082123828 | `["context:[\"repo\",\"swe-rebench:tobymao/sqlglot\"]"]` |

历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。

原始执行输出末尾：

```text
                    [ 67%]
tests/test_helper.py ...                                                 [ 67%]
tests/test_jsonpath.py ...                                               [ 68%]
tests/test_lineage.py ..................                                 [ 71%]
tests/test_optimizer.py ................................................ [ 80%]
.......                                                                  [ 81%]
tests/test_parser.py ................................................... [ 90%]
...                                                                      [ 90%]
tests/test_schema.py .........                                           [ 92%]
tests/test_serde.py ....                                                 [ 93%]
tests/test_time.py .                                                     [ 93%]
tests/test_tokens.py .......                                             [ 94%]
tests/test_transforms.py .....                                           [ 95%]
tests/test_transpile.py ..........................                       [100%]

======================= 567 passed in 146.49s (0:02:26) ========================
/opt/conda/envs/testbed/lib/python3.9/site-packages/pytest_asyncio/plugin.py:217: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))


Exit code: 0
```
