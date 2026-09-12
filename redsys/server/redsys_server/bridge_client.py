from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .config import RedsysServerConfig
from .models import Operation, OperationRequest, build_operation


@dataclass(slots=True)
class BridgeResponse:
    return_code: int
    init_code: int
    stop_code: int
    xml: str
    error: str


class RedsysBridgeClient:
    def __init__(self, config: RedsysServerConfig):
        self.config = config

    def connect(self) -> BridgeResponse:
        return self._call({"action": "connect"})

    def pay(self, request: OperationRequest) -> BridgeResponse:
        payload = {
            "action": "pay",
            "amount": _format_amount(request.amount),
            "invoice": request.invoice or request.request,
            "operation_type": "PAGO",
        }
        return self._call(payload)

    def refund(self, request: OperationRequest) -> BridgeResponse:
        payload = {
            "action": "refund",
            "amount": _format_amount(request.amount),
            "order": request.request or request.base_request or "",
            "invoice": request.invoice or request.request,
        }
        return self._call(payload)

    def query(self, request: OperationRequest | None = None, page: str = "0") -> BridgeResponse:
        request = request or OperationRequest(type="CONSULTA")
        payload = {
            "action": "query",
            "order": request.request,
            "rts": request.authentication or "",
            "invoice": request.invoice,
            "date_from": "",
            "date_to": "",
            "result": "",
            "page": str(page or "0"),
            "operation_type": request.type,
        }
        return self._call(payload)

    def test_connect(self) -> BridgeResponse:
        today = datetime.now().strftime("%Y-%m-%d-00.00.00")
        payload = {
            "action": "query",
            "order": "",
            "rts": "1",
            "invoice": "",
            "date_from": today,
            "date_to": "",
            "result": "AUTORIZADA",
            "page": "0",
            "operation_type": "PAGO",
        }
        return self._call(payload)

    def _call(self, payload: dict[str, Any]) -> BridgeResponse:
        command_payload = {
            "merchant_code": self.config.merchant_code,
            "terminal": self.config.merchant_terminal,
            "signature_key": self.config.merchant_signature_key,
            "port_config": self.config.serial_port_config,
            "tpv_version": self.config.tpv_version or "6.1",
            "dll_path": self.config.bridge_dll_path,
            "lib_dir": str(Path(self.config.bridge_dll_path).resolve().parent),
            "runtime_dir": self.config.bridge_runtime_dir,
            "dependency_dirs": list(self.config.bridge_dependency_dirs),
            **payload,
        }
        encoded = base64.b64encode(json.dumps(command_payload).encode("utf-8")).decode("ascii")
        run_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        completed = subprocess.run(
            self._build_command(encoded),
            capture_output=True,
            text=True,
            timeout=max(self.config.bridge_timeout_seconds, 1),
            check=False,
            env=self._build_env(),
            **run_kwargs,
        )
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout or "").strip()
            response = BridgeResponse(-1, -1, -1, "", error or "Bridge process failed.")
            self._log_bridge_call(command_payload, completed.returncode, completed.stdout, completed.stderr, response)
            return response
        text = (completed.stdout or "").strip()
        if not text:
            response = BridgeResponse(-1, -1, -1, "", "Bridge returned no output.")
            self._log_bridge_call(command_payload, completed.returncode, completed.stdout, completed.stderr, response)
            return response
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            response = BridgeResponse(-1, -1, -1, "", text)
            self._log_bridge_call(command_payload, completed.returncode, completed.stdout, completed.stderr, response)
            return response
        response = BridgeResponse(
            return_code=int(raw.get("return_code", -1)),
            init_code=int(raw.get("init_code", -1)),
            stop_code=int(raw.get("stop_code", -1)),
            xml=str(raw.get("xml", "") or ""),
            error=str(raw.get("error", "") or ""),
        )
        self._log_bridge_call(command_payload, completed.returncode, completed.stdout, completed.stderr, response)
        return response

    def _log_bridge_call(
        self,
        command_payload: dict[str, Any],
        process_return_code: int,
        stdout: str | None,
        stderr: str | None,
        response: BridgeResponse,
    ) -> None:
        log_path = Path(self.config.bridge_log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        safe_payload = dict(command_payload)
        if safe_payload.get("signature_key"):
            safe_payload["signature_key"] = "***"
        record = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "payload": safe_payload,
            "process_return_code": process_return_code,
            "bridge_return_code": response.return_code,
            "init_code": response.init_code,
            "stop_code": response.stop_code,
            "error": response.error,
            "stdout_preview": (stdout or "")[:2000],
            "stderr_preview": (stderr or "")[:2000],
            "xml_preview": (response.xml or "")[:4000],
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")

    def _build_command(self, encoded: str) -> list[str]:
        script_path = Path(self.config.bridge_script_path)
        if script_path.suffix.lower() == ".py":
            return [
                self.config.bridge_powershell_path,
                "-S",
                self.config.bridge_script_path,
                encoded,
            ]

        return [
            self.config.bridge_powershell_path,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            self.config.bridge_script_path,
            "-RequestBase64",
            encoded,
        ]

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.config.bridge_runtime_dir:
            env["PY310_RUNTIME_DIR"] = self.config.bridge_runtime_dir
        return env


def operation_from_bridge(
    request: OperationRequest,
    response: BridgeResponse,
    *,
    terminal: str,
    default_error_code: str,
) -> tuple[Operation, bool]:
    details = _parse_bridge_xml(response.xml)
    success = _bridge_success(response, details)
    message = details.get("message") or details.get("result") or response.error or _fallback_message(
        response
    )
    response_code = details.get("response_code") or details.get("error_code") or (
        "0000" if success else default_error_code
    )
    description = details.get("description") or _bridge_description(response)
    operation = build_operation(
        request,
        terminal=details.get("terminal") or terminal,
        response_code=response_code,
        result=details.get("result") or ("OK" if success else "ERROR"),
        status=_bridge_status(success, response, details),
        message=message,
        description=description,
        authentication=details.get("rts") or details.get("authentication") or request.authentication,
        customer_card=details.get("customer_card") or request.customer_card,
        commerce_card=details.get("commerce_card") or request.commerce_card,
        raw_xml=response.xml,
    )
    operation.request = details.get("request") or operation.request or operation.invoice
    operation.invoice = details.get("invoice") or operation.invoice or operation.request
    return operation, success


def connect_status_from_bridge(response: BridgeResponse, *, legacy_log_path: str = "") -> dict[str, Any]:
    details = _parse_bridge_xml(response.xml)
    if not details.get("device_id"):
        details.update(_extract_legacy_discovery_details(legacy_log_path))
    return {
        "connected": response.return_code == 0 and response.init_code == 0,
        "init_code": response.init_code,
        "return_code": response.return_code,
        "stop_code": response.stop_code,
        "error": response.error,
        "xml": response.xml,
        "details": details,
    }


def _parse_bridge_xml(xml_text: str) -> dict[str, str]:
    if not xml_text.strip():
        return {}
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return {}

    def text_of(*names: str) -> str:
        for name in names:
            node = root.find(f".//{name}")
            if node is not None and node.text:
                return node.text.strip()
        return ""

    details = {
        "request": text_of("pedido"),
        "invoice": text_of("factura"),
        "terminal": text_of("terminal"),
        "result": text_of("resultado"),
        "message": text_of("mensaje"),
        "description": text_of("descripcion", "literal", "autenticadoPorPin"),
        "response_code": text_of("codigoRespuesta", "codigo"),
        "error_code": text_of("codigo"),
        "rts": text_of("identificadorRTS"),
        "customer_card": _normalize_masked_card(
            text_of("tarjetaClienteRecibo", "tarjeta", "panTarjeta", "numTarjeta")
        ),
        "commerce_card": _normalize_masked_card(
            text_of("tarjetaComercioRecibo", "tarjetaComercio", "panComercio")
        ),
        "status": text_of("estado"),
    }
    details.update(_extract_terminal_info(root))
    return details


def _normalize_masked_card(raw_value: str) -> str:
    value = re.sub(r"\s+", "", (raw_value or "").strip())
    if not value:
        return ""

    last_four_match = re.search(r"(\d{4})$", value)
    if not last_four_match:
        return raw_value.strip()

    last_four = last_four_match.group(1)
    masked_prefix = value[: -len(last_four)]
    mask_chars = re.sub(r"[^*Xx#]", "", masked_prefix)
    masked_length = max(len(mask_chars), 12)
    masked = "*" * masked_length
    groups = [masked[i : i + 4] for i in range(0, len(masked), 4)]
    return " ".join([*groups, last_four]).strip()


def _extract_terminal_info(root: ElementTree.Element) -> dict[str, str]:
    terminal_actual = root.find(".//TerminalActual")
    if terminal_actual is None:
        return {}

    def child_text(name: str) -> str:
        node = terminal_actual.find(name)
        if node is not None and node.text:
            return node.text.strip()
        return ""

    return {
        "merchant_name": child_text("nombre_comercio") or child_text("NombreComercio"),
        "merchant_full_code": child_text("comercio"),
        "terminal_code": child_text("terminal"),
        "device_id": child_text("id_terminal"),
        "hardware": child_text("hardware"),
        "device_type": child_text("tipoDispositivo"),
        "device_model": child_text("modeloDispositivo"),
        "port_config": child_text("ConfPuerto"),
        "ws_version": child_text("VerWs"),
        "currency_code": child_text("cod_moneda"),
        "currency": child_text("moneda"),
    }


def _extract_legacy_discovery_details(legacy_log_path: str) -> dict[str, str]:
    if not legacy_log_path:
        return {}
    path = Path(legacy_log_path)

    if not path.exists():
        return {}

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}

    matches = re.findall(r"XmlRespuesta (<Respuesta>.*?</Respuesta>)", text)
    if not matches:
        return {}

    xml_text = matches[-1]
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return {}

    terminal_actual = root.find(".//TerminalActual")
    if terminal_actual is None:
        return {}

    def child_text(name: str) -> str:
        node = terminal_actual.find(name)
        if node is not None and node.text:
            return node.text.strip()
        return ""

    return {
        "merchant_name": child_text("nombre_comercio"),
        "merchant_full_code": child_text("comercio"),
        "terminal_code": child_text("terminal"),
        "device_id": child_text("id_terminal"),
        "hardware": child_text("hardware"),
        "device_type": child_text("tipoDispositivo"),
        "device_model": child_text("modeloDispositivo"),
        "port_config": child_text("ConfPuerto"),
        "ws_version": child_text("VerWs"),
        "currency_code": child_text("cod_moneda"),
        "currency": child_text("moneda"),
    }


_NEGATIVE_MARKERS = ("DENEG", "ERROR", "CANCEL", "ANUL", "RECHAZ")
_POSITIVE_MARKERS = ("AUTORIZADA", "AUTORIZADO", "AUTHORIZED", "AUTHORISED", "APROBADA", "APROBADO")


def _bridge_success(response: BridgeResponse, details: dict[str, str]) -> bool:
    # 通信层失败：DLL 调用未成功。
    if response.return_code != 0:
        return False

    # 没有交易报文说明交易没有真正发起 / 刷卡机无响应，一律不算成功。
    if not (response.xml or "").strip():
        return False

    result = (details.get("result") or "").strip().upper()
    message = (details.get("message") or "").strip().upper()
    description = (details.get("description") or "").strip().upper()
    text = " ".join(filter(None, [result, message, description]))

    # 明确的拒绝 / 取消 / 错误一律失败。
    if any(marker in text for marker in _NEGATIVE_MARKERS):
        return False

    code = (details.get("response_code") or details.get("error_code") or "").strip().upper()
    if code.startswith("TPV-"):
        return False

    # 白名单：只有在明确授权的情况下才算成功（fail-closed）。
    # 其它任何结果（Denegada / 未识别 / 缺字段）都视为失败，避免 Denegada 被当成成功结束订单。
    positive_source = f"{result} {message}"
    return any(marker in positive_source for marker in _POSITIVE_MARKERS)


def _bridge_status(success: bool, response: BridgeResponse, details: dict[str, str]) -> str:
    if success:
        return "done"
    # 失败时不再回传 <estado>（可能为 F=Finalizada），避免下游把它当作交易完成。
    return "error"


def _bridge_description(response: BridgeResponse) -> str:
    parts = []
    if response.error:
        parts.append(response.error)
    if response.init_code not in {0, -13}:
        parts.append(f"init={response.init_code}")
    if response.return_code != 0:
        parts.append(f"call={response.return_code}")
    if response.stop_code not in {0, -1}:
        parts.append(f"stop={response.stop_code}")
    return " | ".join(parts)


def _fallback_message(response: BridgeResponse) -> str:
    if response.return_code != 0:
        return f"Bridge DLL call failed ({response.return_code})."
    return "Operacion procesada sin mensaje XML."


def _format_amount(value: float) -> str:
    return f"{value:.2f}"
