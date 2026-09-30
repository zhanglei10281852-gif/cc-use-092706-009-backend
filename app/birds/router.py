from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.birds.schemas import (
    EvidenceCreate,
    LinkRejectRequest,
    MergeRequest,
    RestoreRequest,
    ReviewRequest,
    WithdrawRequest,
)
from app.birds.service import BirdObservationService
from app.core.security import Principal

router = APIRouter(prefix="/api/birds", tags=["候鸟观测"])


def service() -> BirdObservationService:
    return BirdObservationService()


@router.post("/evidence", status_code=201)
def submit_evidence(payload: EvidenceCreate, principal: Principal = Depends(current_principal)) -> dict:
    return service().submit_evidence(payload.model_dump(), principal)


@router.get("/events")
def list_events(
    review_status: str | None = Query(default=None, pattern="^(none|pending|approved|rejected)$"),
    conflict_only: bool = Query(default=False),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    offset = (page - 1) * size
    result = service().list_events(
        principal, review_status=review_status, conflict_only=conflict_only, limit=size, offset=offset
    )
    return {"page": page, "size": size, **result}


@router.get("/events/{event_id}")
def get_event(event_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().get_event(event_id, principal)


@router.post("/events/{event_id}/candidate-links", status_code=201)
def generate_candidates(
    event_id: int,
    time_window_hours: float = Query(..., gt=0, le=720),
    distance_km: float = Query(2.0, gt=0, le=500),
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    return service().generate_candidates(
        event_id, principal, time_window_hours=time_window_hours, distance_km=distance_km
    )


@router.post("/links/{link_id}/reject")
def reject_link(link_id: int, payload: LinkRejectRequest, principal: Principal = Depends(current_principal)) -> dict:
    return service().reject_link(link_id=link_id, reason=payload.reason, principal=principal)


@router.post("/merge", status_code=201)
def merge_events(payload: MergeRequest, principal: Principal = Depends(current_principal)) -> dict:
    return service().merge_events(payload.event_ids, payload.reason, principal)


@router.get("/review-queue")
def list_review_queue(
    status: str = Query("pending", pattern="^(pending|approved|rejected|all)$"),
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    return service().list_review_queue(principal, status=status)


@router.post("/review-items/{item_id}/decision")
def decide_review(item_id: int, payload: ReviewRequest, principal: Principal = Depends(current_principal)) -> dict:
    return service().decide_review(item_id, payload.decision, payload.resolved_species_code, payload.note, principal)


@router.post("/evidence/{evidence_id}/withdraw")
def withdraw_evidence(evidence_id: int, payload: WithdrawRequest, principal: Principal = Depends(current_principal)) -> dict:
    return service().withdraw_evidence(evidence_id, payload.reason, principal)


@router.post("/evidence/{evidence_id}/restore")
def restore_evidence(evidence_id: int, payload: RestoreRequest, principal: Principal = Depends(current_principal)) -> dict:
    return service().restore_evidence(evidence_id, payload.reason, principal)


@router.get("/evidence/{evidence_id}/audit-trail")
def evidence_audit_trail(evidence_id: int, principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().evidence_audit_trail(evidence_id, principal)
