from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_sidecar_integration import item_result, run_batch
from test_limits import run_batch_with_limits


def make_mail(*, subject: str = "可重试邮件", body: str = "正文", attachment: bool = False) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message.set_content(body)
    if attachment:
        message.add_attachment(b"attachment bytes", maintype="application", subtype="pdf", filename="report.pdf")
    return message.as_bytes()


class RetryAndDeduplicationTests(unittest.TestCase):
    def test_same_content_with_a_different_filename_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.eml"
            second = root / "renamed-copy.eml"
            data = make_mail()
            first.write_bytes(data)
            second.write_bytes(data)
            output = root / "results"

            first_events = run_batch(first, output, batch_id="dedup-first")
            second_events = run_batch(second, output, batch_id="dedup-second")

            self.assertEqual(item_result(first_events)["type"], "item_succeeded")
            skipped = item_result(second_events)
            self.assertEqual(skipped["type"], "item_succeeded")
            self.assertEqual(skipped["status"], "skipped")
            self.assertEqual(second_events[-1]["summary"]["skipped"], 1)
            self.assertEqual(len(list(output.glob("*/邮件.md"))), 1)

    def test_unowned_result_directory_is_preserved_and_uses_a_stable_alternate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = make_mail(subject="collision")
            source = root / "source.eml"
            source.write_bytes(data)
            output = root / "results"
            occupied = output / f"collision-{hashlib.sha256(data).hexdigest()[:12]}"
            occupied.mkdir(parents=True)
            keep = occupied / "keep.txt"
            keep.write_text("用户目录", encoding="utf-8")

            result = item_result(run_batch(source, output, batch_id="collision"))

            self.assertEqual(result["status"], "success")
            self.assertNotEqual(Path(result["mail_dir"]), occupied)
            self.assertEqual(keep.read_text(encoding="utf-8"), "用户目录")
            self.assertTrue((Path(result["mail_dir"]) / ".complete.json").is_file())

    @unittest.skipUnless(hasattr(os, "symlink"), "当前平台不支持符号链接")
    def test_unowned_symlink_tree_is_not_followed_or_modified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = make_mail(subject="symlink")
            source = root / "source.eml"
            source.write_bytes(data)
            output = root / "results"
            external = root / "external.txt"
            external.write_text("外部内容", encoding="utf-8")
            occupied = output / f"symlink-{hashlib.sha256(data).hexdigest()[:12]}"
            occupied.mkdir(parents=True)
            link = occupied / "attachments"
            try:
                link.symlink_to(external)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows 当前进程没有创建符号链接权限（WinError 1314）")
                raise

            result = item_result(run_batch(source, output, batch_id="symlink"))

            self.assertEqual(result["status"], "success")
            self.assertNotEqual(Path(result["mail_dir"]), occupied)
            self.assertTrue(link.is_symlink())
            self.assertEqual(external.read_text(encoding="utf-8"), "外部内容")

    def test_missing_attachment_is_repaired_in_the_same_result_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.eml"
            source.write_bytes(make_mail(attachment=True))
            output = root / "results"

            first = item_result(run_batch(source, output, batch_id="repair-first"))
            mail_dir = Path(first["mail_dir"])
            attachment_path = mail_dir / "attachments" / "report.pdf"
            self.assertTrue(attachment_path.is_file())
            attachment_path.unlink()
            user_file = mail_dir / "keep-for-user.txt"
            user_file.write_text("用户文件", encoding="utf-8")

            retry = item_result(run_batch(source, output, batch_id="repair-second"))

            self.assertEqual(retry["type"], "item_succeeded")
            self.assertEqual(retry["status"], "success")
            self.assertEqual(Path(retry["mail_dir"]), mail_dir)
            self.assertEqual(attachment_path.read_bytes(), b"attachment bytes")
            self.assertEqual(user_file.read_text(encoding="utf-8"), "用户文件")
            self.assertEqual(len(list(output.glob("*-retry-*"))), 0)
            self.assertTrue((mail_dir / ".complete.json").is_file())

    def test_changed_attachment_or_markdown_is_not_considered_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.eml"
            source.write_bytes(make_mail(attachment=True))
            output = root / "results"

            first = item_result(run_batch(source, output, batch_id="integrity-first"))
            mail_dir = Path(first["mail_dir"])
            attachment_path = mail_dir / "attachments" / "report.pdf"
            attachment_path.write_bytes(b"tampered")
            markdown = mail_dir / "邮件.md"
            original_markdown = markdown.read_bytes()
            markdown.write_bytes(original_markdown + "\n用户改动\n".encode("utf-8"))

            retry = item_result(run_batch(source, output, batch_id="integrity-second"))

            self.assertEqual(retry["status"], "success")
            self.assertEqual(Path(retry["mail_dir"]), mail_dir)
            self.assertEqual(attachment_path.read_bytes(), b"attachment bytes")
            self.assertNotIn("用户改动", markdown.read_text(encoding="utf-8"))
            self.assertEqual(len(list(output.glob("*-retry-*"))), 0)

    def test_missing_nested_artifact_is_repaired_without_a_retry_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = EmailMessage()
            child["Subject"] = "子邮件"
            child["From"] = "child@example.test"
            child.set_content("子正文")
            outer = EmailMessage()
            outer["Subject"] = "父邮件"
            outer["From"] = "parent@example.test"
            outer.set_content("父正文")
            outer.add_attachment(child.as_bytes(), maintype="message", subtype="rfc822", filename="child.eml")
            source = root / "source.eml"
            source.write_bytes(outer.as_bytes())
            output = root / "results"

            first = item_result(run_batch(source, output, batch_id="child-first"))
            self.assertEqual(first["status"], "success")
            mail_dir = Path(first["mail_dir"])
            child_markdown = next((mail_dir / "emails").glob("*/邮件.md"))
            child_marker = child_markdown.parent / ".complete.json"
            child_marker.unlink()

            retry = item_result(run_batch(source, output, batch_id="child-second"))

            self.assertEqual(retry["status"], "success")
            self.assertEqual(Path(retry["mail_dir"]), mail_dir)
            self.assertTrue(child_marker.is_file())
            self.assertEqual(len(list(output.glob("*-retry-*"))), 0)

    def test_partial_result_recovers_in_place_when_the_limit_is_raised(self) -> None:
        child = EmailMessage()
        child["Subject"] = "受限子邮件"
        child["From"] = "child@example.test"
        child.set_content("子正文")
        outer = EmailMessage()
        outer["Subject"] = "受限父邮件"
        outer["From"] = "parent@example.test"
        outer.set_content("父正文")
        outer.add_attachment(child.as_bytes(), maintype="message", subtype="rfc822", filename="child.eml")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.eml"
            output = root / "results"
            source.write_bytes(outer.as_bytes())

            limited = item_result(
                run_batch_with_limits(
                    [source],
                    output,
                    {"max_depth": 0, "max_extract_bytes": len(child.as_bytes())},
                    batch_id="partial-first",
                )
            )
            self.assertEqual(limited["status"], "partial_failed")
            mail_dir = Path(limited["mail_dir"])
            self.assertTrue((mail_dir / ".partial.json").is_file())

            recovered = item_result(
                run_batch_with_limits(
                    [source],
                    output,
                    {"max_depth": 10, "max_extract_bytes": len(child.as_bytes())},
                    batch_id="partial-second",
                )
            )

            self.assertEqual(recovered["status"], "success")
            self.assertEqual(Path(recovered["mail_dir"]), mail_dir)
            self.assertTrue((mail_dir / ".complete.json").is_file())
            self.assertFalse((mail_dir / ".partial.json").exists())
            self.assertEqual(list((mail_dir / "attachments").iterdir()), [])
            self.assertEqual(len(list(output.glob("*-retry-*"))), 0)

    def test_user_modified_unexpanded_attachment_is_preserved_on_recovery(self) -> None:
        child = EmailMessage()
        child["Subject"] = "手工修改的子邮件"
        child["From"] = "child@example.test"
        child.set_content("子正文")
        outer = EmailMessage()
        outer["Subject"] = "手工修改的父邮件"
        outer["From"] = "parent@example.test"
        outer.set_content("父正文")
        outer.add_attachment(child.as_bytes(), maintype="message", subtype="rfc822", filename="child.eml")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.eml"
            output = root / "results"
            source.write_bytes(outer.as_bytes())

            limited = item_result(
                run_batch_with_limits(
                    [source],
                    output,
                    {"max_depth": 0, "max_extract_bytes": len(child.as_bytes())},
                    batch_id="user-fallback-first",
                )
            )
            mail_dir = Path(limited["mail_dir"])
            fallback = mail_dir / "attachments" / "child.eml"
            fallback.write_bytes(b"user edited bytes")

            still_limited = item_result(
                run_batch_with_limits(
                    [source],
                    output,
                    {"max_depth": 0, "max_extract_bytes": len(child.as_bytes())},
                    batch_id="user-fallback-repeat",
                )
            )
            self.assertEqual(still_limited["status"], "partial_failed")
            self.assertEqual(fallback.read_bytes(), b"user edited bytes")

            recovered = item_result(
                run_batch_with_limits(
                    [source],
                    output,
                    {"max_depth": 10, "max_extract_bytes": len(child.as_bytes())},
                    batch_id="user-fallback-second",
                )
            )

            self.assertEqual(recovered["status"], "success")
            self.assertEqual(Path(recovered["mail_dir"]), mail_dir)
            self.assertEqual(fallback.read_bytes(), b"user edited bytes")
            self.assertTrue((mail_dir / "emails").is_dir())
            self.assertEqual(len(list(output.glob("*-retry-*"))), 0)


if __name__ == "__main__":
    unittest.main()
