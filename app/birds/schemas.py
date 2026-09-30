from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SourceTier = Literal["trusted", "standard", "low"]


class EvidenceCreate(BaseModel):
    species_code: str = Field(..., min_length=2, max_length=32, description="物种编码")
    observed_at: str = Field(..., min_length=16, max_length=40, description="观测时间 ISO8601")
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    observer: str = Field(..., min_length=1, max_length=80, description="志愿者/提交人")
    location_name: str = Field(default="", max_length=120)
    source_tier: SourceTier = Field(default="standard", description="来源可信度")
    attachment_kind: str = Field(default="note", max_length=24)
    attachment_digest: str = Field(default="", max_length=128, description="附件内容摘要（哈希）")
    attachment_uri: str = Field(default="", max_length=300)
    note: str = Field(default="", max_length=500)
    client_ref: str = Field(default="", max_length=80, description="提交方去重引用，重复提交返回同一观察事件")

    @field_validator("species_code", "observer", "location_name", "attachment_kind", "client_ref")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()


class LinkRejectRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class MergeRequest(BaseModel):
    event_ids: list[int] = Field(..., min_length=2, max_length=50)
    reason: str = Field(default="", max_length=500)

    @field_validator("event_ids")
    @classmethod
    def unique(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("观察事件不能重复")
        return value


class ReviewRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    resolved_species_code: str | None = Field(default=None, min_length=2, max_length=32)
    note: str = Field(default="", max_length=500)

    @field_validator("resolved_species_code")
    @classmethod
    def strip_resolved(cls, value: str | None) -> str | None:
        return value.strip() if value else value


class WithdrawRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class RestoreRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)
