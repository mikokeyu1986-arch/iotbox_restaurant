from __future__ import annotations

from starlette.requests import Request

import app.main as main


def _request(client_host: str, *, method: str = "GET", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request({
        "type": "http",
        "method": method,
        "path": "/api/receipt-template",
        "headers": headers or [],
        "client": (client_host, 51000),
        "server": ("192.168.1.39", 8398),
        "scheme": "https",
        "query_string": b"",
    })


def test_advertised_lan_address_is_treated_as_same_machine(monkeypatch):
    monkeypatch.setattr(main, "IOT_IP", "192.168.1.39:8398")
    assert main._is_local_machine_request(_request("192.168.1.39")) is True


def test_another_lan_device_is_not_treated_as_same_machine(monkeypatch):
    monkeypatch.setattr(main, "IOT_IP", "192.168.1.39:8398")
    assert main._is_local_machine_request(_request("192.168.1.52")) is False


def test_loopback_remains_local(monkeypatch):
    monkeypatch.setattr(main, "IOT_IP", "192.168.1.39:8398")
    assert main._is_local_machine_request(_request("127.0.0.1")) is True


def test_private_network_preflight_is_detected():
    request = _request(
        "192.168.1.88",
        method="OPTIONS",
        headers=[(b"access-control-request-private-network", b"true")],
    )

    assert main._is_private_network_preflight(request) is True


def test_paired_odoo_origin_is_accepted(monkeypatch):
    monkeypatch.setattr(
        main.config_store,
        "get_connection",
        lambda: {"url": "https://restaurante.oduo.es"},
    )
    request = _request(
        "192.168.1.88",
        method="OPTIONS",
        headers=[(b"origin", b"https://restaurante.oduo.es")],
    )

    assert main._is_paired_odoo_origin(request) is True


def test_untrusted_origin_is_rejected(monkeypatch):
    monkeypatch.setattr(
        main.config_store,
        "get_connection",
        lambda: {"url": "https://restaurante.oduo.es"},
    )
    request = _request(
        "192.168.1.88",
        method="OPTIONS",
        headers=[(b"origin", b"https://untrusted.example")],
    )

    assert main._is_paired_odoo_origin(request) is False
