"""领域错误：HTTP 层统一映射为 ``ErrorResponse``。"""

from __future__ import annotations


class TalariaError(Exception):
    """所有可预期领域错误的基类。"""

    code = "INTERNAL"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnauthorizedError(TalariaError):
    code = "UNAUTHORIZED"


class NotFoundError(TalariaError):
    code = "NOT_FOUND"


class InstrumentNotFoundError(NotFoundError):
    code = "INSTRUMENT_NOT_FOUND"


class SnapshotNotFoundError(NotFoundError):
    code = "SNAPSHOT_NOT_FOUND"


class FeedUnavailableError(TalariaError):
    code = "FEED_UNAVAILABLE"


class FeedTransportError(FeedUnavailableError):
    """实时流传输层失败：握手失败、连接断开或帧格式错误。"""


class FeatureNotImplementedError(TalariaError):
    code = "NOT_IMPLEMENTED"


class InvalidRequestError(TalariaError):
    code = "VALIDATION_ERROR"
