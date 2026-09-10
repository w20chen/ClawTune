# Tool-level PMU profiling

ClawTune records four hardware counts for one concrete Tool execution with a
single `perf_event_open` counting group. It does not configure sampling,
`mmap` a ring buffer, or poll counters while the Tool runs.

## Event and attribution contract

The public source of truth is
[`contracts/pmu-profile.schema.json`](../contracts/pmu-profile.schema.json).
The group is attached to the trusted execution root while that process is
still behind the existing exec gate. `enable_on_exec` starts the group at the
payload image and `inherit` includes threads and descendant processes. The
execution ID is used unchanged for the in-memory group, trace resource, and
standalone `pmu-profile-<execution-id>.json` artifact.

| Profile field | Linux perf event |
| --- | --- |
| `cycles` | `PERF_TYPE_HARDWARE / PERF_COUNT_HW_CPU_CYCLES` |
| `instructions` | `PERF_TYPE_HARDWARE / PERF_COUNT_HW_INSTRUCTIONS` |
| `llc_read_accesses` | `PERF_TYPE_HW_CACHE / LL / READ / ACCESS` |
| `llc_read_misses` | `PERF_TYPE_HW_CACHE / LL / READ / MISS` |

The collector intentionally never substitutes the generic
`PERF_COUNT_HW_CACHE_MISSES` event. Linux arm64 maps the two LL read events to
the architectural `LL_CACHE_RD` and `LL_CACHE_MISS_RD` events. HiSilicon's
`hisi_l3c` device is a separate uncore PMU, so its socket/cache-wide values are
not substituted into a task-attributed profile. See the Linux
[`perf_event` UAPI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/perf_event.h),
the [arm PMUv3 mapping](https://github.com/torvalds/linux/blob/master/drivers/perf/arm_pmuv3.c),
and the [HiSilicon uncore PMU documentation](https://www.kernel.org/doc/html/latest/admin-guide/perf/hisi-pmu.html).

Derived values use scaled counts:

- `IPC = instructions / cycles`
- `LLC MPKI = 1000 * llc_read_misses / instructions`
- `LLC miss rate = llc_read_misses / llc_read_accesses`

Raw counts and `time_enabled`/`time_running` are always retained separately.
An undefined ratio remains `null`; other valid metrics from the same reliable
profile remain usable.

## Concurrency and deployment scopes

Every actively profiled execution uses exactly four FDs, independent of CPU
count. A process-wide `max_active` and FD budget reject additional profiling
with reason `resource_budget`; the Tool itself still starts normally. This
avoids the `events x online CPUs` growth of cgroup-mode perf collection.

- In normal ClawTune, one sidecar serves all OpenClaw sessions. Therefore its
  single PMU collector and budget cover the aggregate concurrency of every
  session, not a per-session limit. The default follows the sidecar's global
  Tool concurrency ceiling.
- In ClawBox, the same `pmu.py` is copied from the sibling ClawTune build
  context and runs inside each Tool CubeSandbox VM. The Tool bridge supplies
  the guest-local root PID and execution ID. Each VM budget follows
  `TOOL_MAX_CONCURRENCY`; experiment workers currently use one active Tool per
  VM. Across VMs the minimum exact-attribution cost is four guest FDs per
  actively profiled Tool. Host session/VM admission remains the system-wide
  concurrency bound.

The guest's `time_running / time_enabled` ratio reports PMU scheduling visible
inside that guest. It must not be treated as proof that the host PMU was free
of cross-VM contention; acceptance runs must examine the ratio and host-level
throughput together.

ClawBox bounds each guest-local PMU begin/finish RPC to 100 ms. Normal calls
perform only four opens or four reads and return far below that ceiling; a
wedged helper is marked unavailable and the Tool gate/result continues.

## Quality and graceful degradation

Coverage is one of:

- `reliable`: all four named events ran at or above the configured ratio and
  kernel counting was included;
- `multiplexed`: all events exist, but at least one has
  `time_running < time_enabled`; scaled values remain diagnostic-only even
  when the ratio is above the configured severity threshold;
- `partial`: an event is unsupported or permissions allow user-space-only
  counting;
- `unavailable`: PMU is disabled, the platform/capability is absent, no event
  can be opened, the group cannot be scheduled, or the concurrency/FD budget
  is exhausted.

Unsupported, partial, and multiplexed profiles remain observable but have
`eligible_for_kb=false`. Only reliable derived values enter RuntimeToolResourceKB
PMU evidence used by online calibration. PMU evidence does not change current
placement/admission semantics in this MVP.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLAWTUNE_PMU_ENABLED` | `true` | Enable best-effort counting |
| `CLAWTUNE_PMU_MAX_ACTIVE` | `0` | Zero derives from global Tool concurrency |
| `CLAWTUNE_PMU_MAX_FDS` | automatic | Hard FD budget, reserving 64 process FDs |
| `CLAWTUNE_PMU_RELIABLE_RUNNING_RATIO` | `0.95` | Diagnostic boundary for severe multiplexing; all multiplexing remains KB-ineligible |

The CubeSandbox Tool container needs a guest vPMU exposed by its hypervisor and
permission to call `perf_event_open`; its manifest requests `PERFMON`. Kernel
policy may still require a different `perf_event_paranoid` value or guest
configuration. Failure is reported through coverage and never changes the Tool
exit status.

## Linux acceptance and overhead check

Run on each production architecture, including Kunpeng, from the ClawTune root:

```bash
python3 tools/validate_pmu.py --require-reliable \
  --concurrency 8 --max-active 8 --high-concurrency 64 \
  --benchmark-count 40 --output traces/pmu-validation.json
```

The check exercises a single Tool, concurrent Tools, a high-concurrency wave
larger than the PMU budget, a descendant process, exact execution-ID/root-PID
joins, and identical PMU-off/on workloads. It reports throughput and median/p95
latency instead of imposing a noise-sensitive universal threshold. Repeat the
comparison several times on an otherwise idle host and retain the raw report.
