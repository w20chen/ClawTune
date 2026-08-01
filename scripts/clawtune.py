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
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ARM_ARCHES = {"aarch64", "arm64"}


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
        input=input_text,
        env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def require_linux() -> None:
    if platform.system() != "Linux":
        raise SetupError("ClawTune 的 eBPF 运行环境必须是 Linux；Windows/macOS 仅适合开发和单元测试。")


def host_arch() -> str:
    return platform.machine().lower()


def package_manager() -> str | None:
    if shutil.which("dnf"):
        return "dnf"
    if shutil.which("apt-get"):
        return "apt"
    return None


def kernel_build() -> Path:
    release = platform.release()
    candidate = Path("/lib/modules") / release / "build"
    try:
        return candidate.resolve(strict=True)
    except FileNotFoundError:
        return candidate


def python_has_bcc(executable: Path) -> bool:
    probe = (
        "import importlib.util,sys;"
        "sys.exit(0 if (importlib.util.find_spec('bcc') or "
        "importlib.util.find_spec('bpfcc')) else 1)"
    )
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
            "未找到 dnf 或 apt。请安装 BCC Python 绑定、clang/LLVM、bpftool 和当前内核开发包后重试。"
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
        log("安装 Debian/Ubuntu eBPF 依赖（apt）")
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
        raise SetupError("dnf 仓库中没有找到 Clang、内核开发包或 BCC；请先启用 openEuler OS/update 仓库。")
    log("安装 openEuler/RHEL 系 eBPF 依赖（dnf）")
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
            "系统 Python 仍无法导入 bcc/bpfcc。请运行 doctor；不要在 Conda 中另行 pip install BCC。"
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
        log(f"复用 {VENV}")
    else:
        if VENV.exists():
            raise SetupError(
                f"{VENV} 已存在但看不到系统 BCC。请将它改名或删除后重新运行 setup。"
            )
        log(f"使用 {system_python} 创建可访问系统 BCC 的 .venv")
        result = run(
            [system_python, "-m", "venv", "--system-site-packages", VENV],
            check=False,
        )
        if result.returncode != 0:
            raise SetupError("创建 .venv 失败；请安装该发行版的 python3-venv/python3 包。")
    run([venv_python, "-m", "pip", "install", "-e", "services/scheduler[dev]"])


def copy_defaults() -> None:
    for source, target in (
        (ROOT / ".env.example", ROOT / ".env"),
        (ROOT / "swe_rebench" / "config.example.yaml", ROOT / "swe_rebench" / "config.yaml"),
    ):
        if not target.exists():
            shutil.copy2(source, target)
            log(f"已创建 {target.relative_to(ROOT)}")


def repair_plugin_permissions() -> None:
    dist = ROOT / "packages" / "openclaw-plugin" / "dist"
    if not dist.exists():
        return
    generated_paths = [dist, *dist.rglob("*")]
    if all(os.access(path, os.W_OK) for path in generated_paths):
        return
    if not hasattr(os, "getuid"):
        return
    log("修复此前由 root 生成的插件文件权限")
    run(["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", dist])


def build_plugin() -> None:
    repair_plugin_permissions()
    plugin = ROOT / "packages" / "openclaw-plugin"
    log("安装并构建 OpenClaw 插件")
    subprocess.run(["npm", "install"], cwd=plugin, check=True)
    subprocess.run(["npm", "run", "build"], cwd=plugin, check=True)


def configure_openclaw() -> None:
    openclaw = shutil.which("openclaw")
    assert openclaw is not None
    plugin = ROOT / "packages" / "openclaw-plugin"
    installed = run([openclaw, "plugins", "install", "--link", plugin], check=False, capture=True)
    if installed.returncode != 0:
        combined = f"{installed.stdout}\n{installed.stderr}".lower()
        if "already" not in combined and "exists" not in combined:
            raise SetupError(f"OpenClaw 插件安装失败：\n{installed.stderr.strip()}")
    run([openclaw, "plugins", "enable", "agent-scheduler"])
    launcher = (VENV / "bin" / "claw-launch").resolve()
    patch = {
        "plugins": {
            "entries": {
                "agent-scheduler": {
                    "enabled": True,
                    "config": {
                        "endpoint": "http://127.0.0.1:8765",
                        "autoStartSidecar": False,
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
    log("OpenClaw 插件配置完成")


def setup_qemu_if_needed(skip_qemu: bool) -> None:
    if host_arch() not in ARM_ARCHES or skip_qemu:
        return
    log("Kunpeng/ARM 主机：启用并验证 linux/amd64 容器模拟")
    run(["sudo", "bash", "scripts/setup/arm_qemu_setup.sh", "install"])


def privileged_command(module_args: Sequence[str | Path]) -> list[str]:
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
    return [
        "sudo",
        "env",
        f"PATH={runtime_path}",
        f"PYTHONPATH={ROOT}",
        f"BCC_KERNEL_SOURCE={build}",
        *[str(item) for item in module_args],
    ]


def check_ebpf(output: Path | None = None) -> None:
    command: list[str | Path] = [VENV / "bin" / "python", "tools/check_ebpf.py"]
    if output is not None:
        command.extend(["--output", output])
    run(privileged_command(command))


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
                "bpftool",
            )
        },
        "kernel_headers": {"path": str(build), "present": build.is_dir()},
        "bcc_pythons": [str(path) for path in bcc_pythons()],
        "venv": {
            "path": str(VENV),
            "ready": (VENV / "bin" / "python").exists()
            and python_has_bcc(VENV / "bin" / "python"),
        },
        "kunpeng_amd64_emulation_required": host_arch() in ARM_ARCHES,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    healthy = (
        not required_commands()
        and report["kernel_headers"]["present"]
        and bool(report["bcc_pythons"])
        and report["venv"]["ready"]
    )
    if healthy:
        log("基础环境正常。运行 `python3 scripts/clawtune.py check` 做内核级 eBPF 验证。")
        return 0
    log("环境尚未就绪。运行 `python3 scripts/clawtune.py setup` 自动处理可安装项。")
    return 1


def setup(args: argparse.Namespace) -> None:
    require_linux()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SetupError("请用普通用户运行 setup；需要提权的步骤会自行调用 sudo。")
    build = kernel_build()
    missing_host_tools = [name for name in ("clang", "llc", "bpftool") if not shutil.which(name)]
    needs_host_packages = not bcc_pythons() or not build.is_dir() or bool(missing_host_tools)
    if needs_host_packages and not args.no_system_install:
        install_host_packages()
    system_python = find_bcc_python(install=False)
    build = kernel_build()
    if not build.is_dir():
        raise SetupError(f"找不到当前内核 {platform.release()} 的开发目录：{build}")
    missing_host_tools = [name for name in ("clang", "llc", "bpftool") if not shutil.which(name)]
    if missing_host_tools:
        raise SetupError("缺少 eBPF 工具：" + ", ".join(missing_host_tools))
    missing = required_commands()
    if missing:
        raise SetupError(
            "缺少不会由脚本擅自安装的应用："
            + ", ".join(missing)
            + "。请按 docs/getting-started.md 的“外部软件”一节安装后重试。"
        )
    create_venv(system_python)
    copy_defaults()
    build_plugin()
    setup_qemu_if_needed(args.skip_qemu)
    configure_openclaw()
    check_ebpf(ROOT / "data" / "ebpf-check.json")
    log("安装完成。下一步：编辑 .env/benchmark 配置，然后运行 sidecar 或 benchmark。")


def sidecar() -> None:
    require_linux()
    if not (VENV / "bin" / "python").exists():
        raise SetupError(".venv 不存在，请先运行 setup。")
    command = privileged_command(
        [
            f"AGENT_SCHEDULER_ENV_FILE={ROOT / '.env'}",
            VENV / "bin" / "python",
            "-m",
            "agent_scheduler.main",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
        ]
    )
    run(command)


def benchmark(extra: Sequence[str]) -> None:
    require_linux()
    if not (VENV / "bin" / "python").exists():
        raise SetupError(".venv 不存在，请先运行 setup。")
    config = ROOT / "swe_rebench" / "config.yaml"
    if not config.exists():
        raise SetupError("缺少 swe_rebench/config.yaml，请先运行 setup。")
    env_items: list[str] = []
    if host_arch() in ARM_ARCHES:
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
        ]
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
    sub.add_parser("benchmark", help="Run SWE-Rebench; remaining options go to the runner")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args, extra = parser().parse_known_args(argv)
    if extra and args.command != "benchmark":
        raise SetupError("无法识别的参数：" + " ".join(extra))
    try:
        if args.command == "setup":
            setup(args)
        elif args.command == "doctor":
            return doctor()
        elif args.command == "check":
            check_ebpf(ROOT / "data" / "ebpf-check.json")
        elif args.command == "sidecar":
            sidecar()
        elif args.command == "benchmark":
            benchmark(extra)
    except (SetupError, subprocess.CalledProcessError) as exc:
        log(f"失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
