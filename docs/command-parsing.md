# Command rules

Lattice and Edge use the same rule-derived command features. The bundled rules cover `python`, `pytest`, `git`, `grep`, `sed`, `find`, `ls`, `cat`, `pip`, `head`, `tail`, `bash`, `env`, `nice`, `timeout`, `make`, `npm`, and the `cargo build/check/test/fmt` commands. Pytest rules include xdist's `-n`/`--numprocesses`. Fig supplies static syntax; checked overrides supply interpreter boundaries and corrections where completion specifications differ from parsing grammar. Cargo uses a curated rule because its Fig specification is dynamic. Dynamic completion callbacks are never executed.

`python -`, `python -m pytest`, and `python script.py` have explicit execution modes and entry identities. Interpreter options stop at the entry boundary. Only the executable and execution entry chain are core matching features; ordinary operands can be dropped by the existing subset matcher. A partial parse can still produce a KB prediction.

Entries without a bundled or applicable cached rule use a local heuristic, even without LLM configuration: `--key=value`, `--key value`, and `-k value` become option/value features; 2–3 letter short clusters such as `-xz` become separate flags; `--` ends option parsing. Negative numbers can be values, and positional operands keep their order. Different option names are independently matchable; repeated values of the same option keep their order. This fallback remains `partial` with a `heuristic_arguments:<entry>` diagnostic: a boolean flag followed by a filename may be mistaken for an option/value pair, and short clusters may be ambiguous. It does not infer subcommands or option aliases. Unresolved expansions stop the heuristic and preserve the remaining tail as opaque. Known rules retain their declared arities; unknown options within a known rule still leave an opaque tail.

Lattice candidates and Edge borrowing parents must retain every core feature of the query. Trie and single-command Tool prefix fallback also retain the known executable and execution entry. For example, a single pytest module query cannot fall back to a generic Python node; no matching entry evidence produces an unavailable prediction. Execution mode remains optional. These constraints apply to time and resource predictions and their composed-call outputs.

Tool predicts compound shell calls from an exact command match or the longest available whole-command token prefix (up to four tokens). A prefix may end within the first command; its samples describe complete calls sharing that prefix, including their later commands. Tool does not decompose these labels into clauses. Calls without a command retain tool-level matching. Tool and Trie preserve their per-target exact-key preference. Their public binary/tool aggregates do not identify module or script entries, so an entry-specific single-command query needs suitable repository evidence. Existing snapshots remain readable; coarse history is retained but may no longer qualify for an entry-specific prediction.

When Lattice has observations with the query's complete matching feature set, it uses those observations for the prediction and evidence count, even if there is only one. More specific commands remain in a separate coverage distribution for generalized queries or nodes without their own observations. An exact feature match does not guarantee identical source files, environment, or workload, and a small evidence count is not a calibrated confidence score.

Edge retains its weighted borrowing policy: the selected node's coverage observations have unit weight, and eligible parent-difference observations receive kappa weights. Its exact-node evidence can include more specific commands; it does not use Lattice's exact-own-only policy. Entry constraints also apply during online weight learning. Pending or replayed feedback created under the older policy retains its observations, but local weight updates based on cross-entry borrowing are skipped.

Inspect a command without executing it or contacting a model:

```sh
PYTHONPATH=services/sidecar/src python -m command_parser -- python -m pytest -m slow tests
PYTHONPATH=services/sidecar/src python -m command_parser --repo owner/repo --runtime-id runtime-1 --workspace /work --cwd /work --cache /path/to/artifacts/command-rules -- python scripts/check.py --count 4
```

The output includes parsed layers, token roles, diagnostics, used rule hashes, and the exact feature/core sets. `complete` means the declared grammar consumed the command; it is not proof of runtime success or prediction accuracy. `partial` retains unsupported syntax; `invalid` identifies a known missing value or operand. Large argument payloads become hashes. The default matching budget is 20 features: core and context come first, then options before operands. When the budget is exceeded, one `overflow` feature preserves the remaining content together (hashed when large). Those remaining parameters can distinguish commands but cannot match independently; `omitted_features` counts them.

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

Stored observations keep their bound features. Older opaque-only observations are not automatically reinterpreted by the heuristic; they remain available through compatible broader contexts, but lack its parameter-level features. Retrain from original observations into a new state directory when historical parameter-level matching is needed.

Rule corrections and overflow preservation apply to newly parsed commands. Existing bound observations retain their original features, including any previously discarded parameters; reloading a snapshot does not recover those parameters.
