#!/usr/bin/env python3
"""Build a reproducible native sidecar and Tauri installer for one target."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_BUILDER = PROJECT_ROOT / "scripts" / "build_sidecar.py"


def native_target() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        architecture = "aarch64"
    elif machine in {"x86_64", "amd64"}:
        architecture = "x86_64"
    else:
        raise SystemExit(
            f"无法识别当前 Python 架构：{platform.machine()}；"
            "仅支持 arm64/aarch64 或 x86_64/amd64。"
        )
    if sys.platform == "darwin":
        return f"{architecture}-apple-darwin"
    if sys.platform == "win32":
        return f"{architecture}-pc-windows-msvc"
    raise SystemExit(f"发行构建当前只支持 macOS 和 Windows，当前系统为 {sys.platform}。")


def platform_for(target: str) -> str:
    if "darwin" in target:
        return "macos"
    if "windows" in target:
        return "windows"
    raise SystemExit(f"不支持的 Tauri target：{target}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=None, help="Rust target triple; defaults to the native target")
    parser.add_argument("--python", default=None, help="Python interpreter used by PyInstaller")
    return parser.parse_args()


def build_python(value: str | None) -> str:
    if value:
        # Keep a virtualenv launcher symlink intact; resolving it selects the
        # system interpreter and drops the build dependencies.
        return str(Path(value).expanduser().absolute())
    venv_python = PROJECT_ROOT.parent / ".venv" / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    return str(venv_python if venv_python.is_file() else Path(sys.executable).absolute())


def run(command: list[str]) -> None:
    started = time.monotonic()
    print(f"[build] start: {command}", flush=True)
    try:
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
        print(f"[build] completed in {time.monotonic() - started:.1f}s", flush=True)
    except FileNotFoundError as exc:
        raise SystemExit(f"找不到构建命令：{command[0]}；请先安装对应工具链。") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"发行构建失败，退出码 {exc.returncode}：{' '.join(command)}") from exc


def npm_executable() -> str:
    candidates = ("npm.cmd", "npm.exe", "npm") if sys.platform == "win32" else ("npm",)
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise SystemExit("找不到 npm；请先安装 Node.js 并确保 npm 在 PATH 中。")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_field(digest: "hashlib._Hash", value: bytes) -> None:
    digest.update(len(value).to_bytes(8, byteorder="big"))
    digest.update(value)


def deterministic_tree_sha256(root: Path) -> str:
    """Hash a directory tree without absolute paths or filesystem timestamps.

    Each sorted entry contributes its type, relative POSIX path, permission
    bits, and either its content hash or symlink target. Directory mtimes and
    the host checkout path therefore do not affect the result.
    """

    digest = hashlib.sha256()
    entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for entry in entries:
        relative = entry.relative_to(root).as_posix().encode("utf-8", "surrogateescape")
        metadata = entry.lstat()
        mode = str(stat.S_IMODE(metadata.st_mode)).encode("ascii")
        if stat.S_ISLNK(metadata.st_mode):
            entry_type = b"symlink"
            payload = os.readlink(entry).encode("utf-8", "surrogateescape")
        elif stat.S_ISDIR(metadata.st_mode):
            entry_type = b"directory"
            payload = b""
        elif stat.S_ISREG(metadata.st_mode):
            entry_type = b"file"
            payload = bytes.fromhex(sha256(entry))
        else:
            continue
        for field in (entry_type, relative, mode, payload):
            _hash_field(digest, field)
    return digest.hexdigest()


def artifact_kind(path: Path) -> str:
    if path.is_dir() and not path.is_symlink():
        return "directory_tree"
    return "file"


def artifact_sha256(path: Path) -> str:
    if artifact_kind(path) == "directory_tree":
        return deterministic_tree_sha256(path)
    return sha256(path)


def installer_paths(target: str) -> list[Path]:
    bundle_root = PROJECT_ROOT / "src-tauri" / "target" / target / "release" / "bundle"
    if platform_for(target) == "macos":
        candidates = [bundle_root / "macos", bundle_root / "dmg"]
        suffixes = {".app", ".dmg"}
    else:
        candidates = [bundle_root / "nsis", bundle_root / "msi"]
        suffixes = {".exe", ".msi"}
    return sorted(
        path
        for root in candidates
        if root.exists()
        for path in root.iterdir()
        if path.suffix.lower() in suffixes
    )


def main() -> int:
    args = parse_args()
    target = args.target or native_target()
    python = build_python(args.python)
    expected = platform_for(target)
    if (sys.platform == "darwin") != (expected == "macos") or (sys.platform == "win32") != (expected == "windows"):
        raise SystemExit(
            "PyInstaller 不是 cross-compiler；请在目标操作系统上运行此脚本。"
            f" 当前系统={sys.platform}，目标={target}。"
        )

    run([python, str(SIDECAR_BUILDER), "--target", target, "--python", python])

    # Always install from the lockfile. Reusing an existing node_modules tree
    # could silently build with a different CLI version than package-lock.json.
    run([npm_executable(), "ci", "--ignore-scripts"])
    tauri_binary = PROJECT_ROOT / "node_modules" / (
        ".bin/tauri.cmd" if sys.platform == "win32" else ".bin/tauri"
    )
    if not tauri_binary.is_file():
        raise SystemExit("npm ci 完成后仍找不到 Tauri CLI；请检查 package.json 和 package-lock.json。")

    bundles = "app,dmg" if expected == "macos" else "nsis,msi"
    run([
        str(tauri_binary),
        "build",
        "--target",
        target,
        "--bundles",
        bundles,
    ])
    outputs = installer_paths(target)
    if expected == "macos":
        required = {".app", ".dmg"}
        found = {path.suffix.lower() for path in outputs}
        missing = required - found
        if missing:
            raise SystemExit(f"macOS 构建未生成所需安装产物：{', '.join(sorted(missing))}")
    else:
        required_bundles = {"nsis": ".exe", "msi": ".msi"}
        missing = [
            f"{bundle}/{suffix}"
            for bundle, suffix in required_bundles.items()
            if not any(
                path.parent.name.lower() == bundle and path.suffix.lower() == suffix
                for path in outputs
            )
        ]
        if missing:
            raise SystemExit(
                "Windows 构建未生成所需安装产物：" + ", ".join(missing)
            )
    manifest = {
        "target": target,
        "platform": expected,
        "host": {"system": sys.platform, "machine": platform.machine()},
        "installers": [
            {
                "path": str(path),
                "kind": artifact_kind(path),
                "sha256": artifact_sha256(path),
            }
            for path in outputs
        ],
        "sidecar": str(PROJECT_ROOT / ".build" / "sidecar" / target / "sidecar-manifest.json"),
    }
    manifest_path = PROJECT_ROOT / ".build" / "releases" / target / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
