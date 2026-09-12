from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


@dataclass(slots=True)
class Operation:
    id: str
    created_at: str
    type: str
    amount: float
    coin: int
    customer_card: str = ""
    authentication: str = ""
    trade: str = ""
    invoice: str = ""
    contact_less: str = ""
    base_request: str = ""
    signature: str = ""
    terminal: str = ""
    result: str = ""
    response_code: str = ""
    request: str = ""
    status: str = ""
    commerce_card: str = ""
    message: str = ""
    description: str = ""
    raw_xml: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class OperationRequest:
    type: str
    amount: float = 0.0
    coin: int = 978
    request: str = ""
    terminal: str = ""
    trade: str = ""
    invoice: str = ""
    base_request: str = ""
    customer_card: str = ""
    commerce_card: str = ""
    contact_less: str = "S"
    authentication: str = ""
    signature: str = ""

    @classmethod
    def from_mapping(cls, raw: dict[str, object]) -> "OperationRequest":
        operation_type = str(
            raw.get("type")
            or raw.get("operation")
            or raw.get("accion")
            or raw.get("action")
            or raw.get("op")
            or ""
        ).strip()
        return cls(
            type=operation_type.upper(),
            amount=_as_amount(raw.get("amount") or raw.get("importe") or raw.get("total")),
            coin=_as_int(raw.get("coin") or raw.get("currency") or raw.get("moneda"), 978),
            request=str(raw.get("request") or raw.get("order") or raw.get("pedido") or "").strip(),
            terminal=str(raw.get("terminal") or "").strip(),
            trade=str(raw.get("trade") or raw.get("merchant_code") or raw.get("comercio") or "").strip(),
            invoice=str(raw.get("invoice") or raw.get("factura") or "").strip(),
            base_request=str(raw.get("base_request") or raw.get("baseRequest") or "").strip(),
            customer_card=str(raw.get("customer_card") or raw.get("customerCard") or "").strip(),
            commerce_card=str(raw.get("commerce_card") or raw.get("commerceCard") or "").strip(),
            contact_less=str(raw.get("contact_less") or raw.get("contactLess") or "S").strip(),
            authentication=str(raw.get("authentication") or "").strip(),
            signature=str(raw.get("signature") or "").strip(),
        )


def build_operation(
    request: OperationRequest,
    *,
    terminal: str,
    response_code: str,
    result: str,
    status: str,
    message: str,
    description: str = "",
    authentication: str = "",
    customer_card: str = "",
    commerce_card: str = "",
    raw_xml: str = "",
) -> Operation:
    return Operation(
        id=uuid4().hex,
        created_at=now_iso(),
        type=request.type,
        amount=float(_round_amount(request.amount)),
        coin=request.coin,
        customer_card=customer_card or request.customer_card,
        authentication=authentication or request.authentication,
        trade=request.trade,
        invoice=request.invoice or request.request,
        contact_less=request.contact_less,
        base_request=request.base_request,
        signature=request.signature,
        terminal=terminal or request.terminal,
        result=result,
        response_code=response_code,
        request=request.request,
        status=status,
        commerce_card=commerce_card or request.commerce_card,
        message=message,
        description=description,
        raw_xml=raw_xml,
    )


def _round_amount(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _as_int(value: object, default: int) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    return int(text) if text.isdigit() else default


def _as_amount(value: object) -> float:
    text = str(value or "").strip().replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0
