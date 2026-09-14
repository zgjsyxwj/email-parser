import json
import os
import subprocess
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path


class DiagnosticsTests(unittest.TestCase):
    def test_debug_logs_identify_mail_without_leaking_body_or_breaking_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "故障定位.eml"
            message = EmailMessage()
            message["Subject"] = "日志定位标题"
            message.set_content("PRIVATE_BODY_SENTINEL")
            source.write_bytes(message.as_bytes())
            broken = root / "损坏邮件.msg"
            broken.write_bytes(b"not a MSG")
            command = {"type": "start", "batch_id": "log-test", "request_id": "test",
                       "inputs": [str(source), str(broken)], "output_dir": str(root / "out")}
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "sidecar/main.py"), "--stdio"],
                input=json.dumps(command) + "\n", capture_output=True, text=True,
                encoding="utf-8", timeout=30,
                env={**os.environ, "EMAIL_LOG_LEVEL": "DEBUG", "PYTHONIOENCODING": "utf-8"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(events[-1]["type"], "batch_completed")
            self.assertIn("日志定位标题", result.stderr)
            self.assertIn("损坏邮件.msg", result.stderr)
            self.assertIn("item_failed", result.stderr)
            self.assertNotIn("PRIVATE_BODY_SENTINEL", result.stderr)


if __name__ == "__main__":
    unittest.main()
