#!/usr/bin/env python3
"""Build and stage the dependency-complete Python sidecar.

PyInstaller builds for the interpreter's current operating system and CPU
architecture. The resulting onedir is staged at ``.build/staged/sidecar`` so
Tauri can copy it into the installed app's Resources directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_ROOT = PROJECT_ROOT / "sidecar"
BUILD_ROOT = PROJECT_ROOT / ".build" / "sidecar"
STAGE_ROOT = PROJECT_ROOT / ".build" / "staged" / "sidecar"


def native_architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86_64", "amd64"}:
        return "x86_64"
    raise SystemExit(
        f"无法识别当前 Python 架构：{platform.machine()}；"
        "仅支持 arm64/aarch64 或 x86_64/amd64。"
    )


def target_architecture(target: str) -> str:
    if target.startswith("aarch64-"):
        return "arm64"
    if target.startswith("x86_64-"):
        return "x86_64"
    raise SystemExit(
        f"不支持的目标架构：{target}；仅支持 aarch64 或 x86_64。"
    )


def host_target() -> str:
    system = sys.platform
    machine = native_architecture()
    if system == "darwin":
        if machine == "arm64":
            return "aarch64-apple-darwin"
        if machine == "x86_64":
            return "x86_64-apple-darwin"
    if system == "win32":
        if machine == "x86_64":
            return "x86_64-pc-windows-msvc"
        if machine == "arm64":
            return "aarch64-pc-windows-msvc"
    raise SystemExit(
        f"无法从当前环境推断 sidecar target：{sys.platform}/{platform.machine()}；"
        "请显式传入 --target。"
    )


def target_platform(target: str) -> str:
    if "darwin" in target:
        return "macos"
    if "windows" in target:
        return "windows"
    raise SystemExit(f"不支持的 sidecar target：{target}")


def ensure_native_target(target: str) -> None:
    expected = "macos" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else None
    if expected is None or target_platform(target) != expected:
        raise SystemExit(
            "PyInstaller 不是 cross-compiler；请在目标操作系统上构建 sidecar。"
            f" 当前环境={sys.platform}，目标={target}。"
        )
    target_arch = target_architecture(target)
    host_arch = native_architecture()
    if target_arch != host_arch:
        raise SystemExit(
            "当前 Python 架构与目标架构不一致；请使用对应架构的 Python。"
            f" 当前={platform.machine()}（{host_arch}），目标={target}（{target_arch}）。"
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_pyinstaller(python: str, target: str, work_root: Path) -> Path:
    dist_root = work_root / "dist"
    work_path = work_root / "work"
    spec_path = work_root / "spec"
    shutil.rmtree(work_root, ignore_errors=True)
    dist_root.mkdir(parents=True, exist_ok=True)
    work_path.mkdir(parents=True, exist_ok=True)
    spec_path.mkdir(parents=True, exist_ok=True)

    command = [
        python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "email-sidecar",
        "--distpath",
        str(dist_root),
        "--workpath",
        str(work_path),
        "--specpath",
        str(spec_path),
        "--paths",
        str(SIDECAR_ROOT),
        # extract-msg has optional/dynamic package data. collect-all keeps
        # the release result independent of the build machine's site-packages.
        "--collect-all",
        "extract_msg",
        "--collect-all",
        "bs4",
        "--collect-all",
        "RTFDE",
        "--collect-all",
        "striprtf",
        str(SIDECAR_ROOT / "main.py"),
    ]
    try:
        environment = os.environ.copy()
        # Keep PyInstaller's cache inside the reproducible build tree. The
        # default macOS cache may belong to another user after a prior build.
        environment["PYINSTALLER_CONFIG_DIR"] = str(PROJECT_ROOT / ".build" / "pyinstaller-cache")
        subprocess.run(command, cwd=PROJECT_ROOT, check=True, env=environment)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"无法启动 Python：{python}；请用包含 PyInstaller 的构建 Python 重试。"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"PyInstaller 构建失败，退出码 {exc.returncode}。") from exc

    executable_name = "email-sidecar.exe" if target_platform(target) == "windows" else "email-sidecar"
    executable = dist_root / "email-sidecar" / executable_name
    if not executable.is_file():
        raise SystemExit(f"PyInstaller 未生成预期 sidecar：{executable}")
    return executable.parent


def stage_sidecar(bundle_root: Path, target: str) -> Path:
    if STAGE_ROOT.exists() or STAGE_ROOT.is_symlink():
        if STAGE_ROOT.is_dir() and not STAGE_ROOT.is_symlink():
            shutil.rmtree(STAGE_ROOT)
        else:
            STAGE_ROOT.unlink()
    STAGE_ROOT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle_root, STAGE_ROOT)
    executable = STAGE_ROOT / ("email-sidecar.exe" if target_platform(target) == "windows" else "email-sidecar")
    if target_platform(target) == "macos":
        executable.chmod(executable.stat().st_mode | 0o111)
    return executable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=None, help="Rust target triple; defaults to the native target")
    parser.add_argument("--python", default=None, help="Python interpreter containing requirements-build.txt")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target = args.target or host_target()
    ensure_native_target(target)
    python_value = args.python
    if python_value is None:
        venv_python = PROJECT_ROOT.parent / ".venv" / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        python_value = str(venv_python) if venv_python.is_file() else sys.executable
    # Keep the virtualenv launcher symlink intact. Path.resolve() would turn
    # ``.venv/bin/python`` into the system interpreter and lose site-packages.
    python = str(Path(python_value).expanduser().absolute())
    if not Path(python).is_file():
        raise SystemExit(f"构建 Python 不存在：{python}")
    probe = subprocess.run(
        [python, "-c", "import PyInstaller"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if probe.returncode:
        raise SystemExit(
            f"{python} 未安装 PyInstaller；请运行 `{python} -m pip install -r sidecar/requirements-build.txt`。"
        )

    bundle_root = run_pyinstaller(python, target, BUILD_ROOT / target)
    executable = stage_sidecar(bundle_root, target)
    manifest = {
        "target": target,
        "python": python,
        "format": "onedir",
        "executable": str(executable),
        "sha256": sha256(executable),
        "files": sum(1 for path in STAGE_ROOT.rglob("*") if path.is_file()),
    }
    manifest_path = BUILD_ROOT / target / "sidecar-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
