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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIDECAR = PROJECT_ROOT / "sidecar" / "main.py"


def write_eml(path: Path, *, subject: str = "批量邮件", body: str = "批量正文") -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message.set_content(body)
    path.write_bytes(message.as_bytes())


def write_eml_with_broken_nested_attachment(path: Path) -> None:
    outer = EmailMessage()
    outer["Subject"] = "包含损坏嵌套邮件"
    outer["From"] = "sender@example.test"
    outer["To"] = "recipient@example.test"
    outer.set_content("外层正文")
    outer.add_attachment(
        b"not an email",
        maintype="application",
        subtype="octet-stream",
        filename="broken.eml",
    )
    path.write_bytes(outer.as_bytes())


def run_batch(inputs: list[Path], output: Path, *, batch_id: str = "batch-test") -> list[dict]:
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
    command = {
        "type": "start",
        "batch_id": batch_id,
        "request_id": "request-test",
        "inputs": [str(path) for path in inputs],
        "output_dir": str(output),
    }
    assert process.stdin is not None
    process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
    process.stdin.flush()

    events: list[dict] = []
    assert process.stdout is not None
    lines: queue.Queue[tuple[str, str]] = queue.Queue()

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(("line", line))
        lines.put(("eof", ""))

    threading.Thread(target=read_stdout, name="batch-test-reader", daemon=True).start()
    deadline = time.monotonic() + 30
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("sidecar did not finish within 30 seconds")
            try:
                kind, line = lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise AssertionError("sidecar did not finish within 30 seconds") from exc
            if kind == "eof":
                stderr = process.stderr.read() if process.stderr else ""
                raise AssertionError(f"sidecar closed stdout before completion: {stderr}")
            events.append(json.loads(line))
            if events[-1].get("type") == "batch_completed":
                break
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
    return events


class BatchAndErrorIsolationTests(unittest.TestCase):
    def test_batch_continues_after_one_corrupt_mail_and_reports_source_stage_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.eml"
            broken = root / "broken.eml"
            last = root / "last.eml"
            output = root / "results"
            write_eml(first, subject="第一封")
            broken.write_bytes(b"this is not an email")
            write_eml(last, subject="第三封")

            events = run_batch([first, broken, last], output, batch_id="batch-isolated")

            item_events = [event for event in events if event["type"] in {"item_succeeded", "item_failed"}]
            self.assertEqual(len(item_events), 3)
            self.assertEqual([event["type"] for event in item_events], ["item_succeeded", "item_failed", "item_succeeded"])
            failure = item_events[1]
            self.assertEqual(failure["source_path"], str(broken.resolve()))
            self.assertEqual(failure["stage"], "parse")
            self.assertEqual(failure["code"], "invalid_eml")
            self.assertTrue(failure["error"])
            completed = events[-1]
            self.assertEqual(completed["status"], "failed")
            self.assertEqual(
                completed["summary"],
                {
                    "total": 3,
                    "succeeded": 2,
                    "partial_failed": 0,
                    "skipped": 0,
                    "failed": 1,
                    "cancelled": 0,
                    "skipped_files": 0,
                },
            )
            self.assertEqual(len(list(output.glob("*/邮件.md"))), 2)
            self.assertEqual(len(completed["errors"]), 1)
            self.assertEqual(completed["errors"][0]["source_path"], str(broken.resolve()))

    def test_directory_input_scans_mail_skips_other_files_and_excludes_output_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "incoming"
            output = source_dir / "results"
            source_dir.mkdir()
            write_eml(source_dir / "mail.eml", subject="输入邮件")
            (source_dir / "说明.txt").write_text("独立文件", encoding="utf-8")
            output.mkdir()
            write_eml(output / "old-result.eml", subject="不应扫描")

            events = run_batch([source_dir], output, batch_id="batch-scan")

            skipped = [event for event in events if event["type"] == "input_skipped"]
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["source_path"], str((source_dir / "说明.txt").resolve()))
            self.assertEqual(skipped[0]["stage"], "input")
            self.assertEqual(skipped[0]["code"], "unsupported_format")
            started = next(event for event in events if event["type"] == "batch_started")
            self.assertEqual(started["mail_paths"], [str((source_dir / "mail.eml").resolve())])
            self.assertEqual(events[-1]["summary"]["total"], 1)
            self.assertEqual(events[-1]["summary"]["succeeded"], 1)
            self.assertEqual(events[-1]["summary"]["skipped_files"], 1)
            self.assertFalse(any("不应扫描" in str(event) for event in events))

    def test_input_root_equal_to_output_is_excluded_from_final_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_eml(root / "first.eml", subject="已有结果一")
            write_eml(root / "second.eml", subject="已有结果二")

            events = run_batch([root], root, batch_id="batch-output-root")

            started = next(event for event in events if event["type"] == "batch_started")
            self.assertEqual(started["mail_paths"], [])
            self.assertEqual(started["total"], 0)
            self.assertEqual(started["excluded_output"], 1)
            self.assertEqual(events[-1]["summary"]["total"], 0)
            self.assertFalse(any(event["type"].startswith("item_") for event in events))

    def test_partial_result_has_its_own_terminal_event_and_error_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "nested.eml"
            output = root / "results"
            write_eml_with_broken_nested_attachment(source)

            events = run_batch([source], output, batch_id="batch-partial")

            partial = next(event for event in events if event["type"] == "item_partial_failed")
            self.assertEqual(partial["status"], "partial_failed")
            self.assertEqual(partial["attachments"], 1)
            self.assertEqual(partial["nested_emails"], 1)
            self.assertEqual(partial["errors"][0]["stage"], "attachments")
            self.assertEqual(partial["errors"][0]["location"], "broken.eml")
            self.assertEqual(events[-1]["status"], "partial_failed")
            self.assertEqual(events[-1]["summary"]["partial_failed"], 1)
            self.assertEqual(events[-1]["summary"]["failed"], 0)
            self.assertFalse((Path(partial["mail_dir"]) / ".complete.json").exists())

    def test_batch_of_one_hundred_mails_emits_one_terminal_event_per_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "inputs"
            output = root / "results"
            source_dir.mkdir()
            inputs = []
            for index in range(100):
                path = source_dir / f"mail-{index:03d}.eml"
                write_eml(path, subject=f"第 {index} 封", body=f"正文 {index}")
                inputs.append(path)

            events = run_batch([source_dir], output, batch_id="batch-100")

            self.assertEqual(len([event for event in events if event["type"] == "item_started"]), 100)
            self.assertEqual(len([event for event in events if event["type"] == "item_succeeded"]), 100)
            self.assertEqual(events[-1]["status"], "completed")
            self.assertEqual(events[-1]["summary"]["total"], 100)
            self.assertEqual(events[-1]["summary"]["succeeded"], 100)


if __name__ == "__main__":
    unittest.main()
