"""PaperType schema — for the fixed list of paper types."""

from __future__ import annotations

from pydantic import BaseModel


class PaperTypeOut(BaseModel):
    id: str
    label: str
    description: str = ""
    is_active: bool = True
    sort_order: int = 0
