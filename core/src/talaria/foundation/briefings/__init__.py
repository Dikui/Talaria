"""扫描命中后的协同简报契约与提示词。"""

from talaria.foundation.briefings.contracts import (
    BriefingConsultRequest,
    BriefingConsultResponse,
    BriefingContent,
    BriefingLevelRef,
    BriefingListResponse,
    BriefingResultRequest,
    BriefingStatus,
    ScannerBriefing,
)
from talaria.foundation.briefings.prompt import build_briefing_prompt, parse_briefing_text

__all__ = [
    "BriefingConsultRequest",
    "BriefingConsultResponse",
    "BriefingContent",
    "BriefingLevelRef",
    "BriefingListResponse",
    "BriefingResultRequest",
    "BriefingStatus",
    "ScannerBriefing",
    "build_briefing_prompt",
    "parse_briefing_text",
]
