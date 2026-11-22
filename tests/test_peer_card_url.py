"""Agent-card URL must be reachable by the CALLER (Hermes follows the card).

Hermes a2a_call POSTs to the card's JSONRPC interface URL, not the configured
base URL. Behind an SSH tunnel (the T289 pattern) the caller's address differs
from the listener's, so a fixed card URL sends every call to the wrong place.
With no public_url, the card is derived from the request's Host, like Hermes's
own adapter; an explicit public_url still wins.
"""

import httpx
import pytest
from test_peer_server import registry

from aerys_v2.a2a.task_store import ReceiptStore
from aerys_v2.transports.a2a_server import build_peer_app

CARD = "/.well-known/agent-card.json"


async def _card(app, headers):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://ignored"
        ) as client:
            return await client.get(CARD, headers=headers)


def _iface_url(response):
    return response.json()["supportedInterfaces"][0]["url"]


@pytest.mark.anyio
async def test_card_url_follows_request_host_when_no_public_url(tmp_path):
    def app():
        return build_peer_app(registry(), ReceiptStore(tmp_path / "r.db"), None, url="")

    r = await _card(app(), {"Host": "127.0.0.1:19931"})
    assert r.status_code == 200
    assert _iface_url(r) == "http://127.0.0.1:19931/"
    assert r.json()["supportedInterfaces"][0]["tenant"] == "aerys"
    r = await _card(app(), {"Host": "aerys.internal", "X-Forwarded-Proto": "https"})
    assert _iface_url(r) == "https://aerys.internal/"


@pytest.mark.anyio
async def test_explicit_public_url_still_wins(tmp_path):
    app = build_peer_app(
        registry(), ReceiptStore(tmp_path / "r.db"), None, url="https://fixed.example/"
    )
    r = await _card(app, {"Host": "127.0.0.1:19931"})
    assert _iface_url(r) == "https://fixed.example/"


@pytest.mark.anyio
@pytest.mark.parametrize("host", ["evil.example/path", "a b", "x@y", "h\"q"])
async def test_malformed_host_is_refused(tmp_path, host):
    app = build_peer_app(registry(), ReceiptStore(tmp_path / "r.db"), None, url="")
    r = await _card(app, {"Host": host})
    assert r.status_code == 400


@pytest.mark.anyio
async def test_unknown_forwarded_proto_falls_back_to_http(tmp_path):
    app = build_peer_app(registry(), ReceiptStore(tmp_path / "r.db"), None, url="")
    r = await _card(app, {"Host": "h:1", "X-Forwarded-Proto": "javascript"})
    assert _iface_url(r) == "http://h:1/"
