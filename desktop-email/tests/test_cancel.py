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
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIDECAR = PROJECT_ROOT / "sidecar" / "main.py"
TERMINAL_EVENTS = {
    "item_succeeded",
    "item_partial_failed",
    "item_failed",
    "item_cancelled",
}


def write_eml(path: Path, *, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message.set_content(body)
    path.write_bytes(message.as_bytes())


class SidecarProcess:
    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, str(SIDECAR), "--stdio"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self.process.stdout is not None
        self.lines: queue.Queue[tuple[str, str]] = queue.Queue()

        def read_stdout() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.lines.put(("line", line))
            self.lines.put(("eof", ""))

        threading.Thread(target=read_stdout, name="cancel-test-reader", daemon=True).start()

    def send(self, command: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def events_until_completed(self, *, timeout: float = 30) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("sidecar did not publish batch_completed")
            try:
                kind, line = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise AssertionError("sidecar did not publish batch_completed") from exc
            if kind == "eof":
                stderr = self.process.stderr.read() if self.process.stderr else ""
                raise AssertionError(f"sidecar closed stdout before completion: {stderr}")
            event = json.loads(line)
            events.append(event)
            if event.get("type") == "batch_completed":
                return events

    def close_and_wait(self) -> None:
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        self.process.wait(timeout=10)
        if self.process.returncode:
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise AssertionError(f"sidecar exited with {self.process.returncode}: {stderr}")
        for stream in (self.process.stdout, self.process.stderr):
            if stream and not stream.closed:
                stream.close()

    def kill_on_failure(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream and not stream.closed:
                stream.close()


def start_command(inputs: list[Path], output: Path, batch_id: str) -> dict[str, Any]:
    return {
        "type": "start",
        "batch_id": batch_id,
        "request_id": f"request-{batch_id}",
        "inputs": [str(path) for path in inputs],
        "output_dir": str(output),
    }


class CancelAndResumeTests(unittest.TestCase):
    def test_cancel_after_started_item_keeps_completed_results_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "inputs"
            output = root / "results"
            source_dir.mkdir()
            inputs = []
            for index in range(24):
                path = source_dir / f"mail-{index:03d}.eml"
                write_eml(path, subject=f"第 {index} 封", body=f"正文 {index}")
                inputs.append(path)

            process = SidecarProcess()
            try:
                process.send(start_command(inputs, output, "cancel-first"))
                observed: list[dict[str, Any]] = []
                cancelled_sent = False
                deadline = time.monotonic() + 30
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AssertionError("取消批次没有结束")
                    kind, line = process.lines.get(timeout=remaining)
                    if kind == "eof":
                        raise AssertionError("取消批次在终态前关闭 stdout")
                    event = json.loads(line)
                    observed.append(event)
                    if event.get("type") == "item_started" and event.get("index") == 1 and not cancelled_sent:
                        process.send(
                            {
                                "type": "cancel",
                                "batch_id": "cancel-first",
                                "request_id": "cancel-request",
                            }
                        )
                        cancelled_sent = True
                    if event.get("type") == "batch_completed":
                        break

                self.assertTrue(cancelled_sent)
                self.assertTrue(any(event["type"] == "cancel_requested" for event in observed))
                completed = observed[-1]
                self.assertEqual(completed["status"], "cancelled")
                self.assertGreaterEqual(completed["summary"]["succeeded"], 1)
                self.assertGreaterEqual(completed["summary"]["cancelled"], 1)
                started = [event for event in observed if event["type"] == "item_started"]
                self.assertLess(len(started), len(inputs))
                terminals = [event for event in observed if event["type"] in TERMINAL_EVENTS]
                self.assertEqual(
                    len(terminals),
                    completed["summary"]["succeeded"]
                    + completed["summary"]["partial_failed"]
                    + completed["summary"]["failed"]
                    + completed["summary"]["skipped"]
                    + completed["summary"]["cancelled"],
                )
            finally:
                process.close_and_wait()

            first_terminals = [event for event in observed if event["type"] == "item_succeeded"]
            self.assertTrue(first_terminals)
            for event in first_terminals:
                self.assertTrue((Path(event["mail_dir"]) / ".complete.json").is_file())

            retry = SidecarProcess()
            try:
                retry.send(start_command(inputs, output, "cancel-second"))
                retry_events = retry.events_until_completed()
            finally:
                retry.close_and_wait()

            retry_completed = retry_events[-1]
            self.assertEqual(retry_completed["status"], "completed")
            self.assertGreaterEqual(retry_completed["summary"]["skipped"], len(first_terminals))
            self.assertEqual(
                retry_completed["summary"]["succeeded"] + retry_completed["summary"]["skipped"],
                len(inputs),
            )
            self.assertEqual(len(list(output.glob("*/邮件.md"))), len(inputs))
            self.assertTrue(all((mail_dir / ".complete.json").is_file() for mail_dir in output.iterdir()))

    def test_eof_after_start_waits_for_batch_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "mail.eml"
            output = root / "results"
            write_eml(source, subject="EOF 后继续", body="正文")

            process = SidecarProcess()
            try:
                process.send(start_command([source], output, "eof-start"))
                assert process.process.stdin is not None
                process.process.stdin.close()
                events = process.events_until_completed()
                self.assertEqual(events[-1]["type"], "batch_completed")
                self.assertEqual(events[-1]["status"], "completed")
                self.assertEqual(events[-1]["summary"]["succeeded"], 1)
                process.process.wait(timeout=10)
                self.assertEqual(process.process.returncode, 0)
            finally:
                if process.process.poll() is None:
                    process.kill_on_failure()
                elif process.process.stdout and not process.process.stdout.closed:
                    process.process.stdout.close()
                if process.process.stderr and not process.process.stderr.closed:
                    process.process.stderr.close()


if __name__ == "__main__":
    unittest.main()
