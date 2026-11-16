from pathlib import Path
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DATA_DIR"] = str(ROOT / "backend" / "data_test")
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

db_path = Path(os.environ["DATA_DIR"]) / "app.db"
if db_path.exists():
    db_path.unlink()

from app.main import app  # noqa: E402
from app import keys  # noqa: E402
from app.db import connect  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _attest(client, hid, shift_id):
    """用本地夹具密钥对「交班单id+清单摘要」签名并提交签注。"""
    payload = client.get(f"/api/handovers/{hid}").json()["attest_payload"]
    sig = keys.sign(shift_id, payload)
    return client.post(
        f"/api/handovers/{hid}/attest",
        json={"shift_id": shift_id, "signature": sig},
    )


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["项目"] == "交班台"


def test_handover_and_reconcile(client):
    shifts = client.get("/api/shifts").json()
    assert len(shifts) >= 2
    batches = client.get("/api/batches", params={"owner_shift_id": 1}).json()
    assert any(b["status"] == "在途" for b in batches)

    started = client.post(
        "/api/handovers/start",
        json={"from_shift_id": 1, "to_shift_id": 2},
    )
    assert started.status_code == 200, started.text
    hid = started.json()["id"]
    assert started.json()["status"] == "进行中"
    assert started.json()["manifest_digest"]

    # 并行交班应被拒绝
    again = client.post(
        "/api/handovers/start",
        json={"from_shift_id": 1, "to_shift_id": 3},
    )
    assert again.status_code == 409

    # 双方签注后方可完成过户
    assert _attest(client, hid, 1).status_code == 200
    assert _attest(client, hid, 2).status_code == 200
    done = client.post(f"/api/handovers/{hid}/complete")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "已完成"

    after = client.get("/api/batches", params={"owner_shift_id": 1}).json()
    assert all(b["status"] != "在途" or b["owner_shift_id"] != 1 for b in after) or not any(
        b["status"] == "在途" for b in after
    )
    to_batches = client.get("/api/batches", params={"owner_shift_id": 2}).json()
    assert sum(1 for b in to_batches if b["status"] == "在途") >= 4

    reports = client.get("/api/reconcile").json()
    assert reports
    assert reports[0]["一致"] is True
    assert reports[0]["detail"]["清单摘要"] == started.json()["manifest_digest"]


def test_complete_requires_both_attestations(client):
    # 上一测试后：班次2持有全部在途批次
    started = client.post(
        "/api/handovers/start",
        json={"from_shift_id": 2, "to_shift_id": 3},
    )
    assert started.status_code == 200, started.text
    hid = started.json()["id"]

    # 无任何签注直接完成 → 拒绝，不过户、不标成功
    r = client.post(f"/api/handovers/{hid}/complete")
    assert r.status_code == 409
    assert "签注" in r.json()["detail"]
    h = client.get(f"/api/handovers/{hid}").json()
    assert h["status"] == "进行中"
    owned = client.get("/api/batches", params={"owner_shift_id": 2}).json()
    assert sum(1 for b in owned if b["status"] == "在途") >= 4

    # 仅交班方签注 → 仍拒绝（缺接班方）
    assert _attest(client, hid, 2).status_code == 200
    r = client.post(f"/api/handovers/{hid}/complete")
    assert r.status_code == 409
    assert "接班方" in r.json()["detail"]

    # 非交班双方班次不能签注
    r = client.post(
        f"/api/handovers/{hid}/attest",
        json={"shift_id": 1, "signature": "ab" * 32},
    )
    assert r.status_code == 400

    # 伪造签名 → 验签失败
    r = client.post(
        f"/api/handovers/{hid}/attest",
        json={"shift_id": 3, "signature": "00" * 32},
    )
    assert r.status_code == 400
    assert "验签" in r.json()["detail"]

    # 接班方签注后完成过户
    assert _attest(client, hid, 3).status_code == 200
    done = client.post(f"/api/handovers/{hid}/complete")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "已完成"

    # 完成后签注不可改、不可补签
    assert _attest(client, hid, 2).status_code == 400

    # 交班记录可核对签注双方、摘要与清单（与批次归属同源）
    h = client.get(f"/api/handovers/{hid}").json()
    assert h["manifest_digest"]
    assert keys.manifest_digest([i["biz_key"] for i in h["items"]]) == h["manifest_digest"]
    parties = {a["party"]: a for a in h["attestations"]}
    assert parties["from"]["shift_id"] == 2
    assert parties["to"]["shift_id"] == 3
    assert all(a["digest"] == h["manifest_digest"] for a in h["attestations"])
    assert keys.verify(2, h["attest_payload"], parties["from"]["signature"])
    assert keys.verify(3, h["attest_payload"], parties["to"]["signature"])

    logs = client.get("/api/handovers").json()
    row = next(x for x in logs if x["id"] == hid)
    assert row["attest_count"] == 2
    assert row["manifest_digest"] == h["manifest_digest"]


def test_manifest_tamper_after_attest_rejected(client):
    # 上一测试后：班次3持有全部在途批次
    started = client.post(
        "/api/handovers/start",
        json={"from_shift_id": 3, "to_shift_id": 1},
    )
    assert started.status_code == 200, started.text
    hid = started.json()["id"]
    assert _attest(client, hid, 3).status_code == 200
    assert _attest(client, hid, 1).status_code == 200

    # 签注后篡改清单键集合（直接改库，模拟事后改写）
    with connect() as conn:
        row = conn.execute(
            "SELECT batch_id FROM handover_items WHERE handover_id = ? LIMIT 1",
            (hid,),
        ).fetchone()
        conn.execute(
            "DELETE FROM handover_items WHERE handover_id = ? AND batch_id = ?",
            (hid, row["batch_id"]),
        )
        conn.commit()

    # 完成时必须拒绝并提示摘要不一致，不得用新清单过户却沿用旧签注
    r = client.post(f"/api/handovers/{hid}/complete")
    assert r.status_code == 409
    assert "摘要" in r.json()["detail"]
    h = client.get(f"/api/handovers/{hid}").json()
    assert h["status"] == "进行中"
    owned = client.get("/api/batches", params={"owner_shift_id": 3}).json()
    assert sum(1 for b in owned if b["status"] == "在途") >= 4

    # 恢复清单后签注仍有效，可正常完成
    with connect() as conn:
        conn.execute(
            "INSERT INTO handover_items(handover_id, batch_id) VALUES (?, ?)",
            (hid, row["batch_id"]),
        )
        conn.commit()
    done = client.post(f"/api/handovers/{hid}/complete")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "已完成"
    owned = client.get("/api/batches", params={"owner_shift_id": 1}).json()
    assert sum(1 for b in owned if b["status"] == "在途") >= 4
