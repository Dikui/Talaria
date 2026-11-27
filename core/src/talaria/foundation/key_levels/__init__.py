"""关键位引擎与契约的公共出口（过渡期兼容层：新功能请使用 `talaria.foundation.structures`，见 ADR-0027）。"""

from __future__ import annotations

from talaria.foundation.key_levels.contracts import (
    KEY_LEVEL_METHOD_VERSION,
    KIND_CATEGORY,
    EvidenceClass,
    KeyLevel,
    KeyLevelAdoptionRecord,
    KeyLevelAdoptionRequest,
    KeyLevelCategory,
    KeyLevelFeedbackSummary,
    KeyLevelIgnoreList,
    KeyLevelIgnoreRecord,
    KeyLevelIgnoreRequest,
    KeyLevelKind,
    KeyLevelParams,
    KeyLevelSet,
    KeyLevelStatus,
    SessionProfile,
    VwapAnchor,
    session_profile_from_trait,
)
from talaria.foundation.key_levels.engine import (
    KeyLevelComputation,
    compute_key_levels,
    compute_key_levels_with_notes,
)

__all__ = [
    "KEY_LEVEL_METHOD_VERSION",
    "KIND_CATEGORY",
    "EvidenceClass",
    "KeyLevel",
    "KeyLevelAdoptionRecord",
    "KeyLevelAdoptionRequest",
    "KeyLevelCategory",
    "KeyLevelFeedbackSummary",
    "KeyLevelIgnoreList",
    "KeyLevelIgnoreRecord",
    "KeyLevelIgnoreRequest",
    "KeyLevelKind",
    "KeyLevelParams",
    "KeyLevelSet",
    "KeyLevelComputation",
    "KeyLevelStatus",
    "SessionProfile",
    "VwapAnchor",
    "compute_key_levels",
    "compute_key_levels_with_notes",
    "session_profile_from_trait",
]
