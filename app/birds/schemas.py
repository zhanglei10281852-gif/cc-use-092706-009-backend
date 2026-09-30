"""候鸟观测链 API 的请求模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class AttachmentSummary(BaseModel):
    kind: str = Field(default="photo", max_length=24)
    name: str = Field(default="", max_length=200)
    sha256: str = Field(default="", max_length=64)
    size: int | None = Field(default=None, ge=0)


class EvidenceIngest(BaseModel):
    """志愿者提交的一条原始证据。"""

    observer: str = Field(..., min_length=1, max_length=64)
    observer_role: str = Field(default="volunteer", max_length=32)
    observed_at: str = Field(..., min_length=8, max_length=40)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    species_code: str = Field(..., min_length=1, max_length=40)
    confidence: float = Field(default=0.6, ge=0, le=1)
    source_type: str = Field(default="field_note", max_length=32)
    attachment: AttachmentSummary | None = None
    note: str = Field(default="", max_length=500)
    # 调用方提供的去重键；缺失则按证据内容计算指纹
    client_ref: str = Field(default="", max_length=120)
    # 时空容差，默认 48 小时 / 50 公里
    time_tolerance_hours: float = Field(default=48.0, gt=0, le=24 * 60)
    distance_tolerance_km: float = Field(default=50.0, gt=0, le=2000)


class LinkToleranceQuery(BaseModel):
    time_tolerance_hours: float = Field(default=48.0, gt=0)
    distance_tolerance_km: float = Field(default=50.0, gt=0)


class MergeRequest(BaseModel):
    target_event_id: int = Field(..., ge=1)
    source_event_id: int = Field(..., ge=1)
    reason: str = Field(default="", max_length=300)


class ReviewDecision(BaseModel):
    note: str = Field(default="", max_length=500)


class RevokeRequest(BaseModel):
    reason: str = Field(default="", max_length=300)


class RestoreRequest(BaseModel):
    note: str = Field(default="", max_length=300)
