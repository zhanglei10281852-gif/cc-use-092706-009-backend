"""候鸟观测链模块的 SQLite 结构与种子数据。"""
from __future__ import annotations

SCHEMA = """
-- 观测事件：一次去重后的“同一只候鸟”观察链
CREATE TABLE IF NOT EXISTS bird_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    species_code TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    -- 代表位置（时间加权），随证据变化
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    -- 物种判定状态：conflicted 时绝不自动选取某个物种
    species_status TEXT NOT NULL DEFAULT 'agreed'
        CHECK(species_status IN ('agreed','conflicted')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','pending_review','approved','rejected','revoked','merged')),
    -- 幂等：相同提交指纹直接返回同一事件
    dedupe_fingerprint TEXT NOT NULL UNIQUE,
    -- 合并历史：被并入本事件的其他事件 id 列表
    merged_from_json TEXT NOT NULL DEFAULT '[]',
    review_note TEXT NOT NULL DEFAULT '',
    reviewer TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_events_time ON bird_events(first_seen_at, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_bird_events_status ON bird_events(status, species_status);

-- 证据附件摘要：每条原始证据都保留，绝不覆盖
CREATE TABLE IF NOT EXISTS bird_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES bird_events(id) ON DELETE CASCADE,
    -- 认证提交账号，用于权限与可见范围
    submitted_by TEXT NOT NULL,
    observer TEXT NOT NULL,
    observer_role TEXT NOT NULL DEFAULT 'volunteer',
    observed_at TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    species_code TEXT NOT NULL,
    -- 来源可信度（0~1），低可信不丢弃证据，只降低结论权重
    confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence BETWEEN 0 AND 1),
    source_type TEXT NOT NULL DEFAULT 'field_note',
    attachment_kind TEXT NOT NULL DEFAULT '',
    attachment_name TEXT NOT NULL DEFAULT '',
    attachment_sha256 TEXT NOT NULL DEFAULT '',
    attachment_size INTEGER,
    note TEXT NOT NULL DEFAULT '',
    -- 幂等去重键：相同提交直接返回同一事件
    dedupe_key TEXT NOT NULL UNIQUE,
    -- 撤销（撤回）后软删除，可恢复
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
    revoked_at TEXT,
    revoke_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_evidence_event ON bird_evidence(event_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_bird_evidence_species ON bird_evidence(species_code, observed_at);

-- 候选关联：时间/空间容差下生成，等待复核
CREATE TABLE IF NOT EXISTS bird_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_a_id INTEGER NOT NULL REFERENCES bird_events(id) ON DELETE CASCADE,
    event_b_id INTEGER NOT NULL REFERENCES bird_events(id) ON DELETE CASCADE,
    time_gap_seconds INTEGER NOT NULL,
    distance_m INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK(status IN ('candidate','accepted','rejected','merged')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT NOT NULL DEFAULT '',
    UNIQUE(event_a_id, event_b_id)
);
CREATE INDEX IF NOT EXISTS idx_bird_links_status ON bird_links(status);

-- 人工复核队列
CREATE TABLE IF NOT EXISTS bird_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES bird_events(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'link' CHECK(kind IN ('merge','conflict','revival','link')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','approved','rejected')),
    reason TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    submitted_by TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_reviews_queue ON bird_reviews(status, created_at);

-- 不可变审计日志：合并、撤销、恢复、复核全部留痕
CREATE TABLE IF NOT EXISTS bird_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def ensure_schema(connection) -> None:
    connection.executescript(SCHEMA)
