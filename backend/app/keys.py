"""本地夹具密钥签名：HMAC-SHA256，密钥仅来自 fixtures/keys.json，不接外网证书服务。"""
from __future__ import annotations

import hashlib
import hmac
import json
from functools import lru_cache

from app.db import FIXTURES_DIR


@lru_cache(maxsize=1)
def _keys() -> dict[int, dict]:
    path = FIXTURES_DIR / "keys.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(k["shift_id"]): k for k in data["shifts"]}


def manifest_digest(biz_keys: list[str]) -> str:
    """清单业务键有序摘要：业务键排序后拼接取 SHA-256。"""
    ordered = "\n".join(sorted(biz_keys))
    return hashlib.sha256(ordered.encode("utf-8")).hexdigest()


def attest_payload(handover_id: int, digest: str) -> str:
    """签注载荷：交班单 id + 清单摘要；双方对同一载荷各用各的密钥签名。"""
    return f"handover:{handover_id}:manifest:{digest}"


def sign(shift_id: int, payload: str) -> str:
    key = _keys().get(shift_id)
    if not key:
        raise KeyError(f"班次{shift_id}无本地夹具密钥")
    return hmac.new(
        key["secret"].encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify(shift_id: int, payload: str, signature: str) -> bool:
    try:
        expect = sign(shift_id, payload)
    except KeyError:
        return False
    return hmac.compare_digest(expect, (signature or "").strip().lower())
