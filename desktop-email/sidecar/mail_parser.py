"""邮件正文和结果文件解析。

协议层在 :mod:`main` 中，格式解析和结果落盘集中在这个模块。嵌套邮件在
这里递归解析并写入 ``emails/``，无需改变 sidecar 的 JSON Lines 协议。递归
限制由批次传入，每封根邮件拥有独立的深度和嵌套附件字节预算。
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import quopri
import re
import shutil
import threading
import unicodedata
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import format_datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from bs4 import BeautifulSoup
from striprtf.striprtf import rtf_to_text as strip_rtf_to_text

try:
    from .limits import ExtractionLimits
except ImportError:  # ``python sidecar/mail_parser.py``
    from limits import ExtractionLimits


try:
    import extract_msg
except ImportError:  # pragma: no cover - exercised only in a Python without sidecar dependencies
    extract_msg = None  # type: ignore[assignment]


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{n}" for n in range(1, 10)),
    *(f"LPT{n}" for n in range(1, 10)),
}
_COMPLETE_MARKER_VERSION = 2
_MARKDOWN_LINK = re.compile(r"\]\(<([^>]+)>\)")
_FAILED_NESTED_LINK = re.compile(
    r"\]\(<(attachments/[^>]+)>\)\s+（邮件附件，(?:展开失败|未展开：)"
)
_PARTIAL_MARKER_NAME = ".partial.json"


class SidecarError(RuntimeError):
    """可供批次事件展示的解析错误。"""

    def __init__(self, message: str, *, stage: str = "parse", code: str = "parse_error"):
        super().__init__(message)
        self.stage = stage
        self.code = code


class CancellationRequested(SidecarError):
    """当前批次已取消，结果暂存目录必须被丢弃。"""

    def __init__(self) -> None:
        super().__init__("批次已取消", stage="cancel", code="cancelled")


@dataclass(frozen=True)
class Attachment:
    """一份已从邮件中解码出的附件原件。

    ``data`` 始终是传输编码解码后的原始字节。邮件附件（EML 的
    ``message/rfc822`` 或 MSG 的内嵌 MSG 对象）也保存原件，并以
    ``is_message``/``nested_format`` 标记交由结果落盘阶段递归展开。
    """

    filename: str
    data: bytes
    content_type: str = "application/octet-stream"
    content_id: str = ""
    is_inline: bool = False
    is_message: bool = False
    ordinal: int = 0
    nested_format: str = ""


@dataclass(frozen=True)
class AttachmentError:
    """单个附件读取或状态错误，不阻断同封邮件其他内容。"""

    filename: str
    stage: str
    code: str
    error: str
    ordinal: int = 0

    def as_dict(self, *, source_path: str = "") -> dict[str, Any]:
        return {
            "source_path": source_path,
            "stage": self.stage,
            "code": self.code,
            "error": self.error,
            "location": self.filename,
        }


@dataclass(frozen=True)
class ParsedEmail:
    source_name: str
    source_sha256: str
    subject: str
    sender: str
    recipients: str
    cc: str
    sent_at: str
    body: str
    attachments: tuple[Attachment, ...] = ()
    attachment_errors: tuple[AttachmentError, ...] = ()
    source_path: str = ""


@dataclass(frozen=True)
class WriteResult:
    """一封邮件的落盘结果。

    新批次层读取所有字段，以区分完整成功和部分失败。
    """

    mail_dir: Path
    skipped: bool
    status: str
    attachments_count: int
    nested_emails_count: int
    errors: list[dict[str, Any]]


def _decode_bytes(value: bytes | bytearray | memoryview, context: str) -> str:
    """以邮件常见编码兜底解码正文原始字节。"""

    data = bytes(value)
    for encoding in ("utf-8", "gb18030", "big5", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SidecarError(f"{context}无法解码", stage="decode", code="decode_error")


def _as_text(value: Any, context: str) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _decode_bytes(value, context)
    return str(value)


def html_to_text(value: str | bytes) -> str:
    try:
        soup = BeautifulSoup(_as_text(value, "HTML 正文"), "html.parser")
        for element in soup(["head", "script", "style", "noscript", "template"]):
            element.decompose()
        for element in soup.find_all("br"):
            element.replace_with("\n")
        for element in soup.find_all(
            [
                "address",
                "article",
                "aside",
                "blockquote",
                "div",
                "dl",
                "dt",
                "dd",
                "fieldset",
                "figcaption",
                "figure",
                "footer",
                "form",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "header",
                "hr",
                "li",
                "main",
                "nav",
                "ol",
                "p",
                "pre",
                "section",
                "table",
                "td",
                "th",
                "tr",
                "ul",
            ]
        ):
            element.insert_before("\n")
            element.insert_after("\n")
        value = soup.get_text()
    except Exception as exc:
        raise SidecarError(f"HTML 正文转换失败：{exc}", stage="body", code="html_error") from exc
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [line.rstrip() for line in value.split("\n")]
    return "\n".join(lines).strip()


def _normalise_body(value: str) -> str:
    # 只统一换行并去掉 MIME 部件首尾的空白；历史引用、签名和中间空行全部保留。
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _part_text(part: Any, context: str) -> str:
    try:
        content = part.get_content()
    except Exception:
        try:
            raw = part.get_payload(decode=True)
        except Exception as exc:
            raise SidecarError(f"读取{context}失败：{exc}", stage="body", code="body_read_error") from exc
        if raw is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return bytes(raw).decode(charset)
        except (LookupError, UnicodeDecodeError):
            return _decode_bytes(raw, context)
    return _as_text(content, context)


_BODY_CONTENT_TYPES = {"text/plain", "text/html", "text/rtf", "application/rtf", "text/richtext"}
_MESSAGE_CONTENT_TYPES = {
    "message/rfc822",
    "application/vnd.ms-outlook",
    "application/x-msg",
}
_MSG_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _content_type(part: Any) -> str:
    try:
        return (part.get_content_type() or "").lower()
    except Exception:
        return ""


def _is_message_part(part: Any) -> bool:
    content_type = _content_type(part)
    # A filename is only a weak hint.  MIME message parts are boundaries for
    # the body walker regardless of their filename; ordinary attachments
    # called ``something.eml``/``something.msg`` are classified after their
    # bytes have been inspected.
    return content_type in _MESSAGE_CONTENT_TYPES


def _detect_email_kind(raw: bytes) -> str | None:
    """根据内容判断附件是否为 EML 或 MSG。

    扩展名和 MIME 参数都可能被 Outlook 或转发程序写错，因此它们只在
    强信号（``message/rfc822``、内嵌 MSG 对象）下作为兜底。普通附件则
    只有在内容确实能由对应解析器打开时才会成为嵌套邮件。
    """

    data = bytes(raw)
    if data.startswith(_MSG_MAGIC):
        if extract_msg is None:
            return "msg"
        message: Any = None
        try:
            message = extract_msg.openMsg(data, attachmentsError=False, delayAttachments=True)
        except Exception:
            return None
        finally:
            if message is not None:
                try:
                    message.close()
                except Exception:
                    pass
        return "msg"

    # ``BytesParser`` accepts arbitrary text as a message when it contains a
    # colon. Require a real header/body boundary as well as a known mail
    # header so a text fragment is not promoted to a nested EML.
    if re.search(rb"\r?\n\r?\n", data) is None:
        return None
    try:
        message = BytesParser(policy=policy.default).parsebytes(data)
    except Exception:
        return None
    known_headers = {
        "subject",
        "from",
        "to",
        "cc",
        "date",
        "sender",
        "reply-to",
        "return-path",
        "message-id",
        "in-reply-to",
        "references",
        "received",
        "mime-version",
        "content-type",
        "content-disposition",
        "content-transfer-encoding",
    }
    header_names = {name.lower() for name in message.keys()}
    if not header_names.intersection(known_headers):
        return None
    identity_headers = {
        "subject",
        "from",
        "to",
        "cc",
        "date",
        "sender",
        "reply-to",
        "return-path",
        "message-id",
        "in-reply-to",
        "references",
        "received",
    }
    structure_headers = {
        "mime-version",
        "content-type",
        "content-disposition",
        "content-transfer-encoding",
    }
    identity_count = len(header_names.intersection(identity_headers))
    structure_count = len(header_names.intersection(structure_headers))
    if identity_count >= 2 or (identity_count >= 1 and structure_count >= 1):
        return "eml"
    return None


def _allows_content_email_detection(filename: str, content_type: str) -> bool:
    """判断普通附件是否允许由内容嗅探为邮件。"""

    content_type = (content_type or "").lower()
    if content_type in _MESSAGE_CONTENT_TYPES:
        return True
    if content_type.startswith("text/"):
        return False

    suffix = os.path.splitext(filename or "")[1].lower()
    if suffix in {".eml", ".msg"}:
        return True
    if not suffix:
        return True

    guessed_type, _ = mimetypes.guess_type(filename)
    return not guessed_type or guessed_type in _MESSAGE_CONTENT_TYPES


def _nested_format(
    data: bytes,
    *,
    filename: str,
    content_type: str,
    strong_message: bool = False,
) -> str | None:
    """返回附件的嵌套格式，或 ``None`` 表示普通附件。"""

    filename = filename or ""
    content_type = (content_type or "").lower()
    strong_message = strong_message or content_type in _MESSAGE_CONTENT_TYPES
    suffix = filename.lower()
    named_message = suffix.endswith((".eml", ".msg"))
    detected = (
        _detect_email_kind(data)
        if strong_message or named_message or _allows_content_email_detection(filename, content_type)
        else None
    )
    if detected:
        return detected
    # A mail-looking filename paired with a generic binary MIME type is still
    # useful for diagnosing a corrupt or truncated nested attachment.  It is
    # only a fallback: valid content and parser/type signals above always
    # decide the format first, and content without a mail filename is also
    # detected by _detect_email_kind.
    if content_type in {"", "application/octet-stream", "application/octetstream", "application/binary"}:
        if suffix.endswith(".msg"):
            return "msg"
        if suffix.endswith(".eml"):
            return "eml"
    if strong_message:
        # A declared message attachment must remain visible and become a
        # partial result if its bytes cannot be parsed.  The parser will emit
        # the concrete reason later; this hint prevents silently treating it
        # as an ordinary document.
        if content_type in {"application/vnd.ms-outlook", "application/x-msg"}:
            return "msg"
        return "eml"
    return None


def _iter_mime_parts(message: Any) -> Iterable[Any]:
    """递归遍历 MIME 部件，并把邮件附件作为边界停止展开。"""

    try:
        if _is_message_part(message):
            yield message
            return
        if message.is_multipart():
            for child in message.iter_parts():
                yield from _iter_mime_parts(child)
            return
        yield message
    except Exception as exc:
        raise SidecarError(f"读取 MIME 部件失败：{exc}", stage="body", code="mime_walk_error") from exc


def _iter_body_parts(message: Any) -> Iterable[Any]:
    for part in _iter_mime_parts(message):
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "attachment" or part.get_filename() or part.get("Content-ID") or _is_message_part(part):
            continue
        content_type = _content_type(part)
        if content_type in _BODY_CONTENT_TYPES:
            yield part


def _body_from_message(message: Any) -> str:
    plain: str | None = None
    html_body: str | None = None
    rtf_body: str | bytes | None = None
    rtf_encoding: str | None = None
    for index, part in enumerate(_iter_body_parts(message)):
        context = f"MIME 正文部件 #{index}"
        content_type = (part.get_content_type() or "").lower()
        if content_type in {"text/rtf", "application/rtf", "text/richtext"}:
            if rtf_body is None:
                # ``get_content()`` decodes a text part before we reach the
                # converter.  Keep the transfer-decoded bytes so UTF-8 RTF
                # literal text is not re-encoded through latin-1 later.
                try:
                    raw = part.get_payload(decode=True)
                except Exception:
                    raw = None
                rtf_body = bytes(raw) if raw is not None else _part_text(part, context)
                rtf_encoding = part.get_content_charset() or None
            continue
        value = _part_text(part, context)
        if content_type == "text/plain" and plain is None:
            plain = value
        elif content_type == "text/html" and html_body is None:
            html_body = value
    if plain is not None and _normalise_body(plain):
        return _normalise_body(plain)
    if html_body is not None and _normalise_body(html_body):
        return html_to_text(html_body)
    if rtf_body is not None:
        return rtf_to_text(rtf_body, encoding=rtf_encoding)
    return ""


def _attachment_filename(part: Any) -> str:
    try:
        return _as_text(part.get_filename(), "MIME 附件文件名").strip()
    except Exception:
        return ""


def _raw_message_attachment_payloads(raw: bytes) -> list[bytes]:
    """从原始 MIME 字节提取 message/rfc822 部件的完整传输载荷。

    ``email`` 在解析带传输编码的 ``message/rfc822`` 时会先尝试构造子
    ``Message``，原始编码和边界换行因此无法从对象上恢复。这里仍用
    标准邮件解析器读取 MIME 头，只在原始 multipart 边界上保留载荷字节，
    使 QP/base64 子邮件的 headers、正文和原始换行一并交给解码器。
    """

    payloads: list[bytes] = []

    def split_headers(value: bytes) -> tuple[bytes, bytes] | None:
        separator = re.search(rb"\r?\n\r?\n", value)
        if separator is None:
            return None
        return value[: separator.start()], value[separator.end() :]

    def parse_headers(value: bytes) -> Any | None:
        try:
            return BytesParser(policy=policy.default).parsebytes(value + b"\r\n\r\n")
        except Exception:
            return None

    def decode_payload(value: bytes, transfer_encoding: str) -> bytes:
        if transfer_encoding == "quoted-printable":
            return quopri.decodestring(value)
        if transfer_encoding == "base64":
            return base64.b64decode(value, validate=False)
        return value

    def walk(value: bytes) -> None:
        sections = split_headers(value)
        if sections is None:
            return
        header_bytes, body = sections
        message = parse_headers(header_bytes)
        if message is None:
            return
        content_type = _content_type(message)
        if content_type in _MESSAGE_CONTENT_TYPES:
            transfer_encoding = _as_text(
                message.get("Content-Transfer-Encoding"),
                "MIME 附件编码",
            ).strip().lower()
            payloads.append(decode_payload(body, transfer_encoding))
            return
        if not content_type.startswith("multipart/"):
            return
        boundary = message.get_param("boundary", header="Content-Type")
        if not boundary:
            return
        try:
            delimiter = b"--" + _as_text(boundary, "MIME boundary").encode("ascii")
        except (UnicodeEncodeError, SidecarError):
            return
        for section in body.split(delimiter)[1:]:
            if section.startswith(b"--"):
                break
            if section.startswith(b"\r\n"):
                section = section[2:]
            elif section.startswith(b"\n"):
                section = section[1:]
            walk(section)

    walk(bytes(raw))
    return payloads


def _attachment_bytes(part: Any) -> bytes:
    """取得 MIME 部件的传输编码解码后字节。"""

    content_type = _content_type(part)
    try:
        raw = part.get_payload(decode=True)
    except Exception as exc:
        raise SidecarError(f"读取 MIME 附件内容失败：{exc}", stage="attachments", code="attachment_read_error") from exc
    if raw is not None:
        return bytes(raw)

    payload = part.get_payload()
    if content_type in _MESSAGE_CONTENT_TYPES:
        if isinstance(payload, (list, tuple)):
            message_parts = [item for item in payload if hasattr(item, "as_bytes")]
            if message_parts:
                transfer_encoding = _as_text(part.get("Content-Transfer-Encoding"), "MIME 附件编码").strip().lower()
                if transfer_encoding in {"base64", "quoted-printable"} and len(message_parts) == 1:
                    # A transfer encoding on message/rfc822 wraps the whole
                    # child message. Decoding only child.get_payload() drops
                    # its headers and turns a valid nested mail into a body
                    # fragment. Serialize the complete parsed child before
                    # decoding so headers and body stay together.
                    encoded = message_parts[0].as_bytes(policy=policy.default)
                    compact = b"".join(encoded.splitlines())
                    if transfer_encoding == "base64":
                        return base64.b64decode(compact, validate=False)
                    return quopri.decodestring(encoded)
                return b"\n".join(item.as_bytes(policy=policy.default) for item in message_parts)
        if hasattr(payload, "as_bytes"):
            return payload.as_bytes(policy=policy.default)
    if payload is None:
        return b""
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    if isinstance(payload, str):
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.encode(charset)
        except (LookupError, UnicodeEncodeError):
            return payload.encode("utf-8", errors="replace")
    raise SidecarError(
        f"无法读取 MIME 附件内容类型：{type(payload).__name__}",
        stage="attachments",
        code="attachment_read_error",
    )


def _attachment_error(filename: str, exc: Exception, *, ordinal: int, code: str | None = None) -> AttachmentError:
    if isinstance(exc, SidecarError):
        return AttachmentError(filename, exc.stage, code or exc.code, str(exc), ordinal)
    return AttachmentError(filename, "attachments", code or "attachment_read_error", f"{type(exc).__name__}: {exc}", ordinal)


def _extract_eml_attachments(
    message: Any,
    *,
    raw: bytes | None = None,
) -> tuple[tuple[Attachment, ...], tuple[AttachmentError, ...]]:
    attachments: list[Attachment] = []
    errors: list[AttachmentError] = []
    raw_message_payloads = iter(_raw_message_attachment_payloads(raw)) if raw is not None else iter(())
    for ordinal, part in enumerate(_iter_mime_parts(message), start=1):
        disposition = (part.get_content_disposition() or "").lower()
        filename = _attachment_filename(part)
        content_type = _content_type(part) or "application/octet-stream"
        try:
            content_id = _as_text(part.get("Content-ID"), "MIME Content-ID").strip().strip("<>")
        except Exception:
            content_id = ""
        declared_message = _is_message_part(part)
        is_body = not part.is_multipart() and content_type in _BODY_CONTENT_TYPES
        is_attachment = declared_message or disposition == "attachment" or bool(filename) or bool(content_id) or not is_body
        if not is_attachment:
            continue

        # A message/rfc822 part is yielded as a single boundary by
        # _iter_mime_parts; its child body therefore cannot leak into the
        # parent Markdown body.
        display_name = filename
        if not display_name and content_id:
            display_name = content_id
            if not os.path.splitext(display_name)[1]:
                display_name += mimetypes.guess_extension(content_type) or ".bin"
        try:
            if declared_message and raw is not None:
                try:
                    data = next(raw_message_payloads)
                except StopIteration:
                    data = _attachment_bytes(part)
            else:
                data = _attachment_bytes(part)
        except Exception as exc:
            if declared_message and not display_name:
                display_name = f"embedded-{ordinal:03d}.eml"
            errors.append(_attachment_error(display_name, exc, ordinal=ordinal))
            continue
        nested_format = _nested_format(
            data,
            filename=display_name,
            content_type=content_type,
            strong_message=declared_message,
        )
        if nested_format and not display_name:
            display_name = f"embedded-{ordinal:03d}.{nested_format}"
        attachment = Attachment(
            filename=display_name,
            data=data,
            content_type=content_type,
            content_id=content_id,
            # Content-ID is the reliable signal for embedded images. Some
            # Outlook exports still label those parts as ``attachment``.
            is_inline=disposition == "inline" or bool(content_id),
            is_message=nested_format is not None,
            nested_format=nested_format or "",
            ordinal=ordinal,
        )
        attachments.append(attachment)
    return tuple(attachments), tuple(errors)


def _header(message: Any, name: str) -> str:
    try:
        value = message.get(name)
    except Exception as exc:
        raise SidecarError(f"读取邮件字段 {name} 失败：{exc}", stage="headers", code="header_read_error") from exc
    return _as_text(value, f"邮件字段 {name}").strip()


def _parse_eml_bytes(raw: bytes, *, source_name: str, source_path: str) -> ParsedEmail:
    """从字节解析 EML，供根邮件和 MIME 子邮件共用。"""

    if not raw:
        raise SidecarError("EML 文件为空", stage="parse", code="empty_eml")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise SidecarError(f"解析 EML 失败：{exc}", stage="parse", code="invalid_eml") from exc
    if not message.keys():
        raise SidecarError("EML 缺少邮件标题", stage="parse", code="invalid_eml")
    attachments, attachment_errors = _extract_eml_attachments(message, raw=raw)

    return ParsedEmail(
        source_name=source_name,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        subject=_header(message, "Subject"),
        sender=_header(message, "From"),
        recipients=_header(message, "To"),
        cc=_header(message, "Cc"),
        sent_at=_header(message, "Date"),
        body=_body_from_message(message),
        attachments=attachments,
        attachment_errors=attachment_errors,
        source_path=source_path,
    )


def parse_eml(source: Path) -> ParsedEmail:
    """读取一个 EML，提取字段、完整正文及已解码附件原件。"""

    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise SidecarError(f"读取 EML 失败：{source}：{exc}", stage="read", code="read_error") from exc
    return _parse_eml_bytes(raw, source_name=source.name, source_path=str(source))


def _msg_field(message: Any, name: str) -> str:
    try:
        value = getattr(message, name)
    except Exception as exc:
        raise SidecarError(f"读取 MSG 字段 {name} 失败：{exc}", stage="headers", code="header_read_error") from exc
    if isinstance(value, datetime):
        return format_datetime(value)
    return _as_text(value, f"MSG 字段 {name}").strip()


def _msg_body(message: Any) -> str:
    body_error: Exception | None = None
    try:
        body = _as_text(message.body, "MSG 纯文本正文")
    except Exception as exc:
        body = ""
        body_error = exc
    if _normalise_body(body):
        return _normalise_body(body)

    try:
        html_body = message.htmlBody
    except Exception as exc:
        html_body = None
        body_error = body_error or exc
    if html_body:
        try:
            converted = html_to_text(html_body)
            if converted:
                return converted
        except SidecarError as exc:
            body_error = body_error or exc

    try:
        rtf_body = message.rtfBody
    except Exception as exc:
        rtf_body = None
        body_error = body_error or exc
    if rtf_body:
        try:
            plain = _msg_rtf_body(message, rtf_body)
            if plain:
                return _normalise_body(plain)
        except Exception as exc:
            body_error = body_error or exc
        try:
            converted = rtf_to_text(rtf_body)
            if converted:
                return _normalise_body(converted)
        except SidecarError as exc:
            body_error = body_error or exc

    if body_error is not None:
        raise SidecarError(f"读取 MSG 正文失败：{body_error}", stage="body", code="body_read_error") from body_error
    return ""


def _msg_rtf_body(message: Any, rtf_body: bytes) -> str:
    try:
        from extract_msg.enums import DeencapType
    except (ImportError, AttributeError) as exc:  # pragma: no cover - extract_msg supplies this dependency
        raise RuntimeError("extract_msg 缺少 RTF 解封装类型") from exc
    value = message.deencapsulateBody(rtf_body, DeencapType.PLAIN)
    return _as_text(value, "MSG RTF 正文") if value else ""


def _msg_attachment_bytes(attachment: Any) -> bytes:
    """取得 extract-msg 附件的原始字节，包括内嵌 MSG。"""

    data = attachment.data
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    # EmbeddedMsgAttachment.data is an MSGFile.  exportBytes() is the
    # extract-msg supported way to materialise its internal storage as a
    # standalone MSG, and it also works for signed/message subclasses.
    if hasattr(data, "exportBytes"):
        return bytes(data.exportBytes())
    if hasattr(data, "export"):
        output = BytesIO()
        data.export(output)
        return output.getvalue()
    if data is None:
        raise SidecarError(
            "MSG 附件没有可保存的数据",
            stage="attachments",
            code="attachment_read_error",
        )
    raise SidecarError(
        f"无法读取 MSG 附件数据类型：{type(data).__name__}",
        stage="attachments",
        code="attachment_read_error",
    )


def _msg_attachment_name(attachment: Any) -> str:
    # Avoid extract-msg's randomFilename fallback: a digest-based name is
    # deterministic across retries and therefore traceable.
    for attr in ("name", "displayName", "longFilename", "shortFilename"):
        try:
            value = getattr(attachment, attr, None)
        except Exception:
            continue
        value = _as_text(value, f"MSG 附件 {attr}").strip()
        if value:
            return value
    return ""


def _msg_attachment_type(attachment: Any) -> Any:
    try:
        return attachment.type
    except Exception:
        return None


def _is_msg_embedded_attachment(attachment: Any) -> bool:
    attachment_type = _msg_attachment_type(attachment)
    try:
        if int(getattr(attachment_type, "value", attachment_type)) == 1:
            return True
    except (TypeError, ValueError):
        pass
    return type(attachment).__name__.lower().endswith("embeddedmsgattachment")


def _msg_attachment_content_type(attachment: Any) -> str:
    try:
        value = _as_text(attachment.mimetype, "MSG 附件 MIME 类型").strip().lower()
    except Exception:
        value = ""
    return value or "application/octet-stream"


def _extract_msg_attachments(message: Any) -> tuple[tuple[Attachment, ...], tuple[AttachmentError, ...]]:
    attachments: list[Attachment] = []
    errors: list[AttachmentError] = []
    try:
        source_attachments = list(message.attachments)
    except Exception as exc:
        return (), (_attachment_error("", exc, ordinal=1),)

    for ordinal, source_attachment in enumerate(source_attachments, start=1):
        filename = _msg_attachment_name(source_attachment)
        embedded_message = _is_msg_embedded_attachment(source_attachment)
        content_type = _msg_attachment_content_type(source_attachment)
        try:
            content_id = _as_text(getattr(source_attachment, "cid", ""), "MSG 附件 Content-ID").strip().strip("<>")
        except Exception:
            content_id = ""
        try:
            data = _msg_attachment_bytes(source_attachment)
        except Exception as exc:
            if embedded_message and not filename:
                filename = f"embedded-{ordinal:03d}.msg"
            errors.append(_attachment_error(filename, exc, ordinal=ordinal))
            continue
        nested_format = _nested_format(
            data,
            filename=filename,
            content_type=content_type,
            strong_message=embedded_message or content_type in _MESSAGE_CONTENT_TYPES,
        )
        if nested_format and not filename:
            filename = f"embedded-{ordinal:03d}.{nested_format}"
        attachment = Attachment(
            filename=filename,
            data=data,
            content_type=content_type,
            content_id=content_id,
            is_inline=bool(content_id),
            is_message=nested_format is not None,
            nested_format=nested_format or "",
            ordinal=ordinal,
        )
        attachments.append(attachment)
    return tuple(attachments), tuple(errors)


def _parsed_msg_message(message: Any, raw: bytes, *, source_name: str, source_path: str) -> ParsedEmail:
    attachments, attachment_errors = _extract_msg_attachments(message)
    return ParsedEmail(
        source_name=source_name,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        subject=_msg_field(message, "subject"),
        sender=_msg_field(message, "sender"),
        recipients=_msg_field(message, "to"),
        cc=_msg_field(message, "cc"),
        sent_at=_msg_field(message, "date"),
        body=_msg_body(message),
        attachments=attachments,
        attachment_errors=attachment_errors,
        source_path=source_path,
    )


def _parse_msg_bytes(raw: bytes, *, source_name: str, source_path: str) -> ParsedEmail:
    """从字节解析 MSG，供 Outlook 内嵌 MSG 递归使用。"""

    if extract_msg is None:
        raise SidecarError(
            "当前 Python sidecar 未安装 extract-msg，无法解析 MSG",
            stage="dependency",
            code="missing_dependency",
        )
    if not raw:
        raise SidecarError("MSG 文件为空", stage="parse", code="empty_msg")

    message: Any = None
    try:
        # extract-msg supports bytes as its MSGFile source.  This keeps a child
        # in memory and avoids inventing a temporary path that could escape the
        # selected output directory.
        message = extract_msg.openMsg(raw, attachmentsError=False, delayAttachments=True)
    except Exception as exc:
        raise SidecarError(f"解析 MSG 失败：{exc}", stage="parse", code="invalid_msg") from exc
    try:
        return _parsed_msg_message(message, raw, source_name=source_name, source_path=source_path)
    finally:
        try:
            message.close()
        except Exception:
            pass


def parse_msg(source: Path) -> ParsedEmail:
    """读取一个 Outlook MSG，提取字段、完整正文及附件原件。"""

    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise SidecarError(f"读取 MSG 失败：{source}：{exc}", stage="read", code="read_error") from exc
    return _parse_msg_bytes(raw, source_name=source.name, source_path=str(source))


def parse_email(source: Path) -> ParsedEmail:
    """根据扩展名读取 EML 或 MSG。"""

    suffix = source.suffix.lower()
    if suffix == ".eml":
        return parse_eml(source)
    if suffix == ".msg":
        return parse_msg(source)
    raise SidecarError(
        f"不支持的邮件格式：{source.suffix or '无扩展名'}",
        stage="input",
        code="unsupported_format",
    )


def _rtf_input_encoding(raw: bytes, declared: str | None = None) -> str:
    match = re.search(rb"\\ansicpg(\d+)", raw[:4096], re.IGNORECASE)
    if match:
        candidate = f"cp{int(match.group(1))}"
        try:
            import codecs

            codecs.lookup(candidate)
            return candidate
        except (LookupError, ValueError):
            pass
    if declared:
        try:
            import codecs

            codecs.lookup(declared)
            return declared
        except (LookupError, ValueError):
            pass
    return "cp1252"


def rtf_to_text(value: str | bytes, *, encoding: str | None = None) -> str:
    '''将 RTF 正文转换为纯文本，并兼容 Outlook 的 HTML 封装 RTF。'''

    if isinstance(value, str):
        # Keep already-decoded Unicode text intact.  The UTF-8 bytes here are
        # only for RTFDE/header inspection; striprtf receives the original
        # string below, so literal non-Latin characters cannot be lost.
        raw = value.encode("utf-8")
        source = value
    else:
        raw = bytes(value)
        source = None
    if not raw:
        return ""

    # RTFDE 是 extract-msg 的直接依赖，优先使用它处理 Outlook 的
    # fromhtml1/fromtext 封装；普通 RTF 由 striprtf 处理。
    try:
        from RTFDE import DeEncapsulator

        encapsulated = DeEncapsulator(raw)
        encapsulated.deencapsulate()
        if getattr(encapsulated, "content_type", "") == "html":
            return html_to_text(_as_text(encapsulated.html, "RTF HTML 正文"))
        if getattr(encapsulated, "content_type", "") == "text":
            return _normalise_body(_as_text(encapsulated.text, "RTF 文本正文"))
    except Exception:
        # 普通 RTF 没有封装类型，或少量损坏控制词无法由 RTFDE 读取时，
        # 交给 striprtf 处理。
        pass

    input_encoding = _rtf_input_encoding(raw, encoding)
    if source is None:
        source = raw.decode(input_encoding, errors="replace")
    try:
        converted = strip_rtf_to_text(source, encoding=input_encoding, errors="replace")
    except Exception as exc:
        raise SidecarError(f"RTF 正文转换失败：{exc}", stage="body", code="rtf_error") from exc
    if re.search(rb"\\fromhtml1\b", raw, re.IGNORECASE):
        return html_to_text(converted)
    return _normalise_body(converted)


def _truncate_utf8(value: str, limit: int) -> str:
    if len(value.encode("utf-8")) <= limit:
        return value
    result: list[str] = []
    size = 0
    for character in value:
        character_size = len(character.encode("utf-8"))
        if size + character_size > limit:
            break
        result.append(character)
        size += character_size
    return "".join(result)


def safe_slug(value: str, *, fallback: str = "无标题邮件", limit: int = 80) -> str:
    """生成跨平台目录名，避免保留名、分隔符和尾部点空格。"""

    value = unicodedata.normalize("NFKC", value or "")
    value = _CONTROL_CHARS.sub("_", value)
    value = _INVALID_NAME_CHARS.sub("_", value)
    # These characters have special meaning in a Markdown relative link.
    # Replacing them keeps child-mail links navigable across viewers.
    value = re.sub(r"[\[\]()#%]", "_", value)
    value = _WHITESPACE.sub(" ", value).strip(" .")
    if not value:
        value = fallback
    stem, extension = os.path.splitext(value)
    if stem.upper() in _WINDOWS_RESERVED:
        value = f"_{stem}{extension}"
    return _truncate_utf8(value, limit).rstrip(" .") or fallback


def _safe_attachment_filename(
    value: str,
    data: bytes,
    content_type: str,
    ordinal: int,
    *,
    limit: int = 120,
) -> str:
    """清理附件名并保留可追溯的扩展名和序号。"""

    value = unicodedata.normalize("NFKC", value or "")
    value = _CONTROL_CHARS.sub("_", value)
    value = _INVALID_NAME_CHARS.sub("_", value)
    # These characters are valid on some filesystems but make a Markdown
    # destination ambiguous. Replacing them keeps links portable.
    value = re.sub(r"[\[\]()#%]", "_", value)
    value = _WHITESPACE.sub(" ", value).strip(" .")

    if value:
        stem, extension = os.path.splitext(value)
        stem = stem.rstrip(" .")
        extension = extension.rstrip(" .")
        if stem.upper() in _WINDOWS_RESERVED:
            stem = f"_{stem}"
        value = f"{stem}{extension}" if stem else extension

    if not value:
        extension = mimetypes.guess_extension(content_type or "")
        if not extension and content_type in {"application/vnd.ms-outlook", "application/x-msg"}:
            extension = ".msg"
        extension = extension or ".bin"
        digest = hashlib.sha256(data).hexdigest()[:8]
        value = f"attachment-{ordinal:03d}-{digest}{extension}"

    stem, extension = os.path.splitext(value)
    if len(value.encode("utf-8")) > limit:
        extension = _truncate_utf8(extension, max(1, limit // 3))
        stem = _truncate_utf8(stem, max(1, limit - len(extension.encode("utf-8"))))
        value = f"{stem}{extension}".rstrip(" .")
    return value or f"attachment-{ordinal:03d}.bin"


def _unique_attachment_filename(name: str, used: set[str]) -> str:
    candidate = name
    stem, extension = os.path.splitext(name)
    index = 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({index}){extension}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _attachment_links(parsed: ParsedEmail) -> list[tuple[str, str]]:
    used: set[str] = set()
    links: list[tuple[str, str]] = []
    for attachment in parsed.attachments:
        name = _safe_attachment_filename(
            attachment.filename,
            attachment.data,
            attachment.content_type,
            attachment.ordinal,
        )
        links.append((
            _unique_attachment_filename(name, used),
            "",
        ))
    return links


def _markdown_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("`", "'").strip()


def render_markdown(
    parsed: ParsedEmail,
    attachment_links: Iterable[tuple[str, str]] | None = None,
    nested_links: Iterable[tuple[str, str]] | None = None,
) -> str:
    title = _markdown_text(parsed.subject) or "无标题邮件"
    lines = [f"# {title}", "", f"- **文件名**: {_markdown_text(parsed.source_name)}"]
    fields = (
        ("主题", parsed.subject),
        ("发件人", parsed.sender),
        ("收件人", parsed.recipients),
        ("抄送", parsed.cc),
        ("发送时间", parsed.sent_at),
    )
    for label, value in fields:
        if value:
            lines.append(f"- **{label}**: {_markdown_text(value)}")
    lines.extend(["", "---", "", "## 正文", "", parsed.body or "（无文本正文）"])
    links = list(attachment_links) if attachment_links is not None else _attachment_links(parsed)
    if links:
        lines.extend(["", "## 附件", ""])
        for name, note in links:
            target = f"attachments/{name}"
            suffix = f" {note}" if note else ""
            lines.append(f"- [{_markdown_text(name)}](<{target}>)" + suffix)
    children = list(nested_links) if nested_links is not None else []
    if children:
        lines.extend(["", "## 子邮件", ""])
        for name, target in children:
            lines.append(f"- [{_markdown_text(name)}](<{target}>)")
    lines.append("")
    return "\n".join(lines)


def _safe_result_path(root: Path, relative: str) -> Path | None:
    """将 marker/Markdown 中的相对路径限制在邮件目录内。"""

    if not isinstance(relative, str) or not relative:
        return None
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    target = root.joinpath(*path.parts)
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_manifest(root: Path, relative_paths: Iterable[str]) -> list[dict[str, Any]]:
    """记录本邮件目录直接拥有的正文和附件字节。"""

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for relative in relative_paths:
        if relative in seen:
            continue
        seen.add(relative)
        target = _safe_result_path(root, relative)
        if target is None or not target.is_file() or target.is_symlink():
            raise OSError(f"结果文件不存在或不是普通文件：{relative}")
        entries.append(
            {
                "path": relative,
                "size": target.stat().st_size,
                "sha256": _sha256_file(target),
            }
        )
    return entries


def _failed_nested_fallbacks(markdown: Path) -> set[str]:
    """取得旧 Markdown 中明确标记为未展开邮件的原件名。"""

    try:
        text = markdown.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()
    return {
        match.group(1).split("/", 1)[1]
        for match in _FAILED_NESTED_LINK.finditer(text)
    }


def _fallback_digests(mail_dir: Path) -> dict[str, dict[str, Any]]:
    """读取 partial marker 中记录的未展开原件摘要。"""

    try:
        payload = json.loads((mail_dir / _PARTIAL_MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("fallbacks"), list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for value in payload["fallbacks"]:
        if not isinstance(value, dict):
            continue
        path = value.get("path")
        if isinstance(path, str) and path.startswith("attachments/"):
            result[path.removeprefix("attachments/")] = value
    return result


def _artifact_matches(path: Path, artifact: dict[str, Any]) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    size = artifact.get("size")
    digest = artifact.get("sha256")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return False
    if not isinstance(digest, str) or len(digest) != 64:
        return False
    try:
        return path.stat().st_size == size and _sha256_file(path) == digest
    except OSError:
        return False


def _complete_marker(
    parsed: ParsedEmail,
    *,
    attachments_count: int,
    nested_emails_count: int,
    artifacts: Iterable[dict[str, Any]] = (),
    children: Iterable[str] = (),
    child_sources: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "version": _COMPLETE_MARKER_VERSION,
        "source_sha256": parsed.source_sha256,
        "status": "complete",
        "attachments": attachments_count,
        "nested_emails": nested_emails_count,
        "artifacts": list(artifacts),
        "children": list(children),
        "child_sources": list(child_sources),
    }


def _marker_payload(mail_dir: Path) -> dict[str, Any] | None:
    marker = mail_dir / ".complete.json"
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _tree_has_symlink(root: Path) -> bool:
    """不跟随结果树中的用户符号链接。"""

    if root.is_symlink():
        return True
    if not root.is_dir():
        return False
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        if any((Path(current) / name).is_symlink() for name in directories):
            return True
        if any((Path(current) / name).is_symlink() for name in files):
            return True
    return False


def _result_is_owned(mail_dir: Path, source_sha256: str) -> bool:
    """只有带有本应用 marker 的安全目录才可被重试接管。"""

    if not mail_dir.is_dir() or mail_dir.is_symlink() or _tree_has_symlink(mail_dir):
        return False
    for name in ("邮件.md", ".complete.json", _PARTIAL_MARKER_NAME):
        path = mail_dir / name
        if path.exists() and not path.is_file():
            return False
    for name in ("attachments", "emails"):
        path = mail_dir / name
        if path.exists() and not path.is_dir():
            return False
    complete = _marker_payload(mail_dir)
    partial_path = mail_dir / _PARTIAL_MARKER_NAME
    try:
        partial = json.loads(partial_path.read_text(encoding="utf-8")) if partial_path.is_file() else None
    except (OSError, ValueError, TypeError):
        partial = None
    if complete is not None:
        return (
            complete.get("version") == _COMPLETE_MARKER_VERSION
            and complete.get("source_sha256") == source_sha256
        )
    return (
        isinstance(partial, dict)
        and partial.get("version") == _COMPLETE_MARKER_VERSION
        and partial.get("source_sha256") == source_sha256
        and partial.get("status") == "partial"
    )


def _marker_count(payload: dict[str, Any], field: str) -> int | None:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _is_complete(mail_dir: Path, source_sha256: str, _ancestors: set[Path] | None = None) -> bool:
    """验证应用生成的完整结果，而不是只检查目录和 marker 是否存在。"""

    if not mail_dir.is_dir() or mail_dir.is_symlink():
        return False
    try:
        resolved_dir = mail_dir.resolve()
    except (OSError, RuntimeError):
        return False
    ancestors = set(_ancestors or ())
    if resolved_dir in ancestors:
        return False
    ancestors.add(resolved_dir)

    payload = _marker_payload(mail_dir)
    if payload is None:
        return False
    partial_marker = mail_dir / _PARTIAL_MARKER_NAME
    if partial_marker.exists() or partial_marker.is_symlink():
        return False
    if payload.get("version") != _COMPLETE_MARKER_VERSION:
        return False
    if payload.get("status") != "complete" or payload.get("source_sha256") != source_sha256:
        return False
    attachment_count = _marker_count(payload, "attachments")
    nested_count = _marker_count(payload, "nested_emails")
    if attachment_count is None or nested_count is None:
        return False

    markdown = mail_dir / "邮件.md"
    attachments_dir = mail_dir / "attachments"
    emails_dir = mail_dir / "emails"
    if (
        not markdown.is_file()
        or markdown.is_symlink()
        or not attachments_dir.is_dir()
        or attachments_dir.is_symlink()
        or not emails_dir.is_dir()
        or emails_dir.is_symlink()
    ):
        return False
    try:
        markdown_text = markdown.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False

    attachment_links: list[str] = []
    child_links: list[tuple[str, Path]] = []
    for match in _MARKDOWN_LINK.finditer(markdown_text):
        relative = match.group(1)
        target = _safe_result_path(mail_dir, relative)
        if target is None:
            continue
        parts = PurePosixPath(relative).parts
        if parts and parts[0] == "attachments":
            if len(parts) != 2 or target.parent != attachments_dir or not target.is_file() or target.is_symlink():
                return False
            attachment_links.append(relative)
        elif parts and parts[0] == "emails":
            if len(parts) != 3 or parts[2] != "邮件.md" or target.parent.parent != emails_dir:
                return False
            child_dir = target.parent
            if not child_dir.is_dir() or child_dir.is_symlink():
                return False
            child_links.append((relative, child_dir))

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    expected_artifacts = {"邮件.md", *attachment_links}
    actual_artifacts: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return False
        relative = artifact.get("path")
        if not isinstance(relative, str) or relative in actual_artifacts:
            return False
        target = _safe_result_path(mail_dir, relative)
        size = artifact.get("size")
        digest = artifact.get("sha256")
        if (
            target is None
            or not target.is_file()
            or target.is_symlink()
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            return False
        try:
            if target.stat().st_size != size or _sha256_file(target) != digest:
                return False
        except OSError:
            return False
        actual_artifacts.add(relative)
    if actual_artifacts != expected_artifacts:
        return False

    children = payload.get("children")
    if not isinstance(children, list) or children != [child_dir.name for _, child_dir in child_links]:
        return False
    child_sources = payload.get("child_sources")
    if not isinstance(child_sources, list) or len(child_sources) != len(child_links):
        return False
    if any(not isinstance(source, str) or not source for source in child_sources):
        return False

    actual_attachments = len(attachment_links)
    actual_nested = 0
    for index, (_, child_dir) in enumerate(child_links):
        child_payload = _marker_payload(child_dir)
        if child_payload is None:
            return False
        child_source = child_payload.get("source_sha256")
        if (
            not isinstance(child_source, str)
            or child_source != child_sources[index]
            or not _is_complete(child_dir, child_source, ancestors)
        ):
            return False
        child_attachments, child_nested = _marker_counts(child_dir)
        actual_attachments += child_attachments
        actual_nested += 1 + child_nested
    return actual_attachments == attachment_count and actual_nested == nested_count


def _repairable_unmarked_result(mail_dir: Path) -> bool:
    """判断父结果 marker 指向的缺 marker 子目录是否仍像应用结果。"""

    if not mail_dir.is_dir() or mail_dir.is_symlink() or _tree_has_symlink(mail_dir):
        return False
    for name in ("邮件.md", ".complete.json", _PARTIAL_MARKER_NAME):
        path = mail_dir / name
        if path.exists() and not path.is_file():
            return False
    for name in ("attachments", "emails"):
        path = mail_dir / name
        if path.exists() and not path.is_dir():
            return False
    return any(
        (mail_dir / name).is_file() or (mail_dir / name).is_dir()
        for name in ("邮件.md", "attachments", "emails")
    )


def _existing_child_sources(mail_dir: Path, source_sha256: str) -> set[str]:
    """读取旧结果 marker 中的子邮件归属，供缺 marker 修复使用。"""

    for marker_path in (mail_dir / ".complete.json", mail_dir / _PARTIAL_MARKER_NAME):
        try:
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("source_sha256") != source_sha256:
            continue
        values = payload.get("child_sources")
        if isinstance(values, list) and all(isinstance(value, str) and value for value in values):
            return set(values)
    return set()


def _allocate_mail_dir(
    output_root: Path,
    parsed: ParsedEmail,
    *,
    allow_unmarked_repair: bool = False,
) -> tuple[Path, bool]:
    base = f"{safe_slug(parsed.subject)}-{parsed.source_sha256[:12]}"
    candidate = output_root / base
    if _is_complete(candidate, parsed.source_sha256):
        return candidate, True
    if not candidate.exists() and not candidate.is_symlink():
        return candidate, False
    # 同一内容的失败/损坏结果沿用稳定目录并增量修复，避免每次 retry 都产生新目录。
    if _result_is_owned(candidate, parsed.source_sha256):
        return candidate, False
    if allow_unmarked_repair and _repairable_unmarked_result(candidate):
        return candidate, False
    # 同名用户目录/文件没有归属记录，不能被覆盖或当作旧结果合并。
    index = 1
    while True:
        suffix = "-retry" if index == 1 else f"-retry-{index}"
        alternative = output_root / f"{base}{suffix}"
        if not alternative.exists() and not alternative.is_symlink():
            return alternative, False
        if alternative.is_dir() and not alternative.is_symlink():
            if _is_complete(alternative, parsed.source_sha256):
                return alternative, True
            if _result_is_owned(alternative, parsed.source_sha256):
                return alternative, False
        index += 1


def _write_attachment(attachments_dir: Path, attachment: Attachment, filename: str) -> None:
    target = attachments_dir / filename
    attachments_root = attachments_dir.resolve()
    try:
        if target.resolve().parent != attachments_root:
            raise OSError("附件路径超出邮件附件目录")
        target.write_bytes(attachment.data)
    except OSError:
        raise


def _marker_counts(mail_dir: Path) -> tuple[int, int]:
    try:
        marker = json.loads((mail_dir / ".complete.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, 0
    try:
        attachments = int(marker.get("attachments", 0) or 0)
    except (TypeError, ValueError):
        attachments = 0
    try:
        nested_emails = int(marker.get("nested_emails", 0) or 0)
    except (TypeError, ValueError):
        nested_emails = 0
    return attachments, nested_emails


def _prepare_result_stage(partial: Path, mail_dir: Path, source_sha256: str) -> None:
    """复制旧结果到暂存目录，保留其中的有效及用户附加文件。"""

    if mail_dir.is_dir() and not mail_dir.is_symlink():
        if _tree_has_symlink(mail_dir):
            raise OSError(f"结果目录包含不安全的符号链接：{mail_dir}")
        shutil.copytree(mail_dir, partial)
    else:
        partial.mkdir(parents=True)
    marker = partial / ".complete.json"
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    try:
        (partial / _PARTIAL_MARKER_NAME).unlink()
    except FileNotFoundError:
        pass
    (partial / "attachments").mkdir(exist_ok=True)
    (partial / "emails").mkdir(exist_ok=True)
    (partial / _PARTIAL_MARKER_NAME).write_text(
        json.dumps(
            {
                "version": _COMPLETE_MARKER_VERSION,
                "source_sha256": source_sha256,
                "status": "partial",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _merge_staged_tree(
    partial: Path,
    mail_dir: Path,
    *,
    remove_relative_paths: Iterable[str] = (),
) -> None:
    """把暂存结果合并回稳定目录，保留未由应用管理的旧文件。"""

    if mail_dir.is_symlink() or _tree_has_symlink(mail_dir):
        raise OSError(f"结果目录包含不安全的符号链接：{mail_dir}")
    if mail_dir.exists() and not mail_dir.is_dir():
        raise OSError(f"结果目录不是目录：{mail_dir}")
    mail_dir.mkdir(parents=True, exist_ok=True)
    for relative in remove_relative_paths:
        target = _safe_result_path(mail_dir, relative)
        if target is None or target.parent != mail_dir / "attachments":
            raise OSError(f"要清理的结果路径无效：{relative}")
        if target.is_symlink():
            raise OSError(f"要清理的结果路径包含符号链接：{target}")
        try:
            target.unlink()
        except FileNotFoundError:
            pass

    def merge(source: Path, target: Path) -> None:
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise OSError(f"结果路径冲突或包含符号链接：{target}")
        target.mkdir(parents=True, exist_ok=True)
        marker_source = source / ".complete.json"
        for entry in source.iterdir():
            if entry.name == ".complete.json":
                continue
            destination = target / entry.name
            if destination.is_symlink():
                raise OSError(f"结果路径包含符号链接：{destination}")
            if entry.is_dir() and not entry.is_symlink():
                merge(entry, destination)
            else:
                os.replace(entry, destination)
        if marker_source.is_file():
            os.replace(marker_source, target / ".complete.json")

    has_complete_marker = (partial / ".complete.json").is_file()
    merge(partial, mail_dir)
    if has_complete_marker:
        try:
            (mail_dir / _PARTIAL_MARKER_NAME).unlink()
        except FileNotFoundError:
            pass
    else:
        # 没有新 marker 表示本次结果仍为部分失败，不能留下旧的完整标记。
        try:
            (mail_dir / ".complete.json").unlink()
        except FileNotFoundError:
            pass
    shutil.rmtree(partial)


def _result_error(
    parsed: ParsedEmail,
    *,
    location: str,
    stage: str,
    code: str,
    error: str,
) -> dict[str, Any]:
    return {
        "source_path": parsed.source_path,
        "stage": stage,
        "code": code,
        "error": error,
        "location": location or "root",
    }


def _prefix_error_location(error: dict[str, Any], prefix: str) -> dict[str, Any]:
    """把子邮件的局部错误位置映射到根邮件 Markdown 的层级。"""

    value = dict(error)
    location = value.get("location")
    if not isinstance(location, str) or not location:
        location = "root"
    value["location"] = f"{prefix}/{location}" if location != "root" else prefix
    return value


@dataclass
class _ExtractionBudget:
    """本次根邮件递归展开可消耗的嵌套附件字节数。"""

    remaining: int

    def consume(self, size: int) -> bool:
        if size > self.remaining:
            return False
        self.remaining -= size
        return True


def _nested_limit_error(
    parsed: ParsedEmail,
    attachment: Attachment,
    *,
    depth: int,
    limits: ExtractionLimits,
    budget: _ExtractionBudget,
) -> dict[str, Any] | None:
    location = attachment.filename or f"embedded-{attachment.ordinal:03d}"
    if depth >= limits.max_depth:
        return _result_error(
            parsed,
            location=location,
            stage="limits",
            code="max_depth_exceeded",
            error=(
                f"达到最大嵌套深度 {limits.max_depth}（当前邮件深度 {depth}）；"
                "邮件附件未展开，已保留原件"
            ),
        )
    size = len(attachment.data)
    if not budget.consume(size):
        return _result_error(
            parsed,
            location=location,
            stage="limits",
            code="max_extract_bytes_exceeded",
            error=(
                f"展开邮件附件需要 {size} 字节，超过剩余提取预算 {budget.remaining} 字节；"
                "邮件附件未展开，已保留原件"
            ),
        )
    return None


def _coerce_limits(value: ExtractionLimits | dict[str, Any] | None) -> ExtractionLimits:
    if value is None:
        return ExtractionLimits()
    if isinstance(value, ExtractionLimits):
        return value
    return ExtractionLimits.from_value(value)


def _parse_nested_attachment(attachment: Attachment, parent: ParsedEmail) -> ParsedEmail:
    """解析一份已被识别为邮件的附件。"""

    kind = attachment.nested_format or _detect_email_kind(attachment.data)
    if not kind:
        content_type = (attachment.content_type or "").lower()
        if content_type in {"application/vnd.ms-outlook", "application/x-msg"}:
            kind = "msg"
        elif content_type == "message/rfc822":
            kind = "eml"
        elif (attachment.filename or "").lower().endswith(".msg"):
            kind = "msg"
        elif (attachment.filename or "").lower().endswith(".eml"):
            kind = "eml"
    if kind not in {"eml", "msg"}:
        raise SidecarError(
            "邮件附件内容无法识别为 EML 或 MSG",
            stage="parse",
            code="nested_email_unrecognized",
        )

    source_name = attachment.filename or f"embedded-{attachment.ordinal:03d}.{kind}"
    if kind == "eml":
        return _parse_eml_bytes(
            attachment.data,
            source_name=source_name,
            # Keep the root source path for batch-level error grouping.  The
            # parent/child location is added by _write_tree below.
            source_path=parent.source_path,
        )
    return _parse_msg_bytes(
        attachment.data,
        source_name=source_name,
        source_path=parent.source_path,
    )


def _nested_parse_error(parsed: ParsedEmail, attachment: Attachment, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, SidecarError):
        detail = str(exc)
    else:
        detail = f"{type(exc).__name__}: {exc}"
    return _result_error(
        parsed,
        location=attachment.filename or f"embedded-{attachment.ordinal:03d}",
        # The failure is reported against the parent attachment boundary so
        # the batch UI can distinguish it from a malformed root mail.
        stage="attachments",
        code="nested_email_parse_error",
        error=f"展开邮件附件失败：{detail}",
    )


def _check_cancel(cancel_event: threading.Event | None) -> None:
    """在可安全回滚的落盘边界检查批次取消请求。"""

    if cancel_event is not None and cancel_event.is_set():
        raise CancellationRequested()


def _write_failed_nested_as_attachment(
    partial: Path,
    parsed: ParsedEmail,
    attachment: Attachment,
    used_names: set[str],
    *,
    reason: str,
    preserved_digests: dict[str, dict[str, Any]] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """展开失败时保留原件，避免未读子邮件静默丢失。"""

    filename = _safe_attachment_filename(
        attachment.filename,
        attachment.data,
        attachment.content_type,
        attachment.ordinal,
    )
    filename = _unique_attachment_filename(filename, used_names)
    expected = preserved_digests.get(filename) if preserved_digests else None
    existing = partial / "attachments" / filename
    if expected is not None and existing.is_file() and not existing.is_symlink() and not _artifact_matches(existing, expected):
        # 旧的 fallback 已被用户修改时，重试不能用源邮件原件覆盖用户内容。
        return filename, None
    try:
        _write_attachment(partial / "attachments", attachment, filename)
    except OSError as exc:
        return None, _result_error(
            parsed,
            location=attachment.filename or filename,
            stage="write",
            code="nested_email_write_error",
            error=f"邮件附件展开失败且原件保存失败：{reason}；{exc}",
        )
    return filename, None


def _write_tree(
    parsed: ParsedEmail,
    output_root: Path,
    *,
    limits: ExtractionLimits | dict[str, Any] | None = None,
    depth: int = 0,
    budget: _ExtractionBudget | None = None,
    allow_unmarked_repair: bool = False,
    cancel_event: threading.Event | None = None,
) -> WriteResult:
    """递归写入一封邮件及其所有子邮件。"""

    limits = _coerce_limits(limits)
    budget = budget or _ExtractionBudget(limits.max_extract_bytes)

    try:
        output_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SidecarError(f"创建输出目录失败：{output_root}：{exc}", stage="write", code="output_error") from exc

    mail_dir, skipped = _allocate_mail_dir(
        output_root,
        parsed,
        allow_unmarked_repair=allow_unmarked_repair,
    )
    if skipped:
        attachments_count, nested_emails_count = _marker_counts(mail_dir)
        return WriteResult(
            mail_dir=mail_dir,
            skipped=True,
            status="skipped",
            attachments_count=attachments_count,
            nested_emails_count=nested_emails_count,
            errors=[],
        )

    partial = output_root / f".{mail_dir.name}.partial-{uuid.uuid4().hex}"
    attachment_links: list[tuple[str, str]] = []
    nested_links: list[tuple[str, str]] = []
    nested_sources: list[str] = []
    errors: list[dict[str, Any]] = [
        error.as_dict(source_path=parsed.source_path) for error in parsed.attachment_errors
    ]
    used_names: set[str] = set()
    saved_attachments = 0
    nested_emails = 0
    previous_failed_fallbacks: set[str] = set()
    previous_fallback_digests: dict[str, dict[str, Any]] = _fallback_digests(mail_dir)
    previous_child_sources = _existing_child_sources(mail_dir, parsed.source_sha256)
    try:
        _check_cancel(cancel_event)
        _prepare_result_stage(partial, mail_dir, parsed.source_sha256)
        previous_failed_fallbacks = _failed_nested_fallbacks(partial / "邮件.md")
        previous_failed_fallbacks.update(previous_fallback_digests)
        for attachment in parsed.attachments:
            _check_cancel(cancel_event)
            is_nested = attachment.is_message
            if is_nested:
                # Count the child occurrence even if it is malformed; callers
                # need to know that an input contained a failed child mail.
                nested_emails += 1
                limit_error = _nested_limit_error(
                    parsed,
                    attachment,
                    depth=depth,
                    limits=limits,
                    budget=budget,
                )
                if limit_error is not None:
                    fallback_name, fallback_error = _write_failed_nested_as_attachment(
                        partial,
                        parsed,
                        attachment,
                        used_names,
                        reason=limit_error["error"],
                        preserved_digests=previous_fallback_digests,
                    )
                    if fallback_name is not None:
                        saved_attachments += 1
                        attachment_links.append(
                            (fallback_name, f"（邮件附件，未展开：{limit_error['error']}）")
                        )
                    errors.append(limit_error)
                    if fallback_error is not None:
                        errors.append(fallback_error)
                    continue
                try:
                    child = _parse_nested_attachment(attachment, parsed)
                    _check_cancel(cancel_event)
                except Exception as exc:
                    if isinstance(exc, CancellationRequested):
                        raise
                    nested_error = _nested_parse_error(parsed, attachment, exc)
                    fallback_name, fallback_error = _write_failed_nested_as_attachment(
                        partial,
                        parsed,
                        attachment,
                        used_names,
                        reason=nested_error["error"],
                        preserved_digests=previous_fallback_digests,
                    )
                    if fallback_name is not None:
                        saved_attachments += 1
                        attachment_links.append((fallback_name, "（邮件附件，展开失败）"))
                    errors.append(nested_error)
                    if fallback_error is not None:
                        errors.append(fallback_error)
                    continue

                try:
                    child_result = _write_tree(
                        child,
                        partial / "emails",
                        limits=limits,
                        depth=depth + 1,
                        budget=budget,
                        allow_unmarked_repair=(
                            allow_unmarked_repair
                            or child.source_sha256 in previous_child_sources
                        ),
                        cancel_event=cancel_event,
                    )
                except Exception as exc:
                    if isinstance(exc, CancellationRequested):
                        raise
                    if isinstance(exc, SidecarError):
                        reason = str(exc)
                    else:
                        reason = f"{type(exc).__name__}: {exc}"
                    nested_error = _result_error(
                        parsed,
                        location=attachment.filename or child.source_name,
                        stage="write",
                        code="nested_email_write_error",
                        error=f"子邮件结果写入失败：{reason}",
                    )
                    fallback_name, fallback_error = _write_failed_nested_as_attachment(
                        partial,
                        parsed,
                        attachment,
                        used_names,
                        reason=nested_error["error"],
                        preserved_digests=previous_fallback_digests,
                    )
                    if fallback_name is not None:
                        saved_attachments += 1
                        attachment_links.append((fallback_name, "（邮件附件，展开失败）"))
                    errors.append(nested_error)
                    if fallback_error is not None:
                        errors.append(fallback_error)
                    continue

                _check_cancel(cancel_event)
                child_prefix = f"emails/{child_result.mail_dir.name}"
                errors.extend(_prefix_error_location(error, child_prefix) for error in child_result.errors)
                saved_attachments += child_result.attachments_count
                nested_emails += child_result.nested_emails_count
                nested_links.append(
                    (
                        attachment.filename or child.source_name,
                        f"{child_prefix}/邮件.md",
                    )
                )
                nested_sources.append(child.source_sha256)
                continue

            filename = _safe_attachment_filename(
                attachment.filename,
                attachment.data,
                attachment.content_type,
                attachment.ordinal,
            )
            filename = _unique_attachment_filename(filename, used_names)
            try:
                _write_attachment(partial / "attachments", attachment, filename)
            except OSError as exc:
                errors.append(
                    _result_error(
                        parsed,
                        location=attachment.filename or filename,
                        stage="write",
                        code="attachment_write_error",
                        error=f"写入附件失败：{exc}",
                    )
                )
                continue
            saved_attachments += 1
            attachment_links.append((filename, ""))

        _check_cancel(cancel_event)
        (partial / "邮件.md").write_text(
            render_markdown(parsed, attachment_links, nested_links),
            encoding="utf-8",
            newline="\n",
        )
        if not errors:
            artifact_paths = ["邮件.md", *(f"attachments/{name}" for name, _ in attachment_links)]
            removable_fallbacks: list[str] = []
            for fallback_name in previous_failed_fallbacks:
                expected = previous_fallback_digests.get(fallback_name)
                fallback = partial / "attachments" / fallback_name
                if expected is not None and _artifact_matches(fallback, expected):
                    fallback.unlink()
                    removable_fallbacks.append(f"attachments/{fallback_name}")
            (partial / _PARTIAL_MARKER_NAME).unlink()
            (partial / ".complete.json").write_text(
                json.dumps(
                    _complete_marker(
                        parsed,
                        attachments_count=saved_attachments,
                        nested_emails_count=nested_emails,
                        artifacts=_artifact_manifest(partial, artifact_paths),
                        children=(Path(target).parent.name for _, target in nested_links),
                        child_sources=nested_sources,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
        else:
            fallback_artifacts = _artifact_manifest(
                partial,
                (f"attachments/{name}" for name, note in attachment_links if note),
            )
            for index, artifact in enumerate(fallback_artifacts):
                previous = previous_fallback_digests.get(
                    artifact["path"].removeprefix("attachments/")
                )
                if previous is not None:
                    # 连续的受限重试要保留首次落盘时的摘要；否则用户修改
                    # fallback 后又进行一次受限重试，会把修改后的摘要当成基线。
                    fallback_artifacts[index] = dict(previous)
            (partial / _PARTIAL_MARKER_NAME).write_text(
                json.dumps(
                    {
                        "version": _COMPLETE_MARKER_VERSION,
                        "source_sha256": parsed.source_sha256,
                        "status": "partial",
                        "children": [Path(target).parent.name for _, target in nested_links],
                        "child_sources": nested_sources,
                        "fallbacks": fallback_artifacts,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
        _merge_staged_tree(
            partial,
            mail_dir,
            remove_relative_paths=removable_fallbacks
            if not errors
            else (),
        )
    except CancellationRequested:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(partial, ignore_errors=True)
        raise SidecarError(f"写入邮件结果失败：{mail_dir}：{exc}", stage="write", code="output_error") from exc

    return WriteResult(
        mail_dir=mail_dir,
        skipped=False,
        status="partial_failed" if errors else "success",
        attachments_count=saved_attachments,
        nested_emails_count=nested_emails,
        errors=errors,
    )


def _extended_windows_path(absolute_path: str) -> str:
    """为绝对路径启用 Windows 长路径，兼容盘符和 UNC 共享目录。"""

    if absolute_path.startswith("\\\\?\\"):
        return absolute_path
    if absolute_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute_path[2:]
    return "\\\\?\\" + absolute_path


def write_result(
    parsed: ParsedEmail,
    output_root: Path,
    *,
    limits: ExtractionLimits | dict[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
) -> WriteResult:
    """递归写入邮件 Markdown、附件及子邮件目录。"""

    limits = _coerce_limits(limits)
    result_root = output_root
    if os.name == "nt":
        # 在任何 mkdir/stat/copytree/replace 之前转换；递归子邮件和暂存
        # 目录继承此前缀，避免依赖系统的 LongPathsEnabled 注册表设置。
        output_root = Path(_extended_windows_path(os.path.abspath(output_root)))
    result = _write_tree(parsed, output_root, limits=limits, cancel_event=cancel_event)
    # 扩展前缀仅供文件系统调用使用；对外路径沿用调用者的输出目录形式。
    return replace(result, mail_dir=result_root / result.mail_dir.name)


__all__ = [
    "Attachment",
    "AttachmentError",
    "CancellationRequested",
    "ParsedEmail",
    "SidecarError",
    "WriteResult",
    "html_to_text",
    "parse_email",
    "parse_eml",
    "parse_msg",
    "render_markdown",
    "rtf_to_text",
    "safe_slug",
    "write_result",
]
