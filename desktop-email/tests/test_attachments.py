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
KNOWN_ATTACHMENT_MSG = PROJECT_ROOT.parent / "RE_ Sunmi Malaysia EE benefit inquiry.msg"


def run_batch(source: Path, output: Path, *, batch_id: str) -> list[dict]:
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
        "request_id": "request-attachments",
        "inputs": [str(source)],
        "output_dir": str(output),
    }
    assert process.stdin is not None
    process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
    process.stdin.flush()

    lines: queue.Queue[tuple[str, str]] = queue.Queue()

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(("line", line))
        lines.put(("eof", ""))

    threading.Thread(target=read_stdout, name="attachment-sidecar-reader", daemon=True).start()
    events: list[dict] = []
    deadline = time.monotonic() + 10
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("sidecar did not finish within 10 seconds")
            try:
                kind, line = lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise AssertionError("sidecar did not finish within 10 seconds") from exc
            if kind == "eof":
                stderr = process.stderr.read() if process.stderr else ""
                raise AssertionError(f"sidecar closed stdout before completion: {stderr}")
            event = json.loads(line)
            events.append(event)
            if event.get("type") == "batch_completed":
                break
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=10)
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
    if process.returncode:
        raise AssertionError(f"sidecar exited with {process.returncode}")
    return events


def make_attachment_eml(path: Path) -> dict[str, bytes]:
    data = {
        "report.pdf": b"%PDF-ordinary-attachment\x00\xff",
        "view.html": b"<html><body>attachment html</body></html>",
        "archive.zip": b"PK\x03\x04ordinary zip bytes",
        "image.png": b"\x89PNG\r\ninline image bytes",
        "../../escape.txt": b"path traversal must stay in attachments",
        "CON.txt": b"windows reserved name",
        "same.bin": b"first duplicate",
        "same.bin#2": b"second duplicate",
    }
    long_name = "报告" + ("a" * 250) + ".docx"
    data[long_name] = b"long filename attachment"
    message = EmailMessage()
    message["Subject"] = "附件完整性与安全文件名"
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message.set_content("正文必须完整保留，HTML 附件不能并入正文。")
    message.add_attachment(data["report.pdf"], maintype="application", subtype="pdf", filename="report.pdf")
    message.add_attachment(data["view.html"], maintype="text", subtype="html", filename="view.html")
    message.add_attachment(data["archive.zip"], maintype="application", subtype="zip", filename="archive.zip")
    message.add_attachment(data["image.png"], maintype="image", subtype="png", filename="image.png", cid="inline-image")
    message.add_attachment(data["../../escape.txt"], maintype="text", subtype="plain", filename="../../escape.txt")
    message.add_attachment(data["CON.txt"], maintype="text", subtype="plain", filename="CON.txt")
    message.add_attachment(data["same.bin"], maintype="application", subtype="octet-stream", filename="same.bin")
    message.add_attachment(data["same.bin#2"], maintype="application", subtype="octet-stream", filename="same.bin")
    message.add_attachment(b"nameless attachment", maintype="application", subtype="octet-stream")
    message.add_attachment(data[long_name], maintype="application", subtype="vnd.openxmlformats-officedocument.wordprocessingml.document", filename=long_name)
    path.write_bytes(message.as_bytes())
    return data


def make_nested_eml(path: Path) -> bytes:
    nested = EmailMessage()
    nested["Subject"] = "子邮件"
    nested["From"] = "nested@example.test"
    nested.set_content("nested body")
    nested_bytes = nested.as_bytes()

    message = EmailMessage()
    message["Subject"] = "带邮件附件"
    message["From"] = "sender@example.test"
    message.set_content("父邮件正文")
    message.add_attachment(nested_bytes, maintype="message", subtype="rfc822", filename="child.eml")
    path.write_bytes(message.as_bytes())
    return nested_bytes


class OrdinaryAttachmentBatchTests(unittest.TestCase):
    def test_eml_saves_decoded_bytes_and_safe_unique_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.eml"
            output = root / "results"
            expected = make_attachment_eml(source)

            events = run_batch(source, output, batch_id="batch-eml-attachments")
            item = next(event for event in events if event["type"] in {"item_succeeded", "item_partial_failed"})
            self.assertEqual(item["type"], "item_succeeded")
            self.assertEqual(item["attachments"], 10)
            mail_dir = Path(item["mail_dir"])
            attachment_dir = mail_dir / "attachments"
            saved = {hashlib.sha256(file.read_bytes()).hexdigest(): file for file in attachment_dir.iterdir()}

            for name, value in expected.items():
                digest = hashlib.sha256(value).hexdigest()
                self.assertIn(digest, saved, name)
                self.assertEqual(saved[digest].read_bytes(), value)
            self.assertTrue((attachment_dir / "same.bin").is_file())
            self.assertTrue((attachment_dir / "same (2).bin").is_file())
            self.assertTrue((attachment_dir / "_CON.txt").is_file())
            self.assertTrue(any(file.name.endswith(".docx") and len(file.name) <= 120 for file in attachment_dir.iterdir()))
            self.assertFalse((root / "escape.txt").exists())
            self.assertTrue((mail_dir / ".complete.json").is_file())

            markdown = (mail_dir / "邮件.md").read_text(encoding="utf-8")
            self.assertIn("正文必须完整保留", markdown)
            self.assertNotIn("attachment html", markdown)
            for file in attachment_dir.iterdir():
                self.assertIn(f"attachments/{file.name}", markdown)

    def test_mail_attachment_is_recursively_written_in_its_own_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.eml"
            output = root / "results"
            make_nested_eml(source)

            events = run_batch(source, output, batch_id="batch-nested-attachment")
            item = next(event for event in events if event["type"] == "item_succeeded")
            mail_dir = Path(item["mail_dir"])
            children = list((mail_dir / "emails").glob("*/邮件.md"))
            self.assertEqual(len(children), 1)
            self.assertIn("nested body", children[0].read_text(encoding="utf-8"))
            self.assertIn("父邮件正文", (mail_dir / "邮件.md").read_text(encoding="utf-8"))
            self.assertNotIn("nested body", (mail_dir / "邮件.md").read_text(encoding="utf-8"))
            self.assertTrue((mail_dir / ".complete.json").is_file())
            self.assertEqual(item["nested_emails"], 1)
            self.assertEqual(events[-1]["summary"]["partial_failed"], 0)

    @unittest.skipUnless(KNOWN_ATTACHMENT_MSG.is_file(), "workspace real MSG sample is unavailable")
    def test_real_msg_saves_attachment_bytes(self) -> None:
        import extract_msg

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            expected: list[bytes] = []
            message = extract_msg.openMsg(str(KNOWN_ATTACHMENT_MSG), attachmentsError=False, delayAttachments=True)
            try:
                expected = [attachment.data for attachment in message.attachments if isinstance(attachment.data, bytes)]
            finally:
                message.close()

            events = run_batch(KNOWN_ATTACHMENT_MSG, output, batch_id="batch-msg-attachments")
            item = next(event for event in events if event["type"] in {"item_succeeded", "item_partial_failed"})
            mail_dir = Path(item["mail_dir"])
            actual_digests = {hashlib.sha256(file.read_bytes()).hexdigest() for file in (mail_dir / "attachments").iterdir()}
            self.assertTrue(actual_digests)
            self.assertTrue(all(hashlib.sha256(value).hexdigest() in actual_digests for value in expected))
            self.assertIn("正文", (mail_dir / "邮件.md").read_text(encoding="utf-8") or "")


if __name__ == "__main__":
    unittest.main()
