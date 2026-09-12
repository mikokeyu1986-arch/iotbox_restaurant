from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import Operation


class OdooSyncClient:
    def __init__(self, create_url: str, timeout: float = 5.0):
        self.create_url = create_url
        self.timeout = timeout

    def push(self, operation: Operation) -> tuple[bool, str]:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "args": [
                    {
                        "type": operation.type,
                        "amount": operation.amount,
                        "coin": operation.coin,
                        "customer_card": operation.customer_card,
                        "authentication": operation.authentication,
                        "trade": operation.trade,
                        "invoice": operation.invoice,
                        "contact_less": operation.contact_less,
                        "base_request": operation.base_request,
                        "signature": operation.signature,
                        "terminal": operation.terminal,
                        "result": operation.result,
                        "response_code": operation.response_code,
                        "request": operation.request,
                        "status": operation.status,
                        "commerce_card": operation.commerce_card,
                        "message": operation.message,
                        "description": operation.description,
                        "raw_xml": operation.raw_xml,
                    }
                ]
            },
            "id": 1,
        }
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self.create_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response.read()
        except (HTTPError, URLError, OSError) as exc:
            return False, str(exc)
        return True, ""
