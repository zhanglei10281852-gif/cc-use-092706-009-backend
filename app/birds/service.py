from __future__ import annotations

import json
import math
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.services.audit import AuditContext, AuditService

# 来源可信度只参与结论权重，不会丢弃任何原始证据
SOURCE_WEIGHTS = {"trusted": 1.0, "standard": 0.6, "low": 0.3}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bird_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'none' CHECK(review_status IN ('none','pending','approved','rejected')),
    resolved_species_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bird_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES bird_events(id) ON DELETE RESTRICT,
    submitted_by TEXT NOT NULL,
    observer TEXT NOT NULL,
    species_code TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    location_name TEXT NOT NULL DEFAULT '',
    source_tier TEXT NOT NULL DEFAULT 'standard' CHECK(source_tier IN ('trusted','standard','low')),
    weight REAL NOT NULL,
    attachment_kind TEXT NOT NULL DEFAULT 'note',
    attachment_digest TEXT NOT NULL DEFAULT '',
    attachment_uri TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    client_ref TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','withdrawn')),
    withdrawn_at TEXT,
    withdrawn_by TEXT NOT NULL DEFAULT '',
    withdraw_reason TEXT NOT NULL DEFAULT '',
    restored_at TEXT,
    restored_by TEXT NOT NULL DEFAULT '',
    restore_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bird_evidence_client
    ON bird_evidence(submitted_by, client_ref) WHERE client_ref <> '';
CREATE INDEX IF NOT EXISTS idx_bird_evidence_event ON bird_evidence(event_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_bird_evidence_submitter ON bird_evidence(submitted_by);
CREATE TABLE IF NOT EXISTS bird_event_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_a_id INTEGER NOT NULL REFERENCES bird_events(id) ON DELETE RESTRICT,
    event_b_id INTEGER NOT NULL REFERENCES bird_events(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','confirmed','rejected')),
    distance_km REAL NOT NULL,
    time_gap_minutes REAL NOT NULL,
    basis TEXT NOT NULL DEFAULT 'tolerance',
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_a_id, event_b_id),
    CHECK(event_a_id < event_b_id)
);
CREATE INDEX IF NOT EXISTS idx_bird_links_status ON bird_event_links(status);
CREATE TABLE IF NOT EXISTS bird_review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anchor_event_id INTEGER NOT NULL REFERENCES bird_events(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL DEFAULT 'species_conflict',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    raised_by TEXT NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT,
    resolved_species_code TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_review_status ON bird_review_items(status, id);
CREATE TABLE IF NOT EXISTS bird_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,
    evidence_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_audit_event ON bird_audit(event_id, id);
"""


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return round(2 * radius * math.asin(math.sqrt(a)), 3)


def _fingerprint(payload: dict[str, Any]) -> str:
    import hashlib

    compact = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(compact.encode()).hexdigest()


class BirdObservationService:
    """候鸟观测事件、证据链与人工复核的事务服务。"""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_schema()
        self.clock = SystemClock()
        self.audit = AuditService(self.connection, self.clock)

    # ------------------------------------------------------------------ utils
    def _audit(self, connection: sqlite3.Connection, principal: Principal, action: str, *,
               event_id: int | None = None, evidence_id: int | None = None, detail: dict | None = None) -> None:
        connection.execute(
            "INSERT INTO bird_audit(event_id,evidence_id,action,actor,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (event_id, evidence_id, action, principal.username, json.dumps(detail or {}, ensure_ascii=False), to_storage(self.clock.now())),
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="birds." + action,
            resource_type="bird_event",
            resource_id=event_id,
            after=detail,
        )

    def _require_event(self, connection: sqlite3.Connection, event_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM bird_events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFoundError("观察事件不存在")
        return row

    def _chain_ids(self, connection: sqlite3.Connection, event_id: int) -> list[int]:
        """经 confirmed 关联双向闭包，得到同一观察链上的全部事件。"""
        rows = connection.execute(
            """
            WITH RECURSIVE chain(id) AS (
                SELECT ?
                UNION
                SELECT CASE WHEN l.event_a_id = c.id THEN l.event_b_id ELSE l.event_a_id END
                FROM bird_event_links l
                JOIN chain c ON c.id IN (l.event_a_id, l.event_b_id)
                WHERE l.status = 'confirmed'
            )
            SELECT DISTINCT id FROM chain ORDER BY id
            """,
            (event_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def _active_evidence(self, connection: sqlite3.Connection, event_ids: list[int]) -> list[sqlite3.Row]:
        if not event_ids:
            return []
        placeholders = ",".join("?" for _ in event_ids)
        return connection.execute(
            f"SELECT * FROM bird_evidence WHERE status='active' AND event_id IN ({placeholders}) ORDER BY observed_at,id",
            tuple(event_ids),
        ).fetchall()

    def _consensus(self, evidence: list[sqlite3.Row]) -> dict[str, Any]:
        totals: dict[str, float] = {}
        for item in evidence:
            totals[item["species_code"]] = totals.get(item["species_code"], 0.0) + float(item["weight"])
        weighted = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
        total_weight = round(sum(totals.values()), 4)
        species: list[dict[str, Any]] = [
            {"species_code": code, "weight": round(weight, 3), "share": round(weight / total_weight, 3) if total_weight else 0.0}
            for code, weight in weighted
        ]
        leading = species[0] if species else None
        return {
            "species_weights": species,
            "consensus_species": leading["species_code"] if leading else None,
            "confidence": leading["share"] if leading else 0.0,
            "has_conflict": len(species) > 1,
            "total_weight": total_weight,
        }

    def _anchor(self, evidence: list[sqlite3.Row]) -> dict[str, Any] | None:
        active = [item for item in evidence if item["status"] == "active"] if evidence and "status" in evidence[0].keys() else evidence
        if not active:
            return None
        first = min(active, key=lambda item: (item["observed_at"], item["id"]))
        return {"observed_at": first["observed_at"], "latitude": first["latitude"], "longitude": first["longitude"]}

    def _chain_view(self, connection: sqlite3.Connection, event_id: int) -> dict[str, Any]:
        ids = self._chain_ids(connection, event_id)
        evidence = connection.execute(
            f"SELECT * FROM bird_evidence WHERE event_id IN ({','.join('?' for _ in ids)}) ORDER BY observed_at,id",
            tuple(ids),
        ).fetchall()
        active = [item for item in evidence if item["status"] == "active"]
        consensus = self._consensus(active)
        anchor = self._anchor(evidence)
        review = connection.execute(
            f"SELECT * FROM bird_review_items WHERE anchor_event_id IN ({','.join('?' for _ in ids)}) ORDER BY id DESC LIMIT 1",
            tuple(ids),
        ).fetchone()
        return {"chain_ids": ids, "evidence": evidence, "consensus": consensus, "anchor": anchor, "review": review}

    def _is_privileged(self, principal: Principal) -> bool:
        # 复核员与审计员可查看全部观察链；审计员只读，不产生结论
        return principal.can("birds.review") or principal.can("audit.read")

    def _can_oversee(self, principal: Principal) -> bool:
        # 仅复核员可对任意观察链执行写操作；审计员的全局可见范围是只读的
        return principal.can("birds.review")

    def _ensure_visible(self, connection: sqlite3.Connection, principal: Principal, event_id: int,
                        chain: dict[str, Any] | None = None) -> dict[str, Any]:
        view = chain or self._chain_view(connection, event_id)
        if self._is_privileged(principal):
            return view
        owners = {item["submitted_by"] for item in view["evidence"]}
        if principal.username not in owners:
            raise PermissionDeniedError("该观察链不在当前账号的可见范围内")
        return view

    def _owns_chain(self, view: dict[str, Any], principal: Principal) -> bool:
        return self._can_oversee(principal) or principal.username in {item["submitted_by"] for item in view["evidence"]}

    def _serialize_event(self, connection: sqlite3.Connection, row: sqlite3.Row, *, include_chain: bool = True) -> dict[str, Any]:
        result = dict(row)
        if include_chain:
            view = self._chain_view(connection, row["id"])
            result["chain_event_ids"] = view["chain_ids"]
            result["evidence"] = [dict(item) for item in view["evidence"]]
            result["consensus"] = view["consensus"]
            result["anchor"] = view["anchor"]
            result["review"] = dict(view["review"]) if view["review"] else None
            result["links"] = self._links(connection, view["chain_ids"])
        return result

    def _links(self, connection: sqlite3.Connection, event_ids: list[int]) -> list[dict[str, Any]]:
        if not event_ids:
            return []
        placeholders = ",".join("?" for _ in event_ids)
        rows = connection.execute(
            f"SELECT * FROM bird_event_links WHERE event_a_id IN ({placeholders}) OR event_b_id IN ({placeholders}) ORDER BY id",
            (*event_ids, *event_ids),
        ).fetchall()
        return [dict(item) for item in rows]

    # -------------------------------------------------------------- submission
    def submit_evidence(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("birds.observe")
        observed = from_storage(payload["observed_at"])
        if observed is None:
            raise ValidationError("观测时间格式无效")
        fingerprint_payload = {key: payload[key] for key in (
            "species_code", "observed_at", "latitude", "longitude", "observer",
            "location_name", "source_tier", "attachment_digest", "note", "client_ref")}
        fingerprint = _fingerprint(fingerprint_payload)
        client_ref = payload.get("client_ref", "")

        with transaction(immediate=True) as connection:
            # 重复提交（同一提交方 + client_ref）返回同一观察事件与同一条证据
            if client_ref:
                existing = connection.execute(
                    "SELECT * FROM bird_evidence WHERE submitted_by=? AND client_ref=?",
                    (principal.username, client_ref),
                ).fetchone()
                if existing is not None:
                    event = connection.execute("SELECT * FROM bird_events WHERE id=?", (existing["event_id"],)).fetchone()
                    result = self._serialize_event(connection, event)
                    result["deduplicated"] = True
                    result["evidence_id"] = existing["id"]
                    return result

            now = to_storage(self.clock.now())
            cursor = connection.execute(
                "INSERT INTO bird_events(created_by,created_at,updated_at) VALUES(?,?,?)",
                (principal.username, now, now),
            )
            event_id = int(cursor.lastrowid)
            evidence_cursor = connection.execute(
                "INSERT INTO bird_evidence(event_id,submitted_by,observer,species_code,observed_at,latitude,longitude,"
                "location_name,source_tier,weight,attachment_kind,attachment_digest,attachment_uri,note,client_ref,fingerprint,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id, principal.username, payload["observer"], payload["species_code"], payload["observed_at"],
                    payload["latitude"], payload["longitude"], payload["location_name"], payload["source_tier"],
                    SOURCE_WEIGHTS[payload["source_tier"]], payload["attachment_kind"], payload["attachment_digest"],
                    payload["attachment_uri"], payload["note"], client_ref, fingerprint, now,
                ),
            )
            evidence_id = int(evidence_cursor.lastrowid)
            self._audit(connection, principal, "evidence.submit", event_id=event_id, evidence_id=evidence_id,
                        detail={"species_code": payload["species_code"], "source_tier": payload["source_tier"]})
            event = connection.execute("SELECT * FROM bird_events WHERE id=?", (event_id,)).fetchone()
            result = self._serialize_event(connection, event)
            result["deduplicated"] = False
            result["evidence_id"] = evidence_id
            return result

    # ---------------------------------------------------------------- queries
    def list_events(self, principal: Principal, *, review_status: str | None = None,
                    conflict_only: bool = False, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        principal.require("birds.read")
        rows = self.connection.execute("SELECT * FROM bird_events ORDER BY id").fetchall()
        visible: list[dict[str, Any]] = []
        for row in rows:
            view = self._chain_view(self.connection, row["id"])
            if not self._is_privileged(principal):
                if principal.username not in {item["submitted_by"] for item in view["evidence"]}:
                    continue
            if review_status and row["review_status"] != review_status:
                continue
            if conflict_only and not view["consensus"]["has_conflict"]:
                continue
            visible.append(self._serialize_event(self.connection, row))
        # 链上事件会重复呈现，按观察链最小事件去重
        unique: dict[int, dict[str, Any]] = {}
        for item in visible:
            unique.setdefault(min(item["chain_event_ids"]), item)
        ordered = sorted(unique.values(), key=lambda item: item["id"])
        total = len(ordered)
        return {"total": total, "data": ordered[offset:offset + limit]}

    def get_event(self, event_id: int, principal: Principal) -> dict[str, Any]:
        principal.require("birds.read")
        with transaction() as connection:
            event = connection.execute("SELECT * FROM bird_events WHERE id=?", (event_id,)).fetchone()
            if event is None:
                raise NotFoundError("观察事件不存在")
            self._ensure_visible(connection, principal, event_id)
            return self._serialize_event(connection, event)

    # ----------------------------------------------------- candidate linking
    def generate_candidates(self, event_id: int, principal: Principal, *,
                            time_window_hours: float, distance_km: float) -> list[dict[str, Any]]:
        principal.require("birds.read")
        if time_window_hours <= 0 or distance_km <= 0:
            raise ValidationError("时间与空间容差必须为正数")
        with transaction(immediate=True) as connection:
            self._require_event(connection, event_id)
            view = self._ensure_visible(connection, principal, event_id)
            if not self._owns_chain(view, principal):
                raise PermissionDeniedError("只能为自己提交的观察链生成候选关联")
            self_anchor = view["anchor"]
            if self_anchor is None:
                return []
            chain_ids = set(view["chain_ids"])
            others = connection.execute("SELECT id FROM bird_events ORDER BY id").fetchall()
            now = to_storage(self.clock.now())
            result: list[dict[str, Any]] = []
            for other in others:
                other_id = int(other["id"])
                if other_id in chain_ids:
                    continue
                other_view = self._chain_view(connection, other_id)
                other_anchor = other_view["anchor"]
                if other_anchor is None:
                    continue
                rejected = connection.execute(
                    "SELECT 1 FROM bird_event_links WHERE status='rejected' AND "
                    "((event_a_id=? AND event_b_id=?) OR (event_a_id=? AND event_b_id=?))",
                    (min(event_id, other_id), max(event_id, other_id), min(event_id, other_id), max(event_id, other_id)),
                ).fetchone()
                if rejected:
                    continue
                distance = haversine_km(
                    self_anchor["latitude"], self_anchor["longitude"],
                    other_anchor["latitude"], other_anchor["longitude"],
                )
                t0 = from_storage(self_anchor["observed_at"])
                t1 = from_storage(other_anchor["observed_at"])
                gap_minutes = round(abs((t1 - t0).total_seconds()) / 60.0, 1)
                if distance <= distance_km and abs(t1 - t0) <= timedelta(hours=time_window_hours):
                    a, b = sorted((event_id, other_id))
                    connection.execute(
                        "INSERT INTO bird_event_links(event_a_id,event_b_id,status,distance_km,time_gap_minutes,"
                        "basis,created_by,created_at,updated_at) VALUES(?,?, 'candidate',?,?, 'tolerance',?,?,?) "
                        "ON CONFLICT(event_a_id,event_b_id) DO UPDATE SET distance_km=excluded.distance_km,"
                        "time_gap_minutes=excluded.time_gap_minutes,updated_at=excluded.updated_at "
                        "WHERE bird_event_links.status='candidate'",
                        (a, b, distance, gap_minutes, principal.username, now, now),
                    )
                    link = connection.execute(
                        "SELECT * FROM bird_event_links WHERE event_a_id=? AND event_b_id=?", (a, b)
                    ).fetchone()
                    result.append(dict(link))
            result.sort(key=lambda item: (item["distance_km"], item["time_gap_minutes"]))
            return result

    def reject_link(self, link_id: int, reason: str, principal: Principal) -> dict[str, Any]:
        principal.require("birds.read")
        with transaction(immediate=True) as connection:
            link = connection.execute("SELECT * FROM bird_event_links WHERE id=?", (link_id,)).fetchone()
            if link is None:
                raise NotFoundError("候选关联不存在")
            view = self._chain_view(connection, link["event_a_id"])
            self._ensure_visible(connection, principal, link["event_a_id"], view)
            if not self._owns_chain(view, principal):
                raise PermissionDeniedError("只能驳回与自己观察链相关的候选关联")
            if link["status"] == "confirmed":
                raise ConflictError("已确认合并的关联不能按候选驳回")
            now = to_storage(self.clock.now())
            connection.execute(
                "UPDATE bird_event_links SET status='rejected',decided_by=?,decided_at=?,reason=?,updated_at=? WHERE id=?",
                (principal.username, now, reason, now, link_id),
            )
            self._audit(connection, principal, "link.reject", event_id=link["event_a_id"],
                        detail={"link_id": link_id, "other_event_id": link["event_b_id"], "reason": reason})
            return dict(connection.execute("SELECT * FROM bird_event_links WHERE id=?", (link_id,)).fetchone())

    # ---------------------------------------------------------------- merging
    def merge_events(self, event_ids: list[int], reason: str, principal: Principal) -> dict[str, Any]:
        if not (principal.can("birds.observe") or principal.can("birds.review")):
            raise PermissionDeniedError("缺少权限：birds.observe")
        ordered = sorted(set(event_ids))
        if len(ordered) < 2:
            raise ValidationError("合并至少需要两个观察事件")
        with transaction(immediate=True) as connection:
            for event_id in ordered:
                self._require_event(connection, event_id)
            # 权限：复核员可合并任意链；志愿者只能合并包含自己证据，或已通过容差候选关联
            # 与自己观察链相连的事件，不能把毫无关联的他人事件拉入链中。
            if not self._can_oversee(principal):
                chains = {event_id: self._chain_ids(connection, event_id) for event_id in ordered}
                own_chains = {
                    tuple(ids) for ids in chains.values()
                    if principal.username in {
                        item["submitted_by"] for item in self._active_evidence(connection, ids)
                    }
                }
                if not own_chains:
                    raise PermissionDeniedError("不能合并不包含自己证据的观察事件")
                own_events = {event_id for ids in own_chains for event_id in ids}
                # 从自己的事件出发，沿候选/已确认关联可达的事件才允许纳入
                reachable = set(own_events)
                while True:
                    added = set()
                    placeholders = ",".join("?" for _ in reachable) or "SELECT 1 WHERE 0"
                    if reachable:
                        rows = connection.execute(
                            f"SELECT event_a_id,event_b_id FROM bird_event_links "
                            f"WHERE status IN ('candidate','confirmed') AND "
                            f"(event_a_id IN ({placeholders}) OR event_b_id IN ({placeholders}))",
                            tuple(reachable) + tuple(reachable),
                        ).fetchall()
                        for row in rows:
                            added.update((row["event_a_id"], row["event_b_id"]))
                    if added <= reachable:
                        break
                    reachable |= added
                for event_id in ordered:
                    if event_id not in reachable:
                        raise PermissionDeniedError("不能合并不含自己证据且无候选关联的观察事件")
            now = to_storage(self.clock.now())
            for index, a in enumerate(ordered):
                for b in ordered[index + 1:]:
                    lo, hi = sorted((a, b))
                    existing = connection.execute(
                        "SELECT * FROM bird_event_links WHERE event_a_id=? AND event_b_id=?", (lo, hi)
                    ).fetchone()
                    if existing and existing["status"] == "rejected":
                        raise ConflictError(f"事件 {lo} 与 {hi} 的关联已被驳回，无法合并")
                    anchors = [self._chain_view(connection, lo)["anchor"], self._chain_view(connection, hi)["anchor"]]
                    distance = haversine_km(anchors[0]["latitude"], anchors[0]["longitude"], anchors[1]["latitude"], anchors[1]["longitude"])
                    gap = abs((from_storage(anchors[1]["observed_at"]) - from_storage(anchors[0]["observed_at"])).total_seconds()) / 60.0
                    if existing:
                        connection.execute(
                            "UPDATE bird_event_links SET status='confirmed',decided_by=?,decided_at=?,reason=?,"
                            "distance_km=?,time_gap_minutes=?,updated_at=? WHERE id=?",
                            (principal.username, now, reason, distance, round(gap, 1), now, existing["id"]),
                        )
                    else:
                        connection.execute(
                            "INSERT INTO bird_event_links(event_a_id,event_b_id,status,distance_km,time_gap_minutes,"
                            "basis,decided_by,decided_at,reason,created_by,created_at,updated_at) "
                            "VALUES(?,?, 'confirmed',?,?,'manual',?,?,?, 'system',?,?)",
                            (lo, hi, distance, round(gap, 1), principal.username, now, reason, now, now),
                        )
            chain_ids = self._chain_ids(connection, ordered[0])
            evidence = self._active_evidence(connection, chain_ids)
            consensus = self._consensus(evidence)
            review_item = None
            if consensus["has_conflict"]:
                open_item = connection.execute(
                    f"SELECT * FROM bird_review_items WHERE status='pending' AND anchor_event_id IN "
                    f"({','.join('?' for _ in chain_ids)}) ORDER BY id LIMIT 1",
                    tuple(chain_ids),
                ).fetchone()
                if open_item is None:
                    cursor = connection.execute(
                        "INSERT INTO bird_review_items(anchor_event_id,kind,status,raised_by,note,created_at,updated_at) "
                        "VALUES(?, 'species_conflict','pending',?,?,?,?)",
                        (chain_ids[0], principal.username, reason, now, now),
                    )
                    review_item = cursor.lastrowid
                else:
                    review_item = open_item["id"]
                connection.execute(
                    f"UPDATE bird_events SET review_status='pending',updated_at=? WHERE id IN ({','.join('?' for _ in chain_ids)})",
                    (now, *chain_ids),
                )
            self._audit(connection, principal, "events.merge", event_id=chain_ids[0],
                        detail={"event_ids": chain_ids, "reason": reason, "conflict": consensus["has_conflict"],
                                "review_item_id": review_item})
            return self._serialize_event(connection, self._require_event(connection, chain_ids[0]))

    # ------------------------------------------------------------- review queue
    def list_review_queue(self, principal: Principal, *, status: str = "pending") -> list[dict[str, Any]]:
        principal.require("birds.review")
        if status == "all":
            rows = self.connection.execute("SELECT * FROM bird_review_items ORDER BY id").fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM bird_review_items WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            view = self._chain_view(self.connection, row["anchor_event_id"])
            item["chain_event_ids"] = view["chain_ids"]
            item["consensus"] = view["consensus"]
            item["evidence_count"] = len([e for e in view["evidence"] if e["status"] == "active"])
            items.append(item)
        return items

    def decide_review(self, item_id: int, decision: str, resolved_species: str | None, note: str,
                      principal: Principal) -> dict[str, Any]:
        principal.require("birds.review")
        with transaction(immediate=True) as connection:
            item = connection.execute("SELECT * FROM bird_review_items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("复核任务不存在")
            if item["status"] != "pending":
                raise ConflictError("该复核任务已处理")
            chain_ids = self._chain_ids(connection, item["anchor_event_id"])
            consensus = self._consensus(self._active_evidence(connection, chain_ids))
            now = to_storage(self.clock.now())
            resolved = ""
            if decision == "approved":
                resolved = (resolved_species or consensus["consensus_species"] or "").strip()
                if not resolved:
                    raise ValidationError("缺少可裁定的物种编码")
                connection.execute(
                    f"UPDATE bird_events SET review_status='approved',resolved_species_code=?,updated_at=? WHERE id IN "
                    f"({','.join('?' for _ in chain_ids)})",
                    (resolved, now, *chain_ids),
                )
            else:
                # 驳回不会删除任何证据，只把结论标记为不可采纳
                connection.execute(
                    f"UPDATE bird_events SET review_status='rejected',resolved_species_code='',updated_at=? WHERE id IN "
                    f"({','.join('?' for _ in chain_ids)})",
                    (now, *chain_ids),
                )
            connection.execute(
                "UPDATE bird_review_items SET status=?,decided_by=?,decided_at=?,resolved_species_code=?,note=?,updated_at=? WHERE id=?",
                (decision, principal.username, now, resolved, note, now, item_id),
            )
            self._audit(connection, principal, "review." + decision, event_id=item["anchor_event_id"],
                        detail={"item_id": item_id, "resolved_species_code": resolved, "note": note,
                                "species_weights": consensus["species_weights"]})
            return dict(connection.execute("SELECT * FROM bird_review_items WHERE id=?", (item_id,)).fetchone())

    # ------------------------------------------------- withdraw / restore evidence
    def withdraw_evidence(self, evidence_id: int, reason: str, principal: Principal) -> dict[str, Any]:
        principal.require("birds.observe")
        with transaction(immediate=True) as connection:
            evidence = connection.execute("SELECT * FROM bird_evidence WHERE id=?", (evidence_id,)).fetchone()
            if evidence is None:
                raise NotFoundError("证据不存在")
            view = self._chain_view(connection, evidence["event_id"])
            self._ensure_visible(connection, principal, evidence["event_id"], view)
            if not self._can_oversee(principal) and evidence["submitted_by"] != principal.username:
                raise PermissionDeniedError("只能撤销本人提交的证据")
            if evidence["status"] == "withdrawn":
                raise ConflictError("证据已处于撤销状态")
            now = to_storage(self.clock.now())
            connection.execute(
                "UPDATE bird_evidence SET status='withdrawn',withdrawn_at=?,withdrawn_by=?,withdraw_reason=? WHERE id=?",
                (now, principal.username, reason, evidence_id),
            )
            self._audit(connection, principal, "evidence.withdraw", event_id=evidence["event_id"], evidence_id=evidence_id,
                        detail={"reason": reason})
            return dict(connection.execute("SELECT * FROM bird_evidence WHERE id=?", (evidence_id,)).fetchone())

    def restore_evidence(self, evidence_id: int, reason: str, principal: Principal) -> dict[str, Any]:
        principal.require("birds.observe")
        with transaction(immediate=True) as connection:
            evidence = connection.execute("SELECT * FROM bird_evidence WHERE id=?", (evidence_id,)).fetchone()
            if evidence is None:
                raise NotFoundError("证据不存在")
            view = self._chain_view(connection, evidence["event_id"])
            self._ensure_visible(connection, principal, evidence["event_id"], view)
            if not self._can_oversee(principal) and evidence["submitted_by"] != principal.username:
                raise PermissionDeniedError("只能恢复本人提交的证据")
            if evidence["status"] != "withdrawn":
                raise ConflictError("证据未被撤销，无需恢复")
            now = to_storage(self.clock.now())
            connection.execute(
                "UPDATE bird_evidence SET status='active',restored_at=?,restored_by=?,restore_reason=? WHERE id=?",
                (now, principal.username, reason, evidence_id),
            )
            self._audit(connection, principal, "evidence.restore", event_id=evidence["event_id"], evidence_id=evidence_id,
                        detail={"reason": reason, "previous_withdraw_reason": evidence["withdraw_reason"]})
            return dict(connection.execute("SELECT * FROM bird_evidence WHERE id=?", (evidence_id,)).fetchone())

    def evidence_audit_trail(self, evidence_id: int, principal: Principal) -> list[dict[str, Any]]:
        principal.require("birds.read")
        evidence = self.connection.execute("SELECT * FROM bird_evidence WHERE id=?", (evidence_id,)).fetchone()
        if evidence is None:
            raise NotFoundError("证据不存在")
        with transaction() as connection:
            self._ensure_visible(connection, principal, evidence["event_id"])
        rows = self.connection.execute(
            "SELECT * FROM bird_audit WHERE evidence_id=? ORDER BY id", (evidence_id,)
        ).fetchall()
        return [dict(row) for row in rows]
