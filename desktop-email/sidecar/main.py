#!/usr/bin/env python3
"""邮件解析桌面应用的 Python sidecar。

sidecar 使用 JSON Lines 与 Tauri/Rust 通信：一行命令进入 stdin，一行事件
从 stdout 返回。格式解析和结果落盘由 ``mail_parser`` 提供，协议层保持独立，
方便后续任务增加附件与嵌套邮件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
import os
import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from .mail_parser import (
        CancellationRequested,
        ParsedEmail,
        SidecarError,
        WriteResult,
        parse_email,
        parse_eml,
        parse_msg,
        write_result,
    )
    from .limits import ExtractionLimits, LimitConfigError
except ImportError:  # ``python sidecar/main.py --stdio``
    from mail_parser import CancellationRequested, ParsedEmail, SidecarError, WriteResult, parse_email, parse_eml, parse_msg, write_result
    from limits import ExtractionLimits, LimitConfigError


PROTOCOL_VERSION = 1
EMAIL_SUFFIXES = {".eml", ".msg"}
LOGGER = logging.getLogger("email_sidecar")


def diagnostic_event(event: dict[str, Any]) -> dict[str, Any]:
    """Only log identifiers and operational metadata; never body or attachment data."""
    fields = ("type", "batch_id", "request_id", "item_id", "index", "status",
              "subject", "stage", "code", "total", "attachments", "nested_emails")
    result = {key: event[key] for key in fields if key in event}
    if event.get("source_path"):
        result["file"] = Path(event["source_path"]).name
    if event.get("errors"):
        result["errors"] = [{key: error[key] for key in ("stage", "code") if key in error}
                            for error in event["errors"] if isinstance(error, dict)]
    return result


@dataclass(frozen=True)
class InputScan:
    """批次开始前发现的邮件和输入扫描结果。"""

    mail_paths: tuple[Path, ...]
    skipped_files: tuple[dict[str, Any], ...]
    scan_errors: tuple[dict[str, Any], ...]
    excluded_output: int


@dataclass(frozen=True)
class StartContext:
    """已校验并登记为活动批次的处理上下文。"""

    batch_id: str
    request_id: str
    inputs: tuple[Any, ...]
    output_root: Path
    limits: ExtractionLimits
    cancel_event: threading.Event


def _path_from_item(item: Any) -> Path:
    if isinstance(item, dict):
        value = item.get("path")
    else:
        value = item
    if not isinstance(value, str) or not value.strip():
        raise SidecarError("输入项缺少文件路径", stage="input", code="invalid_input")
    return Path(value).expanduser().resolve()


def _item_id(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _is_email_path(path: Path) -> bool:
    return path.suffix.lower() in EMAIL_SUFFIXES


def _is_inside(path: Path, root: Path) -> bool:
    """判断路径是否为 root 或其后代，两个路径都必须已解析。"""

    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _error_record(
    source: Path,
    *,
    stage: str,
    code: str,
    error: str,
    location: str = "root",
) -> dict[str, Any]:
    return {
        "source_path": str(source),
        "stage": stage,
        "code": code,
        "error": error,
        "location": location,
    }


def _normalise_result_error(value: Any, source: Path) -> dict[str, Any]:
    """把解析器提供的错误收敛为批次协议的固定字段。"""

    if not isinstance(value, dict):
        return _error_record(
            source,
            stage="result",
            code="invalid_error",
            error=f"错误记录不是对象：{value!r}",
        )
    source_value = value.get("source_path")
    source_path = Path(source_value).expanduser().resolve() if isinstance(source_value, str) and source_value else source
    stage = value.get("stage") if isinstance(value.get("stage"), str) else "result"
    code = value.get("code") if isinstance(value.get("code"), str) else "result_error"
    reason = value.get("error")
    if not isinstance(reason, str) or not reason.strip():
        reason = "解析结果包含未说明的错误"
    location = value.get("location") if isinstance(value.get("location"), str) else "root"
    return _error_record(source_path, stage=stage, code=code, error=reason, location=location)


def _scan_file(
    path: Path,
    output_root: Path,
    mail_paths: list[Path],
    seen: set[str],
    skipped_files: list[dict[str, Any]],
    *,
    excluded_output: list[int],
) -> None:
    if _is_inside(path, output_root):
        excluded_output[0] += 1
        return
    if _is_email_path(path):
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            mail_paths.append(path)
        return
    skipped_files.append(
        _error_record(
            path,
            stage="input",
            code="unsupported_format",
            error=f"已跳过独立非邮件文件：{path.name or path}",
            location="input",
        )
    )


def _scan_directory(
    root: Path,
    output_root: Path,
    mail_paths: list[Path],
    seen: set[str],
    skipped_files: list[dict[str, Any]],
    scan_errors: list[dict[str, Any]],
    *,
    excluded_output: list[int],
) -> None:
    if _is_inside(root, output_root):
        excluded_output[0] += 1
        return

    def on_error(error: OSError) -> None:
        source = Path(error.filename or root)
        scan_errors.append(
            _error_record(
                source,
                stage="input",
                code="scan_error",
                error=f"扫描目录失败：{error}",
                location="input",
            )
        )

    try:
        walker = os.walk(root, topdown=True, onerror=on_error, followlinks=False)
        for current, directories, files in walker:
            current_path = Path(current).resolve()
            retained_directories: list[str] = []
            for name in sorted(directories):
                directory = (current_path / name).resolve()
                if _is_inside(directory, output_root):
                    excluded_output[0] += 1
                else:
                    retained_directories.append(name)
            directories[:] = retained_directories
            for name in sorted(files):
                _scan_file(
                    (current_path / name).resolve(),
                    output_root,
                    mail_paths,
                    seen,
                    skipped_files,
                    excluded_output=excluded_output,
                )
    except OSError as exc:
        scan_errors.append(
            _error_record(
                root,
                stage="input",
                code="scan_error",
                error=f"扫描目录失败：{exc}",
                location="input",
            )
        )


def _discover_inputs(inputs: Iterable[Any], output_root: Path) -> InputScan:
    """展开文件和目录输入，并在进入解析循环前排除输出目录。"""

    mail_paths: list[Path] = []
    skipped_files: list[dict[str, Any]] = []
    scan_errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    excluded_output = [0]
    resolved_output = output_root.expanduser().resolve()

    for value in inputs:
        try:
            path = _path_from_item(value)
        except SidecarError as exc:
            raw_path = Path(str(value)).expanduser().resolve()
            scan_errors.append(
                _error_record(raw_path, stage=exc.stage, code=exc.code, error=str(exc), location="input")
            )
            continue
        if _is_inside(path, resolved_output):
            excluded_output[0] += 1
            continue
        if path.is_dir():
            _scan_directory(
                path,
                resolved_output,
                mail_paths,
                seen,
                skipped_files,
                scan_errors,
                excluded_output=excluded_output,
            )
        else:
            _scan_file(
                path,
                resolved_output,
                mail_paths,
                seen,
                skipped_files,
                excluded_output=excluded_output,
            )

    return InputScan(tuple(mail_paths), tuple(skipped_files), tuple(scan_errors), excluded_output[0])


class SidecarServer:
    """stdin/stdout JSON Lines 服务，批次线程工作，主线程持续接收取消。"""

    def __init__(self) -> None:
        self._commands: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._active: tuple[str, str, threading.Event] | None = None
        self._active_lock = threading.Lock()
        self._emit_lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        LOGGER.debug("event %s", json.dumps(diagnostic_event(event), ensure_ascii=False))
        payload = {"protocol_version": PROTOCOL_VERSION, **event}
        with self._emit_lock:
            self._emit_locked(payload)

    @staticmethod
    def _emit_locked(payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def _read_commands(self) -> None:
        try:
            for line in sys.stdin:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    self.emit({"type": "protocol_error", "code": "invalid_json", "error": str(exc)})
                    continue
                if isinstance(value, dict):
                    self._commands.put(value)
                else:
                    self.emit({"type": "protocol_error", "code": "invalid_command", "error": "命令必须是 JSON 对象"})
        finally:
            self._commands.put(None)

    def _handle_cancel(self, command: dict[str, Any]) -> None:
        batch_id = command.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            self.emit({"type": "cancel_rejected", "code": "invalid_batch_id", "error": "缺少 batch_id"})
            return
        with self._emit_lock:
            with self._active_lock:
                active = self._active
            if active is None or active[0] != batch_id:
                self._emit_locked(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "type": "cancel_rejected",
                        "batch_id": batch_id,
                        "code": "batch_not_found",
                        "error": "批次不存在",
                    }
                )
                return
            active[2].set()
            self._emit_locked(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "cancel_requested",
                    "batch_id": batch_id,
                    "request_id": command.get("request_id"),
                    "status": "cancelling",
                }
            )

    def _prepare_start(self, command: dict[str, Any]) -> StartContext | None:
        batch_id = command.get("batch_id")
        request_id = command.get("request_id")
        inputs = command.get("inputs")
        output_value = command.get("output_dir")
        limits_value = command.get("limits")
        if not isinstance(batch_id, str) or not batch_id:
            self.emit({"type": "batch_rejected", "code": "invalid_batch_id", "error": "缺少 batch_id"})
            return None
        if not isinstance(request_id, str) or not request_id:
            self.emit({"type": "batch_rejected", "batch_id": batch_id, "code": "invalid_request_id", "error": "缺少 request_id"})
            return None
        if not isinstance(inputs, list) or not inputs:
            self.emit({"type": "batch_rejected", "batch_id": batch_id, "request_id": request_id, "code": "empty_inputs", "error": "至少需要一封邮件"})
            return None
        if not isinstance(output_value, str) or not output_value.strip():
            self.emit({"type": "batch_rejected", "batch_id": batch_id, "request_id": request_id, "code": "invalid_output_dir", "error": "缺少输出目录"})
            return None
        try:
            limits = ExtractionLimits.from_value(limits_value)
        except LimitConfigError as exc:
            self.emit(
                {
                    "type": "batch_rejected",
                    "batch_id": batch_id,
                    "request_id": request_id,
                    "stage": "limits",
                    "code": "invalid_limits",
                    "error": str(exc),
                    "location": "limits",
                }
            )
            return None
        busy = False
        with self._active_lock:
            if self._active is not None:
                busy = True
            else:
                cancel_event = threading.Event()
                self._active = (batch_id, request_id, cancel_event)
        if busy:
            self.emit({"type": "batch_rejected", "batch_id": batch_id, "request_id": request_id, "code": "batch_busy", "error": "已有批次正在处理"})
            return None
        return StartContext(
            batch_id=batch_id,
            request_id=request_id,
            inputs=tuple(inputs),
            output_root=Path(output_value).expanduser().resolve(),
            limits=limits,
            cancel_event=cancel_event,
        )

    def _emit_cancelled_items(
        self,
        *,
        batch_id: str,
        request_id: str,
        mail_paths: tuple[Path, ...],
        start: int,
        item_id: str | None = None,
    ) -> int:
        """为尚未完成的输入发出终态，避免 UI 把它们误留为待解析。"""

        for index in range(start, len(mail_paths)):
            source = mail_paths[index]
            event: dict[str, Any] = {
                "type": "item_cancelled",
                "batch_id": batch_id,
                "request_id": request_id,
                "index": index,
                "source_path": str(source),
                "status": "cancelled",
            }
            if index == start and item_id is not None:
                event["item_id"] = item_id
            self.emit(event)
        return max(0, len(mail_paths) - start)

    def _handle_start(self, context: StartContext) -> None:
        batch_id = context.batch_id
        request_id = context.request_id
        inputs = context.inputs
        output_root = context.output_root
        limits = context.limits
        cancel_event = context.cancel_event
        try:
            scan = _discover_inputs(inputs, output_root)
        except Exception as exc:  # pragma: no cover - defensive boundary for unexpected scan failures
            scan_error = _error_record(
                Path("<batch-inputs>"),
                stage="input",
                code="scan_error",
                error=f"扫描输入失败：{type(exc).__name__}: {exc}",
                location="input",
            )
            with self._active_lock:
                self._active = None
            summary = {
                "total": 0,
                "succeeded": 0,
                "partial_failed": 0,
                "skipped": 0,
                "failed": 1,
                "cancelled": 0,
                "skipped_files": 0,
            }
            self.emit({
                "type": "batch_started",
                "batch_id": batch_id,
                "request_id": request_id,
                "total": 0,
                "mail_paths": [],
                "input_count": len(inputs),
                "skipped_files": 0,
                "excluded_output": 0,
                "limits": limits.as_dict(),
            })
            self.emit({"type": "input_failed", "batch_id": batch_id, "request_id": request_id, "status": "failed", **scan_error})
            self.emit({
                "type": "batch_completed",
                "batch_id": batch_id,
                "request_id": request_id,
                "status": "failed",
                "summary": summary,
                "errors": [scan_error],
            })
            return
        total = len(scan.mail_paths)
        summary = {
            "total": total,
            "succeeded": 0,
            "partial_failed": 0,
            "skipped": 0,
            "failed": len(scan.scan_errors),
            "cancelled": 0,
            "skipped_files": len(scan.skipped_files),
        }
        errors: list[dict[str, Any]] = list(scan.scan_errors)
        self.emit(
            {
                "type": "batch_started",
                "batch_id": batch_id,
                "request_id": request_id,
                "total": total,
                "mail_paths": [str(path) for path in scan.mail_paths],
                "input_count": len(inputs),
                "skipped_files": len(scan.skipped_files),
                "excluded_output": scan.excluded_output,
                "limits": limits.as_dict(),
            }
        )
        try:
            for skipped_file in scan.skipped_files:
                self.emit(
                    {
                        "type": "input_skipped",
                        "batch_id": batch_id,
                        "request_id": request_id,
                        "status": "skipped",
                        **skipped_file,
                    }
                )
            for scan_error in scan.scan_errors:
                self.emit(
                    {
                        "type": "input_failed",
                        "batch_id": batch_id,
                        "request_id": request_id,
                        "status": "failed",
                        **scan_error,
                    }
                )
            for index, source in enumerate(scan.mail_paths):
                if cancel_event.is_set():
                    summary["cancelled"] += self._emit_cancelled_items(
                        batch_id=batch_id,
                        request_id=request_id,
                        mail_paths=scan.mail_paths,
                        start=index,
                    )
                    break
                item_id = _item_id(source)

                self.emit(
                    {
                        "type": "item_started",
                        "batch_id": batch_id,
                        "request_id": request_id,
                        "item_id": item_id,
                        "index": index,
                        "source_path": str(source),
                        "status": "running",
                    }
                )
                try:
                    item_started = time.monotonic()
                    parsed = parse_email(source)
                    LOGGER.debug("parsed batch=%r index=%s file=%r subject=%r elapsed_ms=%.1f",
                                 batch_id, index, source.name, parsed.subject,
                                 (time.monotonic() - item_started) * 1000)
                    if cancel_event.is_set():
                        summary["cancelled"] += self._emit_cancelled_items(
                            batch_id=batch_id,
                            request_id=request_id,
                            mail_paths=scan.mail_paths,
                            start=index,
                            item_id=item_id,
                        )
                        break
                    result: WriteResult = write_result(
                        parsed,
                        output_root,
                        limits=limits,
                        cancel_event=cancel_event,
                    )
                    if not isinstance(result.errors, list):
                        raise SidecarError(
                            "解析结果的错误列表格式无效",
                            stage="result",
                            code="invalid_result",
                        )
                    result_errors = [_normalise_result_error(value, source) for value in result.errors]
                    result_status = result.status
                    if result.skipped and result_status == "skipped" and not result_errors:
                        summary["skipped"] += 1
                        result_status = "skipped"
                        event_type = "item_succeeded"
                    elif result_status == "success" and not result_errors:
                        summary["succeeded"] += 1
                        event_type = "item_succeeded"
                    elif result_status == "partial_failed" or result_errors:
                        if not result_errors:
                            result_errors = [
                                _error_record(
                                    source,
                                    stage="result",
                                    code="partial_result",
                                    error="邮件结果标记为部分失败，但未提供具体原因",
                                )
                            ]
                        summary["partial_failed"] += 1
                        errors.extend(result_errors)
                        event_type = "item_partial_failed"
                        result_status = "partial_failed"
                    else:
                        raise SidecarError(
                            f"解析结果状态无效：{result_status!r}",
                            stage="result",
                            code="invalid_result",
                        )
                    if result_errors and event_type != "item_partial_failed":
                        errors.extend(result_errors)
                    item_event = {
                        "type": event_type,
                        "batch_id": batch_id,
                        "request_id": request_id,
                        "item_id": item_id,
                        "index": index,
                        "source_path": str(source),
                        "status": result_status,
                        "mail_dir": str(result.mail_dir),
                        "markdown_path": str(result.mail_dir / "邮件.md"),
                        "subject": parsed.subject,
                        "body_chars": len(parsed.body),
                        "attachments": result.attachments_count,
                        "nested_emails": result.nested_emails_count,
                    }
                    if result_errors:
                        item_event["errors"] = result_errors
                    LOGGER.debug("item_finished batch=%r index=%s elapsed_ms=%.1f",
                                 batch_id, index, (time.monotonic() - item_started) * 1000)
                    self.emit(item_event)
                except CancellationRequested:
                    summary["cancelled"] += self._emit_cancelled_items(
                        batch_id=batch_id,
                        request_id=request_id,
                        mail_paths=scan.mail_paths,
                        start=index,
                        item_id=item_id,
                    )
                    break
                except SidecarError as exc:
                    summary["failed"] += 1
                    item_error = _error_record(
                        source,
                        stage=exc.stage,
                        code=exc.code,
                        error=str(exc),
                    )
                    errors.append(item_error)
                    self.emit(
                        {
                            "type": "item_failed",
                            "batch_id": batch_id,
                            "request_id": request_id,
                            "item_id": item_id,
                            "index": index,
                            "source_path": str(source),
                            "status": "failed",
                            **item_error,
                        }
                    )
                except Exception as exc:  # pragma: no cover - unexpected failures are protocol-visible
                    summary["failed"] += 1
                    item_error = _error_record(
                        source,
                        stage="internal",
                        code="internal_error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    errors.append(item_error)
                    self.emit(
                        {
                            "type": "item_failed",
                            "batch_id": batch_id,
                            "request_id": request_id,
                            "item_id": item_id,
                            "index": index,
                            "source_path": str(source),
                            "status": "failed",
                            **item_error,
                        }
                    )
        finally:
            with self._active_lock:
                self._active = None
        cancelled = summary["cancelled"] > 0
        if cancelled:
            status = "cancelled"
        elif summary["failed"]:
            status = "failed"
        elif summary["partial_failed"]:
            status = "partial_failed"
        else:
            status = "completed"
        self.emit(
            {
                "type": "batch_completed",
                "batch_id": batch_id,
                "request_id": request_id,
                "status": status,
                "summary": summary,
                "errors": errors,
            }
        )

    def run(self) -> int:
        reader = threading.Thread(target=self._read_commands, name="sidecar-stdin", daemon=True)
        reader.start()
        workers: list[threading.Thread] = []
        while True:
            command = self._commands.get()
            if command is None:
                # Closing stdin is a normal one-shot runner pattern. It must
                # not cancel a start command that has already been accepted;
                # wait for all work to publish its terminal event first.
                for worker in workers:
                    worker.join()
                return 0
            command_type = command.get("type")
            if command_type == "start":
                context = self._prepare_start(command)
                if context is None:
                    continue
                worker = threading.Thread(
                    target=self._handle_start,
                    args=(context,),
                    name="sidecar-batch",
                )
                workers.append(worker)
                worker.start()
            elif command_type == "cancel":
                self._handle_cancel(command)
            else:
                self.emit({"type": "protocol_error", "code": "unknown_command", "error": f"不支持的命令：{command_type}"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="邮件解析 Python sidecar")
    parser.add_argument("--stdio", action="store_true", help="通过 stdin/stdout 提供 JSON Lines 批次服务")
    args = parser.parse_args(argv)
    if not args.stdio:
        parser.error("当前只支持 --stdio")
    # The Rust bridge sends UTF-8 bytes, independent of Windows' local code page.
    # Configure streams here as frozen executables also use this entry point.
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger().setLevel(logging.WARNING)
    LOGGER.setLevel(logging.DEBUG if os.environ.get("EMAIL_LOG_LEVEL") == "DEBUG" else logging.INFO)
    LOGGER.info("sidecar_start python=%s platform=%s frozen=%s pid=%s",
                sys.version.split()[0], sys.platform, bool(getattr(sys, "frozen", False)), os.getpid())
    try:
        return SidecarServer().run()
    finally:
        LOGGER.info("sidecar_exit")


if __name__ == "__main__":
    raise SystemExit(main())
