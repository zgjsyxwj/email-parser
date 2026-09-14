from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from quopri import encodestring

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The test suite is also runnable as a standalone file, where the tests
# directory is not automatically added to sys.path.
from test_sidecar_integration import item_result, run_batch
from test_limits import run_batch_with_limits


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE07 = (
    PROJECT_ROOT.parent
    / "测试邮件"
    / "inputs"
    / (
        "转发_ POSTA CERTIFICATA_ Messaggio PEC inoltrato _POSTA CERTIFICATA_ "
        "INPS_ Invito a regolarizzare per la richiesta 50419618_.msg"
    )
)


def make_eml(subject: str, body: str, *, nested: tuple[bytes, str, str] | None = None) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{subject}@example.test"
    message.set_content(body)
    if nested is not None:
        data, filename, content_type = nested
        maintype, subtype = content_type.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


def make_qp_nested_eml(child: bytes) -> bytes:
    encoded = encodestring(child)
    return (
        b"Subject: QP parent\r\n"
        b"From: parent@example.test\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=qp-boundary\r\n"
        b"\r\n"
        b"--qp-boundary\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"parent body\r\n"
        b"--qp-boundary\r\n"
        b"Content-Type: message/rfc822; name=child.eml\r\n"
        b"Content-Disposition: attachment; filename=child.eml\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"\r\n"
        + encoded
        + b"--qp-boundary--\r\n"
    )


class NestedEmailTests(unittest.TestCase):
    def test_content_detection_recurses_without_extension_or_message_mime(self) -> None:
        leaf = (
            "Subject: child-without-other-headers\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "子邮件正文\r\n"
        ).encode("utf-8")
        outer = make_eml(
            "内容识别父邮件",
            "父邮件正文",
            nested=(leaf, "forwarded-mail", "application/octet-stream"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outer.eml"
            source.write_bytes(outer)
            events = run_batch(source, root / "results", batch_id="nested-content")

            item = item_result(events)
            self.assertEqual(item["type"], "item_succeeded")
            self.assertEqual(item["nested_emails"], 1)
            mail_dir = Path(item["mail_dir"])
            child_markdowns = list((mail_dir / "emails").glob("*/邮件.md"))
            self.assertEqual(len(child_markdowns), 1)
            self.assertIn("子邮件正文", child_markdowns[0].read_text(encoding="utf-8"))
            parent_markdown = (mail_dir / "邮件.md").read_text(encoding="utf-8")
            self.assertIn("emails/", parent_markdown)
            self.assertIn("父邮件正文", parent_markdown)
            self.assertNotIn("子邮件正文", parent_markdown)

    def test_subject_prefixed_text_attachment_stays_ordinary(self) -> None:
        message = EmailMessage()
        message["Subject"] = "普通文本附件"
        message["From"] = "sender@example.test"
        message.set_content("父邮件正文")
        notes = "Subject: 只是笔记\r\n\r\n这不是一封邮件。\r\n".encode("utf-8")
        message.add_attachment(
            notes,
            maintype="application",
            subtype="octet-stream",
            filename="notes.txt",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outer.eml"
            source.write_bytes(message.as_bytes())
            events = run_batch(source, root / "results", batch_id="nested-ordinary-subject")

            item = item_result(events)
            self.assertEqual(item["type"], "item_succeeded")
            self.assertEqual(item["nested_emails"], 0)
            self.assertEqual(item["attachments"], 1)
            mail_dir = Path(item["mail_dir"])
            self.assertEqual((mail_dir / "attachments" / "notes.txt").read_bytes(), notes)
            self.assertFalse(list((mail_dir / "emails").glob("*/邮件.md")))

    def test_named_eml_text_attachment_uses_strict_content_detection(self) -> None:
        child = make_eml("text/plain 子邮件", "子邮件正文")
        outer = make_eml(
            "text/plain 父邮件",
            "父邮件正文",
            nested=(child, "forwarded.eml", "text/plain"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outer.eml"
            source.write_bytes(outer)
            events = run_batch(source, root / "results", batch_id="nested-named-text")

            item = item_result(events)
            self.assertEqual(item["type"], "item_succeeded")
            self.assertEqual(item["nested_emails"], 1)
            self.assertEqual(item["attachments"], 0)
            mail_dir = Path(item["mail_dir"])
            child_markdown = next((mail_dir / "emails").glob("*/邮件.md"))
            self.assertIn("子邮件正文", child_markdown.read_text(encoding="utf-8"))

    def test_quoted_printable_nested_part_keeps_complete_child(self) -> None:
        child = b"Subject: QP child\r\nFrom: child@example.test\r\n\r\nbody = value\r\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outer.eml"
            source.write_bytes(make_qp_nested_eml(child))
            events = run_batch(source, root / "results", batch_id="nested-qp")

            item = item_result(events)
            self.assertEqual(item["type"], "item_succeeded")
            self.assertEqual(item["nested_emails"], 1)
            mail_dir = Path(item["mail_dir"])
            child_dir = next((mail_dir / "emails").glob("*/"))
            marker = json.loads((child_dir / ".complete.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["source_sha256"], hashlib.sha256(child).hexdigest())
            self.assertIn("body = value", (child_dir / "邮件.md").read_text(encoding="utf-8"))

    def test_quoted_printable_nested_part_at_depth_zero_keeps_exact_original(self) -> None:
        child = b"Subject: QP child\r\nFrom: child@example.test\r\n\r\nbody = value\r\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outer.eml"
            source.write_bytes(make_qp_nested_eml(child))
            events = run_batch_with_limits(
                [source],
                root / "results",
                {"max_depth": 0, "max_extract_bytes": len(child)},
                batch_id="nested-qp-depth-zero",
            )

            item = next(event for event in events if event["type"] == "item_partial_failed")
            self.assertEqual(item["nested_emails"], 1)
            self.assertEqual(item["attachments"], 1)
            self.assertEqual(item["errors"][0]["stage"], "limits")
            mail_dir = Path(item["mail_dir"])
            self.assertEqual((mail_dir / "attachments" / "child.eml").read_bytes(), child)

    def test_same_nested_content_keeps_a_link_under_each_parent(self) -> None:
        child = make_eml("共享子邮件", "共享正文")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.eml"
            second = root / "second.eml"
            first.write_bytes(
                make_eml("第一父邮件", "第一正文", nested=(child, "child.eml", "message/rfc822"))
            )
            second.write_bytes(
                make_eml("第二父邮件", "第二正文", nested=(child, "child.eml", "message/rfc822"))
            )

            output = root / "results"
            first_events = run_batch(first, output, batch_id="nested-parent-1")
            second_events = run_batch(second, output, batch_id="nested-parent-2")
            first_dir = Path(item_result(first_events)["mail_dir"])
            second_dir = Path(item_result(second_events)["mail_dir"])
            first_child = next((first_dir / "emails").glob("*/邮件.md"))
            second_child = next((second_dir / "emails").glob("*/邮件.md"))
            self.assertNotEqual(first_child, second_child)
            self.assertEqual(first_child.parent.parent, first_dir / "emails")
            self.assertEqual(second_child.parent.parent, second_dir / "emails")
            first_text = first_child.read_text(encoding="utf-8")
            second_text = second_child.read_text(encoding="utf-8")
            first_body = first_text.split("## 正文\n\n", 1)[1].split("\n## ", 1)[0]
            second_body = second_text.split("## 正文\n\n", 1)[1].split("\n## ", 1)[0]
            self.assertEqual(first_body, second_body)
            self.assertIn("共享子邮件", first_text)
            self.assertIn("emails/", (first_dir / "邮件.md").read_text(encoding="utf-8"))
            self.assertIn("emails/", (second_dir / "邮件.md").read_text(encoding="utf-8"))

    def test_multilevel_corrupt_child_keeps_raw_bytes_and_prefixed_error(self) -> None:
        middle = make_eml(
            "中层邮件",
            "中层正文",
            nested=(b"not an email", "broken.eml", "application/octet-stream"),
        )
        outer = make_eml(
            "多层父邮件",
            "父层正文",
            nested=(middle, "middle.eml", "message/rfc822"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outer.eml"
            source.write_bytes(outer)
            events = run_batch(source, root / "results", batch_id="nested-partial")

            item = item_result(events)
            self.assertEqual(item["type"], "item_partial_failed")
            self.assertEqual(item["nested_emails"], 2)
            self.assertEqual(item["attachments"], 1)
            self.assertEqual(len(item["errors"]), 1)
            self.assertEqual(item["errors"][0]["stage"], "attachments")
            self.assertEqual(item["errors"][0]["location"].split("/")[-1], "broken.eml")

            mail_dir = Path(item["mail_dir"])
            middle_dirs = list((mail_dir / "emails").glob("*"))
            self.assertEqual(len(middle_dirs), 1)
            self.assertEqual((middle_dirs[0] / "attachments" / "broken.eml").read_bytes(), b"not an email")
            self.assertTrue((middle_dirs[0] / "邮件.md").is_file())
            self.assertFalse((mail_dir / ".complete.json").exists())
            self.assertFalse((middle_dirs[0] / ".complete.json").exists())

    @unittest.skipUnless(CASE07.is_file(), "workspace case-07 MSG sample is unavailable")
    def test_real_case07_recurses_msg_msg_eml_and_preserves_each_layer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            events = run_batch(CASE07, output, batch_id="nested-case07")
            item = item_result(events)
            self.assertEqual(item["type"], "item_succeeded")
            self.assertEqual(item["nested_emails"], 4)
            self.assertEqual(item["attachments"], 6)
            mail_dir = Path(item["mail_dir"])
            markdowns = list(mail_dir.rglob("邮件.md"))
            self.assertEqual(len(markdowns), 5)
            self.assertIn("emails/", (mail_dir / "邮件.md").read_text(encoding="utf-8"))
            self.assertTrue(any(path.name == "smime.p7s" for path in mail_dir.rglob("smime.p7s")))
            self.assertTrue(any(path.name == "daticert.xml" for path in mail_dir.rglob("daticert.xml")))
            self.assertTrue(any(path.name.endswith(".pdf") for path in mail_dir.rglob("*.pdf")))
            self.assertTrue((mail_dir / ".complete.json").is_file())

    @unittest.skipUnless(CASE07.is_file(), "workspace case-07 MSG sample is unavailable")
    def test_eml_parent_detects_msg_child_from_content(self) -> None:
        outer = make_eml(
            "EML 外层",
            "外层正文",
            nested=(CASE07.read_bytes(), "forwarded-message", "application/octet-stream"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "outer.eml"
            source.write_bytes(outer)
            events = run_batch(source, root / "results", batch_id="nested-reverse")
            item = item_result(events)
            self.assertEqual(item["type"], "item_succeeded")
            self.assertEqual(item["nested_emails"], 5)
            self.assertEqual(item["attachments"], 6)
            mail_dir = Path(item["mail_dir"])
            self.assertEqual(len(list(mail_dir.rglob("邮件.md"))), 6)
            self.assertIn("EML 外层", (mail_dir / "邮件.md").read_text(encoding="utf-8"))
            self.assertIn("外层正文", (mail_dir / "邮件.md").read_text(encoding="utf-8"))
            self.assertTrue((mail_dir / ".complete.json").is_file())


if __name__ == "__main__":
    unittest.main()
