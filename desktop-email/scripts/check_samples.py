"""通过真实 sidecar 验证本地样本；报告只包含统计，不包含业务邮件内容。"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_batch(command: list[str], inputs: list[Path], output: Path, batch_id: str) -> dict:
    request = {
        "type": "start", "batch_id": batch_id, "request_id": batch_id,
        "inputs": [str(path) for path in inputs], "output_dir": str(output),
    }
    process = subprocess.run(
        command + ["--stdio"],
        input=json.dumps(request, ensure_ascii=False) + "\n",
        capture_output=True, text=True, encoding="utf-8", timeout=600,
    )
    if process.returncode:
        raise RuntimeError(f"sidecar 退出码 {process.returncode}；未写入回归通过报告")
    events = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    completed = [event for event in events if event.get("type") == "batch_completed"]
    if len(completed) != 1 or completed[0]["summary"]["total"] != len(inputs):
        raise RuntimeError("批次终态缺失或输入总数不符")
    terminal = [event for event in events if event.get("type") in {
        "item_succeeded", "item_partial_failed", "item_failed", "item_cancelled",
    }]
    if len(terminal) != len(inputs):
        raise RuntimeError("单项终态数量与输入不符")
    failures = Counter(
        (error.get("stage", ""), error.get("code", ""))
        for error in completed[0].get("errors", [])
    )
    mail_dirs = {Path(event["mail_dir"]) for event in terminal if "mail_dir" in event}
    markdowns = list(output.rglob("邮件.md"))
    link_count = 0
    for markdown in markdowns:
        for target in re.findall(r"\]\(<((?:attachments|emails)/[^>]+)>\)", markdown.read_text(encoding="utf-8")):
            link_count += 1
            if not (markdown.parent / target).is_file():
                raise RuntimeError("邮件 Markdown 包含缺失的附件或子邮件链接")
    return {
        "status": completed[0]["status"],
        "summary": completed[0]["summary"],
        "unique_result_directories": len(mail_dirs),
        "mail_markdowns_on_disk": len(markdowns),
        "verified_local_links": link_count,
        "attachment_files_on_disk": sum(
            1 for path in output.rglob("*") if path.is_file() and path.parent.name == "attachments"
        ),
        "nested_email_occurrences": sum(event.get("nested_emails", 0) for event in terminal),
        "error_categories": [
            {"stage": stage, "code": code, "count": count}
            for (stage, code), count in sorted(failures.items())
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="必须是尚不存在的独立输出目录")
    parser.add_argument("--sidecar", type=Path, help="发行版可执行 sidecar；省略则运行源码")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    output = args.output.resolve()
    if output.exists():
        parser.error("输出目录已存在；请为本次回归选择新目录")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    sources = [(workspace / entry["path"], entry["sha256"]) for entry in baseline]
    if any(not path.is_file() or digest(path) != expected for path, expected in sources):
        raise RuntimeError("源文件与基线不一致；未执行回归")
    selected_root = workspace / "测试邮件" / "inputs"
    selected = [path for path, _ in sources if path.is_relative_to(selected_root)]
    historical = [path for path, _ in sources if not path.is_relative_to(selected_root)]
    if len(selected) != 13 or len(historical) != 111:
        raise RuntimeError("基线必须包含精选 13 份与历史 111 份邮件")
    command = [str(args.sidecar.resolve())] if args.sidecar else [
        sys.executable, str(Path(__file__).resolve().parents[1] / "sidecar" / "main.py"),
    ]
    output.mkdir(parents=True)
    report = {"source_files": len(sources), "sets": {}}
    for name, paths in (("selected", selected), ("historical", historical)):
        destination = output / name
        report["sets"][name] = {
            "inputs": len(paths), "unique_contents": len({digest(path) for path in paths}),
            "first": run_batch(command, paths, destination, name + "-first"),
            "repeat": run_batch(command, paths, destination, name + "-repeat"),
        }
    report["sources_unchanged"] = all(digest(path) == expected for path, expected in sources)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if not report["sources_unchanged"]:
        raise RuntimeError("回归后源文件与基线不一致")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    for name, expected_unique in (("selected", 12), ("historical", 110)):
        result = report["sets"][name]
        if result["unique_contents"] != expected_unique:
            raise RuntimeError(f"{name} 样本内容去重数量不符")
        for phase in ("first", "repeat"):
            summary = result[phase]["summary"]
            expected_success = expected_unique if phase == "first" else 0
            if (
                summary["succeeded"] != expected_success
                or summary["skipped"] != result["inputs"] - expected_success
                or any(summary[key] for key in ("partial_failed", "failed", "cancelled"))
            ):
                raise RuntimeError(f"{name}/{phase} 样本回归未通过；实际统计见 report.json")


if __name__ == "__main__":
    main()
