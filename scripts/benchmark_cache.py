#!/usr/bin/env python3
"""Optional Docker cache warming. Does not execute or configure benchmarks."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services/sidecar/src"))

from benchmarks.adapters import NAMES, default_source, load

TERMINAL_REVISION = "d28711d0da2675d0bb1d56de45ae5df6082438a3"


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def capture(argv, **kwargs):
    return subprocess.check_output(argv, text=True, timeout=60, **kwargs)


def mappings(values):
    result = {}
    for value in values:
        name, sep, path = value.partition("=")
        if not sep or name not in NAMES or not path or name in result:
            raise ValueError("expected a unique BENCHMARK=PATH: " + value)
        result[name] = Path(path).expanduser().resolve()
    return result


def docker_prefix(sudo):
    return ["sudo", "-n", "docker"] if sudo else ["docker"]


def available(image, platform, docker):
    result = subprocess.run(docker + ["image", "inspect", image], capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        return None
    parts = platform.split("/")
    for item in json.loads(result.stdout):
        if (item.get("Os") == parts[0] and item.get("Architecture") == parts[1]
                and (len(parts) < 3 or item.get("Variant") == parts[2])):
            return {"id": item["Id"], "digests": item.get("RepoDigests", [])}
    return None


def registry_url(image):
    first = image.split("/", 1)[0]
    registry = first if "/" in image and ("." in first or ":" in first or first == "localhost") else "docker.io"
    if registry in ("docker.io", "index.docker.io"):
        registry = "registry-1.docker.io"
    return "https://" + registry + "/v2/"


def network_probe(image):
    result = subprocess.run(["curl", "-sS", "-o", os.devnull, "-w", "%{http_code}",
                             "--connect-timeout", "15", "--max-time", "30", registry_url(image)],
                            capture_output=True, text=True, timeout=35)
    if result.returncode or result.stdout.strip() not in ("200", "401"):
        raise RuntimeError(f"registry probe failed: rc={result.returncode}, HTTP={result.stdout.strip()}")


def fetch_source(name, out, revision):
    """Write downloads only under a new cache directory, never the reference tree."""
    target = out / "datasets"
    target.mkdir(exist_ok=True)
    if name == "swe-bench-verified":
        from datasets import load_dataset
        path = target / "verified.jsonl"
        load_dataset("princeton-nlp/SWE-bench_Verified", split="test").to_json(str(path))
        return path
    if name == "terminal-bench":
        path = target / "terminal-bench"
        # The checkout is isolated and pinned; default branch updates cannot reorder it.
        subprocess.run(["git", "clone", "--no-checkout", "--depth", "1",
                        "https://github.com/laude-institute/terminal-bench.git", str(path)],
                       check=True, timeout=900)
        subprocess.run(["git", "-C", str(path), "fetch", "--depth", "1", "origin", revision],
                       check=True, timeout=900)
        subprocess.run(["git", "-C", str(path), "checkout", "--detach", "FETCH_HEAD"],
                       check=True, timeout=120)
        return path / "original-tasks"
    raise ValueError(f"no default download for {name}; supply --dataset {name}=PATH")


def base_images(text, build_args):
    """Read FROM/ARG without changing Dockerfiles; reject unresolved expressions."""
    args = dict(build_args)
    aliases, images = set(), set()
    for line in text.replace("\\\n", " ").splitlines():
        if not re.match(r"^\s*(FROM|ARG)\s", line, re.I):
            continue
        tokens = shlex.split(line, comments=True)
        if not tokens:
            continue
        if tokens[0].upper() == "ARG" and len(tokens) == 2:
            key, sep, value = tokens[1].partition("=")
            if sep and key not in args:
                args[key] = value
        if tokens[0].upper() != "FROM":
            continue
        tokens = [v for v in tokens[1:] if not v.startswith("--platform=")]
        value = re.sub(r"\$\{(\w+)\}|\$(\w+)",
                       lambda m: str(args.get(m[1] or m[2], m[0])), tokens[0])
        if "$" in value:
            raise ValueError("unresolved Dockerfile FROM: " + value)
        if value.lower() not in aliases and value.lower() != "scratch":
            images.add(value)
        if len(tokens) >= 3 and tokens[1].upper() == "AS":
            aliases.add(tokens[2].lower())
    return images


def terminal_plan(task, out, compose, platform):
    import yaml
    # Match TerminalBackend's copy-before-Compose behavior, retaining all task bytes.
    dest = out / "terminal-tasks" / task.directory_name
    shutil.copytree(task.payload["task_path"], dest)
    cp = next((dest / n for n in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
               if (dest / n).exists()), None)
    if cp is None:
        cp = dest / "docker-compose.yaml"
        cp.write_text(yaml.safe_dump({"services": {"client": {
            "build": {"context": ".", "dockerfile": "Dockerfile"},
            "image": "${T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}"}}}), encoding="utf-8")
    project = "ctpre-" + task.directory_name
    logs = out / "task-logs" / task.directory_name
    (logs / "agent").mkdir(parents=True)
    env = dict(os.environ, T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME=project + "-image",
               T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME=project + "-client",
               T_BENCH_TASK_DOCKER_NAME_PREFIX=project, T_BENCH_TASK_LOGS_PATH=str(logs),
               T_BENCH_TASK_AGENT_LOGS_PATH=str(logs / "agent"), T_BENCH_CONTAINER_LOGS_PATH="/logs",
               T_BENCH_CONTAINER_AGENT_LOGS_PATH="/agent-logs", T_BENCH_TEST_DIR="/tests")
    resolved = json.loads(capture(compose + ["-p", project, "-f", str(cp), "config", "--format", "json"], env=env))
    images, builds = set(), []
    for service, value in resolved["services"].items():
        if value.get("platform", platform) != platform:
            raise ValueError(f"{task.task_id}/{service}: platform differs from {platform}")
        build = value.get("build")
        if not build:
            if value.get("image"):
                images.add(value["image"])
            continue
        # Do not silently drop advanced build semantics when translating to docker build.
        unsupported = set(build) - {"context", "dockerfile", "args", "target"}
        if unsupported:
            raise ValueError(f"{task.task_id}: unsupported build options {sorted(unsupported)}")
        context = Path(build["context"]).resolve()
        dockerfile = (context / build.get("dockerfile", "Dockerfile")).resolve()
        if not context.is_relative_to(dest) or not dockerfile.is_relative_to(dest):
            raise ValueError("Terminal build must stay within its copied task")
        args = build.get("args") or {}
        if any(v is None for v in args.values()):
            raise ValueError("Terminal build args must resolve to explicit values")
        bases = base_images(dockerfile.read_text(encoding="utf-8"), args)
        images.update(bases)
        builds.append({"task": task.task_id, "service": service, "image": value["image"],
                       "context": str(context), "dockerfile": str(dockerfile), "args": args,
                       "target": build.get("target"), "bases": sorted(bases)})
    return images, builds


def prepare(args):
    sources, configs = mappings(args.dataset), mappings(args.config)
    out = args.directory.expanduser().resolve()
    external = Path(os.getenv("AGENT_TEST_BENCH_ROOT", str(ROOT.parent / "agent-test-bench"))).resolve()
    protected = [external, *[p if p.is_dir() else p.parent for p in sources.values()]]
    if any(out.is_relative_to(p) for p in protected):
        raise ValueError("cache output must be outside input/reference datasets")
    if not any(getattr(args, n.replace("-", "_")) for n in NAMES):
        raise ValueError("select at least one benchmark with a positive count")
    # A JSON task list can reference task directories outside its own parent.
    # Check those before creating even an empty output directory.
    for name in NAMES:
        if name != "terminal-bench" or not getattr(args, "terminal_bench"):
            continue
        source = sources.get(name) or default_source(name, external, ROOT)
        if source is not None:
            for task in load(name, source):
                if out.is_relative_to(Path(task.payload["task_path"]).resolve()):
                    raise ValueError("cache output must be outside referenced Terminal tasks")
    out.mkdir(parents=True, exist_ok=False)
    images, builds, selections = set(), [], {}
    for name in NAMES:
        count = getattr(args, name.replace("-", "_"))
        if not count:
            continue
        if name == "bfcl":
            selections[name] = {"requested": count, "status": "no_task_images_required"}
            continue
        source = sources.get(name) or default_source(name, external, ROOT)
        if source is None and args.download_missing:
            source = fetch_source(name, out, args.terminal_revision)
        if source is None:
            raise ValueError(f"{name}: dataset not found; pass --dataset or --download-missing")
        tasks = load(name, source)[:count]
        selections[name] = {"requested": count, "selected": len(tasks), "source": str(source),
                            "ids": [t.task_id for t in tasks]}
        if name == "terminal-bench":
            git = subprocess.run(["git", "-C", str(source if source.is_dir() else source.parent),
                                  "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
            selections[name]["source_commit"] = git.stdout.strip() if git.returncode == 0 else None
            for task in tasks:
                extra, jobs = terminal_plan(task, out, args.compose, args.platform)
                images.update(extra)
                if args.build_terminal:
                    builds.extend(jobs)
        elif name == "deep-research-bench":
            from deep_research_bench.config import DRBConfig
            config = configs.get(name, ROOT / "configs/benchmark.yaml")
            if name not in configs and not config.exists():
                config = ROOT / "deep_research_bench/config.yaml"
            images.add(DRBConfig.from_yaml(config, repo_root=ROOT).sandbox.image)
            selections[name]["config"] = str(config)
        else:
            images.update(t.image for t in tasks)
    manifest = {"platform": args.platform, "docker": docker_prefix(args.sudo),
                "selections": selections, "images": sorted(images), "builds": builds}
    write_json(out / "manifest.json", manifest)
    write_json(out / "status.json", {"status": "prepared", "pull_total": len(images),
                                    "build_total": len(builds), "failed": 0})
    log(f"PREPARED {len(images)} images, {len(builds)} builds: {out}")


def pull(image, index, manifest, out):
    docker, platform = manifest["docker"], manifest["platform"]
    cached = available(image, platform, docker)
    if cached:
        return {"image": image, "status": "cached", **cached}
    error = ""
    for attempt in range(1, 7):
        log(f"PULL {index} attempt={attempt}/6 {image}")
        try:
            network_probe(image)
            with (out / f"image-{index:03}.log").open("a", encoding="utf-8") as stream:
                stream.write(f"\nATTEMPT {attempt} {image}\n"); stream.flush()
                subprocess.run(docker + ["pull", "--platform", platform, image], check=True,
                               stdout=stream, stderr=subprocess.STDOUT, timeout=1800)
            cached = available(image, platform, docker)
            if not cached:
                raise RuntimeError("pulled image did not match the requested platform")
            return {"image": image, "status": "pulled", "attempts": attempt, **cached}
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            error = str(exc)
            log(f"RETRY {image}: {error}")
            if attempt < 6:
                time.sleep(min(30 * 2 ** (attempt - 1), 300))
    return {"image": image, "status": "failed", "error": error}


def build_command(job, manifest):
    argv = manifest["docker"] + ["build", "--platform", manifest["platform"], "--pull=false",
                                "-t", job["image"], "-f", job["dockerfile"]]
    for key, value in job["args"].items():
        argv += ["--build-arg", key + "=" + str(value)]
    if job.get("target"):
        argv += ["--target", job["target"]]
    return argv + [job["context"]]


def build(job, index, manifest, out):
    # Always ask Docker to build: an existing tag alone does not prove fresh inputs.
    error = ""
    for attempt in range(1, 5):
        log(f"BUILD {index} attempt={attempt}/4 {job['task']}")
        try:
            with (out / f"build-{index:03}.log").open("a", encoding="utf-8") as stream:
                subprocess.run(build_command(job, manifest), check=True, stdout=stream,
                               stderr=subprocess.STDOUT, timeout=7200,
                               env=dict(os.environ, DOCKER_BUILDKIT="0"))
            cached = available(job["image"], manifest["platform"], manifest["docker"])
            if not cached:
                raise RuntimeError("built image platform mismatch")
            return {"task": job["task"], "status": "built", **cached}
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            error = str(exc)
            log(f"BUILD RETRY {job['task']}: {error}")
            if attempt < 4:
                time.sleep(60 * attempt)
    return {"task": job["task"], "status": "failed", "error": error}


def run(args):
    out = args.directory.expanduser().resolve()
    if not (out / "manifest.json").is_file():
        raise ValueError("run requires a successful prepare: " + str(out))
    if args.detach:
        # Worker takes an exclusive lock too, so two launchers cannot run duplicate jobs.
        with (out / "progress.log").open("a", encoding="utf-8") as stream:
            process = subprocess.Popen(["nohup", sys.executable, "-u", str(Path(__file__).resolve()),
                                        "run", "--directory", str(out), "--workers", str(args.workers)],
                                       stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=True, cwd=ROOT)
        time.sleep(0.3)
        if process.poll() is not None and process.returncode:
            raise ValueError("worker failed to start; inspect " + str(out / "progress.log"))
        log(f"STARTED pid={process.pid} logs={out / 'progress.log'}")
        return 0
    import fcntl
    with (out / "worker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (out / "pid").write_text(str(os.getpid()) + "\n")
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        state = {"status": "running", "phase": "pull", "pull_total": len(manifest["images"]),
                 "pull_completed": 0, "build_total": len(manifest["builds"]), "build_completed": 0,
                 "failed": 0, "results": []}
        def record(result, kind):
            state["results"].append(result)
            state[kind + "_completed"] += 1
            state["failed"] += result["status"] == "failed"
            write_json(out / "status.json", state)
            log(f"DONE {kind} {state[kind + '_completed']}/{state[kind + '_total']} {result}")
        write_json(out / "status.json", state)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {executor.submit(pull, im, i, manifest, out): im
                           for i, im in enumerate(manifest["images"], 1)}
                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"image": futures[future], "status": "failed", "error": str(exc)}
                    record(result, "pull")
            state["phase"] = "build"
            write_json(out / "status.json", state)
            for i, job in enumerate(manifest["builds"], 1):
                record(build(job, i, manifest, out), "build")
            state["status"] = "failed" if state["failed"] else "completed"
            state["phase"] = "finished"
        except BaseException as exc:
            state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
            raise
        finally:
            write_json(out / "status.json", state)
        return 1 if state["failed"] else 0


def status(args):
    # Also reads the two original kunpeng job formats without changing their files.
    for directory in args.directory:
        out = directory.expanduser().resolve()
        state = json.loads((out / "status.json").read_text(encoding="utf-8"))
        alive = False
        if (out / "pid").exists():
            try:
                os.kill(int((out / "pid").read_text()), 0)
                alive = True
            except ProcessLookupError:
                pass
        print(f"{out}\n  status={state['status']} alive={alive} phase={state.get('phase', 'pull')}")
        print(f"  pulls={state.get('pull_completed', state.get('completed', 0))}/{state.get('pull_total', state.get('total', 0))}"
              f" builds={state.get('build_completed', 0)}/{state.get('build_total', 0)} failed={state.get('failed', 0)}")
        for row in state.get("results", []) + state.get("pull_results", []) + state.get("build_results", []):
            if row["status"] == "failed":
                print("  FAILED", row)


def positive(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return n


def nonnegative(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    plan = subs.add_parser("prepare", help="Select tasks and write an optional cache plan")
    plan.add_argument("--directory", type=Path, required=True, help="New output directory outside datasets")
    for name in NAMES:
        plan.add_argument("--" + name, type=nonnegative, default=0, metavar="N")
    plan.add_argument("--dataset", action="append", default=[], metavar="BENCHMARK=PATH")
    plan.add_argument("--config", action="append", default=[], metavar="BENCHMARK=PATH")
    plan.add_argument("--platform", default="linux/amd64", choices=("linux/amd64", "linux/arm64"))
    plan.add_argument("--sudo", action="store_true", help="Use sudo -n docker (same daemon required)")
    plan.add_argument("--download-missing", action="store_true", help="Fetch missing Verified/Terminal sources")
    plan.add_argument("--terminal-revision", default=TERMINAL_REVISION)
    plan.add_argument("--compose", nargs="+", default=["docker", "compose"], help="Compose argv, or standalone binary")
    plan.add_argument("--build-terminal", action="store_true", help="Also warm Dockerfile build layers")
    execute = subs.add_parser("run", help="Pull/build; may be rerun after failure")
    execute.add_argument("--directory", type=Path, required=True)
    execute.add_argument("--detach", action="store_true", help="Start a nohup worker")
    execute.add_argument("--workers", type=positive, default=2)
    report = subs.add_parser("status", help="Read new or original kunpeng job progress")
    report.add_argument("--directory", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args)
        elif args.command == "run":
            return run(args)
        else:
            status(args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
