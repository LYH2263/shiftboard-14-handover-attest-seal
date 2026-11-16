from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from app import keys
from app.db import connect, rows_to_dicts


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_shifts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM shifts ORDER BY id").fetchall())


def list_batches(owner_shift_id: int | None = None) -> list[dict]:
    with connect() as conn:
        if owner_shift_id is None:
            rows = conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                ORDER BY b.id
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                WHERE b.owner_shift_id = ?
                ORDER BY b.id
                """,
                (owner_shift_id,),
            ).fetchall()
        return rows_to_dicts(rows)


def list_handovers() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT h.*,
                   fs.name AS from_shift_name,
                   ts.name AS to_shift_name,
                   (SELECT COUNT(*) FROM handover_attestations a
                    WHERE a.handover_id = h.id) AS attest_count
            FROM handovers h
            JOIN shifts fs ON fs.id = h.from_shift_id
            JOIN shifts ts ON ts.id = h.to_shift_id
            ORDER BY h.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def get_handover(handover_id: int) -> dict:
    with connect() as conn:
        h = conn.execute(
            """
            SELECT h.*,
                   fs.name AS from_shift_name,
                   ts.name AS to_shift_name
            FROM handovers h
            JOIN shifts fs ON fs.id = h.from_shift_id
            JOIN shifts ts ON ts.id = h.to_shift_id
            WHERE h.id = ?
            """,
            (handover_id,),
        ).fetchone()
        if not h:
            raise HTTPException(404, "交班单不存在")
        items = conn.execute(
            """
            SELECT b.*
            FROM handover_items hi
            JOIN batches b ON b.id = hi.batch_id
            WHERE hi.handover_id = ?
            ORDER BY b.id
            """,
            (handover_id,),
        ).fetchall()
        attestations = conn.execute(
            """
            SELECT a.*, s.name AS shift_name, s.keeper AS keeper
            FROM handover_attestations a
            JOIN shifts s ON s.id = a.shift_id
            WHERE a.handover_id = ?
            ORDER BY a.party
            """,
            (handover_id,),
        ).fetchall()
        out = dict(h)
        out["items"] = rows_to_dicts(items)
        # 核对入口：签注双方、所签摘要、清单业务键与批次归属同源同库
        out["attestations"] = rows_to_dicts(attestations)
        out["attest_payload"] = keys.attest_payload(handover_id, out["manifest_digest"])
        return out


def dashboard() -> dict:
    with connect() as conn:
        shifts = rows_to_dicts(conn.execute("SELECT * FROM shifts ORDER BY id").fetchall())
        inflight = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE status = '在途'"
        ).fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM handovers WHERE status = '进行中'"
        ).fetchone()["c"]
        last = conn.execute(
            "SELECT * FROM handovers ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "班次数": len(shifts),
            "在途批次数": inflight,
            "进行中交班": active,
            "最近交班": dict(last) if last else None,
            "班次": shifts,
        }


def start_handover(from_shift_id: int, to_shift_id: int) -> dict:
    if from_shift_id == to_shift_id:
        raise HTTPException(400, "交班双方不能是同一班次")
    with connect() as conn:
        fs = conn.execute("SELECT * FROM shifts WHERE id = ?", (from_shift_id,)).fetchone()
        ts = conn.execute("SELECT * FROM shifts WHERE id = ?", (to_shift_id,)).fetchone()
        if not fs or not ts:
            raise HTTPException(404, "班次不存在")
        active = conn.execute(
            "SELECT id FROM handovers WHERE status = '进行中' LIMIT 1"
        ).fetchone()
        if active:
            raise HTTPException(409, f"已有进行中的交班单#{active['id']}，禁止并行交班")
        batches = conn.execute(
            "SELECT id, biz_key FROM batches WHERE owner_shift_id = ? AND status = '在途' ORDER BY id",
            (from_shift_id,),
        ).fetchall()
        if not batches:
            raise HTTPException(400, "交班方没有在途批次可交接")
        # 发起时固化清单摘要（业务键有序哈希），签注与完成过户均以它为准
        digest = keys.manifest_digest([b["biz_key"] for b in batches])
        cur = conn.execute(
            """
            INSERT INTO handovers(from_shift_id, to_shift_id, status, batch_count, manifest_digest, started_at)
            VALUES (?, ?, '进行中', ?, ?, ?)
            """,
            (from_shift_id, to_shift_id, len(batches), digest, _now()),
        )
        hid = cur.lastrowid
        for b in batches:
            conn.execute(
                "INSERT INTO handover_items(handover_id, batch_id) VALUES (?, ?)",
                (hid, b["id"]),
            )
        conn.execute("UPDATE shifts SET status = '交班中' WHERE id = ?", (from_shift_id,))
        conn.execute("UPDATE shifts SET status = '接班中' WHERE id = ?", (to_shift_id,))
        conn.commit()
    return get_handover(hid)


def attest_handover(handover_id: int, shift_id: int, signature: str) -> dict:
    """收集一方签注：仅进行中可签；签名须覆盖「交班单id+发起时清单摘要」。

    签注先于过户：完成后拒绝任何签注改动，签注与摘要落库即不可改。
    """
    with connect() as conn:
        h = conn.execute("SELECT * FROM handovers WHERE id = ?", (handover_id,)).fetchone()
        if not h:
            raise HTTPException(404, "交班单不存在")
        if h["status"] != "进行中":
            raise HTTPException(400, "只能签注进行中的交班单，完成后签注不可改")
        if shift_id == h["from_shift_id"]:
            party = "from"
        elif shift_id == h["to_shift_id"]:
            party = "to"
        else:
            raise HTTPException(400, "仅交班双方班次可签注")
        payload = keys.attest_payload(handover_id, h["manifest_digest"])
        if not keys.verify(shift_id, payload, signature):
            raise HTTPException(400, "签注验签失败：签名与班次密钥或清单摘要不匹配")
        conn.execute(
            """
            INSERT OR REPLACE INTO handover_attestations(
              handover_id, party, shift_id, digest, signature, signed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                handover_id,
                party,
                shift_id,
                h["manifest_digest"],
                signature.strip().lower(),
                _now(),
            ),
        )
        conn.commit()
    return get_handover(handover_id)


def _check_attestations(conn, h: sqlite3.Row, handover_id: int) -> None:
    """过户门禁：清单摘要未被改写 + 双方签注齐全且验签通过。失败即抛错，不产生任何写。"""
    current_keys = [
        r["biz_key"]
        for r in conn.execute(
            """
            SELECT b.biz_key FROM handover_items hi
            JOIN batches b ON b.id = hi.batch_id
            WHERE hi.handover_id = ?
            """,
            (handover_id,),
        ).fetchall()
    ]
    if keys.manifest_digest(current_keys) != h["manifest_digest"]:
        raise HTTPException(409, "清单摘要不一致：签注后交班单清单键集合已被改动，禁止过户")
    atts = {
        r["party"]: r
        for r in conn.execute(
            "SELECT * FROM handover_attestations WHERE handover_id = ?", (handover_id,)
        ).fetchall()
    }
    payload = keys.attest_payload(handover_id, h["manifest_digest"])
    for party, shift_id, label in (
        ("from", h["from_shift_id"], "交班方"),
        ("to", h["to_shift_id"], "接班方"),
    ):
        att = atts.get(party)
        if not att:
            raise HTTPException(409, f"缺少{label}签注，禁止过户")
        if att["shift_id"] != shift_id or att["digest"] != h["manifest_digest"]:
            raise HTTPException(409, f"{label}签注与发起时清单摘要不匹配，禁止过户")
        if not keys.verify(shift_id, payload, att["signature"]):
            raise HTTPException(409, f"{label}签注验签失败，禁止过户")


def complete_handover(handover_id: int) -> dict:
    with connect() as conn:
        h = conn.execute("SELECT * FROM handovers WHERE id = ?", (handover_id,)).fetchone()
        if not h:
            raise HTTPException(404, "交班单不存在")
        if h["status"] != "进行中":
            raise HTTPException(400, "只能完成进行中的交班单")
        # 同一失败闭：先校验签注与摘要（无任何写），再原子过户，同事务一次提交
        _check_attestations(conn, h, handover_id)
        items = conn.execute(
            "SELECT batch_id FROM handover_items WHERE handover_id = ?",
            (handover_id,),
        ).fetchall()
        before = len(items)
        # 交班完成：整批过户，禁止只改交班单状态不改归属
        for it in items:
            conn.execute(
                "UPDATE batches SET owner_shift_id = ? WHERE id = ? AND status = '在途'",
                (h["to_shift_id"], it["batch_id"]),
            )
        after_from = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE owner_shift_id = ? AND status = '在途'",
            (h["from_shift_id"],),
        ).fetchone()["c"]
        after_to = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE owner_shift_id = ? AND status = '在途'",
            (h["to_shift_id"],),
        ).fetchone()["c"]
        consistent = 1 if after_from == 0 and after_to >= before else 0
        detail = {
            "交班前在途数": before,
            "交班后交班方在途": after_from,
            "交班后接班方在途": after_to,
            "清单摘要": h["manifest_digest"],
            "签注": {"交班方班次": h["from_shift_id"], "接班方班次": h["to_shift_id"]},
        }
        conn.execute(
            """
            UPDATE handovers
            SET status = ?, finished_at = ?, batch_count = ?
            WHERE id = ?
            """,
            ("已完成" if consistent else "失败", _now(), before, handover_id),
        )
        if consistent:
            conn.execute("UPDATE shifts SET status = '已交班' WHERE id = ?", (h["from_shift_id"],))
            conn.execute("UPDATE shifts SET status = '值班中' WHERE id = ?", (h["to_shift_id"],))
        else:
            conn.execute(
                "UPDATE handovers SET reason = ? WHERE id = ?",
                ("交班后批次归属与清单不一致", handover_id),
            )
            conn.execute("UPDATE shifts SET status = '值班中' WHERE id = ?", (h["from_shift_id"],))
            conn.execute("UPDATE shifts SET status = '空闲' WHERE id = ?", (h["to_shift_id"],))
        conn.execute(
            """
            INSERT INTO reconcile_reports(
              handover_id, before_count, after_from_count, after_to_count,
              consistent, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                handover_id,
                before,
                after_from,
                after_to,
                consistent,
                json.dumps(detail, ensure_ascii=False),
                _now(),
            ),
        )
        conn.commit()
    return get_handover(handover_id)


def list_reconcile() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            d["一致"] = bool(d.pop("consistent"))
            out.append(d)
        return out
