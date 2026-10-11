# Command rules

Lattice and Edge use the same rule-derived command features. The bundled rules cover `python`, `pytest`, `git`, `grep`, `sed`, `find`, `ls`, `cat`, `pip`, `head`, `tail`, `bash`, `env`, `nice`, `timeout`, `make`, and `npm`. Fig supplies static syntax; checked overrides supply interpreter boundaries and corrections. Dynamic completion callbacks are never executed.

`python -`, `python -m pytest`, and `python script.py` have explicit execution modes and entry identities. Interpreter options stop at the entry boundary. Only the executable and execution entry chain are core matching features; ordinary operands can be dropped by the existing subset matcher. Unknown argument tails remain ordered opaque features. A partial parse can therefore still produce a KB prediction.

Inspect a command without executing it or contacting a model:

```sh
PYTHONPATH=services/sidecar/src python -m command_parser -- python -m pytest -m slow tests
PYTHONPATH=services/sidecar/src python -m command_parser --repo owner/repo --runtime-id runtime-1 --workspace /work --cwd /work --cache /path/to/artifacts/command-rules -- python scripts/check.py --count 4
```

The output includes parsed layers, token roles, diagnostics, used rule hashes, and the exact feature/core sets. `complete` means the declared grammar consumed the command; it is not proof of runtime success or prediction accuracy. `partial` retains unsupported syntax; `invalid` identifies a known missing value or operand. Large argument payloads become hashes, and matching features are capped at 20.

## Automatic rule generation

Missing rules and unknown option branches enqueue background generation. The first call uses conservative features immediately. A rule published during execution applies to subsequent calls; completion learning keeps the original call's features. Generated rules use the same [JSON grammar](../contracts/command-rule.schema.json) as bundled rules.

Generation inherits the sidecar's upstream URL, API key and explicitly configured model. Override any of them independently with:

- `CLAWTUNE_COMMAND_RULE_LLM_BASE_URL`
- `CLAWTUNE_COMMAND_RULE_LLM_API_KEY`
- `CLAWTUNE_COMMAND_RULE_LLM_MODEL`

The provider must support OpenAI-compatible chat completions with JSON output. Missing configuration leaves generation disabled. The queue permits one active job, 64 waiting jobs, 10 new jobs per repository per process, a 45-second request timeout, one repair request, and a 10-minute retry cooldown. Cached rules live in the sidecar artifact directory's `command-rules/` folder and are validated on reload.

The plugin sends runtime context using `CLAWTUNE_EXEC_WORKDIR`, `CLAWTUNE_SANDBOX_HOST_WORKSPACE`, `CLAWTUNE_SANDBOX_CONTAINER_WORKSPACE`, `CLAWTUNE_SANDBOX_CONTAINER_ID`, and `CLAWTUNE_SANDBOX_IMAGE`. Set these to the actual runtime paths and image; a container path must not be supplied as a host workspace. Native repositories use the host workspace and cwd. Without a registered workspace, source-based generation is unavailable.

Native discovery checks root/scripts Python files and declared `pyproject.toml`/`package.json` entries. Source reads are bounded to the registered workspace; direct local Python helper imports participate in the source hash. Docker reads use the named container's bounded file archive API; container discovery currently happens on invocation. Changes to readable source invalidate its rule. Schema, spelling-evidence and fixture checks reject unsupported generated output, but they do not establish that every model-inferred arity is correct.

Help probes require a locally available Docker image and, when needed, a read-only host/workspace mapping. They run in a separate container with networking disabled, resource/time/output limits, dropped capabilities and cleared image environment secrets. Probes wait outside the sidecar's active measured execution windows. No unrestricted native `--help` fallback is used. Missing isolation or unsuccessful help leaves the command partially parsed.

## State and offline evaluation

Start a new writable state directory for this representation. Existing `seeds/bootstrap-v1` files remain unchanged: initialization retains Tool/Trie data and initializes incompatible Lattice/Edge components empty. Current-format training snapshots load normally; old feature snapshots are rejected when loaded directly.

The offline trace path trains run-local KBs and freezes bundled rules before held-out evaluation. It makes no LLM calls and does not modify release seeds or input traces. Its report includes parse-status counts; those counts are coverage diagnostics, not semantic correctness labels. See [offline evaluation](benchmarks.md#5-fixed-trace-offline-evaluation).
