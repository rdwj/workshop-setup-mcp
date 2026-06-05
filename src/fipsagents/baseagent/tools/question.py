"""Stub — ask_user disabled for this agent.

gpt-oss-20b uses ask_user as its default output channel instead of
generating text responses. Removing STOCK_TOOL_SPEC prevents
registration while keeping the module importable.
"""
from __future__ import annotations
from pydantic import BaseModel, model_validator

class QuestionOption(BaseModel):
    label: str
    description: str | None = None
    value: str | None = None
    @model_validator(mode="after")
    def _default_value_to_label(self) -> "QuestionOption":
        if self.value is None:
            self.value = self.label
        return self

class QuestionAnswer(BaseModel):
    selected: list[str] = []
    custom_text: str | None = None
