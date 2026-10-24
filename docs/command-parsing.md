# Command rules

Lattice and EdgeKappa extract command features from syntax rules. Bundled rules cover `python`, `pytest`, `git`, `grep`, `sed`, `find`, `ls`, `cat`, `pip`, `head`, `tail`, `bash`, `env`, `nice`, `timeout`, `make`, `npm`, and `cargo build/check/test/fmt`. Pytest includes `-n`/`--numprocesses`. Rules describe syntax; they do not execute shell-completion callbacks.

`python -`, `python -m pytest`, and `python script.py` have explicit execution modes and entry identities. Interpreter options stop at the entry boundary. Only the executable and execution entry chain are core matching features; ordinary operands can be dropped by the existing subset matcher. A partial parse can still produce a KB prediction.

Entries without a bundled or applicable cached rule use a local heuristic, even without LLM configuration: `--key=value`, `--key value`, and `-k value` become option/value features; 2–3 letter short clusters such as `-xz` become separate flags; `--` ends option parsing. Negative numbers can be values, and positional operands keep their order. Different option names are independently matchable; repeated values of the same option keep their order. This fallback remains `partial` with a `heuristic_arguments:<entry>` diagnostic: a boolean flag followed by a filename may be mistaken for an option/value pair, and short clusters may be ambiguous. It does not infer subcommands or option aliases. Unresolved expansions stop the heuristic and preserve the remaining tail as opaque. Known rules retain their declared arities; unknown options within a known rule still leave an opaque tail.

Matching distinguishes executable entries such as `python -m pytest` and
`python script.py`. Command-prefix fallback does not discard a known module or
script identity. Broader tool-level fallback remains available in ToolKB.
The [prediction methods](technical-report.md#3-historical-evidence-and-empirical-predictions)
describe each model's matching order and limits.

Inspect a command without executing it or contacting a model:

```sh
PYTHONPATH=services/sidecar/src python -m command_parser -- python -m pytest -m slow tests
PYTHONPATH=services/sidecar/src python -m command_parser --repo owner/repo --runtime-id runtime-1 --workspace /work --cwd /work --cache /path/to/artifacts/command-rules -- python scripts/check.py --count 4
```

The output includes parsed layers, token roles, diagnostics, used rule hashes, and the exact feature/core sets. `complete` means the declared grammar consumed the command; it is not proof of runtime success or prediction accuracy. `partial` retains unsupported syntax; `invalid` identifies a known missing value or operand. Large argument payloads become hashes. The default matching budget is 20 features: core and context come first, then options before operands. When the budget is exceeded, one `overflow` feature preserves the remaining content together (hashed when large). Those remaining parameters can distinguish commands but cannot match independently; `omitted_features` counts them.

## Automatic rule generation

Missing rules and unknown option branches enqueue background generation. The first call uses conservative features immediately. A rule published during execution applies to subsequent calls; completion learning keeps the original call's features. Generated rules use the same [JSON grammar](../contracts/command-rule.schema.json) as bundled rules.

Generation inherits the sidecar's upstream URL, API key and explicitly configured model. Its URL and model can be overridden independently with:

- `CLAWTUNE_COMMAND_RULE_LLM_BASE_URL`
- `CLAWTUNE_COMMAND_RULE_LLM_MODEL`

Rule generation uses the same sidecar API key as the proxy; see [credential configuration](getting-started.md#2-daily-openclaw-operation). The provider must support OpenAI-compatible chat completions with JSON output. Generation is disabled when credentials or an explicit model are missing. Requests run in the background with a 45-second timeout and a 10-minute retry cooldown; ordinary tool calls do not wait for them. Cached rules are saved under `command-rules/` in the service artifact directory.

The plugin sends runtime context using `CLAWTUNE_EXEC_WORKDIR`, `CLAWTUNE_SANDBOX_HOST_WORKSPACE`, `CLAWTUNE_SANDBOX_CONTAINER_WORKSPACE`, `CLAWTUNE_SANDBOX_CONTAINER_ID`, and `CLAWTUNE_SANDBOX_IMAGE`. Set these to the actual runtime paths and image; a container path must not be supplied as a host workspace. Native repositories use the host workspace and cwd. Without a registered workspace, source-based generation is unavailable.

Source-based generation reads Python scripts and declared `pyproject.toml`/`package.json` entries within the registered workspace. Source changes invalidate affected cached rules. Generated rules are checked against the schema and examples, but model-inferred syntax can still be wrong; inspect the parser output when a command is misclassified.

Help probes require a locally available Docker image and, when needed, a read-only host/workspace mapping. They run in a separate container with networking disabled, resource/time/output limits, dropped capabilities and cleared image environment secrets. Probes wait outside the sidecar's active measured execution windows. No unrestricted native `--help` fallback is used. Missing isolation or unsuccessful help leaves the command partially parsed.

## State and offline evaluation

Offline evaluation freezes the available rules and makes no LLM calls. It does
not change input traces or initialization bundles. Parse-status counts describe
syntax coverage, not prediction accuracy. See [offline evaluation](benchmarks.md#5-fixed-trace-offline-evaluation).

Rule changes apply to newly parsed commands. Saved observations retain their
original features; loading a snapshot does not reinterpret old arguments.
Rebuild from original traces into new state when historical observations need
the updated rules. See [state compatibility](getting-started.md#rebuilding-and-compatibility).
