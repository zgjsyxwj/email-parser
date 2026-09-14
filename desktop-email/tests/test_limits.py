from __future__ import annotations

import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIDECAR = PROJECT_ROOT / "sidecar" / "main.py"


def make_eml(
    subject: str,
    body: str,
    *,
    ordinary: tuple[bytes, str] | None = None,
    nested: tuple[bytes, str] | None = None,
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message.set_content(body)
    if ordinary is not None:
        data, filename = ordinary
        message.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
    if nested is not None:
        data, filename = nested
        message.add_attachment(
            data,
            maintype="application",
            subtype="octet-stream",
            filename=filename,
        )
    return message.as_bytes()


def _read_events(process: subprocess.Popen[str], *, wait_for_completion: bool) -> list[dict[str, Any]]:
    assert process.stdout is not None
    lines: queue.Queue[tuple[str, str]] = queue.Queue()

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(("line", line))
        lines.put(("eof", ""))

    threading.Thread(target=read_stdout, name="limits-test-reader", daemon=True).start()
    deadline = time.monotonic() + 30
    events: list[dict[str, Any]] = []
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("sidecar did not finish within 30 seconds")
            kind, line = lines.get(timeout=remaining)
            if kind == "eof":
                stderr = process.stderr.read() if process.stderr else ""
                raise AssertionError(f"sidecar closed stdout before completion: {stderr}")
            events.append(json.loads(line))
            if not wait_for_completion or events[-1].get("type") == "batch_completed":
                return events
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


def run_batch_with_limits(
    inputs: list[Path],
    output: Path,
    limits: dict[str, Any] | None = None,
    *,
    batch_id: str = "batch-limits",
    wait_for_completion: bool = True,
) -> list[dict[str, Any]]:
    process = subprocess.Popen(
        [sys.executable, str(SIDECAR), "--stdio"],
        cwd=PROJECT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    command: dict[str, Any] = {
        "type": "start",
        "batch_id": batch_id,
        "request_id": f"request-{batch_id}",
        "inputs": [str(path) for path in inputs],
        "output_dir": str(output),
    }
    if limits is not None:
        command["limits"] = limits
    assert process.stdin is not None
    process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
    process.stdin.flush()
    return _read_events(process, wait_for_completion=wait_for_completion)


def reject_limits(limits: dict[str, Any]) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "mail.eml"
        source.write_bytes(make_eml("非法配置测试", "正文"))
        return run_batch_with_limits(
            [source],
            root / "results",
            limits,
            batch_id=f"reject-{abs(hash(repr(limits)))}",
            wait_for_completion=False,
        )


class RecursiveLimitTests(unittest.TestCase):
    def test_depth_limit_zero_keeps_nested_original_and_reports_partial(self) -> None:
        child = make_eml("子邮件", "子邮件正文")
        outer = make_eml("深度受限", "父邮件正文", nested=(child, "child.eml"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outer.eml"
            output = root / "results"
            source.write_bytes(outer)

            events = run_batch_with_limits(
                [source],
                output,
                {"max_depth": 0, "max_extract_bytes": len(child)},
                batch_id="limits-depth-zero",
            )

            started = next(event for event in events if event["type"] == "batch_started")
            self.assertEqual(
                started["limits"],
                {"max_depth": 0, "max_extract_bytes": len(child)},
            )
            item = next(event for event in events if event["type"] == "item_partial_failed")
            self.assertEqual(item["status"], "partial_failed")
            self.assertEqual(item["nested_emails"], 1)
            self.assertEqual(item["attachments"], 1)
            self.assertEqual(item["errors"][0]["stage"], "limits")
            self.assertEqual(item["errors"][0]["code"], "max_depth_exceeded")
            self.assertEqual(item["errors"][0]["location"], "child.eml")
            self.assertEqual(events[-1]["summary"]["partial_failed"], 1)
            self.assertEqual(events[-1]["summary"]["failed"], 0)

            mail_dir = Path(item["mail_dir"])
            self.assertEqual((mail_dir / "attachments" / "child.eml").read_bytes(), child)
            self.assertFalse(list((mail_dir / "emails").glob("*/邮件.md")))
            self.assertFalse((mail_dir / ".complete.json").exists())
            markdown = (mail_dir / "邮件.md").read_text(encoding="utf-8")
            self.assertIn("child.eml", markdown)
            self.assertIn("深度", markdown)

    def test_depth_limit_one_allows_first_child_and_retains_second(self) -> None:
        leaf = make_eml("叶邮件", "叶邮件正文")
        middle = make_eml("中层邮件", "中层正文", nested=(leaf, "leaf.eml"))
        outer = make_eml("一层限制", "父邮件正文", nested=(middle, "middle.eml"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outer.eml"
            output = root / "results"
            source.write_bytes(outer)

            events = run_batch_with_limits(
                [source],
                output,
                {"max_depth": 1, "max_extract_bytes": len(middle) + len(leaf)},
                batch_id="limits-depth-one",
            )

            item = next(event for event in events if event["type"] == "item_partial_failed")
            self.assertEqual(item["nested_emails"], 2)
            self.assertEqual(item["attachments"], 1)
            self.assertEqual(item["errors"][0]["code"], "max_depth_exceeded")
            mail_dir = Path(item["mail_dir"])
            middle_dir = next((mail_dir / "emails").glob("*/"))
            self.assertIn("中层正文", (middle_dir / "邮件.md").read_text(encoding="utf-8"))
            self.assertEqual((middle_dir / "attachments" / "leaf.eml").read_bytes(), leaf)
            self.assertFalse((middle_dir / ".complete.json").exists())

    def test_size_limit_exact_boundary_allows_child(self) -> None:
        child = make_eml("大小边界子邮件", "子邮件正文")
        outer = make_eml("大小边界父邮件", "父邮件正文", nested=(child, "child.eml"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outer.eml"
            output = root / "results"
            source.write_bytes(outer)

            events = run_batch_with_limits(
                [source],
                output,
                {"max_depth": 1, "max_extract_bytes": len(child)},
                batch_id="limits-size-equal",
            )

            item = next(event for event in events if event["type"] == "item_succeeded")
            self.assertEqual(item["nested_emails"], 1)
            self.assertEqual(item["attachments"], 0)
            self.assertEqual(events[-1]["status"], "completed")
            mail_dir = Path(item["mail_dir"])
            self.assertTrue((mail_dir / "emails" / next((mail_dir / "emails").iterdir()).name / ".complete.json").is_file())

    def test_size_budget_excludes_body_and_ordinary_attachment_bytes(self) -> None:
        child = make_eml("计量范围子邮件", "子邮件正文")
        ordinary = ("普通附件不占递归预算" * 1024).encode("utf-8")
        outer = make_eml(
            "计量范围父邮件",
            "父邮件正文" * 1024,
            ordinary=(ordinary, "document.bin"),
            nested=(child, "child.eml"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outer.eml"
            output = root / "results"
            source.write_bytes(outer)

            events = run_batch_with_limits(
                [source],
                output,
                {"max_depth": 1, "max_extract_bytes": len(child)},
                batch_id="limits-scope",
            )

            item = next(event for event in events if event["type"] == "item_succeeded")
            self.assertEqual(item["attachments"], 1)
            self.assertEqual(item["nested_emails"], 1)
            self.assertEqual(events[-1]["status"], "completed")
            mail_dir = Path(item["mail_dir"])
            self.assertEqual((mail_dir / "attachments" / "document.bin").read_bytes(), ordinary)

    def test_size_limit_overflow_keeps_child_and_other_input_continues(self) -> None:
        child = make_eml("超限子邮件", "子邮件正文")
        limited = make_eml("超限父邮件", "父邮件正文", nested=(child, "child.eml"))
        allowed = make_eml("继续处理", "仍然处理")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            limited_source = root / "limited.eml"
            allowed_source = root / "allowed.eml"
            output = root / "results"
            limited_source.write_bytes(limited)
            allowed_source.write_bytes(allowed)

            events = run_batch_with_limits(
                [limited_source, allowed_source],
                output,
                {"max_depth": 1, "max_extract_bytes": len(child) - 1},
                batch_id="limits-size-overflow",
            )

            terminals = [
                event
                for event in events
                if event["type"] in {"item_succeeded", "item_partial_failed", "item_failed"}
            ]
            self.assertEqual([event["type"] for event in terminals], ["item_partial_failed", "item_succeeded"])
            self.assertEqual(terminals[0]["errors"][0]["code"], "max_extract_bytes_exceeded")
            self.assertEqual(terminals[0]["errors"][0]["stage"], "limits")
            self.assertEqual((Path(terminals[0]["mail_dir"]) / "attachments" / "child.eml").read_bytes(), child)
            self.assertTrue((Path(terminals[1]["mail_dir"]) / ".complete.json").is_file())
            self.assertEqual(events[-1]["status"], "partial_failed")
            self.assertEqual(events[-1]["summary"]["succeeded"], 1)
            self.assertEqual(events[-1]["summary"]["partial_failed"], 1)

    def test_invalid_limits_are_rejected_before_batch(self) -> None:
        for limits in (
            {"max_depth": -1, "max_extract_bytes": 100},
            {"max_depth": 1.5, "max_extract_bytes": 100},
            {"max_depth": 1, "max_extract_bytes": -1},
            {"max_depth": 1, "max_extract_bytes": "100"},
            {"max_depth": 1},
        ):
            with self.subTest(limits=limits):
                events = reject_limits(limits)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["type"], "batch_rejected")
                self.assertEqual(events[0]["code"], "invalid_limits")
                self.assertEqual(events[0]["stage"], "limits")
                self.assertTrue(events[0]["error"])


if __name__ == "__main__":
    unittest.main()
