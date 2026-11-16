from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import handover as svc

router = APIRouter(prefix="/handovers", tags=["交班"])


class StartBody(BaseModel):
    from_shift_id: int = Field(..., description="交班班次")
    to_shift_id: int = Field(..., description="接班班次")


class AttestBody(BaseModel):
    shift_id: int = Field(..., description="签注班次（须为交班双方之一）")
    signature: str = Field(..., description="对「交班单id+清单摘要」的本地夹具密钥签名")


@router.get("")
def get_handovers():
    return svc.list_handovers()


@router.get("/{handover_id}")
def get_one(handover_id: int):
    return svc.get_handover(handover_id)


@router.post("/start")
def start(body: StartBody):
    return svc.start_handover(body.from_shift_id, body.to_shift_id)


@router.post("/{handover_id}/attest")
def attest(handover_id: int, body: AttestBody):
    return svc.attest_handover(handover_id, body.shift_id, body.signature)


@router.post("/{handover_id}/complete")
def complete(handover_id: int):
    return svc.complete_handover(handover_id)
