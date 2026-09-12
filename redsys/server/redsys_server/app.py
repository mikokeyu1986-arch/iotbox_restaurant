from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import RedsysServerConfig
from .gateway import RealRedsysBridgeGateway, SimulatedRedsysGateway
from .models import Operation, OperationRequest
from .odoo_client import OdooSyncClient
from .storage import OperationStore


class RedsysService:
    def __init__(self, config: RedsysServerConfig, base_dir: Path):
        self.config = config
        self.base_dir = base_dir
        self.store = OperationStore(Path(config.storage_path))
        self.gateway = (
            SimulatedRedsysGateway(config, self.store)
            if config.simulate
            else RealRedsysBridgeGateway(config, self.store)
        )
        self.odoo_client = OdooSyncClient(config.odoo_create_url)

    def health(self) -> dict[str, Any]:
        latest = self.store.latest()
        return {
            "service": "redsys-local-server",
            "simulate": self.config.simulate,
            "host": self.config.host,
            "port": self.config.port,
            "merchant_code": self.config.merchant_code,
            "merchant_terminal": self.config.merchant_terminal,
            "latest_operation_id": latest.id if latest else "",
        }

    def consulta(self, operation_id: str = "") -> bytes:
        if operation_id:
            operation = self.store.find(operation_id)
            operations = [operation] if operation else []
        else:
            operations = self.store.list()[-100:]
        return _consulta_xml(operations)

    def consulta_legacy(self) -> bytes:
        operations = self.store.list()
        return _consulta_legacy_xml(operations, self.config.merchant_code)

    def consulta_dataphone(self, params: dict[str, str]) -> bytes:
        """Query the datáfono records via _OperConsulta and return its raw XML."""
        invoice = params.get("invoice") or params.get("id") or ""
        request = OperationRequest(
            type="CONSULTA",
            invoice=invoice,
            request=params.get("order", ""),
            authentication=params.get("rts", ""),
        )
        response = self.gateway.consulta(request, params.get("page", "0") or "0")
        xml = (response.xml or "").strip()
        if xml:
            return xml.encode("utf-8")
        return _consulta_bridge_error_xml(response.error)

    def operate(self, raw_payload: dict[str, Any]) -> tuple[Operation, bool, str]:
        request = OperationRequest.from_mapping(raw_payload)
        operation, success = self.gateway.process(request)
        if operation is None:
            raise RuntimeError("Operation processing returned no result.")
        pushed, push_error = self.odoo_client.push(operation)
        if not pushed:
            push_error = f"Odoo sync skipped: {push_error}"
        return operation, success, push_error

    def connect(self) -> dict[str, Any]:
        if not hasattr(self.gateway, "connect"):
            return {
                "connected": True,
                "simulate": True,
                "message": "Simulated gateway does not require explicit connect.",
            }
        status = self.gateway.connect()
        status["simulate"] = self.config.simulate
        return status


def create_server(config: RedsysServerConfig, base_dir: Path) -> ThreadingHTTPServer:
    service = RedsysService(config, base_dir)

    class RedsysRequestHandler(BaseHTTPRequestHandler):
        server_version = "RedsysLocal/2.0"

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self._set_common_headers()
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path or "/"
            path_lower = path.lower()
            query = parse_qs(parsed.query, keep_blank_values=True)

            if path_lower in {"/health", "/api/health"}:
                self._send_json(service.health())
                return
            if path_lower in {"/connect", "/connect/"}:
                try:
                    self._send_json(service.connect())
                except Exception as exc:
                    self._send_json({"connected": False, "error": f"Redsys bridge error: {exc}"}, status=HTTPStatus.BAD_GATEWAY)
                return
            if path_lower in {"/favicon.ico", "/img/logo.ico"}:
                self._send_favicon()
                return
            if path_lower in {"/", "/ui", "/ui/"} and not query:
                self._send_html(_payment_ui_html(service.config))
                return
            if path_lower in {"/consulta", "/consulta/"} or path_lower.startswith("/consulta/"):
                suffix = path_lower.removeprefix("/consulta").strip("/")
                params = {
                    "id": suffix or _first(query, "id"),
                    "invoice": _first(query, "invoice") or _first(query, "factura"),
                    "order": _first(query, "pedido") or _first(query, "order"),
                    "rts": _first(query, "rts") or _first(query, "authentication"),
                    "page": _first(query, "page") or _first(query, "pagina"),
                }
                self._send_xml(service.consulta_dataphone(params))
                return
            if path_lower in {"/operacion", "/operacion/"} and not query:
                self._send_xml(_legacy_parameter_error_xml())
                return
            if path_lower.startswith("/operacion/"):
                payload = _payload_from_legacy_path(path)
                if payload is None:
                    self._send_xml(_legacy_parameter_error_xml())
                    return
                operation, success, push_error = service.operate(payload)
                self._send_xml(_operation_xml(operation, success, push_error))
                return
            if path_lower in {"/", "/operacion", "/operacion/"} and query:
                payload = _flatten_query_dict(query)
                operation, success, push_error = service.operate(payload)
                self._send_xml(_operation_xml(operation, success, push_error))
                return

            self._send_json({"error": f"Unsupported path: {path}"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path or "/"
            payload = self._read_payload()

            if path in {"/pago", "/pay"}:
                payload["type"] = payload.get("type") or "PAGO"
            elif path in {"/cancel", "/anular"}:
                payload["type"] = payload.get("type") or "CANCELADA"
            elif path in {"/refund", "/devolucion"}:
                payload["type"] = payload.get("type") or "REFUND"

            if path in {"/connect", "/connect/"}:
                try:
                    self._send_json(service.connect())
                except Exception as exc:
                    self._send_json({"connected": False, "error": f"Redsys bridge error: {exc}"}, status=HTTPStatus.BAD_GATEWAY)
                return

            if path in {"/api/operations", "/operacion", "/operacion/", "/pago", "/pay", "/cancel", "/anular", "/refund", "/devolucion"}:
                operation, success, push_error = service.operate(payload)
                self._send_json(
                    {
                        "success": success,
                        "warning": push_error,
                        "operation": operation.to_dict(),
                    },
                    status=HTTPStatus.OK if success else HTTPStatus.BAD_REQUEST,
                )
                return

            self._send_json({"error": f"Unsupported path: {path}"}, status=HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _read_payload(self) -> dict[str, Any]:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""
            content_type = self.headers.get("Content-Type", "")
            if "application/json" in content_type:
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                except json.JSONDecodeError:
                    return {}
                return payload if isinstance(payload, dict) else {}
            if "application/x-www-form-urlencoded" in content_type:
                return _flatten_query_dict(parse_qs(raw_body.decode("utf-8"), keep_blank_values=True))
            return {}

        def _set_common_headers(self, content_type: str = "application/json; charset=utf-8") -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", config.allow_origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Cache-Control", "no-store")

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8")
            self.send_response(status)
            self._set_common_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_xml(self, payload: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self._set_common_headers("application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_html(self, payload: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = payload.encode("utf-8")
            self.send_response(status)
            self._set_common_headers("text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_favicon(self) -> None:
            favicon_path = base_dir.parent / "img" / "logo.ico"
            if not favicon_path.exists():
                self.send_response(HTTPStatus.NOT_FOUND)
                self.end_headers()
                return
            data = favicon_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._set_common_headers("image/x-icon")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer((config.host, config.port), RedsysRequestHandler)


def _payment_ui_html(config: RedsysServerConfig) -> str:
    """Small local operator screen for testing/using the Redsys bridge."""
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Redsys Datáfono</title>
<style>
body{{font-family:Arial,sans-serif;background:#f3f4f6;margin:0;padding:32px;color:#172033}}
main{{max-width:520px;margin:auto;background:white;border-radius:14px;padding:28px;box-shadow:0 8px 30px #0002}}
h1{{margin-top:0}} label{{display:block;font-weight:600;margin:18px 0 6px}}
input{{width:100%;box-sizing:border-box;padding:13px;font-size:22px;border:1px solid #cbd5e1;border-radius:8px}}
button{{padding:12px 18px;border:0;border-radius:8px;font-size:16px;cursor:pointer;margin:18px 8px 0 0}}
.pay{{background:#1167d8;color:white}} .test{{background:#0f766e;color:white}} .cancel{{background:#e5e7eb}} #status{{margin-top:22px;padding:12px;background:#eef2ff;border-radius:8px;white-space:pre-wrap}}
.meta{{color:#64748b;font-size:14px}}
</style></head><body><main>
<h1>REDSYS · Pago con tarjeta</h1><div class="meta">Terminal {config.merchant_terminal} · Servicio {config.host}:{config.port}</div>
<label for="amount">Importe (€)</label><input id="amount" inputmode="decimal" value="0.01" autocomplete="off">
<label for="order">Pedido / referencia</label><input id="order" value="POS-" autocomplete="off">
<button class="test" onclick="testReader()">测试读卡器</button><button class="pay" onclick="pay()">Iniciar刷卡</button><button class="cancel" onclick="cancelPay()">Cancelar</button>
<div id="status">Listo para iniciar una operación.</div>
<script>
const statusBox=document.getElementById('status');
function setStatus(v){{statusBox.textContent=v;}}
async function testReader(){{
  setStatus('正在测试 COM 连接…');
  try{{
    const r=await fetch('/connect'); const result=await r.json();
    if(result.connected){{
      setStatus('读卡器连接成功。COM 端口和 REDSYS 桥接均正常。');
    }} else {{
      setStatus('读卡器连接失败：'+(result.error||'未知错误'));
    }}
  }} catch(e){{setStatus('无法测试读卡器：'+e.message);}}
}}
async function pay(){{
  const raw=document.getElementById('amount').value.replace(',','.');
  const amount=Math.round(Number(raw)*100); const order=document.getElementById('order').value.trim()||('POS-'+Date.now());
  if(!Number.isFinite(amount)||amount<=0){{setStatus('Importe no válido.');return;}}
  setStatus('Esperando tarjeta…');
  try{{const r=await fetch('/operacion/'+amount+'/'+encodeURIComponent(order)); const text=await r.text(); setStatus(r.ok?'Respuesta Redsys:\n'+text:'Error Redsys:\n'+text);}}
  catch(e){{setStatus('No se pudo conectar con Redsys: '+e.message);}}
}}
function cancelPay(){{setStatus('Operación cancelada por el operador.');}}
</script></main></body></html>"""


def _flatten_query_dict(query: dict[str, list[str]]) -> dict[str, str]:
    return {key: values[-1] if values else "" for key, values in query.items()}


def _first(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[-1] if values else ""


def _consulta_xml(operations: list[Operation | None]) -> bytes:
    parts = ["<Operaciones>"]
    for operation in operations:
        if operation is None:
            continue
        parts.extend(
            [
                "<Operacion>",
                f"<id>{_xml_escape(operation.id)}</id>",
                f"<fecha>{_xml_escape(operation.created_at)}</fecha>",
                f"<tipo>{_xml_escape(operation.type)}</tipo>",
                f"<importe>{operation.amount:.2f}</importe>",
                f"<moneda>{operation.coin}</moneda>",
                f"<terminal>{_xml_escape(operation.terminal)}</terminal>",
                f"<pedido>{_xml_escape(operation.request)}</pedido>",
                f"<factura>{_xml_escape(operation.invoice)}</factura>",
                f"<resultado>{_xml_escape(operation.result)}</resultado>",
                f"<codigo>{_xml_escape(operation.response_code)}</codigo>",
                f"<estado>{_xml_escape(operation.status)}</estado>",
                f"<mensaje>{_xml_escape(operation.message)}</mensaje>",
                "</Operacion>",
            ]
        )
    parts.append("</Operaciones>")
    return "".join(parts).encode("utf-8")


def _consulta_legacy_xml(operations: list[Operation], merchant_code: str) -> bytes:
    timestamp = _legacy_timestamp()
    payload = (
        '<consultas version="2.2">'
        "<resultadoConsulta>"
        f"<numoperaciones>{len(operations)}</numoperaciones>"
        "<numpagina>0</numpagina>"
        "<totalpaginas>0</totalpaginas>"
        f"<comercio>{_xml_escape(merchant_code)}</comercio>"
        f"<timestamp>{timestamp}</timestamp>"
        f"<firma>{_legacy_signature(operations, merchant_code, timestamp)}</firma>"
        "</resultadoConsulta>"
        "</consultas>"
    )
    return payload.encode("utf-8")


def _consulta_bridge_error_xml(error: str) -> bytes:
    payload = (
        '<consultas version="2.2">'
        "<resultadoConsulta>"
        "<numoperaciones>0</numoperaciones>"
        f"<error>{_xml_escape(error or 'Bridge process failed.')}</error>"
        "</resultadoConsulta>"
        "</consultas>"
    )
    return payload.encode("utf-8")


def _payload_from_legacy_path(path: str) -> dict[str, str] | None:
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2 or segments[0].lower() != "operacion":
        return None

    amount = _parse_legacy_amount(segments[1])
    if amount is None:
        return None

    payload: dict[str, str] = {
        "type": "PAGO",
        "importe": f"{amount:.2f}",
    }

    for segment in segments[2:]:
        token = segment.strip()
        if not token:
            continue
        upper = token.upper()
        if upper in {"PAGO", "PAY", "SALE", "TRADE"}:
            payload["type"] = "PAGO"
            continue
        if upper in {"CANCEL", "CANCELADA", "ANULAR", "ANULACION"}:
            payload["type"] = "CANCELADA"
            continue
        if upper in {"REFUND", "DEVOLUCION", "DEVO", "RETURN"}:
            payload["type"] = "REFUND"
            continue
        if "pedido" not in payload:
            payload["pedido"] = token
            payload["factura"] = token
            continue
        if "base_request" not in payload:
            payload["base_request"] = token

    return payload


def _parse_legacy_amount(value: str) -> float | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    if text.isdigit():
        amount = int(text) / 100
        return amount if amount > 0 else None
    try:
        amount = float(text)
    except ValueError:
        return None
    return amount if amount > 0 else None


def _operation_xml(operation: Operation, success: bool, push_error: str) -> bytes:
    if operation.raw_xml:
        return operation.raw_xml.encode("utf-8")

    tag = "Success" if success else "Error"
    description = operation.description or push_error
    payload = (
        "<Operaciones>"
        f"<{tag}>"
        f"<codigo>{_xml_escape(operation.response_code)}</codigo>"
        f"<mensaje>{_xml_escape(operation.message)}</mensaje>"
        f"<descripcion>{_xml_escape(description)}</descripcion>"
        f"</{tag}>"
        "<Operacion>"
        f"<id>{_xml_escape(operation.id)}</id>"
        f"<tipo>{_xml_escape(operation.type)}</tipo>"
        f"<pedido>{_xml_escape(operation.request)}</pedido>"
        f"<factura>{_xml_escape(operation.invoice)}</factura>"
        f"<terminal>{_xml_escape(operation.terminal)}</terminal>"
        f"<importe>{operation.amount:.2f}</importe>"
        f"<estado>{_xml_escape(operation.status)}</estado>"
        "</Operacion>"
        "</Operaciones>"
    )
    return payload.encode("utf-8")


def _legacy_parameter_error_xml() -> bytes:
    payload = (
        "<Operaciones>"
        "<Error>"
        "<codigo>OPERACION CANCELADA.</codigo>"
        "<mensaje>El parametro es incorrecto</mensaje>"
        "<descripcion></descripcion>"
        "</Error>"
        "</Operaciones>"
    )
    return payload.encode("utf-8")


def _legacy_timestamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d %H%M%S")


def _legacy_signature(operations: list[Operation], merchant_code: str, timestamp: str) -> str:
    import hashlib

    seed = f"{merchant_code}|{len(operations)}|{timestamp}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest().upper()


def _xml_escape(value: object) -> str:
    text = str(value or "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
