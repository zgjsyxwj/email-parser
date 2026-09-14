from __future__ import annotations

import hashlib
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
KNOWN_NO_ATTACHMENT_MSG = PROJECT_ROOT.parent / "WTS ID  BPJS Health Portal (eDABU) Implementing MFA.msg"
KNOWN_ATTACHMENT_MSG = PROJECT_ROOT.parent / "RE_ Sunmi Malaysia EE benefit inquiry.msg"


def make_eml(path: Path) -> bytes:
    message = EmailMessage()
    message["Subject"] = "项目进度回顾"
    message["From"] = "Alice <alice@example.test>"
    message["To"] = "Bob <bob@example.test>"
    message["Cc"] = "Carol <carol@example.test>"
    message["Date"] = "Mon, 14 Sep 2026 09:00:00 +0800"
    message.set_content(
        "第一段正文。\n\n"
        "-----Original Message-----\n"
        "From: old@example.test\n"
        "历史引用仍需保留。\n\n"
        "Best regards,\n"
        "Alice\n"
    )
    data = message.as_bytes()
    path.write_bytes(data)
    return data


def make_html_only_eml(path: Path) -> bytes:
    message = EmailMessage()
    message["Subject"] = "HTML 正文与历史引用"
    message["From"] = "发件人 <sender@example.test>"
    message["To"] = "收件人 <recipient@example.test>"
    message["Date"] = "Mon, 14 Sep 2026 10:00:00 +0800"
    message.add_alternative(
        "<html><head><style>.hidden { display: none; }</style><script>bad()</script></head>"
        "<body><p>第一段正文。</p><blockquote>历史引用仍需保留。</blockquote>"
        "<p>Best regards,<br>Alice</p></body></html>",
        subtype="html",
    )
    data = message.as_bytes()
    path.write_bytes(data)
    return data


def make_rtf_only_eml(path: Path) -> bytes:
    raw = (
        b"Subject: =?utf-8?b?UlRGIOa1i+ivlQ==?=\r\n"
        b"From: sender@example.test\r\n"
        b"To: recipient@example.test\r\n"
        b"Date: Mon, 14 Sep 2026 11:00:00 +0800\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/rtf; charset=us-ascii\r\n"
        b"Content-Transfer-Encoding: 7bit\r\n"
        b"\r\n"
        b"{\\rtf1\\ansi\\ansicpg1252 RTF body\\par \\u20013?\\u25991? body\\par Best regards,\\par Alice}"
    )
    path.write_bytes(raw)
    return raw


def make_utf8_rtf_only_eml(path: Path) -> bytes:
    raw = (
        b"Subject: UTF-8 RTF\r\n"
        b"From: sender@example.test\r\n"
        b"To: recipient@example.test\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/rtf; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n"
    ) + r"{\rtf1\ansi\ansicpg65001 UTF-8 中文正文\par 第二行}".encode("utf-8")
    path.write_bytes(raw)
    return raw


def make_empty_body_eml(path: Path) -> bytes:
    message = EmailMessage()
    message["Subject"] = "只有主题的邮件"
    message.set_content("")
    data = message.as_bytes()
    path.write_bytes(data)
    return data


def run_batch(source: Path, output: Path, *, batch_id: str = "batch-test") -> list[dict]:
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
        "inputs": [str(source)],
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

    threading.Thread(target=read_stdout, name="sidecar-test-reader", daemon=True).start()
    deadline = time.monotonic() + 10
    def stop_process() -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("sidecar did not finish within 10 seconds")
            try:
                kind, line = lines.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError("sidecar did not finish within 10 seconds")
            if kind == "eof":
                stderr = process.stderr.read() if process.stderr else ""
                raise AssertionError(f"sidecar closed stdout before completion: {stderr}")
            event = json.loads(line)
            events.append(event)
            if event.get("type") == "batch_completed":
                break
    except BaseException:
        stop_process()
        raise
    process.stdin.close()
    process.wait(timeout=10)
    if process.returncode:
        stderr = process.stderr.read() if process.stderr else ""
        raise AssertionError(f"sidecar exited with {process.returncode}: {stderr}")
    if process.stdout:
        process.stdout.close()
    if process.stderr:
        process.stderr.close()
    return events


def item_result(events: list[dict]) -> dict:
    results = [event for event in events if event["type"] in {"item_succeeded", "item_failed", "item_partial_failed"}]
    if len(results) != 1:
        raise AssertionError(f"单封邮件应有一个终态，实际为 {len(results)}")
    return results[0]


class SingleEmlBatchTests(unittest.TestCase):
    def test_single_eml_writes_markdown_and_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.eml"
            output = root / "results"
            source_bytes = make_eml(source)
            source_hash = hashlib.sha256(source_bytes).hexdigest()

            events = run_batch(source, output)

            types = [event["type"] for event in events]
            self.assertLess(types.index("batch_started"), types.index("item_started"))
            self.assertLess(types.index("item_started"), types.index("item_succeeded"))
            self.assertEqual(types[-1], "batch_completed")
            self.assertTrue(all(event["batch_id"] == "batch-test" for event in events))
            succeeded = item_result(events)
            self.assertEqual(succeeded["status"], "success")
            self.assertEqual(succeeded["subject"], "项目进度回顾")

            mail_dir = Path(succeeded["mail_dir"])
            self.assertEqual(mail_dir.parent, output.resolve())
            self.assertTrue(mail_dir.is_dir())
            self.assertEqual(sorted(path.name for path in mail_dir.iterdir()), [".complete.json", "attachments", "emails", "邮件.md"])
            markdown = (mail_dir / "邮件.md").read_text(encoding="utf-8")
            self.assertIn("# 项目进度回顾", markdown)
            self.assertIn("Alice <alice@example.test>", markdown)
            self.assertIn("Bob <bob@example.test>", markdown)
            self.assertIn("Carol <carol@example.test>", markdown)
            self.assertIn("历史引用仍需保留。", markdown)
            self.assertIn("Best regards,\nAlice", markdown)
            self.assertNotIn("（未提供）", markdown)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)

            completed = events[-1]
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["summary"], {"total": 1, "succeeded": 1, "partial_failed": 0, "skipped": 0, "failed": 0, "cancelled": 0, "skipped_files": 0})

    def test_html_only_eml_falls_back_to_readable_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "中文邮件 名称.eml"
            output = root / "results"
            make_html_only_eml(source)

            events = run_batch(source, output, batch_id="batch-html")

            self.assertEqual(item_result(events)["type"], "item_succeeded")
            markdown = Path(item_result(events)["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("第一段正文。", markdown)
            self.assertIn("历史引用仍需保留。", markdown)
            self.assertIn("Best regards,\nAlice", markdown)
            self.assertNotIn("bad()", markdown)

    def test_rtf_only_eml_falls_back_to_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "RTF 中文.eml"
            output = root / "results"
            make_rtf_only_eml(source)

            events = run_batch(source, output, batch_id="batch-rtf")

            self.assertEqual(item_result(events)["type"], "item_succeeded")
            markdown = Path(item_result(events)["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("RTF body", markdown)
            self.assertIn("中文 body", markdown)
            self.assertIn("Best regards,\nAlice", markdown)

    def test_utf8_rtf_eml_preserves_literal_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "UTF-8 RTF 中文.eml"
            output = root / "results"
            make_utf8_rtf_only_eml(source)

            events = run_batch(source, output, batch_id="batch-rtf-utf8")

            self.assertEqual(item_result(events)["type"], "item_succeeded")
            markdown = Path(item_result(events)["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("UTF-8 中文正文", markdown)
            self.assertIn("第二行", markdown)
            self.assertNotIn("?", markdown)

    def test_empty_body_and_missing_headers_are_reported_without_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "缺失字段.eml"
            output = root / "results"
            make_empty_body_eml(source)

            events = run_batch(source, output, batch_id="batch-empty")

            self.assertEqual(item_result(events)["type"], "item_succeeded")
            markdown = Path(item_result(events)["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("# 只有主题的邮件", markdown)
            self.assertIn("（无文本正文）", markdown)
            self.assertNotIn("发件人", markdown)
            self.assertNotIn("收件人", markdown)

    @unittest.skipUnless(KNOWN_ATTACHMENT_MSG.is_file(), "workspace real MSG sample is unavailable")
    def test_real_msg_ordinary_attachments_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"

            events = run_batch(KNOWN_ATTACHMENT_MSG, output, batch_id="batch-msg-attachment")

            self.assertEqual(item_result(events)["type"], "item_succeeded")
            self.assertEqual(item_result(events)["status"], "success")
            self.assertEqual(item_result(events)["attachments"], 4)
            import extract_msg
            with extract_msg.openMsg(KNOWN_ATTACHMENT_MSG) as message:
                expected = sorted(hashlib.sha256(attachment.data).hexdigest() for attachment in message.attachments)
            saved = list((Path(item_result(events)["mail_dir"]) / "attachments").iterdir())
            self.assertEqual(len(saved), 4)
            self.assertEqual(sorted(hashlib.sha256(path.read_bytes()).hexdigest() for path in saved), expected)
            self.assertEqual(events[-1]["status"], "completed")
            self.assertEqual(events[-1]["summary"]["succeeded"], 1)

    @unittest.skipUnless(KNOWN_NO_ATTACHMENT_MSG.is_file(), "workspace real MSG sample is unavailable")
    def test_real_msg_without_attachments_writes_consistent_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"

            events = run_batch(KNOWN_NO_ATTACHMENT_MSG, output, batch_id="batch-msg")

            self.assertEqual(item_result(events)["type"], "item_succeeded")
            self.assertEqual(item_result(events)["status"], "success")
            self.assertEqual(item_result(events)["subject"], "WTS ID: BPJS Health Portal (eDABU) Implementing MFA")
            markdown = Path(item_result(events)["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("Outsource Asia <outsource.asia@payroll2u.com>", markdown)
            self.assertIn("Hi Betty,", markdown)
            self.assertIn("Regards,\nAmirah", markdown)
            self.assertEqual(events[-1]["summary"], {"total": 1, "succeeded": 1, "partial_failed": 0, "skipped": 0, "failed": 0, "cancelled": 0, "skipped_files": 0})

    def test_invalid_eml_reports_item_failure_and_batch_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "broken.eml"
            output = root / "results"
            source.write_bytes(b"this is not an email")

            events = run_batch(source, output, batch_id="batch-invalid")

            self.assertEqual(item_result(events)["type"], "item_failed")
            self.assertEqual(item_result(events)["stage"], "parse")
            self.assertEqual(item_result(events)["status"], "failed")
            self.assertEqual(events[-1]["type"], "batch_completed")
            self.assertEqual(events[-1]["status"], "failed")
            self.assertEqual(events[-1]["summary"]["failed"], 1)
            self.assertFalse(output.exists() and any(output.iterdir()))


if __name__ == "__main__":
    unittest.main()
