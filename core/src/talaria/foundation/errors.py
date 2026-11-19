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


class DrawingNotFoundError(NotFoundError):
    code = "DRAWING_NOT_FOUND"


class AlertNotFoundError(NotFoundError):
    code = "ALERT_NOT_FOUND"


class ConsultationNotFoundError(NotFoundError):
    code = "CONSULTATION_NOT_FOUND"


class FeedUnavailableError(TalariaError):
    code = "FEED_UNAVAILABLE"


class FeedTransportError(FeedUnavailableError):
    """实时流传输层失败：握手失败、连接断开或帧格式错误。"""


class FeatureNotImplementedError(TalariaError):
    code = "NOT_IMPLEMENTED"


class InvalidRequestError(TalariaError):
    code = "VALIDATION_ERROR"


class SingleWriterViolationError(TalariaError):
    """同一进程内对同一个 SQLite 库打开了第二个写者。"""

    code = "SINGLE_WRITER_VIOLATION"


class ColdStoreError(TalariaError):
    """冷库发布的基类错误。"""

    code = "COLD_STORE_ERROR"


class ColdStoreIntegrityError(ColdStoreError):
    """内容哈希或不可变文件校验失败。"""

    code = "COLD_STORE_INTEGRITY"


class ColdStoreQualityError(ColdStoreError):
    """staging 批次未通过完整性/单调性/精度质检。"""

    code = "COLD_STORE_QUALITY"
