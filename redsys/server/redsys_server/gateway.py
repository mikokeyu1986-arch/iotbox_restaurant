from __future__ import annotations

from random import randint

from .bridge_client import (
    BridgeResponse,
    RedsysBridgeClient,
    connect_status_from_bridge,
    operation_from_bridge,
)
from .config import RedsysServerConfig
from .models import Operation, OperationRequest, build_operation
from .storage import OperationStore


class SimulatedRedsysGateway:
    def __init__(self, config: RedsysServerConfig, store: OperationStore):
        self.config = config
        self.store = store

    def process(self, request: OperationRequest) -> tuple[Operation | None, bool]:
        operation_type = request.type.upper().strip()
        if operation_type in {"PAGO", "PAY", "TRADE", "SALE"}:
            if request.amount <= 0:
                return self._error(request, "Importe invalido."), False
            operation = build_operation(
                request,
                terminal=self.config.merchant_terminal,
                response_code=self.config.success_code,
                result="OK",
                status="done",
                message="Pago registrado correctamente.",
                authentication=str(randint(100000, 999999)),
                customer_card=request.customer_card or "****1111",
                commerce_card=request.commerce_card or "****2222",
            )
            self.store.add(operation)
            return operation, True

        if operation_type in {"CANCEL", "CANCELADA", "ANULAR", "ANULACION"}:
            target = self.store.find_by_request(request.request or request.base_request or request.invoice)
            if target is None:
                return self._error(request, "No se encontro la operacion a cancelar."), False
            operation = build_operation(
                request,
                terminal=self.config.merchant_terminal,
                response_code=self.config.cancel_code,
                result="CANCELADA",
                status="cancel",
                message="Operacion cancelada.",
                authentication=target.authentication,
                customer_card=target.customer_card,
                commerce_card=target.commerce_card,
            )
            self.store.add(operation)
            return operation, True

        if operation_type in {"REFUND", "DEVOLUCION", "DEVO", "RETURN"}:
            if request.amount <= 0:
                return self._error(request, "Importe invalido para devolucion."), False
            operation = build_operation(
                request,
                terminal=self.config.merchant_terminal,
                response_code=self.config.success_code,
                result="OK",
                status="done",
                message="Devolucion registrada correctamente.",
                authentication=str(randint(100000, 999999)),
                customer_card=request.customer_card or "****1111",
                commerce_card=request.commerce_card or "****2222",
            )
            self.store.add(operation)
            return operation, True

        return self._error(request, f"Operacion no soportada: {operation_type or 'N/A'}"), False

    def _error(self, request: OperationRequest, message: str) -> Operation:
        operation = build_operation(
            request,
            terminal=self.config.merchant_terminal,
            response_code=self.config.error_code,
            result="ERROR",
            status="error",
            message=message,
        )
        self.store.add(operation)
        return operation

    def consulta(self, request: OperationRequest, page: str = "0") -> BridgeResponse:
        xml = (
            '<consultas version="2.2">'
            "<resultadoConsulta>"
            f"<numoperaciones>{len(self.store.list())}</numoperaciones>"
            "<numpagina>0</numpagina>"
            "<totalpaginas>0</totalpaginas>"
            f"<comercio>{self.config.merchant_code}</comercio>"
            "</resultadoConsulta>"
            "</consultas>"
        )
        return BridgeResponse(0, 0, 0, xml, "")


class RealRedsysBridgeGateway:
    def __init__(self, config: RedsysServerConfig, store: OperationStore):
        self.config = config
        self.store = store
        self.bridge = RedsysBridgeClient(config)
        self._connected = False

    def connect(self) -> dict[str, object]:
        response = self.bridge.connect()
        status = connect_status_from_bridge(
            response,
            legacy_log_path=self.config.legacy_discovery_log_path,
        )
        self._connected = bool(status["connected"])
        return status

    def process(self, request: OperationRequest) -> tuple[Operation | None, bool]:
        if not self._connected:
            status = self.connect()
            if not status.get("connected"):
                operation = build_operation(
                    request,
                    terminal=self.config.merchant_terminal,
                    response_code=self.config.error_code,
                    result="ERROR",
                    status="error",
                    message="PinPad is not connected. Call CONNECT first.",
                    description=str(status.get("error") or ""),
                )
                self.store.add(operation)
                return operation, False

        operation_type = request.type.upper().strip()
        if operation_type in {"PAGO", "PAY", "TRADE", "SALE"}:
            response = self.bridge.pay(request)
        elif operation_type in {"REFUND", "DEVOLUCION", "DEVO", "RETURN"}:
            response = self.bridge.refund(request)
        else:
            operation = build_operation(
                request,
                terminal=self.config.merchant_terminal,
                response_code=self.config.error_code,
                result="ERROR",
                status="error",
                message=f"Operacion no soportada en bridge real: {operation_type or 'N/A'}",
            )
            self.store.add(operation)
            return operation, False

        operation, success = operation_from_bridge(
            request,
            response,
            terminal=self.config.merchant_terminal,
            default_error_code=self.config.error_code,
        )
        self.store.add(operation)
        return operation, success

    def consulta(self, request: OperationRequest, page: str = "0") -> BridgeResponse:
        if not self._connected:
            status = self.connect()
            if not status.get("connected"):
                return BridgeResponse(
                    -1,
                    -1,
                    -1,
                    "",
                    str(status.get("error") or "PinPad is not connected. Call CONNECT first."),
                )
        return self.bridge.query(request, page=page)
