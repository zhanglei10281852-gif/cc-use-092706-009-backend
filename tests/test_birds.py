from __future__ import annotations


def make_user(client, admin, username, role_codes, display_name=None, password="Bird!23456"):
    response = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": username,
            "password": password,
            "display_name": display_name or username,
            "role_codes": role_codes,
        },
    )
    assert response.status_code == 201, response.text
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}", "username": username}


def evidence(species, observed_at, lat, lon, *, observer, tier="standard", client_ref="", digest="h", note=""):
    return {
        "species_code": species,
        "observed_at": observed_at,
        "latitude": lat,
        "longitude": lon,
        "observer": observer,
        "source_tier": tier,
        "attachment_kind": "photo",
        "attachment_digest": digest,
        "note": note,
        "client_ref": client_ref,
    }


def test_duplicate_submission_returns_same_event(client, admin):
    vol = make_user(client, admin, "vol.dup", ["volunteer"])
    payload = evidence("ANASPLA", "2026-09-20T08:00:00+00:00", 30.20, 120.10, observer="甲", client_ref="ref-1")
    first = client.post("/api/birds/evidence", headers=vol, json=payload)
    second = client.post("/api/birds/evidence", headers=vol, json=payload)
    assert first.status_code == second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["evidence_id"] == second.json()["evidence_id"]
    assert second.json()["deduplicated"] is True
    assert first.json()["deduplicated"] is False


def test_cross_day_migration_candidate_link_and_merge(client, admin):
    # 两名志愿者在相邻地点、跨多日记录同一只候鸟
    vol_a = make_user(client, admin, "vol.cross.a", ["volunteer"], display_name="志愿者A")
    vol_b = make_user(client, admin, "vol.cross.b", ["volunteer"], display_name="志愿者B")
    first = client.post(
        "/api/birds/evidence", headers=vol_a,
        json=evidence("LUSCIN", "2026-09-20T08:00:00+00:00", 30.2000, 120.1000, observer="A", client_ref="day1", digest="a"),
    ).json()
    second = client.post(
        "/api/birds/evidence", headers=vol_b,
        json=evidence("LUSCIN", "2026-09-22T09:30:00+00:00", 30.2150, 120.1100, observer="B", client_ref="day3", digest="b"),
    ).json()

    # 跨日但在 48 小时 + 5 公里容差内 → 生成候选关联
    candidates = client.post(
        f"/api/birds/events/{first['id']}/candidate-links?time_window_hours=52&distance_km=5",
        headers=vol_a,
    )
    assert candidates.status_code == 201, candidates.text
    links = candidates.json()
    assert len(links) == 1
    link = links[0]
    assert link["status"] == "candidate"
    assert set([link["event_a_id"], link["event_b_id"]]) == {first["id"], second["id"]}
    assert link["distance_km"] <= 5
    assert link["time_gap_minutes"] > 24 * 60  # 确认是跨日

    # 合并成一条可复核观察链，两条原始证据都保留
    merged = client.post("/api/birds/merge", headers=vol_a, json={"event_ids": [first["id"], second["id"]], "reason": "同一只"})
    assert merged.status_code == 201, merged.text
    body = merged.json()
    assert body["chain_event_ids"] == [first["id"], second["id"]]
    assert len(body["evidence"]) == 2
    assert body["consensus"]["has_conflict"] is False
    assert body["consensus"]["consensus_species"] == "LUSCIN"
    # 双方现在都能看到同一条链
    assert client.get(f"/api/birds/events/{first['id']}", headers=vol_b).status_code == 200


def test_conflicting_species_goes_to_review_and_weights(client, admin):
    vol_a = make_user(client, admin, "vol.conf.a", ["volunteer"])
    vol_b = make_user(client, admin, "vol.conf.b", ["volunteer"])
    reviewer = make_user(client, admin, "rev.conf", ["conservationist"])

    first = client.post(
        "/api/birds/evidence", headers=vol_a,
        json=evidence("SPECIES_X", "2026-09-20T08:00:00+00:00", 30.20, 120.10, observer="A", tier="trusted", client_ref="x1"),
    ).json()
    # 低可信来源的冲突物种：不会被丢弃，只降低权重
    second = client.post(
        "/api/birds/evidence", headers=vol_b,
        json=evidence("SPECIES_Y", "2026-09-20T09:00:00+00:00", 30.205, 120.105, observer="B", tier="low", client_ref="y1"),
    ).json()
    merged = client.post("/api/birds/merge", headers=reviewer,
                         json={"event_ids": [first["id"], second["id"]], "reason": "需人工裁定"}).json()
    assert merged["consensus"]["has_conflict"] is True
    weights = {item["species_code"]: item["weight"] for item in merged["consensus"]["species_weights"]}
    assert weights["SPECIES_X"] == 1.0
    assert weights["SPECIES_Y"] == 0.3  # 低可信只影响权重，证据仍在
    assert len(merged["evidence"]) == 2

    # 进入人工复核队列
    queue = client.get("/api/birds/review-queue?status=pending", headers=reviewer)
    assert queue.status_code == 200
    item = next(it for it in queue.json() if first["id"] in it["chain_event_ids"])
    assert item["status"] == "pending"
    assert item["consensus"]["has_conflict"] is True

    # 复核通过，裁定物种
    decision = client.post(
        f"/api/birds/review-items/{item['id']}/decision", headers=reviewer,
        json={"decision": "approved", "resolved_species_code": "SPECIES_X", "note": "采纳高可信证据"},
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["resolved_species_code"] == "SPECIES_X"
    detail = client.get(f"/api/birds/events/{first['id']}", headers=reviewer).json()
    assert detail["review_status"] == "approved"
    # 被否决的物种证据仍然保留
    assert {e["species_code"] for e in detail["evidence"]} == {"SPECIES_X", "SPECIES_Y"}


def test_review_rejection_does_not_delete_evidence(client, admin):
    vol = make_user(client, admin, "vol.rej", ["volunteer"])
    reviewer = make_user(client, admin, "rev.rej", ["conservationist"])
    created = client.post(
        "/api/birds/evidence", headers=vol,
        json=evidence("BIRD_Z", "2026-09-20T08:00:00+00:00", 30.20, 120.10, observer="A", client_ref="z1"),
    ).json()
    # 单条链无冲突也可由复核员发起驳回：构造一次合并产生待复核后驳回
    other = client.post(
        "/api/birds/evidence", headers=vol,
        json=evidence("BIRD_W", "2026-09-20T09:00:00+00:00", 30.201, 120.101, observer="A", client_ref="w1"),
    ).json()
    client.post("/api/birds/merge", headers=reviewer, json={"event_ids": [created["id"], other["id"]]})
    item = client.get("/api/birds/review-queue", headers=reviewer).json()[0]
    rejected = client.post(
        f"/api/birds/review-items/{item['id']}/decision", headers=reviewer,
        json={"decision": "rejected", "note": "证据不足"},
    )
    assert rejected.status_code == 200
    detail = client.get(f"/api/birds/events/{created['id']}", headers=reviewer).json()
    assert detail["review_status"] == "rejected"
    assert len(detail["evidence"]) == 2  # 驳回不删除证据
    # 已处理任务不能重复裁定
    again = client.post(
        f"/api/birds/review-items/{item['id']}/decision", headers=reviewer,
        json={"decision": "approved"},
    )
    assert again.status_code == 409


def test_withdraw_and_restore_preserves_record(client, admin):
    vol = make_user(client, admin, "vol.withdraw", ["volunteer"])
    created = client.post(
        "/api/birds/evidence", headers=vol,
        json=evidence("BIRD_R", "2026-09-20T08:00:00+00:00", 30.20, 120.10, observer="A", client_ref="r1"),
    ).json()
    evidence_id = created["evidence_id"]

    withdrawn = client.post(f"/api/birds/evidence/{evidence_id}/withdraw", headers=vol, json={"reason": "误录"})
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "withdrawn"
    # 撤销后证据仍在，但不再计入共识
    detail = client.get(f"/api/birds/events/{created['id']}", headers=vol).json()
    assert len(detail["evidence"]) == 1
    assert detail["consensus"]["consensus_species"] is None

    # 重复撤销报错
    assert client.post(f"/api/birds/evidence/{evidence_id}/withdraw", headers=vol, json={"reason": "x"}).status_code == 409

    restored = client.post(f"/api/birds/evidence/{evidence_id}/restore", headers=vol, json={"reason": "核实无误"})
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    detail = client.get(f"/api/birds/events/{created['id']}", headers=vol).json()
    assert detail["consensus"]["consensus_species"] == "BIRD_R"
    assert detail["evidence"][0]["withdraw_reason"] == "误录"  # 撤销记录被保留

    trail = client.get(f"/api/birds/evidence/{evidence_id}/audit-trail", headers=vol)
    assert trail.status_code == 200
    actions = [row["action"] for row in trail.json()]
    assert "evidence.submit" in actions
    assert "evidence.withdraw" in actions
    assert "evidence.restore" in actions


def test_role_visibility_ranges(client, admin):
    vol_a = make_user(client, admin, "vol.vis.a", ["volunteer"])
    vol_b = make_user(client, admin, "vol.vis.b", ["volunteer"])
    reviewer = make_user(client, admin, "rev.vis", ["conservationist"])
    auditor = make_user(client, admin, "aud.vis", ["auditor"])
    # 无鸟类权限的普通角色
    outsider = make_user(client, admin, "clerk.vis", ["clerk"])

    created = client.post(
        "/api/birds/evidence", headers=vol_a,
        json=evidence("BIRD_V", "2026-09-20T08:00:00+00:00", 30.20, 120.10, observer="A", client_ref="v1"),
    ).json()
    event_id = created["id"]

    # 提交者可见
    assert client.get(f"/api/birds/events/{event_id}", headers=vol_a).status_code == 200
    # 其他志愿者不可见
    assert client.get(f"/api/birds/events/{event_id}", headers=vol_b).status_code == 403
    # 复核员可见
    assert client.get(f"/api/birds/events/{event_id}", headers=reviewer).status_code == 200
    # 审计员只读可见
    assert client.get(f"/api/birds/events/{event_id}", headers=auditor).status_code == 200
    # 审计员不能提交/复核
    assert client.post(
        "/api/birds/evidence", headers=auditor,
        json=evidence("BIRD_V", "2026-09-20T08:00:00+00:00", 30.2, 120.1, observer="X", client_ref="aud1"),
    ).status_code == 403
    assert client.get("/api/birds/review-queue", headers=auditor).status_code == 403
    # 无权限角色
    assert client.get(f"/api/birds/events/{event_id}", headers=outsider).status_code == 403

    # 列表可见范围：志愿者只见自己，复核员见全部
    vol_list = client.get("/api/birds/events", headers=vol_b).json()
    assert vol_list["total"] == 0
    review_list = client.get("/api/birds/events", headers=reviewer).json()
    assert review_list["total"] >= 1

    # 志愿者不能访问复核队列
    assert client.get("/api/birds/review-queue", headers=vol_a).status_code == 403


def test_volunteer_cannot_merge_foreign_event(client, admin):
    vol_a = make_user(client, admin, "vol.mf.a", ["volunteer"])
    vol_b = make_user(client, admin, "vol.mf.b", ["volunteer"])
    e1 = client.post("/api/birds/evidence", headers=vol_a,
                     json=evidence("BIRD_1", "2026-09-20T08:00:00+00:00", 30.2, 120.1, observer="A", client_ref="mf1")).json()
    e2 = client.post("/api/birds/evidence", headers=vol_b,
                     json=evidence("BIRD_2", "2026-09-20T09:00:00+00:00", 30.2, 120.1, observer="B", client_ref="mf2")).json()
    # A 尝试合并不含自己证据的 B 事件链（只含 B 的那条）
    response = client.post("/api/birds/merge", headers=vol_a, json={"event_ids": [e1["id"], e2["id"]]})
    assert response.status_code == 403


def test_rejected_link_not_suggested_again(client, admin):
    vol_a = make_user(client, admin, "vol.rl.a", ["volunteer"])
    vol_b = make_user(client, admin, "vol.rl.b", ["volunteer"])
    e1 = client.post("/api/birds/evidence", headers=vol_a,
                     json=evidence("BIRD_Q", "2026-09-20T08:00:00+00:00", 30.20, 120.10, observer="A", client_ref="rl1")).json()
    e2 = client.post("/api/birds/evidence", headers=vol_b,
                     json=evidence("BIRD_Q", "2026-09-20T09:00:00+00:00", 30.205, 120.105, observer="B", client_ref="rl2")).json()
    links = client.post(f"/api/birds/events/{e1['id']}/candidate-links?time_window_hours=6&distance_km=2",
                        headers=vol_a).json()
    assert len(links) == 1
    client.post(f"/api/birds/links/{links[0]['id']}/reject", headers=vol_a, json={"reason": "不是同一只"})
    again = client.post(f"/api/birds/events/{e1['id']}/candidate-links?time_window_hours=6&distance_km=2",
                        headers=vol_a).json()
    assert again == []
