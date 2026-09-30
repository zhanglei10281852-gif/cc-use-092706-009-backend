"""候鸟观测链端到端 API 验证。

覆盖：跨日迁徙候选关联、冲突物种不丢失、复核通过/驳回、
证据撤销/恢复、重复提交返回同一事件、不同角色的可见范围。
"""
from __future__ import annotations


def _make_user(client, admin_headers, username, roles):
    password = "Passw0rd!x"
    created = client.post(
        "/api/users",
        json={"username": username, "password": password, "display_name": username, "role_codes": roles},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": password, "client_label": "test"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


def _evidence(observer, when, *, species="ANS_CYGN", lat=30.0, lon=120.0,
              confidence=0.8, client_ref=""):
    payload = {
        "observer": observer,
        "observed_at": when,
        "latitude": lat,
        "longitude": lon,
        "species_code": species,
        "confidence": confidence,
        "note": f"{observer} 的目击记录",
        "attachment": {"kind": "photo", "name": f"{observer}.jpg", "sha256": "abc", "size": 1024},
    }
    if client_ref:
        payload["client_ref"] = client_ref
    return payload


def _users(client, admin):
    return {
        "reviewer": _make_user(client, admin["headers"], "reviewer1", ["bird_reviewer"]),
        "auditor": _make_user(client, admin["headers"], "auditor1", ["auditor"]),
        "alice": _make_user(client, admin["headers"], "alice", ["bird_volunteer"]),
        "bob": _make_user(client, admin["headers"], "bob", ["bird_volunteer"]),
    }


def test_duplicate_submission_returns_same_event(client, admin):
    users = _users(client, admin)
    alice = users["alice"]["headers"]
    payload = _evidence("Alice", "2026-09-20T10:00:00+00:00", client_ref="field-log-42")
    first = client.post("/api/birds/evidence", json=payload, headers=alice)
    assert first.status_code == 201, first.text
    second = client.post("/api/birds/evidence", json=payload, headers=alice)
    assert second.status_code == 200, second.text
    assert second.json()["deduplicated"] is True
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["evidence_id"] == first.json()["evidence_id"]


def test_cross_day_migration_link_merge_and_chain(client, admin):
    users = _users(client, admin)
    alice, bob, reviewer = users["alice"]["headers"], users["bob"]["headers"], users["reviewer"]["headers"]

    # 相邻地点、相隔约 28 小时（跨日）的两次记录，视为同一只候鸟
    e1 = client.post(
        "/api/birds/evidence",
        json=_evidence("Alice", "2026-09-20T10:00:00+00:00", lat=30.0, lon=120.0),
        headers=alice,
    ).json()
    e2 = client.post(
        "/api/birds/evidence",
        json=_evidence("Bob", "2026-09-21T14:00:00+00:00", lat=30.2, lon=120.0),
        headers=bob,
    ).json()
    assert e1["id"] != e2["id"]

    # 志愿者不能跨全部事件扫描生成关联；复核员可以
    denied = client.post("/api/birds/links/generate", headers=alice)
    assert denied.status_code == 403, denied.text

    # 第二条证据接入时已按容差自动产生候选；显式扫描确认不重复制造
    links_resp = client.post(
        "/api/birds/links/generate?time_tolerance_hours=48&distance_tolerance_km=50",
        headers=reviewer,
    )
    assert links_resp.status_code == 200, links_resp.text
    candidates = client.get("/api/birds/links?status=candidate", headers=reviewer).json()
    candidates = [c for c in candidates if {c["event_a_id"], c["event_b_id"]} == {e1["id"], e2["id"]}]
    assert len(candidates) == 1, candidates
    link = candidates[0]
    assert links_resp.json()["created"] == 0
    assert link["event_a_id"] == e1["id"]
    assert link["event_b_id"] == e2["id"]
    assert 27 * 3600 < link["time_gap_seconds"] <= 28 * 3600 + 60
    assert link["distance_m"] < 30_000

    # Alice 只能提议自己参与双方的合并会被拒绝（不含 Bob 的事件）
    bad = client.post(
        "/api/birds/merges",
        json={"target_event_id": e1["id"], "source_event_id": e2["id"], "reason": "同一只"},
        headers=alice,
    )
    assert bad.status_code == 403

    # 复核员发起合并并通过
    proposal = client.post(
        "/api/birds/merges",
        json={"target_event_id": e1["id"], "source_event_id": e2["id"], "reason": "跨日连续迁飞"},
        headers=reviewer,
    )
    assert proposal.status_code == 201, proposal.text
    review_id = proposal.json()["id"]
    approved = client.post(f"/api/birds/reviews/{review_id}/approve", json={"note": "链完整"}, headers=reviewer)
    assert approved.status_code == 200, approved.text

    target = client.get(f"/api/birds/events/{e1['id']}", headers=reviewer).json()
    source = client.get(f"/api/birds/events/{e2['id']}", headers=reviewer).json()
    assert target["status"] == "approved"
    assert source["status"] == "merged"
    # 两条原始证据都保留，没有重复计数
    assert target["active_evidence_count"] == 2
    assert len(target["evidence"]) == 2
    assert target["merged_from"] == [e2["id"]]
    assert target["first_seen_at"] == "2026-09-20T10:00:00+00:00"
    assert target["last_seen_at"] == "2026-09-21T14:00:00+00:00"
    # 合并后 Bob 也成为目标事件参与人，可以看到
    bob_view = client.get(f"/api/birds/events/{e1['id']}", headers=bob)
    assert bob_view.status_code == 200


def test_conflicting_species_is_kept_and_routed_to_review(client, admin):
    users = _users(client, admin)
    alice, bob, reviewer = users["alice"]["headers"], users["bob"]["headers"], users["reviewer"]["headers"]

    e1 = client.post(
        "/api/birds/evidence",
        json=_evidence("Alice", "2026-09-22T08:00:00+00:00", species="ANS_CYGN", lat=31.0),
        headers=alice,
    ).json()
    e2 = client.post(
        "/api/birds/evidence",
        json=_evidence("Bob", "2026-09-22T09:00:00+00:00", species="BRT_CANAD", lat=31.1),
        headers=bob,
    ).json()
    client.post("/api/birds/links/generate", headers=reviewer)
    proposal = client.post(
        "/api/birds/merges",
        json={"target_event_id": e1["id"], "source_event_id": e2["id"], "reason": "同地点疑似同一只"},
        headers=reviewer,
    ).json()
    client.post(f"/api/birds/reviews/{proposal['id']}/approve", json={"note": "先合并再判物种"}, headers=reviewer)

    target = client.get(f"/api/birds/events/{e1['id']}", headers=reviewer).json()
    # 冲突绝不自动丢弃任何一方
    assert target["species_status"] == "conflicted"
    assert target["status"] == "pending_review"
    tally = {item["species_code"]: item["evidence_count"] for item in target["species_tally"]}
    assert tally == {"ANS_CYGN": 1, "BRT_CANAD": 1}
    assert len(target["evidence"]) == 2

    conflict_reviews = [r for r in target["reviews"] if r["kind"] == "conflict" and r["status"] == "pending"]
    assert len(conflict_reviews) == 1
    conflict_id = conflict_reviews[0]["id"]
    rejected = client.post(f"/api/birds/reviews/{conflict_id}/reject", json={"note": "无法裁定"}, headers=reviewer)
    assert rejected.status_code == 200
    target = client.get(f"/api/birds/events/{e1['id']}", headers=reviewer).json()
    assert target["status"] == "rejected"
    assert len(target["evidence"]) == 2  # 驳回链也不删证据

    # 审计轨迹完整
    audit = client.get(f"/api/birds/audit?event_id={e1['id']}", headers=reviewer).json()
    actions = {row["action"] for row in audit}
    assert "merge.execute" in actions
    assert "review.conflict.reject" in actions


def test_revoke_and_restore_evidence(client, admin):
    users = _users(client, admin)
    alice, bob, reviewer = users["alice"]["headers"], users["bob"]["headers"], users["reviewer"]["headers"]

    created = client.post(
        "/api/birds/evidence",
        json=_evidence("Alice", "2026-09-23T08:00:00+00:00"),
        headers=alice,
    ).json()
    event_id, evidence_id = created["id"], created["evidence_id"]

    # Bob 不能撤销 Alice 的证据
    forbidden = client.post(f"/api/birds/evidence/{evidence_id}/revoke", json={"reason": "误报"}, headers=bob)
    assert forbidden.status_code == 403

    revoked = client.post(
        f"/api/birds/evidence/{evidence_id}/revoke", json={"reason": "照片模糊"}, headers=alice
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] == 1
    event = client.get(f"/api/birds/events/{event_id}", headers=alice).json()
    assert event["status"] == "revoked"
    assert event["active_evidence_count"] == 0
    # 原始证据记录仍在
    assert event["evidence"][0]["revoke_reason"] == "照片模糊"

    # 撤销可恢复
    restored = client.post(
        f"/api/birds/evidence/{evidence_id}/restore", json={"note": "复核照片可用"}, headers=alice
    )
    assert restored.status_code == 200
    assert restored.json()["revoked"] == 0
    event = client.get(f"/api/birds/events/{event_id}", headers=reviewer).json()
    assert event["status"] == "open"
    assert event["active_evidence_count"] == 1

    # 重复恢复报错
    dup = client.post(f"/api/birds/evidence/{evidence_id}/restore", json={"note": "x"}, headers=alice)
    assert dup.status_code == 409


def test_low_confidence_only_changes_weight_not_preservation(client, admin):
    users = _users(client, admin)
    alice, bob, reviewer = users["alice"]["headers"], users["bob"]["headers"], users["reviewer"]["headers"]

    # 同一地点：Alice 高可信判 A，Bob 低可信判 B
    e1 = client.post(
        "/api/birds/evidence",
        json=_evidence("Alice", "2026-09-25T06:00:00+00:00", species="SP_A", confidence=0.95, lat=34.0),
        headers=alice,
    ).json()
    e2 = client.post(
        "/api/birds/evidence",
        json=_evidence("Bob", "2026-09-25T06:30:00+00:00", species="SP_B", confidence=0.1, lat=34.02),
        headers=bob,
    ).json()
    client.post("/api/birds/links/generate", headers=reviewer)
    proposal = client.post(
        "/api/birds/merges",
        json={"target_event_id": e1["id"], "source_event_id": e2["id"]},
        headers=reviewer,
    ).json()
    client.post(f"/api/birds/reviews/{proposal['id']}/approve", json={"note": "ok"}, headers=reviewer)

    target = client.get(f"/api/birds/events/{e1['id']}", headers=reviewer).json()
    assert target["species_status"] == "conflicted"
    # 低可信证据仍然保留
    assert len(target["evidence"]) == 2
    tally = {item["species_code"]: item for item in target["species_tally"]}
    assert tally["SP_A"]["weight"] > tally["SP_B"]["weight"]
    # 加权结论倾向高可信方，但不丢弃低可信方
    assert target["species_code"] == "SP_A"
    assert set(tally) == {"SP_A", "SP_B"}


def test_role_visibility_ranges(client, admin):
    users = _users(client, admin)
    alice, bob = users["alice"]["headers"], users["bob"]["headers"]
    auditor, reviewer = users["auditor"]["headers"], users["reviewer"]["headers"]

    alice_event = client.post(
        "/api/birds/evidence",
        json=_evidence("Alice", "2026-09-24T08:00:00+00:00", lat=32.0),
        headers=alice,
    ).json()
    client.post(
        "/api/birds/evidence",
        json=_evidence("Bob", "2026-09-24T09:00:00+00:00", lat=33.0),
        headers=bob,
    )

    # 未认证拒绝
    assert client.get("/api/birds/events").status_code == 401

    # 志愿者只能看到自己参与的事件
    bob_list = client.get("/api/birds/events", headers=bob).json()
    assert bob_list["total"] == 1
    assert bob_list["data"][0]["id"] != alice_event["id"]
    assert client.get(f"/api/birds/events/{alice_event['id']}", headers=bob).status_code == 403

    # 复核员与只读审计员可看全部
    assert client.get("/api/birds/events", headers=reviewer).json()["total"] == 2
    assert client.get("/api/birds/events", headers=auditor).json()["total"] == 2

    # 审计员只读：不能提交证据、不能复核
    assert client.post(
        "/api/birds/evidence",
        json=_evidence("Auditor", "2026-09-24T10:00:00+00:00"),
        headers=auditor,
    ).status_code == 403
    assert client.post("/api/birds/reviews/999/approve", json={"note": "x"}, headers=auditor).status_code == 403

    # 审计员可查看审计轨迹
    assert client.get("/api/birds/audit", headers=auditor).status_code == 200

    # 志愿者看不到审计轨迹和全量复核队列
    assert client.get("/api/birds/audit", headers=alice).status_code == 403
