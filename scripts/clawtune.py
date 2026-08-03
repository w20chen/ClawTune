#!/usr/bin/env python3
"""One entry point for installing, checking, and running ClawTune on Linux."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ARM_ARCHES = {"aarch64", "arm64"}
PRIVILEGED_RUNTIME_PRESERVE_ENV = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "XDG_RUNTIME_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)
BENCHMARK_PRESERVE_ENV = (
    "LLM_API_KEY",
    "LLM_API_KEY_FILE",
    "AGENT_TEST_BENCH_ROOT",
    "SWE_REBENCH_DOCKER_PLATFORM",
    *PRIVILEGED_RUNTIME_PRESERVE_ENV,
)


class SetupError(RuntimeError):
    """An actionable environment/setup failure."""


def log(message: str) -> None:
    print(f"[ClawTune] {message}", flush=True)


def run(
    command: Sequence[str | Path],
    *,
    check: bool = True,
    capture: bool = False,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    rendered = [str(item) for item in command]
    return subprocess.run(
        rendered,
        cwd=ROOT,
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=input_text,
        env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def require_linux() -> None:
    if platform.system() != "Linux":
        raise SetupError(
            "ClawTune eBPF runtime requires Linux; Windows and macOS support "
            "development and unit tests only."
        )


def host_arch() -> str:
    return platform.machine().lower()


def cgroup_v2_available() -> bool:
    return Path("/sys/fs/cgroup/cgroup.controllers").is_file()


def package_manager() -> str | None:
    if shutil.which("dnf"):
        return "dnf"
    if shutil.which("apt-get"):
        return "apt"
    return None


def kernel_build() -> Path:
    configured = os.getenv("BCC_KERNEL_SOURCE")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = ROOT / candidate
    else:
        candidate = Path("/lib/modules") / platform.release() / "build"
    try:
        return candidate.resolve(strict=True)
    except FileNotFoundError:
        return candidate.resolve(strict=False)


def python_has_bcc(executable: Path) -> bool:
    probe = """
import importlib

for name in ("bcc", "bpfcc"):
    try:
        module = importlib.import_module(name)
    except (ImportError, OSError):
        continue
    if all(hasattr(module, attribute) for attribute in ("BPF", "PerfSWConfig", "PerfType")):
        raise SystemExit(0)
raise SystemExit(1)
"""
    return run([executable, "-c", probe], check=False, capture=True).returncode == 0


def bcc_pythons() -> list[Path]:
    candidates: list[Path] = []
    for raw in ("/usr/bin/python3", shutil.which("python3"), sys.executable):
        if not raw:
            continue
        path = Path(raw).resolve()
        if path not in candidates and path.exists():
            candidates.append(path)
    return [path for path in candidates if python_has_bcc(path)]


def dnf_package_exists(name: str) -> bool:
    result = run(
        ["dnf", "-q", "list", name],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def install_host_packages() -> None:
    manager = package_manager()
    if manager is None:
        raise SetupError(
            "Neither dnf nor apt was found. Install the BCC Python bindings, "
            "clang/LLVM, bpftool, and development package for the running kernel."
        )

    release = platform.release()
    if manager == "apt":
        packages = [
            "bpfcc-tools",
            "python3-bpfcc",
            "clang",
            "llvm",
            "bpftool",
            "python3-venv",
            f"linux-headers-{release}",
        ]
        log("Installing Debian/Ubuntu eBPF dependencies with apt")
        run(["sudo", "apt-get", "update"])
        run(["sudo", "apt-get", "install", "-y", *packages])
        return

    packages = [
        name
        for name in ("clang", "llvm", "bpftool", "python3", "python3-pip")
        if dnf_package_exists(name)
    ]
    header_candidates = (f"kernel-devel-{release}", "kernel-devel")
    bcc_candidates = ("python3-bpfcc", "python3-bcc", "bcc", "bcc-tools")
    for group in (header_candidates, bcc_candidates):
        selected = next((name for name in group if dnf_package_exists(name)), None)
        if selected:
            packages.append(selected)
    if not packages:
        raise SetupError(
            "The enabled dnf repositories do not provide Clang, kernel-devel, "
            "or BCC. Enable the openEuler OS/update repositories and retry."
        )
    log("Installing openEuler/RHEL eBPF dependencies with dnf")
    run(["sudo", "dnf", "install", "-y", *dict.fromkeys(packages)])


def find_bcc_python(*, install: bool) -> Path:
    matches = bcc_pythons()
    if matches:
        return matches[0]
    if install:
        install_host_packages()
        matches = bcc_pythons()
    if not matches:
        raise SetupError(
            "No system Python can import bcc/bpfcc. Run doctor; do not install "
            "the unrelated PyPI BCC package into Conda."
        )
    return matches[0]


def required_commands() -> list[str]:
    return [
        name
        for name in ("docker", "node", "npm", "openclaw", "sudo")
        if shutil.which(name) is None
    ]


def create_venv(system_python: Path) -> None:
    venv_python = VENV / "bin" / "python"
    if venv_python.exists() and python_has_bcc(venv_python):
        log(f"Reusing {VENV}")
    else:
        if VENV.exists():
            raise SetupError(
                f"{VENV} exists but cannot import the system BCC bindings. "
                "Move it aside and run setup again."
            )
        log(f"Creating .venv with {system_python} and system BCC access")
        result = run(
            [system_python, "-m", "venv", "--system-site-packages", VENV],
            check=False,
        )
        if result.returncode != 0:
            raise SetupError(
                "Could not create .venv. Install this distribution's "
                "python3-venv or python3 package."
            )
    run([venv_python, "-m", "pip", "install", "-e", "services/scheduler[dev]"])
    runtime_probe = run(
        [
            venv_python,
            "-c",
            (
                "from importlib.metadata import version; "
                "import agent_scheduler, fastapi, httpx, numpy, pydantic, "
                "prometheus_client, psutil, typing_extensions, uvicorn; "
                "assert version('agent-scheduler') == '0.1.0'"
            ),
        ],
        check=False,
        capture=True,
    )
    if runtime_probe.returncode != 0:
        detail = (runtime_probe.stderr or runtime_probe.stdout).strip()
        raise SetupError(
            "Scheduler installation completed but its runtime dependencies "
            f"cannot be imported with {venv_python}:\n{detail}"
        )


def copy_defaults() -> None:
    for source, target in (
        (ROOT / ".env.example", ROOT / ".env"),
        (ROOT / "swe_rebench" / "config.example.yaml", ROOT / "swe_rebench" / "config.yaml"),
    ):
        if not target.exists():
            shutil.copy2(source, target)
            log(f"Created {target.relative_to(ROOT)}")


def repair_plugin_permissions() -> None:
    dist = ROOT / "packages" / "openclaw-plugin" / "dist"
    if not dist.exists():
        return
    generated_paths = [dist, *dist.rglob("*")]
    if all(os.access(path, os.W_OK) for path in generated_paths):
        return
    if not hasattr(os, "getuid"):
        return
    log("Repairing plugin files previously generated as root")
    run(["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", dist])


def build_plugin() -> None:
    repair_plugin_permissions()
    plugin = ROOT / "packages" / "openclaw-plugin"
    log("Installing and building the OpenClaw plugin")
    subprocess.run(["npm", "install"], cwd=plugin, check=True)
    subprocess.run(["npm", "run", "build"], cwd=plugin, check=True)


def openclaw_config_path() -> Path:
    configured = os.getenv("OPENCLAW_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".openclaw" / "openclaw.json"


def stale_clawtune_plugin_link(output: str) -> bool:
    normalized = output.lower()
    return (
        "plugins.load.paths" in normalized
        and "plugin path not found" in normalized
        and "openclaw-plugin" in normalized
    )


def backup_openclaw_config() -> Path | None:
    config = openclaw_config_path()
    if not config.is_file():
        return None
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = config.with_name(f"{config.name}.clawtune-backup-{timestamp}")
    try:
        shutil.copy2(config, backup)
    except OSError as exc:
        raise SetupError(f"Could not back up OpenClaw config {config}: {exc}") from exc
    log(f"Backed up OpenClaw config to {backup}")
    return backup


def remove_stale_clawtune_plugin_paths() -> bool:
    """Remove stale openclaw-plugin references from the OpenClaw config.

    Cleans up two things that can cause OpenClaw to treat the config as
    invalid and auto-restore from its last-known-good backup:

    * ``plugins.load.paths`` entries whose leaf name is
      ``openclaw-plugin`` (stale checkout paths).
    * ``plugins.entries.agent-scheduler`` (orphaned entry that
      references a plugin no longer in the registry).
      ``configure_openclaw()`` re-adds this entry with the correct
      configuration immediately after ``plugins install`` succeeds.

    Returns ``True`` when at least one reference was removed so the
    caller can decide whether a follow-up repair (e.g. ``openclaw
    doctor --fix``) is needed.
    """
    config = openclaw_config_path()
    try:
        document = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"Could not read OpenClaw config {config}: {exc}") from exc

    changed = False

    # ── Clean plugins.load.paths ──────────────────────────────────────
    plugins = document.get("plugins")
    load = plugins.get("load") if isinstance(plugins, dict) else None
    paths = load.get("paths") if isinstance(load, dict) else None
    if isinstance(paths, list):
        # Remove *every* plugins.load.paths entry whose leaf name is
        # "openclaw-plugin", regardless of whether it still exists on
        # disk.  A path may exist (old checkout directory is still
        # present) yet be unusable (e.g. a broken symlink or an
        # incompatible plugin version).
        #
        # OpenClaw may store path entries as plain strings *or* as
        # objects with a ``path`` key — handle both.
        def _is_clawtune_plugin_entry(value: object) -> bool:
            def leaf(path: str) -> str:
                # Configs can outlive an OS migration. pathlib only recognizes
                # separators for the current host, so normalize both forms.
                return path.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]

            if isinstance(value, str):
                return leaf(value) == "openclaw-plugin"
            if isinstance(value, dict):
                p = value.get("path")
                if isinstance(p, str):
                    return leaf(p) == "openclaw-plugin"
            return False

        retained = [v for v in paths if not _is_clawtune_plugin_entry(v)]
        removed = len(paths) - len(retained)
        log(
            f"plugins.load.paths: {len(paths)} total, "
            f"{removed} openclaw-plugin entries removed, "
            f"{len(retained)} retained"
        )
        if removed > 0:
            load["paths"] = retained
            changed = True
    else:
        log(
            "plugins.load.paths is not a list in the OpenClaw config; "
            "the stale plugin reference is stored in OpenClaw's internal "
            "state rather than in plugins.load.paths."
        )

    # ── Clean plugins.entries.agent-scheduler ─────────────────────────
    # A stale agent-scheduler entry (plugin not found in registry) makes
    # OpenClaw treat the config as invalid and auto-restore from its
    # last-known-good backup — even after plugins.load.paths is clean.
    # configure_openclaw() re-adds this entry with the correct
    # configuration after install succeeds, so removing it here is safe.
    if isinstance(plugins, dict):
        entries = plugins.get("entries")
        if isinstance(entries, dict) and "agent-scheduler" in entries:
            log("Removing stale plugins.entries.agent-scheduler from config")
            del entries["agent-scheduler"]
            changed = True

    if not changed:
        log(
            "No stale openclaw-plugin references found in the OpenClaw "
            "config.  The stale reference lives in OpenClaw's internal "
            "plugin state rather than in the JSON config."
        )
        return False

    temporary = config.with_name(f".{config.name}.clawtune.tmp")
    try:
        temporary.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, config)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SetupError(f"Could not repair OpenClaw config {config}: {exc}") from exc
    return True


def install_openclaw_plugin(openclaw: str, plugin: Path) -> None:
    command: list[str | Path] = [openclaw, "plugins", "install", "--link", plugin]
    installed = run(command, check=False, capture=True)
    combined = f"{installed.stdout}\n{installed.stderr}"

    if installed.returncode != 0 and stale_clawtune_plugin_link(combined):
        log("Repairing a stale ClawTune plugin path in the OpenClaw config")
        backup_openclaw_config()
        removed = remove_stale_clawtune_plugin_paths()

        if not removed:
            # Neither plugins.load.paths nor plugins.entries contained
            # the stale reference.  The reference is in OpenClaw's
            # internal plugin registry.  Run doctor --fix so OpenClaw
            # can reconcile its internal state.  Doctor may restore
            # stale paths into the JSON config from a last-known-good
            # backup, so run the removal again afterwards.
            log(
                "Running openclaw doctor --fix to repair OpenClaw's "
                "internal plugin state"
            )
            run([openclaw, "doctor", "--fix"], check=False)
            remove_stale_clawtune_plugin_paths()

        # Run config validate so OpenClaw accepts the now-clean config
        # as its new last-known-good backup.  Without this step every
        # subsequent OpenClaw command auto-restores the stale config
        # from the old last-known-good backup, undoing our repair.
        log("Validating repaired config to update last-known-good backup")
        validated = run(
            [openclaw, "config", "validate"],
            check=False,
            capture=True,
        )
        if validated.returncode != 0:
            detail = (validated.stderr or validated.stdout).strip()
            raise SetupError(
                "OpenClaw rejected the repaired config; the plugin was not "
                f"reinstalled. Restore the generated backup if needed:\n{detail}"
            )

        # Retry the install against the clean config and updated
        # last-known-good backup.
        installed = run(command, check=False, capture=True)
        combined = f"{installed.stdout}\n{installed.stderr}"

    if installed.returncode != 0:
        normalized = combined.lower()
        if "already" not in normalized and "exists" not in normalized:
            detail = (installed.stderr or installed.stdout).strip()
            raise SetupError(f"OpenClaw plugin installation failed:\n{detail}")


def configure_openclaw() -> None:
    openclaw = shutil.which("openclaw")
    assert openclaw is not None
    plugin = ROOT / "packages" / "openclaw-plugin"
    install_openclaw_plugin(openclaw, plugin)
    run([openclaw, "plugins", "enable", "agent-scheduler"])
    launcher = (VENV / "bin" / "claw-launch").resolve()
    patch = {
        "plugins": {
            "entries": {
                "agent-scheduler": {
                    "enabled": True,
                    "config": {
                        "endpoint": "http://127.0.0.1:8765",
                        "autoStartSidecar": True,
                        # Empty by design: the plugin resolves the checkout,
                        # venv and running-kernel build tree when it starts.
                        # Persisting those absolute paths makes the config
                        # stale whenever the repository is moved.
                        "sidecarCommand": "",
                        "recordRawTrace": True,
                        "executionBackend": "managed-wrapper",
                        "launcherPath": str(launcher),
                        "enableCgroup": True,
                        "securityBoundaryAccepted": True,
                    },
                }
            }
        }
    }
    run([openclaw, "config", "patch", "--stdin"], input_text=json.dumps(patch))
    run([openclaw, "config", "validate"])
    log("OpenClaw plugin configuration is ready")


def setup_qemu_if_needed(skip_qemu: bool) -> None:
    if host_arch() not in ARM_ARCHES or skip_qemu:
        return
    log("Kunpeng/ARM host: enabling and testing linux/amd64 container emulation")
    run(["sudo", "bash", "scripts/setup/arm_qemu_setup.sh", "install"])


def privileged_command(
    module_args: Sequence[str | Path],
    *,
    preserve_env: Sequence[str] = (),
) -> list[str]:
    build = kernel_build()
    runtime_path = SYSTEM_PATH
    command_dirs = [
        str(Path(value).parent)
        for value in (
            shutil.which("openclaw"),
            shutil.which("node"),
            shutil.which("docker"),
        )
        if value
    ]
    if command_dirs:
        runtime_path = ":".join(dict.fromkeys([*command_dirs, runtime_path]))
    sudo_command = ["sudo"]
    # Keep elevation predictable: preserve only explicitly allow-listed
    # variables.  Their names, but never their values, enter the process argv.
    present_names = [
        name
        for name in dict.fromkeys(preserve_env)
        if name in os.environ
    ]
    if present_names:
        sudo_command.append("--preserve-env=" + ",".join(present_names))
    return [
        *sudo_command,
        "env",
        f"PATH={runtime_path}",
        f"PYTHONPATH={ROOT}{os.pathsep}{ROOT / 'services' / 'scheduler' / 'src'}",
        f"BCC_KERNEL_SOURCE={build}",
        *[str(item) for item in module_args],
    ]


def check_ebpf(output: Path | None = None) -> None:
    command: list[str | Path] = [VENV / "bin" / "python", "tools/check_ebpf.py"]
    if output is not None:
        command.extend(["--output", output])
    run(privileged_command(command))


def check_mvdan_adapter() -> None:
    code = (
        "from tool_resource.mvdan_client import ensure_compatible_adapter; "
        "print('Mvdan adapter:', ensure_compatible_adapter())"
    )
    run(
        privileged_command(
            [VENV / "bin" / "python", "-c", code],
            preserve_env=PRIVILEGED_RUNTIME_PRESERVE_ENV,
        )
    )


def sidecar_health() -> dict[str, object]:
    endpoint = "http://127.0.0.1:8765/health/ready"
    try:
        with urlopen(endpoint, timeout=1.0) as response:  # noqa: S310 - fixed loopback URL
            status = response.status
            body = response.read(4096).decode("utf-8", errors="replace")
        payload = json.loads(body)
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return {"running": False, "endpoint": endpoint, "error": str(exc)}
    identity_ok = (
        isinstance(payload, dict)
        and payload.get("service") == "clawtune-scheduler"
        and payload.get("schema_version") == "scheduler.health.v1"
        and payload.get("ready") is True
    )
    return {
        "running": 200 <= status < 300 and identity_ok,
        "endpoint": endpoint,
        "status": status,
        "response": body,
        **({} if identity_ok else {"error": "port is not a compatible ClawTune sidecar"}),
    }


def doctor() -> int:
    require_linux()
    build = kernel_build()
    report = {
        "host": {
            "architecture": host_arch(),
            "kernel": platform.release(),
            "package_manager": package_manager(),
        },
        "commands": {
            name: shutil.which(name)
            for name in (
                "docker",
                "node",
                "npm",
                "openclaw",
                "sudo",
                "clang",
                "llc",
                "bpftool",
            )
        },
        "cgroup_v2": {
            "path": "/sys/fs/cgroup/cgroup.controllers",
            "present": cgroup_v2_available(),
        },
        "kernel_headers": {"path": str(build), "present": build.is_dir()},
        "bcc_pythons": [str(path) for path in bcc_pythons()],
        "venv": {
            "path": str(VENV),
            "ready": (VENV / "bin" / "python").exists()
            and python_has_bcc(VENV / "bin" / "python"),
        },
        "sidecar": sidecar_health(),
        "kunpeng_amd64_emulation_required": host_arch() in ARM_ARCHES,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    healthy = (
        not required_commands()
        and all(
            report["commands"].get(name)
            for name in ("clang", "llc", "bpftool")
        )
        and report["kernel_headers"]["present"]
        and bool(report["bcc_pythons"])
        and report["venv"]["ready"]
        and report["cgroup_v2"]["present"]
    )
    if healthy:
        log(
            "Base environment is ready. Run `python3 scripts/clawtune.py check` "
            "for the eBPF check."
        )
        if not report["sidecar"]["running"]:
            log(
                "The sidecar is not running; this is normal. `openclaw agent` "
                "starts it automatically before the first request."
            )
        return 0
    log("Environment is not ready. Run `python3 scripts/clawtune.py setup`.")
    return 1


def setup(args: argparse.Namespace) -> None:
    require_linux()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SetupError(
            "Run setup as a normal user; it invokes sudo only for privileged steps."
        )
    build = kernel_build()
    missing_host_tools = [name for name in ("clang", "llc", "bpftool") if not shutil.which(name)]
    needs_host_packages = not bcc_pythons() or not build.is_dir() or bool(missing_host_tools)
    if needs_host_packages and not args.no_system_install:
        install_host_packages()
    system_python = find_bcc_python(install=False)
    build = kernel_build()
    if not build.is_dir():
        raise SetupError(
            f"Development tree for running kernel {platform.release()} was not found: {build}"
        )
    missing_host_tools = [name for name in ("clang", "llc", "bpftool") if not shutil.which(name)]
    if missing_host_tools:
        raise SetupError("Missing eBPF tools: " + ", ".join(missing_host_tools))
    if not cgroup_v2_available():
        raise SetupError(
            "cgroup v2 is not mounted at /sys/fs/cgroup. ClawTune's required "
            "eBPF attribution cannot run on a cgroup-v1-only host."
        )
    missing = required_commands()
    if missing:
        raise SetupError(
            "Missing external applications that setup does not install automatically: "
            + ", ".join(missing)
            + ". Install them as described in docs/getting-started.md and retry."
        )
    create_venv(system_python)
    copy_defaults()
    build_plugin()
    setup_qemu_if_needed(args.skip_qemu)
    configure_openclaw()
    check_mvdan_adapter()
    check_ebpf(ROOT / "data" / "ebpf-check.json")
    log("Setup and eBPF validation passed; the validation process has exited.")
    log("The OpenClaw plugin starts and waits for the eBPF sidecar automatically.")
    log("Run an agent directly: openclaw agent <options>")
    log("Run a benchmark: python3 scripts/clawtune.py benchmark --sample 1")


def sidecar() -> None:
    require_linux()
    if not (VENV / "bin" / "python").exists():
        raise SetupError(".venv is missing; run setup first.")
    run(sidecar_command())


def sidecar_command() -> list[str]:
    return privileged_command(
        [
            f"AGENT_SCHEDULER_ENV_FILE={ROOT / '.env'}",
            VENV / "bin" / "python",
            "-m",
            "agent_scheduler.main",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
        ],
        preserve_env=(
            *PRIVILEGED_RUNTIME_PRESERVE_ENV,
            *(
                name
                for name in os.environ
                if name.startswith(("LC_", "AGENT_SCHEDULER_", "CLAWTUNE_"))
            ),
        ),
    )


def wait_for_sidecar(child: subprocess.Popen[bytes], log_path: Path) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if sidecar_health()["running"]:
            return
        return_code = child.poll()
        if return_code is not None:
            detail = ""
            try:
                detail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            except OSError:
                pass
            raise SetupError(
                f"Sidecar auto-start failed with exit code {return_code}:\n{detail}"
            )
        time.sleep(0.2)
    raise SetupError(f"Sidecar was not ready within 30 seconds; log: {log_path}")


def stop_managed_sidecar(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    if platform.system() != "Linux":
        child.terminate()
        return

    kill_executable = shutil.which("kill") or "/bin/kill"

    def signal_group(name: str, *, non_interactive: bool) -> int:
        sudo_args = ["sudo"]
        if non_interactive:
            sudo_args.append("-n")
        result = subprocess.run(
            [*sudo_args, kill_executable, f"-{name}", "--", f"-{child.pid}"],
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL if non_interactive else None,
        )
        return result.returncode

    if signal_group("TERM", non_interactive=True) != 0:
        log("Stopping the privileged sidecar requires sudo confirmation")
        signal_group("TERM", non_interactive=False)
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if signal_group("KILL", non_interactive=True) != 0:
            signal_group("KILL", non_interactive=False)


def agent(extra: Sequence[str]) -> None:
    require_linux()
    if not (VENV / "bin" / "python").exists():
        raise SetupError(".venv is missing; run setup first.")
    openclaw = shutil.which("openclaw")
    if openclaw is None:
        raise SetupError("openclaw was not found; install it and run setup.")

    managed_sidecar: subprocess.Popen[bytes] | None = None
    log_handle = None
    if sidecar_health()["running"]:
        log("Reusing the running sidecar")
    else:
        log_path = ROOT / "data" / "sidecar-auto.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("ab")
        log("Starting the eBPF sidecar; sudo may request your password")
        managed_sidecar = subprocess.Popen(
            sidecar_command(),
            cwd=ROOT,
            stdin=None,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            wait_for_sidecar(managed_sidecar, log_path)
        except BaseException:
            stop_managed_sidecar(managed_sidecar)
            log_handle.close()
            raise
        log("Sidecar is ready; starting OpenClaw")

    try:
        run([openclaw, "agent", *extra])
    finally:
        if managed_sidecar is not None:
            log("OpenClaw finished; stopping the sidecar started for this run")
            stop_managed_sidecar(managed_sidecar)
        if log_handle is not None:
            log_handle.close()


def benchmark(extra: Sequence[str]) -> None:
    require_linux()
    if not (VENV / "bin" / "python").exists():
        raise SetupError(".venv is missing; run setup first.")
    config = ROOT / "swe_rebench" / "config.yaml"
    if not config.exists():
        raise SetupError("swe_rebench/config.yaml is missing; run setup first.")
    env_items: list[str] = []
    if (
        host_arch() in ARM_ARCHES
        and "SWE_REBENCH_DOCKER_PLATFORM" not in os.environ
    ):
        env_items.append("SWE_REBENCH_DOCKER_PLATFORM=linux/amd64")
    command = privileged_command(
        [
            *env_items,
            VENV / "bin" / "python",
            "-m",
            "swe_rebench.runner",
            "run",
            "--config",
            config,
            "--prepare",
            "--export",
            *extra,
        ],
        preserve_env=(
            *BENCHMARK_PRESERVE_ENV,
            *(name for name in os.environ if name.startswith("LC_")),
        ),
    )
    run(command)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Install and run ClawTune without manually composing "
            "Python/BCC/sudo environments."
        )
    )
    sub = result.add_subparsers(dest="command", required=True)
    setup_parser = sub.add_parser("setup", help="Prepare this Linux host and verify eBPF")
    setup_parser.add_argument(
        "--no-system-install",
        action="store_true",
        help="Only check distro packages",
    )
    setup_parser.add_argument(
        "--skip-qemu",
        action="store_true",
        help="Skip amd64 container setup on ARM",
    )
    sub.add_parser("doctor", help="Show one consolidated environment report")
    sub.add_parser("check", help="Run the real eBPF compile/attach/exec smoke test")
    sub.add_parser("sidecar", help="Start the privileged Scheduler sidecar")
    sub.add_parser("agent", help="Start eBPF sidecar, run OpenClaw agent, then clean up")
    sub.add_parser("benchmark", help="Run SWE-Rebench; remaining options go to the runner")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args, extra = parser().parse_known_args(argv)
    if extra and args.command not in {"agent", "benchmark"}:
        raise SetupError("Unrecognized arguments: " + " ".join(extra))
    try:
        if args.command == "setup":
            setup(args)
        elif args.command == "doctor":
            return doctor()
        elif args.command == "check":
            check_ebpf(ROOT / "data" / "ebpf-check.json")
        elif args.command == "sidecar":
            sidecar()
        elif args.command == "agent":
            agent(extra)
        elif args.command == "benchmark":
            benchmark(extra)
    except (SetupError, subprocess.CalledProcessError) as exc:
        log(f"Failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
