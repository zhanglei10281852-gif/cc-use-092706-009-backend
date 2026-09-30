"""候鸟观测链领域服务：证据去重、候选关联、合并、复核与撤销恢复。

设计约束：
- 每条原始证据都保留（软撤销，可恢复），合并不覆盖、不重复计数；
- 物种冲突只标记、进入人工复核，绝不自动丢弃任何一方；
- 来源可信度（confidence）只参与结论权重，不影响证据是否保留；
- 重复提交（client_ref 或内容指纹）返回同一事件。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.birds.schema import ensure_schema
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction

EARTH_RADIUS_M = 6_371_000.0

REVIEW_PERMISSION = "birds.review"
READ_PERMISSION = "birds.read"
WRITE_PERMISSION = "birds.write"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("observed_at 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return int(round(2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))))


class BirdObservationService:
    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_schema(self.connection)

    # ------------------------------------------------------------------ 审计
    def _audit(self, connection: sqlite3.Connection, event_id: int | None, action: str,
               actor: str, before: dict | None = None, after: dict | None = None) -> None:
        connection.execute(
            "INSERT INTO bird_audit(event_id,action,actor,before_json,after_json,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (event_id, action, actor, json.dumps(before or {}, ensure_ascii=False),
             json.dumps(after or {}, ensure_ascii=False), _now()),
        )

    # ------------------------------------------------------------ 可见范围
    def _scope_sql(self, principal: Principal, alias: str = "e") -> tuple[str, list[Any]]:
        """返回按角色收窄的 WHERE 片段。

        - 拥有 birds.review（或通配）：全部可见；
        - 仅有 birds.read（如审计员）：全部只读可见；
        - 仅有 birds.write（志愿者）：只能看到自己提交过证据的事件。
        """
        if principal.can(REVIEW_PERMISSION) or principal.can(READ_PERMISSION):
            return "", []
        return (
            f"EXISTS(SELECT 1 FROM bird_evidence ev WHERE ev.event_id={alias}.id"
            " AND ev.submitted_by=?)",
            [principal.username],
        )

    def _require_event_visible(self, principal: Principal, event: sqlite3.Row) -> None:
        if principal.can(REVIEW_PERMISSION) or principal.can(READ_PERMISSION):
            return
        owner = self.connection.execute(
            "SELECT 1 FROM bird_evidence WHERE event_id=? AND submitted_by=? LIMIT 1",
            (event["id"], principal.username),
        ).fetchone()
        if owner is None:
            raise PermissionDeniedError("只能访问自己参与提交的观测事件")

    def _require_event_writable(self, principal: Principal, event: sqlite3.Row) -> None:
        principal.require(WRITE_PERMISSION)
        if principal.can(REVIEW_PERMISSION):
            return
        self._require_event_visible(principal, event)

    # -------------------------------------------------------------- 聚合计算
    def _species_tally(self, evidence_rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """按可信度加权统计物种；低可信只降低权重，证据仍然保留。"""
        weights: dict[str, float] = {}
        counts: dict[str, int] = {}
        for row in evidence_rows:
            if row["revoked"]:
                continue
            code = row["species_code"]
            weights[code] = weights.get(code, 0.0) + float(row["confidence"])
            counts[code] = counts.get(code, 0) + 1
        tally = [
            {"species_code": code, "weight": round(weights[code], 4), "evidence_count": counts[code]}
            for code in sorted(weights, key=lambda item: (-weights[item], item))
        ]
        return tally

    def _recompute_event(self, connection: sqlite3.Connection, event_id: int) -> None:
        """根据事件下全部有效证据重算代表位置、时间窗与物种冲突状态。"""
        evidence = connection.execute(
            "SELECT * FROM bird_evidence WHERE event_id=? AND revoked=0 ORDER BY observed_at, id",
            (event_id,),
        ).fetchall()
        event = connection.execute("SELECT * FROM bird_events WHERE id=?", (event_id,)).fetchone()
        if not evidence:
            connection.execute(
                "UPDATE bird_events SET status='revoked', species_status='agreed', updated_at=? WHERE id=?",
                (_now(), event_id),
            )
            return
        total_weight = sum(float(item["confidence"]) for item in evidence) or 1.0
        lat = sum(float(item["latitude"]) * float(item["confidence"]) for item in evidence) / total_weight
        lon = sum(float(item["longitude"]) * float(item["confidence"]) for item in evidence) / total_weight
        tally = self._species_tally(evidence)
        winner = tally[0]["species_code"] if tally else evidence[0]["species_code"]
        conflicted = len(tally) > 1
        first_seen = min(item["observed_at"] for item in evidence)
        last_seen = max(item["observed_at"] for item in evidence)
        # 已合并/已驳回的事件不被自动改判；仅在开放态之间流转
        new_status = event["status"]
        if event["status"] in {"open", "pending_review", "revoked"}:
            new_status = "pending_review" if conflicted else "open"
        connection.execute(
            "UPDATE bird_events SET species_code=?, first_seen_at=?, last_seen_at=?, latitude=?,"
            " longitude=?, species_status=?, status=?, updated_at=? WHERE id=?",
            (winner, first_seen, last_seen, round(lat, 6), round(lon, 6),
             "conflicted" if conflicted else "agreed", new_status, _now(), event_id),
        )

    def _open_conflict_review(self, connection: sqlite3.Connection, event_id: int, actor: str) -> None:
        exists = connection.execute(
            "SELECT 1 FROM bird_reviews WHERE event_id=? AND kind='conflict' AND status='pending' LIMIT 1",
            (event_id,),
        ).fetchone()
        if exists:
            return
        now = _now()
        connection.execute(
            "INSERT INTO bird_reviews(event_id,kind,status,reason,submitted_by,created_at,updated_at)"
            " VALUES(?,'conflict','pending',?,?,?,?)",
            (event_id, "证据存在物种判断冲突，需人工裁定（不会自动丢弃任何证据）", actor, now, now),
        )

    # ----------------------------------------------------------- 证据接入
    def ingest_evidence(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require(WRITE_PERMISSION)
        observed = _parse_time(payload["observed_at"])
        observed_at = observed.isoformat(timespec="seconds")
        attachment = payload.get("attachment") or {}
        observer = payload["observer"].strip() or principal.username

        # 去重键：调用方 client_ref 优先，否则按证据内容算指纹 —— 重复提交返回同一事件
        if payload.get("client_ref"):
            dedupe_key = "ref:" + hashlib.sha256(
                f"{principal.username}|{payload['client_ref']}".encode()
            ).hexdigest()
        else:
            basis = "|".join([
                principal.username, observer, observed_at, repr(payload["latitude"]),
                repr(payload["longitude"]), payload["species_code"],
                attachment.get("sha256", ""), attachment.get("name", ""),
            ])
            dedupe_key = "sha:" + hashlib.sha256(basis.encode()).hexdigest()

        with transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM bird_evidence WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            if existing is not None:
                event = connection.execute("SELECT * FROM bird_events WHERE id=?", (existing["event_id"],)).fetchone()
                result = self._serialize_event(connection, event)
                result["deduplicated"] = True
                result["evidence_id"] = existing["id"]
                return result

            now = _now()
            cursor = connection.execute(
                "INSERT INTO bird_events(species_code,first_seen_at,last_seen_at,latitude,longitude,"
                "species_status,status,dedupe_fingerprint,created_at,updated_at)"
                " VALUES(?,?,?,?,?,'agreed','open',?,?,?)",
                (payload["species_code"], observed_at, observed_at, payload["latitude"],
                 payload["longitude"], dedupe_key, now, now),
            )
            event_id = int(cursor.lastrowid)
            ev_cursor = connection.execute(
                "INSERT INTO bird_evidence(event_id,submitted_by,observer,observer_role,observed_at,latitude,longitude,"
                "species_code,confidence,source_type,attachment_kind,attachment_name,attachment_sha256,"
                "attachment_size,note,dedupe_key,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, principal.username, observer, payload.get("observer_role", "volunteer"), observed_at,
                 payload["latitude"], payload["longitude"], payload["species_code"],
                 payload["confidence"], payload.get("source_type", "field_note"),
                 attachment.get("kind", ""), attachment.get("name", ""), attachment.get("sha256", ""),
                 attachment.get("size"), payload.get("note", ""), dedupe_key, now),
            )
            evidence_id = int(ev_cursor.lastrowid)
            self._audit(connection, event_id, "evidence.submit", principal.username,
                        after={"evidence_id": evidence_id, "species_code": payload["species_code"]})
            self._generate_links_for(connection, event_id,
                                     payload["time_tolerance_hours"], payload["distance_tolerance_km"])
            event = connection.execute("SELECT * FROM bird_events WHERE id=?", (event_id,)).fetchone()
            result = self._serialize_event(connection, event)
            result["deduplicated"] = False
            result["evidence_id"] = evidence_id
            return result

    # ----------------------------------------------------------- 候选关联
    def _generate_links_for(self, connection: sqlite3.Connection, event_id: int,
                            time_tolerance_hours: float, distance_tolerance_km: float) -> list[dict[str, Any]]:
        """按时间/空间容差为指定事件生成候选关联（跨日迁徙同样可命中）。"""
        target = connection.execute("SELECT * FROM bird_events WHERE id=?", (event_id,)).fetchone()
        if target is None or target["status"] in {"merged", "revoked", "rejected"}:
            return []
        candidates = connection.execute(
            "SELECT * FROM bird_events WHERE id<>? AND status IN ('open','pending_review') ORDER BY id",
            (event_id,),
        ).fetchall()
        created: list[dict[str, Any]] = []
        for other in candidates:
            a, b = sorted([target, other], key=lambda row: row["id"])
            gap = self._time_gap_seconds(a, b)
            if gap > time_tolerance_hours * 3600:
                continue
            distance = haversine_m(target["latitude"], target["longitude"],
                                   other["latitude"], other["longitude"])
            if distance > distance_tolerance_km * 1000:
                continue
            existing = connection.execute(
                "SELECT id FROM bird_links WHERE event_a_id=? AND event_b_id=?", (a["id"], b["id"])
            ).fetchone()
            if existing:
                continue
            now = _now()
            cursor = connection.execute(
                "INSERT INTO bird_links(event_a_id,event_b_id,time_gap_seconds,distance_m,reason,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (a["id"], b["id"], gap, distance,
                 f"时间差 {gap / 3600:.1f} 小时、空间距离 {distance / 1000:.1f} 公里，落在容差内", now),
            )
            created.append({"id": int(cursor.lastrowid), "event_a_id": a["id"], "event_b_id": b["id"],
                            "time_gap_seconds": gap, "distance_m": distance})
        return created

    @staticmethod
    def _time_gap_seconds(a: sqlite3.Row, b: sqlite3.Row) -> int:
        """两个时间窗的间隔：重叠或相接时为 0。支持跨日。"""
        a_start = _parse_time(a["first_seen_at"])
        a_end = _parse_time(a["last_seen_at"])
        b_start = _parse_time(b["first_seen_at"])
        b_end = _parse_time(b["last_seen_at"])
        if a_end >= b_start and b_end >= a_start:
            return 0
        delta = min(abs((b_start - a_end).total_seconds()), abs((a_start - b_end).total_seconds()))
        return int(delta)

    def generate_links(self, principal: Principal, time_hours: float, distance_km: float,
                       event_id: int | None = None) -> list[dict[str, Any]]:
        principal.require(WRITE_PERMISSION)
        with transaction(immediate=True) as connection:
            if event_id is not None:
                event = connection.execute("SELECT * FROM bird_events WHERE id=?", (event_id,)).fetchone()
                if event is None:
                    raise NotFoundError("观测事件不存在")
                self._require_event_writable(principal, event)
                created = self._generate_links_for(connection, event_id, time_hours, distance_km)
            else:
                # 全量扫描会触及他人事件，仅复核员可用；志愿者须指定本人事件
                principal.require(REVIEW_PERMISSION)
                rows = connection.execute(
                    "SELECT id FROM bird_events WHERE status IN ('open','pending_review') ORDER BY id"
                ).fetchall()
                created = []
                for row in rows:
                    created.extend(self._generate_links_for(connection, row["id"], time_hours, distance_km))
            return created

    # ------------------------------------------------------------- 查询
    def list_events(self, principal: Principal, *, status: str | None, species_code: str | None,
                    limit: int, offset: int) -> dict[str, Any]:
        if not (principal.can(READ_PERMISSION) or principal.can(WRITE_PERMISSION)):
            raise PermissionDeniedError("缺少权限：birds.read")
        where, params = self._scope_sql(principal)
        if status:
            where = ("(" + where + ") AND " if where else "") + "status=?"
            params.append(status)
        if species_code:
            where = ("(" + where + ") AND " if where else "") + "species_code=?"
            params.append(species_code)
        where_sql = f"WHERE {where}" if where else ""
        total = self.connection.execute(
            f"SELECT COUNT(*) FROM bird_events e {where_sql}", params
        ).fetchone()[0]
        rows = self.connection.execute(
            f"SELECT * FROM bird_events e {where_sql} ORDER BY first_seen_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        data = [self._serialize_event(self.connection, row, include_evidence=False) for row in rows]
        return {"total": total, "page": offset // limit + 1, "size": limit,
                "pages": (total + limit - 1) // limit, "data": data}

    def get_event(self, principal: Principal, event_id: int) -> dict[str, Any]:
        event = self.connection.execute("SELECT * FROM bird_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise NotFoundError("观测事件不存在")
        self._require_event_visible(principal, event)
        return self._serialize_event(self.connection, event)

    def list_links(self, principal: Principal, status: str | None) -> list[dict[str, Any]]:
        if not (principal.can(READ_PERMISSION) or principal.can(WRITE_PERMISSION)):
            raise PermissionDeniedError("缺少权限：birds.read")
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("l.status=?")
            params.append(status)
        if not (principal.can(REVIEW_PERMISSION) or principal.can(READ_PERMISSION)):
            # 志愿者只能看到与自己事件相关的候选关联
            conditions.append(
                "(EXISTS(SELECT 1 FROM bird_evidence ev WHERE ev.event_id=l.event_a_id AND ev.submitted_by=?)"
                " OR EXISTS(SELECT 1 FROM bird_evidence ev WHERE ev.event_id=l.event_b_id AND ev.submitted_by=?))"
            )
            params.extend([principal.username, principal.username])
        sql = "SELECT l.* FROM bird_links l"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY l.id DESC"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def list_reviews(self, principal: Principal, status: str | None) -> list[dict[str, Any]]:
        if not (principal.can(REVIEW_PERMISSION) or principal.can(WRITE_PERMISSION)):
            raise PermissionDeniedError("缺少权限：birds.review")
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("r.status=?")
            params.append(status)
        if not principal.can(REVIEW_PERMISSION):
            # 志愿者只能看到与自己事件相关的复核单
            conditions.append(
                "EXISTS(SELECT 1 FROM bird_evidence ev WHERE ev.event_id=r.event_id AND ev.submitted_by=?)"
            )
            params.append(principal.username)
        sql = "SELECT r.* FROM bird_reviews r"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, r.created_at"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def _serialize_event(self, connection: sqlite3.Connection, event: sqlite3.Row,
                         include_evidence: bool = True) -> dict[str, Any]:
        result = dict(event)
        result["merged_from"] = json.loads(event["merged_from_json"] or "[]")
        result.pop("merged_from_json", None)
        evidence = connection.execute(
            "SELECT * FROM bird_evidence WHERE event_id=? ORDER BY observed_at, id", (event["id"],)
        ).fetchall()
        result["species_tally"] = self._species_tally(evidence)
        result["active_evidence_count"] = sum(1 for item in evidence if not item["revoked"])
        if include_evidence:
            result["evidence"] = [dict(item) for item in evidence]
            result["links"] = [
                dict(row) for row in connection.execute(
                    "SELECT * FROM bird_links WHERE event_a_id=? OR event_b_id=? ORDER BY id",
                    (event["id"], event["id"]),
                ).fetchall()
            ]
            result["reviews"] = [
                dict(row) for row in connection.execute(
                    "SELECT * FROM bird_reviews WHERE event_id=? ORDER BY id", (event["id"],)
                ).fetchall()
            ]
        return result

    # ------------------------------------------------------------- 合并
    def propose_merge(self, principal: Principal, target_id: int, source_id: int, reason: str) -> dict[str, Any]:
        principal.require(WRITE_PERMISSION)
        if target_id == source_id:
            raise ValidationError("不能合并同一个事件")
        with transaction(immediate=True) as connection:
            target = connection.execute("SELECT * FROM bird_events WHERE id=?", (target_id,)).fetchone()
            source = connection.execute("SELECT * FROM bird_events WHERE id=?", (source_id,)).fetchone()
            if target is None or source is None:
                raise NotFoundError("观测事件不存在")
            self._require_event_writable(principal, target)
            self._require_event_writable(principal, source)
            if target["status"] == "merged" or source["status"] == "merged":
                raise ConflictError("已合并的事件不能再次合并")
            pending = connection.execute(
                "SELECT id FROM bird_reviews WHERE kind='merge' AND status='pending' AND ("
                " (event_id=? AND json_extract(detail_json,'$.source_event_id')=?)"
                " OR (event_id=? AND json_extract(detail_json,'$.source_event_id')=?))",
                (target_id, source_id, source_id, target_id),
            ).fetchone()
            if pending:
                raise ConflictError("已存在待复核的合并申请")
            a_id, b_id = sorted([target_id, source_id])
            link = connection.execute(
                "SELECT * FROM bird_links WHERE event_a_id=? AND event_b_id=?", (a_id, b_id)
            ).fetchone()
            if link is None:
                gap = self._time_gap_seconds(target, source)
                distance = haversine_m(target["latitude"], target["longitude"],
                                       source["latitude"], source["longitude"])
                now = _now()
                cursor = connection.execute(
                    "INSERT INTO bird_links(event_a_id,event_b_id,time_gap_seconds,distance_m,reason,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (a_id, b_id, gap, distance, "合并申请时补充的关联", now),
                )
                link_id = int(cursor.lastrowid)
            else:
                link_id = int(link["id"])
            now = _now()
            detail = {"target_event_id": target_id, "source_event_id": source_id, "link_id": link_id}
            cursor = connection.execute(
                "INSERT INTO bird_reviews(event_id,kind,status,reason,detail_json,submitted_by,created_at,updated_at)"
                " VALUES(?,'merge','pending',?,?,?,?,?)",
                (target_id, reason or "志愿者申请合并候选关联",
                 json.dumps(detail, ensure_ascii=False), principal.username, now, now),
            )
            connection.execute(
                "UPDATE bird_events SET status='pending_review', updated_at=? WHERE id IN (?,?) AND status='open'",
                (now, target_id, source_id),
            )
            self._audit(connection, target_id, "merge.propose", principal.username,
                        after={"source_event_id": source_id, "reason": reason})
            return dict(connection.execute("SELECT * FROM bird_reviews WHERE id=?", (cursor.lastrowid,)).fetchone())

    def decide_review(self, principal: Principal, review_id: int, approved: bool, note: str) -> dict[str, Any]:
        principal.require(REVIEW_PERMISSION)
        with transaction(immediate=True) as connection:
            review = connection.execute("SELECT * FROM bird_reviews WHERE id=?", (review_id,)).fetchone()
            if review is None:
                raise NotFoundError("复核单不存在")
            if review["status"] != "pending":
                raise ConflictError("该复核单已经处理")
            now = _now()
            connection.execute(
                "UPDATE bird_reviews SET status=?,decided_by=?,decided_at=?,decision_note=?,updated_at=? WHERE id=?",
                ("approved" if approved else "rejected", principal.username, now, note, now, review_id),
            )
            if review["kind"] == "merge":
                self._decide_merge(connection, review, approved, principal, note)
            elif review["kind"] == "conflict":
                self._decide_conflict(connection, review, approved, principal, note)
            else:  # revival 等其它复核：按通过/驳回更新事件状态
                event_id = review["event_id"]
                connection.execute(
                    "UPDATE bird_events SET status=?, reviewer=?, reviewed_at=?, review_note=?, updated_at=? WHERE id=?",
                    ("approved" if approved else "rejected", principal.username, now, note, now, event_id),
                )
            self._audit(connection, review["event_id"],
                        f"review.{review['kind']}.{'approve' if approved else 'reject'}",
                        principal.username, before=dict(review), after={"note": note})
            return dict(connection.execute("SELECT * FROM bird_reviews WHERE id=?", (review_id,)).fetchone())

    def _decide_merge(self, connection: sqlite3.Connection, review: sqlite3.Row,
                      approved: bool, principal: Principal, note: str) -> None:
        detail = json.loads(review["detail_json"] or "{}")
        target_id = int(detail["target_event_id"])
        source_id = int(detail["source_event_id"])
        link_id = detail.get("link_id")
        target = connection.execute("SELECT * FROM bird_events WHERE id=?", (target_id,)).fetchone()
        source = connection.execute("SELECT * FROM bird_events WHERE id=?", (source_id,)).fetchone()
        if not approved:
            if link_id:
                connection.execute("UPDATE bird_links SET status='rejected',decided_at=?,decided_by=? WHERE id=?",
                                   (_now(), principal.username, link_id))
            for event in (target, source):
                if event is not None and event["status"] == "pending_review":
                    connection.execute("UPDATE bird_events SET status='open',updated_at=? WHERE id=?",
                                       (_now(), event["id"]))
            return
        if source is None or target is None:
            raise ConflictError("合并前事件已不存在")
        # 执行合并：原始证据整体迁移并保留，不覆盖、不丢证据
        connection.execute(
            "UPDATE bird_evidence SET event_id=? WHERE event_id=?", (target_id, source_id)
        )
        merged_from = json.loads(target["merged_from_json"] or "[]")
        merged_from.append(source_id)
        connection.execute(
            "UPDATE bird_events SET merged_from_json=?, updated_at=? WHERE id=?",
            (json.dumps(merged_from, ensure_ascii=False), _now(), target_id),
        )
        connection.execute(
            "UPDATE bird_events SET status='merged', reviewer=?, reviewed_at=?, review_note=?, updated_at=? WHERE id=?",
            (principal.username, _now(), note, _now(), source_id),
        )
        if link_id:
            connection.execute("UPDATE bird_links SET status='merged',decided_at=?,decided_by=? WHERE id=?",
                               (_now(), principal.username, link_id))
        self._audit(connection, target_id, "merge.execute", principal.username,
                    before={"source_event_id": source_id}, after={"merged_from": merged_from})
        self._recompute_event(connection, target_id)
        # 冲突物种：绝不自动丢弃，生成冲突复核单交给人工
        refreshed = connection.execute("SELECT * FROM bird_events WHERE id=?", (target_id,)).fetchone()
        if refreshed["species_status"] == "conflicted":
            self._open_conflict_review(connection, target_id, principal.username)
            connection.execute("UPDATE bird_events SET status='pending_review',updated_at=? WHERE id=?",
                               (_now(), target_id))
        else:
            connection.execute(
                "UPDATE bird_events SET status='approved',reviewer=?,reviewed_at=?,review_note=?,updated_at=? WHERE id=?",
                (principal.username, _now(), note, _now(), target_id),
            )

    def _decide_conflict(self, connection: sqlite3.Connection, review: sqlite3.Row,
                         approved: bool, principal: Principal, note: str) -> None:
        # 通过=接受存在分歧的观察链（全部证据保留）；驳回=整条链驳回，证据依旧保留
        connection.execute(
            "UPDATE bird_events SET status=?,reviewer=?,reviewed_at=?,review_note=?,updated_at=? WHERE id=?",
            ("approved" if approved else "rejected", principal.username, _now(), note, _now(), review["event_id"]),
        )

    # ------------------------------------------------------- 撤销 / 恢复
    def revoke_evidence(self, principal: Principal, evidence_id: int, reason: str) -> dict[str, Any]:
        principal.require(WRITE_PERMISSION)
        with transaction(immediate=True) as connection:
            evidence = connection.execute("SELECT * FROM bird_evidence WHERE id=?", (evidence_id,)).fetchone()
            if evidence is None:
                raise NotFoundError("证据不存在")
            event = connection.execute("SELECT * FROM bird_events WHERE id=?", (evidence["event_id"],)).fetchone()
            if not principal.can(REVIEW_PERMISSION) and evidence["submitted_by"] != principal.username:
                raise PermissionDeniedError("只能撤销自己提交的证据")
            if evidence["revoked"]:
                raise ConflictError("证据已处于撤销状态")
            now = _now()
            connection.execute(
                "UPDATE bird_evidence SET revoked=1,revoked_at=?,revoke_reason=? WHERE id=?",
                (now, reason, evidence_id),
            )
            self._audit(connection, evidence["event_id"], "evidence.revoke", principal.username,
                        before={"evidence_id": evidence_id}, after={"reason": reason})
            self._recompute_event(connection, evidence["event_id"])
            return dict(connection.execute("SELECT * FROM bird_evidence WHERE id=?", (evidence_id,)).fetchone())

    def restore_evidence(self, principal: Principal, evidence_id: int, note: str) -> dict[str, Any]:
        principal.require(WRITE_PERMISSION)
        with transaction(immediate=True) as connection:
            evidence = connection.execute("SELECT * FROM bird_evidence WHERE id=?", (evidence_id,)).fetchone()
            if evidence is None:
                raise NotFoundError("证据不存在")
            if not principal.can(REVIEW_PERMISSION) and evidence["submitted_by"] != principal.username:
                raise PermissionDeniedError("只能恢复自己提交的证据")
            if not evidence["revoked"]:
                raise ConflictError("证据未被撤销，无需恢复")
            event = connection.execute("SELECT * FROM bird_events WHERE id=?", (evidence["event_id"],)).fetchone()
            # 被合并走的源事件不允许直接恢复证据到已归档链
            if event["status"] == "merged":
                raise ConflictError("证据所属事件已合并，请在合并后的事件上下文中处理")
            connection.execute(
                "UPDATE bird_evidence SET revoked=0,revoked_at=NULL,revoke_reason='' WHERE id=?",
                (evidence_id,),
            )
            self._audit(connection, evidence["event_id"], "evidence.restore", principal.username,
                        before={"evidence_id": evidence_id}, after={"note": note})
            self._recompute_event(connection, evidence["event_id"])
            restored_event = connection.execute(
                "SELECT * FROM bird_events WHERE id=?", (evidence["event_id"],)
            ).fetchone()
            if restored_event["species_status"] == "conflicted":
                self._open_conflict_review(connection, restored_event["id"], principal.username)
                connection.execute(
                    "UPDATE bird_events SET status='pending_review',updated_at=? WHERE id=?",
                    (_now(), restored_event["id"]),
                )
            return dict(connection.execute("SELECT * FROM bird_evidence WHERE id=?", (evidence_id,)).fetchone())
