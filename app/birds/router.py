"""候鸟观测链 HTTP 接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.api.dependencies import current_principal
from app.birds.schemas import (
    EvidenceIngest,
    MergeRequest,
    RestoreRequest,
    RevokeRequest,
    ReviewDecision,
)
from app.birds.service import BirdObservationService
from app.core.security import Principal

router = APIRouter(prefix="/api/birds", tags=["候鸟观测链"])


def service() -> BirdObservationService:
    return BirdObservationService()


@router.post("/evidence")
def ingest_evidence(payload: EvidenceIngest, response: Response,
                    principal: Principal = Depends(current_principal)) -> dict:
    result = service().ingest_evidence(payload.model_dump(), principal)
    response.status_code = 200 if result.get("deduplicated") else 201
    return result


@router.get("/events")
def list_events(
    status: str | None = None,
    species_code: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    return service().list_events(
        principal, status=status, species_code=species_code,
        limit=size, offset=(page - 1) * size,
    )


@router.get("/events/{event_id}")
def get_event(event_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().get_event(principal, event_id)


@router.post("/links/generate")
def generate_links(
    time_tolerance_hours: float = Query(48.0, gt=0, le=24 * 60),
    distance_tolerance_km: float = Query(50.0, gt=0, le=2000),
    event_id: int | None = Query(None),
    principal: Principal = Depends(current_principal),
) -> dict:
    created = service().generate_links(
        principal, time_tolerance_hours, distance_tolerance_km, event_id
    )
    return {"created": len(created), "links": created}


@router.get("/links")
def list_links(status: str | None = None, principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().list_links(principal, status)


@router.post("/merges", status_code=201)
def propose_merge(payload: MergeRequest, principal: Principal = Depends(current_principal)) -> dict:
    return service().propose_merge(
        principal, payload.target_event_id, payload.source_event_id, payload.reason
    )


@router.get("/reviews")
def list_reviews(status: str | None = None, principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().list_reviews(principal, status)


@router.post("/reviews/{review_id}/approve")
def approve_review(review_id: int, payload: ReviewDecision,
                   principal: Principal = Depends(current_principal)) -> dict:
    return service().decide_review(principal, review_id, True, payload.note)


@router.post("/reviews/{review_id}/reject")
def reject_review(review_id: int, payload: ReviewDecision,
                  principal: Principal = Depends(current_principal)) -> dict:
    return service().decide_review(principal, review_id, False, payload.note)


@router.post("/evidence/{evidence_id}/revoke")
def revoke_evidence(evidence_id: int, payload: RevokeRequest,
                    principal: Principal = Depends(current_principal)) -> dict:
    return service().revoke_evidence(principal, evidence_id, payload.reason)


@router.post("/evidence/{evidence_id}/restore")
def restore_evidence(evidence_id: int, payload: RestoreRequest,
                     principal: Principal = Depends(current_principal)) -> dict:
    return service().restore_evidence(principal, evidence_id, payload.note)


@router.get("/audit")
def list_audit(event_id: int | None = None, principal: Principal = Depends(current_principal)) -> list[dict]:
    # 审计轨迹对复核员与只读审计角色开放
    if not (principal.can("birds.review") or principal.can("birds.read")):
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError("缺少权限：birds.read")
    sql = "SELECT * FROM bird_audit"
    params: list = []
    if event_id is not None:
        sql += " WHERE event_id=?"
        params.append(event_id)
    sql += " ORDER BY id DESC LIMIT 500"
    return [dict(row) for row in service().connection.execute(sql, params).fetchall()]
