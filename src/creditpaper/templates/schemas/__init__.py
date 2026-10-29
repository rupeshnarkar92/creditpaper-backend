"""Templates module schemas (Pydantic request/response models)."""

from creditpaper.templates.schemas.paper_type import PaperTypeOut
from creditpaper.templates.schemas.template import (
    SaveSectionsRequest,
    SectionItem,
    TemplateCreate,
    TemplateOut,
    TemplatePaperOut,
    TemplatesListResponse,
    TemplateUpdate,
)

__all__ = [
    "PaperTypeOut",
    "SaveSectionsRequest",
    "SectionItem",
    "TemplateCreate",
    "TemplateOut",
    "TemplatePaperOut",
    "TemplateUpdate",
    "TemplatesListResponse",
]
