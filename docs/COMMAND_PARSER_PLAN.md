# Command parser implementation plan

Status: implementation specification. Scope and the four choices marked "Confirmed" were agreed on 2026-10-11; the remaining details are concrete engineering defaults for implementation. This document specifies work to implement; it does not claim that the new parser already exists.

## 1. Scope and decisions

- `phase1` archives the previous implementation and its research artifacts. Development continues on `main`.
- The new design is the only supported command representation. Replace the heuristic normalizer; do not add a legacy mode, snapshot migration, dual reads, or preservation of old learned state. Start the new release with fresh KB state and remove phase1 seeds from its default initialization.
- Keep the Lattice/Edge node construction, subset coverage, selection, resource eligibility, and learning algorithms. Their inputs change to rule-derived features. Algorithmic prediction improvement is a separate concern.
- Reuse mvdan shell parsing. Do not change OpenClaw core or collector attribution. One measured static shell clause remains one observation even when its syntax contains wrappers and delegated commands.
- Fig imports and LLM generation produce the same declarative JSON rules. JSON Schema is authoritative. Neither rule source supplies matching weights, core-feature settings, resource estimates, or executable parser callbacks.
- Confirmed: unknown commands trigger asynchronous generation; the first prediction does not wait.
- Confirmed: rule generation inherits the configured upstream provider/model, with independent overrides available.
- Confirmed: automatic help execution is allowed in a restricted environment. Lack of an isolation provider makes this evidence source unavailable; it does not authorize unrestricted execution.
- Confirmed: only executable and execution entry identities belong to `core`; ordinary files, search expressions, and other operands are optional features. Execution mode is also optional. No new hard barrier is added to subset matching.
- No unresolved product decision is currently blocking the first implementation milestone. Change this document and consult the user if implementation evidence requires changing these decisions.

## 2. One data path

```text
Pinned Fig source -> static converter -> bundled CommandSchema JSON --+
                                                                    |
Repository/tool evidence -> async LLM -> validation -> cached JSON ---+-> RuleRegistry
                                                                          |
raw shell + execution context -> existing mvdan AST -> CommandParser <-----+
                                                          |
                                                     ParsedCommand
                                                          |
                                                    FeatureProjector
                                                          |
                                                    CommandFeatures
                                                          |
                        Lattice/Edge build, query, completion, persistence
```

Parse from the existing clause's ordered words and word intents. Do not reconstruct an executable shell string and parse it again inside the KB. Raw text remains available for diagnostics and display only.

Prediction uses only evidence available before execution. Runtime-expanded argv or source files produced by the command must not replace the inputs of that command's earlier prediction.

## 3. Contracts and ownership

Add these schemas under `contracts/`, with `additionalProperties: false` at defined object boundaries and bounded collections/strings:

| Contract | Required information |
| --- | --- |
| `command-rule.schema.json` | Format identifier, command identities, command grammar, evidence/provenance, applicability, verification fixtures |
| `parsed-command.schema.json` | Original clause identity, ordered invocation layers, entry/mode, parsed occurrences, token spans, opaque regions, diagnostics, used rule digests |
| `command-features.schema.json` | Format identifier, exactly one tool identity, unique sorted feature/core arrays, projection status, parse digest |
| `command-context.schema.json` | Runtime identity, repository identity, runtime cwd, workspace mapping, optional tool version/environment fingerprint |
| `command-evidence.schema.json` | Source identity, content hash, source kind, bounded text, acquisition time, and source availability |

Python dataclasses are runtime representations, validated against the schemas at file/protocol boundaries. Do not create a competing set of handwritten public validation rules.

New implementation package: `services/sidecar/src/command_parser/`:

| Module | Responsibility |
| --- | --- |
| `models.py`, `validation.py` | Internal records; Schema loading and semantic validation |
| `shell.py` | Adapt existing mvdan clause data; preserve causal and lexical information |
| `parser.py` | Deterministic grammar engine; no per-tool Python branches |
| `projection.py` | Sole mapping to KB features/core |
| `registry.py` | Rule lookup, applicability, immutable rule records, atomic activation |
| `evidence.py`, `help_runner.py` | Runtime-correct file/document/help acquisition |
| `generation.py` | Provider client, bounded queue, generation and repair prompts |
| `service.py` | Shared parser/registry lifecycle and pre-execution feature cache |
| `rules/` | Bundled converted JSON rules and import manifest |

Integration code belongs in `clawtune_sidecar` and `tool_time`. Change `tool_resource` only where carrying pre-execution clause identity/context to eligible completion observations is necessary; do not alter memory measurement or execution ownership.

## 4. Rule grammar

Rules contain a root command and nested subcommand definitions. Each has stable rule-local IDs; spelling aliases map to the same ID. An option's canonical spelling is explicit in generated JSON. The converter chooses a declared long spelling when available, otherwise a declared short spelling, with a deterministic lexical tie-break. LLM output must use an evidenced spelling, not invent a new flag.

Required grammar fields:

- `names`: executable/subcommand spellings; aliases apply only when declared.
- `options`: ID, spellings, canonical spelling, value slots with `min/max` arity, permitted attachment forms, optional-value consumption, dash-value policy, and repetition semantics.
- `positionals`: ordered slots, arity, and role (`operand`, `script_entry`, `module_entry`, `command_tail`, or `opaque_payload`). Only a trailing slot may be variadic unless a literal terminator makes its boundary unambiguous.
- `subcommands`: child grammar plus declared inheritance of parent options. Parent options must not automatically become valid after every subcommand.
- `syntax`: short-cluster support, long `=` support, option/operand intermixing, end-of-options token, exact option matching versus declared abbreviation, and optional literal terminators for command/expression tails.
- `transitions`: declarative trigger by option/positional/subcommand; select execution mode, consume entry/payload, stop the current grammar, and optionally delegate remaining tokens to another registered command or repository script.
- `applicability`: global tool family or specific repository/runtime entry; optional evidenced tool-version constraints. Unknown version does not satisfy a version-specific rule.
- `evidence`: origin, source revision/content hashes, and text/code spans supporting generated grammar. Keep unsupported constructs explicit.

The grammar has no arbitrary expressions, regex callbacks, network access, imports, or code execution. Transitions use a fixed engine-defined set of operations. They may consume tokens but must never rewind into an already consumed parent layer. Reject overlapping aliases, unresolved references, non-progressing transitions, and cyclic delegation without token consumption.

Initial engine limits: 256 KiB per rule, at most eight invocation layers, and at most 16,384 word records per command. Honor any stricter existing shell-input limit. Limit violations yield explicit partial/unavailable diagnostics; they must not produce an apparently complete truncated parse. Reference resolution is bounded and cycle-checked. Matching-feature limits are separate from parser-input limits.

Token consumption is deterministic:

1. Resolve the current grammar and explicit parsing state.
2. Match an exact option spelling or declared attached form; interpret short clusters only if that grammar allows them. A value-taking cluster member consumes its remainder according to the declared form.
3. Consume required/optional values using the rule's arity, attachment and dash-value policy. Do not assume every token beginning with `-` is an option, or that every following non-option is a value.
4. Apply an entry/delegation transition at its declared boundary. Interpreter options stop there.
5. Otherwise match the next positional slot or an allowed subcommand.
6. At an unknown option/ambiguous boundary, stop semantic consumption of that region and preserve its remaining ordered tokens as opaque. Do not guess the option arity and shift all subsequent operands.

Unknown commands still produce a partial parse with executable identity and opaque arguments. Missing required values and invalid known forms produce `invalid`; unavailable grammar or unresolved expansion produces `partial`; complete means all applicable tokens have a supported interpretation, not proof that runtime execution will succeed.

Preserve quotes/expansion provenance from mvdan. A quoted literal `$x` and an unresolved expansion `$x` are different. Do not evaluate substitutions, glob the local filesystem on behalf of a remote command, or infer runtime argument counts. Literal `cd` and wrapper cwd changes may update the parse context only when shell control flow proves the resulting cwd; otherwise record unknown cwd.

## 5. Invocation layers and initial coverage

Retain invocation layers in order. A wrapper remains the outer executable identity in the first implementation; its explicit delegated executable/entry chain contributes to the one target identity, not another top-level `tool=` feature. For example, `env python -m pytest` has `tool=env` and `target:["python","pytest"]`. Wrapper flags remain optional. Do not silently equate `env python ...` with `python ...`, or `pytest ...` with `python -m pytest ...`.

Interpreter entry behavior must be covered explicitly:

- `python -u script.py`: interpreter flag, then script entry, then script-owned arguments.
- `python -m pytest -m slow`: module entry `pytest`, then pytest's marker option.
- `python -c '...' -m pytest`: inline source payload, then user arguments; no module transition from the later `-m`.
- `python - args...`: stdin execution, then user arguments; `-` is not an ordinary option.
- Bare `python`: mode `default`; infer stdin versus interactive behavior only if pre-execution input/TTY evidence establishes it.
- `bash -c '...'`: opaque inline shell payload in the parent clause. Existing mvdan clause ownership is authoritative; do not synthesize extra labeled observations from nested text.

First import/verification batch: `python`, `pytest`, `git`, `grep`, `sed`, `find`, `ls`, `cat`, `pip`, `head`, `tail`, `bash`, `env`, `nice`, `timeout`, `make`, `npm`. These cover the observed dominant families and exercise interpreters, subcommands, operands, expressions, and wrappers. This is a verification order, not a permanent allowlist.

For complex expression languages such as `find` predicates and sed programs, initially preserve expression payloads and boundaries. Add generic terminator-based forms where adequate, e.g. a `find -exec` tail ending at `;` or `+`. Do not pretend CLI option rules fully parse an embedded language. Missing Fig specs use the same rule-generation/curation path as other missing tools, with their provenance recorded.

## 6. Fig import

Initial source pin: `withfig/autocomplete` commit `aef52acff84c45edde61ae610cc2c964802b9a38`.

Implement `scripts/command_rules/import_fig.mjs` using the TypeScript compiler AST API, in a small tooling package with a lockfile. Statically resolve literals, arrays, objects, bounded local constant references/spreads and literal relative imports. Do not regex-extract objects or execute imported specs. Unresolved expressions produce a location-specific import report; a grammar-critical unresolved field marks that branch partial rather than silently becoming a zero-argument option.

Convert `name`, `options`, `subcommands`, `args`, optional/variadic argument declarations and repeatability where their semantics are supported. Drop descriptions, icons, suggestions and completion-only generators from runtime grammar. Dynamic grammar (`generateSpec`/computed `loadSpec`) is an unresolved grammar source unless a declarative override supplies it.

Keep explicit corrections and extra entry/delegation semantics in `scripts/command_rules/overrides/`. Apply each override to its pinned import with checked targets; fail on an unmatched override. Emit bundled JSON, a manifest (source SHA, per-rule hashes, converter format, coverage), and a diagnostic report. Retain the upstream license and attribution. No runtime download or TypeScript dependency in the sidecar.

Source references: [Fig repository](https://github.com/withfig/autocomplete), [Python spec](https://github.com/withfig/autocomplete/blob/aef52acff84c45edde61ae610cc2c964802b9a38/src/python.ts), [pytest spec](https://github.com/withfig/autocomplete/blob/aef52acff84c45edde61ae610cc2c964802b9a38/src/pytest.ts).

## 7. Registry, discovery and LLM generation

Lookup precedence is deterministic: exact repository-script/content identity, matching runtime/tool/version rule, bundled generic rule, then unknown. An automatically generated script rule cannot shadow a global executable by basename. A generic Fig rule may parse known tokens while a missing branch triggers generation for the narrower runtime identity.

The registry stores immutable JSON objects by content hash. Cache identity includes runtime identity, resolved entry path or module, source content/dependency hashes when available, and observed tool version. Hash dirty working-tree files; Git commit alone is insufficient. Unknown tool versions keep generated rules runtime-scoped. These cache identities are metadata, not KB matching features.

Discovery triggers:

- At repository registration, inspect entry manifests (`pyproject.toml`, `package.json`, known script directories) and enqueue a bounded set of declared entries. No recursive full-repository LLM scan.
- At a missing rule or unsupported branch, enqueue that exact entry. Deduplicate concurrent requests.
- When an entry or evidenced local dependency changes, invalidate its cached applicability and request a new rule. Validate identity again before activating a completed generation task.

Generation input consists of the unified schema, bounded source/help/config excerpts, actual invocation examples and their provenance. Do not send resource labels, desired predictions or a request to invent feature weights. For Python scripts, include argument declarations and directly referenced local parser helpers when obtainable. A sample invocation alone does not establish option arity.

Generation lifecycle: `missing -> queued -> generating -> validating -> ready`, or `unavailable/failed`. Activate only after JSON Schema checks, semantic grammar checks and source-grounded fixtures pass. A model-authored fixture is not independent evidence of correctness; retain partial coverage for unsupported claims. No per-command manual approval gate is added.

Default limits, configurable without changing semantics: one active generation per sidecar, queue length 64, 10 new generations per repository per run, 64 KiB evidence text per request, 45-second provider timeout, at most one repair request for a structurally invalid response, and a 10-minute failed-evidence cooldown. Evidence changes reset the cooldown. Queue saturation or missing provider configuration leaves deterministic partial parsing available.

Inherit the upstream URL/key/model. Overrides: `CLAWTUNE_COMMAND_RULE_LLM_BASE_URL`, `CLAWTUNE_COMMAND_RULE_LLM_API_KEY`, `CLAWTUNE_COMMAND_RULE_LLM_MODEL`; empty values inherit. If no explicit usable model is configured, report generation unavailable rather than guessing a provider's model. Use a separate HTTP client/job queue; do not send internal requests through the agent-facing proxy or write them as agent turns.

## 8. Evidence and restricted help execution

Introduce an `EvidenceReader` interface. First release supports native local workspaces and explicitly identified Docker workspaces. Resolve scripts using runtime cwd/root mapping and read the exact source belonging to that runtime. Do not join a container path onto the sidecar's current directory. Other runtimes can supply the same evidence bundle; absent a reader/bundle, script generation remains unavailable rather than reading a similarly named host file.

For Docker evidence, use bounded archive/file reads from the identified container, and reject traversal/symlink escape outside authorized roots. For local evidence, resolve paths and enforce the registered workspace boundary. Script input dependencies remain bounded and must be attributable to that entry.

Automatic help probes run in an ephemeral isolated environment representing the target tool, never inside its active measured execution scope. For an image-backed tool, use the same image digest and an explicitly read-only workspace mount. Native Linux help requires an available isolation provider with equivalent guarantees; a process timeout alone is insufficient.

Required probe controls: no network, no provider credentials/inherited secrets, no host/Docker socket, no writable repository, read-only root plus disposable tmpfs, dropped capabilities, no-new-privileges, process limit 64, memory limit 256 MiB, CPU limit 1, wall timeout 3 seconds, and combined output limit 64 KiB. Use an argv array, not shell interpolation. Schedule probes outside active benchmark measurement windows; otherwise defer them. Failure/timeout produces unavailable evidence, never an unrestricted retry.

Probe a tool as `<resolved executable> --help`, a Python module as `<interpreter> -m <module> --help`, and a supported repository script through its established interpreter with `--help`. Do not append help to the entire original user command with its side-effecting arguments. Do not automatically execute arbitrary nested subcommands or package scripts to discover their grammar. Static package-script declarations may still supply evidence.

## 9. Feature projection contract

`CommandFeatures = {format, tool, features, core, parse_digest, status}`. In memory, feature collections are `frozenset[str]`; persisted arrays are sorted and unique. `core` must be a subset of `features` and contain exactly the single executable feature `tool=<canonical-name>`.

For other features, serialize the identity key and value as a compact JSON tuple prefixed by its kind. This makes separators and arbitrary argument content unambiguous. Rule-local field IDs must not become feature names: canonical option spelling and positional ordinal provide stable matching across Fig and LLM rules.

Encoding examples:

```text
tool=python
target:["pytest"]
mode:["module"]
flag:["L0/python","-u"]
opt:["L1/pytest","-m",["slow"]]
arg:["L1/pytest",0,"tests/test_example.py"]
context:["repo","owner/project"]
opaque:["L1/script",["--unknown","x"]]
```

Layer scope is a deterministic depth plus canonical entry name. Parent and child options with the same spelling cannot collide. The full known entry path is encoded as a single ordered `target:[...]` feature, e.g. `target:["remote","add"]`; do not add one redundant core atom for every prefix. A token that is an entry selector is represented by the target, rather than also emitted as an ordinary option or positional feature.

Core policy:

- Root executable is core.
- Its explicit subcommand path, module/script entry, or declared wrapper-delegation entry chain is one core target feature.
- An ordinary first operand is never promoted to target. `grep pattern file` has only `tool=grep` as core.
- Mode, wrapper flags/options, ordinary operands and repo/cwd/environment context are optional. Entry selection is distinguished from wrapper options that merely alter execution conditions.
- No synthetic target for `-c` source, stdin content, or a missing entry. Distinctions among those execution modes remain optional features.

Projection rules:

- Declared aliases collapse to one canonical spelling. Equivalent attachment forms (`--jobs=4`/`--jobs 4`) yield the same feature only if the grammar permits both.
- One option occurrence with multiple values yields one bound tuple. Do not emit its individual values as unrelated features.
- Repetition policy is explicit: `last` keeps the last effective value, `count` emits a count, `ordered` emits the complete ordered list, and `set` sorts/deduplicates only where order-independence is evidenced. Unknown repetition semantics use ordered encoding. Related mutually overriding options must preserve their joint order unless an evidenced rule specifies their effective value.
- Options with proven independent semantics can reorder without changing features; dependencies or order-sensitive interactions remain an ordered group. Do not infer independence merely from different flag names.
- Positionals use their layer-local ordinal; preserve argument order. Do not rename arbitrary LLM-chosen argument labels into matching keys.
- Preserve numeric spelling, case, regular expressions and ordinary string values. Do not bucket numbers, truncate path basenames, inspect file size, or classify workloads in this change.
- Resolve a script target to a workspace-relative path only when runtime cwd/workspace evidence proves it. Do not lexically collapse symlink-sensitive `..` paths. Opaque/unresolved paths retain their spelling and status.
- Inline program/heredoc payloads are opaque syntax content. Represent a known payload by a domain-separated SHA-256 digest as an optional feature; do not extract imports or ask LLM to classify its resource behavior. Unknown stdin has no content feature.
- Opaque tails produce one ordered optional feature. Known prefix options survive; unresolved token expansion is explicitly tagged within the opaque encoding. Very large values use a domain-separated digest rather than a truncated value that can collide.
- Rule hashes, file fingerprints, generator identity, confidence, timestamps and parse diagnostics do not become matching features. Payload hashes above describe actual arguments, not rule provenance.

The rule grammar therefore also permits explicit option interaction groups with `ordered` or evidenced `last_effective` semantics. Emit one feature per such group instead of both group and member features. When interaction semantics are unknown, preserve the affected option occurrence sequence as a group; prioritize verified independence for the common bundled rules so that ordinary independent flags remain independently matchable.

Keep the existing matching-feature budget of 20 for the initial controlled change. Parsing itself remains lossless within input limits. Retain core first, then mode/context, then optional semantic atoms sorted by their canonical key; keep a deterministic prefix within the budget. Record omitted-feature count in metadata. Do not add a redundant count feature or duplicate aliases to inflate specificity. Exceeding the budget must be visible in coverage metrics.

Examples (context omitted):

| Command | Core | Selected optional features |
| --- | --- | --- |
| `python -u script.py` | `tool=python`, `target:["script.py"]` | `mode:["script"]`, `flag:["L0/python","-u"]` |
| `python -m pytest -m slow` | `tool=python`, `target:["pytest"]` | `mode:["module"]`, `opt:["L1/pytest","-m",["slow"]]` |
| `python -c 'print(1)' -m pytest` | `tool=python` | inline mode, payload digest, opaque user argv; no pytest module target |
| `python - data` | `tool=python` | stdin mode and user argv; no option named `-` |
| `git -C repo status` | `tool=git`, `target:["status"]` | git cwd option with its value |
| `grep -n pattern file` | `tool=grep` | line-number flag, ordered pattern/file operands |

Adding mode makes narrower contexts possible; it does not prohibit fallback to an existing broader node. The established KB policy decides that fallback.

## 10. KB integration and feature lifetime

Introduce `CommandFeatureService` at the sidecar owner and inject it into prediction/ingestion adapters. It has deterministic synchronous parsing/projection and separate asynchronous discovery. No global mutable normalizer hidden inside the selector.

At prediction time, resolve an immutable rule view, parse/project once per clause, and bind that result to the existing call/clause identity. Cache the feature record until completion or cancellation. Publication of a new rule must not change an in-flight command's features or Edge prediction token. Eligible completion labels use the bound pre-execution record; ambiguous observation correlation must remain ineligible for a gradient rather than matching another command by coincidentally equal features.

Prefer existing explicit clause identity. If a completion path currently omits it, carry that identity through its internal observation adapter; this is a necessary scoped `tool_resource` change. Preserve the current eligibility/ownership conditions.

Required call-site changes:

| Existing path | Change |
| --- | --- |
| `tool_time/lattice_kb.py` | Accept projected query/observation records; remove repeated string normalization |
| `tool_time/resource_lattice.py` | Use the same feature records for per-target accumulation and candidate lookup |
| `_lattice_vendor/nodes.py`, `selector.py`, `schemas.py` | Accept precomputed features/core while retaining their numerical algorithms |
| `_lattice_vendor/features.py` | Move/import the feature-set type without depending on the removed parser |
| `tool_time/edge_kappa_adapter.py` | Adapt `CommandFeatures` directly to `FeatureQuery` |
| `clawtune_sidecar/predictors/edge_kappa.py` | Use bound pre-execution features for begin/complete; eliminate completion-time reparse |
| Sidecar owner, contract models and plugin context | Carry runtime cwd/workspace and clause identity where needed |
| Offline building, packaging and tests | Use the same new entry point and ship rules/contracts in wheels and release artifacts |

Persist the projected record with each newly collected observation. Restarts load these records; they do not reinterpret observations through whatever rules happen to be current. Missing-rule observations carry conservative features and retain them after a more specific rule becomes available. This naturally leaves broader evidence available under existing subset coverage without inventing missing parameters.

New rules are activated for future calls. If a published rule is later found to have asserted incorrect semantics, replace it and start fresh affected release/run KB state; do not build a selective historical repair or migration framework in this project phase. Representation changes require fresh state. Old phase1 snapshots fail clearly on format checks and are never automatically imported.

The feature contract version identifies feature semantics, not a hash of the entire registry. Unrelated rule additions must not invalidate every cached feature or become a cross-tool matching barrier.

## 11. Implementation milestones and acceptance

Each milestone is a reviewable commit on `main`; `phase1` remains fixed.

1. **Contracts and projection fixtures.** Add the five schemas, internal records, deterministic feature encoding and manually specified cases above. Fix exact target, alias, scope, repetition, partial and invalid semantics before changing KB consumers.
2. **Parser plus Fig.** Implement static import, bundle the first rules and entry overrides, and consume existing mvdan words. Verify parent/child boundaries, wrappers, expressions and malformed inputs. Use a small actual Lattice query fixture to exercise the new projection end to end.
3. **Feature-based KB entry points.** Replace every heuristic-normalizer caller, carry pre-action records through completion/persistence, remove legacy normalization/default seeds, and verify Lattice/Edge numerical behavior on fixed feature inputs.
4. **Evidence and LLM generation.** Implement local/Docker evidence readers, restricted help provider, bounded async generation and registry activation. Complete one repository script and one previously unseen tool through the exact same schema/parser/projector/KB path used by Fig.
5. **Delivery and evaluation.** Include schemas/rules/licenses in packaging; document provider overrides and rule status; verify restart, cancellation and queue behavior; run parser correctness and fixed-algorithm matching comparisons.

The first full release includes both rule sources. LLM generation is not deferred to an unspecified later project.

Acceptance categories:

- **Grammar correctness:** independent fixtures for aliases, attached/separate values, optional/multiple values, negative values, short clusters, repeat order, `--`, subcommands, interpreter boundaries, wrapper boundaries, unresolved expansions and unknown tails.
- **Projection:** equivalent declared forms equal; different ownership/values remain distinct; ordinary operands remain optional; core is a subset; no metadata leakage or alias duplication; deterministic feature limits.
- **Causal integration:** a rule published between begin/complete cannot change that call's training representation; concurrent same-argv clauses retain distinct lifecycle identities; missing eligible telemetry remains missing.
- **Generation:** schema-invalid, unsupported and hallucinated branches do not become confident grammar; retries deduplicate; cancellation/timeout releases resources; content changes invalidate stale candidates; source-specific rules do not leak across repositories.
- **Help isolation:** attempted file writes/network access/child leaks are contained; probe output/time/resource bounds work; probes do not overlap measured benchmark windows. Test in the actual supported Linux/Docker environment.
- **KB behavior:** fixed feature inputs retain existing candidate sets, coverage and estimates. Command-driven results may differ because representation improved; this is expected. Lattice and Edge consume the same feature records.
- **Coverage and effectiveness:** report complete/partial/invalid rates, incorrect token-role assignments, generation success/latency/cost, feature truncation, candidate availability and prediction error. Freeze rules before held-out evaluation or simulate causal online acquisition; never generate them using held-out outcomes.
- **Read-only research:** existing trace datasets may supply command samples and evaluation labels without modification. They are not a compatibility target or the new release's runtime seed.

Proposed validation commands after the corresponding files exist:

```sh
PYTHONPATH=services/sidecar/src:. python -m pytest services/sidecar/tests/test_command_parser.py services/sidecar/tests/test_command_projection.py services/sidecar/tests/test_command_rule_generation.py -q
PYTHONPATH=services/sidecar/src:. python -m pytest services/sidecar/tests/test_lattice_time_kb.py services/sidecar/tests/test_lattice_resources.py services/sidecar/tests/test_edge_kappa_runtime.py tests/test_kb_ingress_parity.py tests/test_release_packaging.py -q
PYTHONPATH=services/sidecar/src:. python -m pytest services/sidecar/tests tests -q -rs --basetemp /tmp/clawtune-tests
```

Run plugin tests with `npm.cmd test` on Windows or `npm test` on Linux from `packages/clawtune-plugin`. Run importer tests/build in its locked tooling package. Planning alone does not establish a test pass. When implementing, record any validation command that cannot run, its missing prerequisite and remaining check in `docs/CURRENT_PLAN.md`; remove resolved entries rather than accumulating an execution diary.
