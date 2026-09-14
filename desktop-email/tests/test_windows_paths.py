from __future__ import annotations

import os
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sidecar"))
import mail_parser as parser


class WindowsOutputPathTests(unittest.TestCase):
    def test_windows_path_forms(self):
        for source, expected in (
            ("C:\\邮件\\结果", "\\\\?\\C:\\邮件\\结果"),
            ("\\\\server\\share\\结果", "\\\\?\\UNC\\server\\share\\结果"),
            ("\\\\?\\C:\\结果", "\\\\?\\C:\\结果"),
            ("\\\\?\\UNC\\server\\share", "\\\\?\\UNC\\server\\share"),
        ):
            with self.subTest(source=source):
                self.assertEqual(parser._extended_windows_path(source), expected)

    def test_writer_enters_recursion_with_extended_path(self):
        # 模拟未开启长路径策略的 Windows 写入边界；覆盖公开写入入口。
        with patch.object(parser, "os", wraps=os) as windows_os:
            windows_os.name = "nt"
            with patch.object(parser, "_write_tree") as writer:
                def require_extended_path(parsed, root, **kwargs):
                    if not str(root).startswith("\\\\?\\"):
                        raise OSError("Windows 长路径必须使用扩展路径")
                    return "written"
                writer.side_effect = require_extended_path
                self.assertEqual(parser.write_result(None, Path("results")), "written")

    @unittest.skipUnless(os.name == "nt", "需要 Windows 文件系统")
    def test_nested_long_paths_write_repair_and_skip(self):
        message = EmailMessage()
        message["Subject"] = "child-" + "x" * 74
        message.set_content("nested body")
        message.add_attachment(b"payload", maintype="application", subtype="octet-stream", filename="data.bin")
        for depth in range(4):
            parent = EmailMessage()
            parent["Subject"] = str(depth) + "y" * 79
            parent.set_content("parent body")
            parent.add_attachment(message)
            message = parent
        with tempfile.TemporaryDirectory() as directory:
            # 扩展路径也用于测试清理，不依赖机器的 LongPathsEnabled 设置。
            root = Path(parser._extended_windows_path(str(Path(directory).absolute())))
            source = root / "input.eml"
            source.write_bytes(message.as_bytes())
            parsed = parser.parse_email(source)
            result = parser.write_result(parsed, root / "results")
            self.assertEqual(result.status, "success", result.errors)
            attachment = next(result.mail_dir.rglob("data.bin"))
            self.assertGreater(len(str(attachment)), 260)
            self.assertEqual(attachment.read_bytes(), b"payload")
            self.assertTrue(parser.write_result(parsed, root / "results").skipped)
            attachment.unlink()
            repaired = parser.write_result(parsed, root / "results")
            self.assertEqual(repaired.status, "success", repaired.errors)
            self.assertEqual(attachment.read_bytes(), b"payload")
            self.assertFalse(any(".partial-" in p.name for p in root.rglob("*")))
            parser.shutil.rmtree(root / "results")


if __name__ == "__main__":
    unittest.main()
