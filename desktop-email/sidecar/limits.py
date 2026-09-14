"""可配置的嵌套邮件递归限制。

限制属于一次批次请求，而不是邮件结果本身。``max_depth`` 使用根邮件为
深度 0；``max_extract_bytes`` 只计算允许递归展开的嵌套邮件附件原始字节。
因达到限制而保留的附件原件不扣除这项预算，普通附件也不扣除预算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_MAX_DEPTH = 10
DEFAULT_MAX_EXTRACT_BYTES = 500 * 1024 * 1024
MAX_ALLOWED_DEPTH = 100
MAX_ALLOWED_EXTRACT_BYTES = 10 * 1024 * 1024 * 1024


class LimitConfigError(ValueError):
    """批次递归限制配置无效。"""


@dataclass(frozen=True)
class ExtractionLimits:
    """一次批次使用的递归限制。"""

    max_depth: int = DEFAULT_MAX_DEPTH
    max_extract_bytes: int = DEFAULT_MAX_EXTRACT_BYTES

    def __post_init__(self) -> None:
        _validate_integer(
            self.max_depth,
            field="max_depth",
            minimum=0,
            maximum=MAX_ALLOWED_DEPTH,
            label="最大嵌套深度",
        )
        _validate_integer(
            self.max_extract_bytes,
            field="max_extract_bytes",
            minimum=0,
            maximum=MAX_ALLOWED_EXTRACT_BYTES,
            label="最大提取字节数",
        )

    @classmethod
    def from_value(cls, value: Any) -> "ExtractionLimits":
        """把 JSON 请求中的 ``limits`` 对象解析为强类型配置。

        未提供整个对象时使用默认值；对象内字段必须同时提供，避免把
        拼写错误静默当成默认配置。
        """

        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise LimitConfigError("limits 必须是对象，包含 max_depth 和 max_extract_bytes")
        expected = {"max_depth", "max_extract_bytes"}
        unknown = sorted(set(value) - expected)
        if unknown:
            names = ", ".join(str(name) for name in unknown)
            raise LimitConfigError(f"limits 包含未知字段：{names}")
        missing = sorted(expected - set(value))
        if missing:
            names = ", ".join(missing)
            raise LimitConfigError(f"limits 缺少字段：{names}")
        max_depth = value["max_depth"]
        max_extract_bytes = value["max_extract_bytes"]
        _validate_integer(
            max_depth,
            field="max_depth",
            minimum=0,
            maximum=MAX_ALLOWED_DEPTH,
            label="最大嵌套深度",
        )
        _validate_integer(
            max_extract_bytes,
            field="max_extract_bytes",
            minimum=0,
            maximum=MAX_ALLOWED_EXTRACT_BYTES,
            label="最大提取字节数",
        )
        return cls(max_depth=max_depth, max_extract_bytes=max_extract_bytes)

    def as_dict(self) -> dict[str, int]:
        return {
            "max_depth": self.max_depth,
            "max_extract_bytes": self.max_extract_bytes,
        }


def _validate_integer(
    value: Any,
    *,
    field: str,
    minimum: int,
    maximum: int,
    label: str,
) -> None:
    # bool 是 int 的子类，但不是有效的 JSON 数值配置。
    if isinstance(value, bool) or not isinstance(value, int):
        raise LimitConfigError(f"{field} 必须是整数（{label}范围 {minimum} 至 {maximum}）")
    if value < minimum or value > maximum:
        raise LimitConfigError(f"{field} 必须在 {minimum} 至 {maximum} 之间（{label}）")


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_EXTRACT_BYTES",
    "ExtractionLimits",
    "LimitConfigError",
    "MAX_ALLOWED_DEPTH",
    "MAX_ALLOWED_EXTRACT_BYTES",
]
