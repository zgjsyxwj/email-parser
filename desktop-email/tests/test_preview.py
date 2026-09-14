from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

SIDECAR = Path(__file__).resolve().parents[1] / "sidecar" / "main.py"


def preview(path: Path) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(SIDECAR), "--stdio", "--preview", str(path)],
        capture_output=True, timeout=30,
    )
    return result.returncode, json.loads(result.stdout)


class PreviewTests(unittest.TestCase):
    def test_preview_preserves_body_headers_and_leaves_input_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "原邮件.eml"
            mail = EmailMessage()
            mail["Subject"] = "原邮件主题"
            mail["From"] = "sender@example.test"
            mail["To"] = "recipient@example.test"
            mail["Cc"] = "copy@example.test"
            mail["Date"] = "Mon, 14 Sep 2026 09:00:00 +0800"
            mail.set_content("当前正文\n\n-----Original Message-----\n历史引用\n签名\n<script>alert(1)</script>")
            mail.add_attachment(b"attachment", maintype="application", subtype="octet-stream", filename="普通附件.txt")
            original = mail.as_bytes()
            source.write_bytes(original)
            code, data = preview(source)
            self.assertEqual(code, 0)
            self.assertEqual(data["subject"], "原邮件主题")
            self.assertEqual(data["sender"], "sender@example.test")
            self.assertEqual(data["recipients"], "recipient@example.test")
            self.assertEqual(data["cc"], "copy@example.test")
            self.assertIn("2026", data["sent_at"])
            self.assertIn("历史引用\n签名", data["body"])
            self.assertIn("<script>", data["body"])
            self.assertEqual(data["attachments"], ["普通附件.txt"])
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [source])

    def test_html_only_body_is_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "html.eml"
            mail = EmailMessage()
            mail.set_content("<html><body><p>正文</p><blockquote>历史引用</blockquote></body></html>", subtype="html")
            source.write_bytes(mail.as_bytes())
            code, data = preview(source)
            self.assertEqual(code, 0)
            self.assertIn("正文", data["body"])
            self.assertIn("历史引用", data["body"])

    def test_missing_and_invalid_files_return_readable_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.eml"
            damaged = Path(directory) / "damaged.msg"
            damaged.write_bytes(b"not an MSG file")
            for source in (missing, damaged):
                with self.subTest(source=source.name):
                    code, data = preview(source)
                    self.assertNotEqual(code, 0)
                    self.assertTrue(data["error"])

    def test_real_msg_preview(self):
        source = SIDECAR.parents[2] / "WTS ID  BPJS Health Portal (eDABU) Implementing MFA.msg"
        if not source.exists():
            self.skipTest("本机没有 MSG 样例")
        original = source.read_bytes()
        code, data = preview(source)
        self.assertEqual(code, 0)
        self.assertTrue(data["subject"])
        self.assertTrue(data["body"])
        self.assertEqual(source.read_bytes(), original)
