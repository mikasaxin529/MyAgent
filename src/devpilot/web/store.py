"""会话持久化存储（SQLite，WAL 模式）。

为什么是 SQLite 而不是 Postgres/Mongo：
    - DevPilot 是单机单进程部署（docker compose 一个 app 容器），没有多写者；
    - SQLite WAL 在这个规模（单用户～几十会话）下读写 <1ms，零运维零网络；
    - 业内同类（Letta/Mem0 自托管起步）也均为嵌入式优先，等真有多实例
      才值得上 Postgres——到时候表结构直接搬，代码只换 driver。

数据分层（对齐业内 agent 记忆分层，MemGPT/Letta 范式）：
    sessions        会话元数据（标题、agent、active 指针）
    messages        会话上下文（短期记忆：完整消息轨迹，含 steps/files 元数据）
    summaries       中期记忆：每 N 轮 / 每次压缩产生的会话摘要（滚动窗口外的内容）
    facts           长期记忆：跨会话用户事实/偏好（抽取自对话，全部会话共享）

写入策略：
    - messages 整段 upsert（前端流结束后一次性落盘），流中不写库——
      SSE 断连由前端 finally 兜底落盘，与 localStorage 时代行为一致。
    - facts 只追加（append-only），读取时按 updated_at 倒序取最近 N 条。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# 数据库位置：优先 DEVPILOT_DATA_DIR（Docker 里 pip 非 editable 安装时
# __file__ 在 site-packages，仓库相对路径推断会指向只读区），
# 否则项目根/.devpilot/store.db（.devpilot 已 gitignore，含对话内容不入库）。
_DB_PATH = (Path(os.environ.get("DEVPILOT_DATA_DIR", ""))
            if os.environ.get("DEVPILOT_DATA_DIR")
            else Path(__file__).resolve().parents[3] / ".devpilot") / "store.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '新对话',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS summaries (
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,          -- 覆盖到的消息范围上界（不含）
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fact        TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',   -- 产生该事实的 session id
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_time ON facts(created_at DESC);
"""

# 单连接 + 锁：uvicorn 是单进程多协程，sqlite3 check_same_thread=False +
# threading.Lock 足够；比连接池简单且无跨连接事务歧义。
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


# ----------------------------------------------------------------------
# 会话 CRUD
# ----------------------------------------------------------------------
def list_sessions(agent_id: str | None = None) -> list[dict[str, Any]]:
    """列出会话摘要（不含消息体，列表页够用）。agent_id 为空列全部。"""
    with _lock, _connect() as c:
        if agent_id:
            rows = c.execute(
                "SELECT id, agent_id, title, created_at, updated_at FROM sessions "
                "WHERE agent_id=? ORDER BY updated_at DESC",
                (agent_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, agent_id, title, created_at, updated_at FROM sessions "
                "ORDER BY updated_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_session(session_id: str) -> dict[str, Any] | None:
    """取整条会话（元数据 + 消息数组）。"""
    with _lock, _connect() as c:
        row = c.execute(
            "SELECT id, agent_id, title, created_at, updated_at FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        msgs = c.execute(
            "SELECT role, content, payload FROM messages WHERE session_id=? ORDER BY seq",
            (session_id,),
        ).fetchall()
    out = dict(row)
    out["messages"] = [json.loads(m["payload"]) for m in msgs]
    return out


def upsert_session(session_id: str, agent_id: str, title: str,
                   messages: list[dict[str, Any]]) -> None:
    """整段 upsert：先 DELETE 再 INSERT，避免逐条 diff。

    前端流结束时一次性落全量消息（与 localStorage 持久化节奏一致），
    单会话消息数在百级，整段重写开销可忽略。
    """
    now = time.time()
    with _lock, _connect() as c:
        c.execute(
            "INSERT INTO sessions(id, agent_id, title, created_at, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "title=excluded.title, updated_at=excluded.updated_at",
            (session_id, agent_id, title, now, now),
        )
        c.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        c.executemany(
            "INSERT INTO messages(session_id, seq, role, content, payload) VALUES(?,?,?,?,?)",
            [
                (session_id, i, m.get("role", "user"),
                 str(m.get("content", "")), json.dumps(m, ensure_ascii=False))
                for i, m in enumerate(messages)
            ],
        )


def delete_session(session_id: str) -> bool:
    """删除会话（消息/摘要级联）。返回是否真的删了。"""
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    return cur.rowcount > 0


# ----------------------------------------------------------------------
# 中期记忆：会话摘要
# ----------------------------------------------------------------------
def save_summary(session_id: str, upto_seq: int, content: str) -> None:
    """存一段会话摘要（滚动窗口外的历史压缩结果）。同 seq 覆盖。

    摘要可能在会话行落库前先到（compress_memory 在图尾部跑，前端流结束才
    PUT /api/sessions）——所以这里兜底先插一条占位会话行（ON CONFLICT 无操作），
    保证外键成立；前端随后 upsert 会补全标题与消息。
    """
    with _lock, _connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO sessions(id, agent_id, title, created_at, updated_at) "
            "VALUES(?,'unknown','新对话',?,?)",
            (session_id, time.time(), time.time()),
        )
        c.execute(
            "INSERT INTO summaries(session_id, seq, content, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(session_id, seq) DO UPDATE SET "
            "content=excluded.content, created_at=excluded.created_at",
            (session_id, upto_seq, content, time.time()),
        )


def latest_summary(session_id: str) -> dict[str, Any] | None:
    """取该会话最新一段摘要（供重建上下文时前置）。"""
    with _lock, _connect() as c:
        row = c.execute(
            "SELECT seq, content, created_at FROM summaries WHERE session_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


# ----------------------------------------------------------------------
# 长期记忆：跨会话用户事实
# ----------------------------------------------------------------------
def add_facts(facts: list[str], source: str = "") -> None:
    """追加一批长期事实（extract_memory 抽取结果）。"""
    if not facts:
        return
    now = time.time()
    with _lock, _connect() as c:
        c.executemany(
            "INSERT INTO facts(fact, source, created_at) VALUES(?,?,?)",
            [(f, source, now) for f in facts],
        )


def recent_facts(limit: int = 30) -> list[dict[str, Any]]:
    """最近 N 条长期事实（注入 system prompt 的长期记忆层）。"""
    with _lock, _connect() as c:
        rows = c.execute(
            "SELECT id, fact, source, created_at FROM facts "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
