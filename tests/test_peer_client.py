import asyncio

import httpx
import pytest
from test_peer_server import registry

from aerys_v2.a2a.client import PeerClient
from aerys_v2.a2a.contracts import ReconciliationRequired
from aerys_v2.a2a.registry import Peer, Registry
from aerys_v2.a2a.task_store import ReceiptStore
from aerys_v2.transports.a2a_server import build_peer_app


def test_sdk_client_profile_roundtrip_reconcile_and_card_substitution(tmp_path):
    async def run():
        calls = []

        async def execute(req):
            calls.append(req)
            return "roundtrip"

        remote = ReceiptStore(tmp_path / "remote.db")
        app = build_peer_app(
            registry(), remote, execute, url="http://localhost/", tenant="aerys"
        )
        peers = Registry(
            [
                Peer(
                    id="aerys",
                    url="http://localhost/",
                    tenant="aerys",
                    card_name="Aerys peer assistant",
                    token_env="TOKEN",
                )
            ],
            {"TOKEN": "alice-token"},
        )
        local = ReceiptStore(tmp_path / "local.db")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app)
            ) as http:
                client = PeerClient(peers, local, http)
                task = await client.submit("aerys", "hello", dispatch_id="d1")
                assert task.artifacts[0].parts[0].text == "roundtrip"
                assert (
                    await client.submit("aerys", "hello", dispatch_id="d1")
                ).id == task.id
                assert (await client.status("d1")).id == task.id
                assert len(calls) == 1
                wrong = Registry(
                    [
                        Peer(
                            id="aerys",
                            url="http://localhost/",
                            tenant="root",
                            card_name="Aerys peer assistant",
                            token_env="TOKEN",
                        )
                    ],
                    {"TOKEN": "alice-token"},
                )
                with pytest.raises(ValueError):
                    await PeerClient(wrong, local, http).submit(
                        "aerys", "hello", dispatch_id="d2"
                    )
        local.close()

    asyncio.run(run())


def test_lost_response_never_blindly_resubmits_and_restart_reconciles(tmp_path):
    async def run():
        calls = []

        async def execute(req):
            calls.append(req)
            return "accepted before response loss"

        remote = ReceiptStore(tmp_path / "remote.db")
        app = build_peer_app(
            registry(), remote, execute, url="http://localhost/", tenant="aerys"
        )
        peers = Registry(
            [
                Peer(
                    id="aerys",
                    url="http://localhost/",
                    tenant="aerys",
                    card_name="Aerys peer assistant",
                    token_env="TOKEN",
                )
            ],
            {"TOKEN": "alice-token"},
        )
        upstream = httpx.ASGITransport(app=app)

        class LostOnce(httpx.AsyncBaseTransport):
            lost = False

            async def handle_async_request(self, req):
                response = await upstream.handle_async_request(req)
                if req.method == "POST" and not self.lost:
                    self.lost = True
                    raise httpx.ReadTimeout("response lost")
                return response

        path = tmp_path / "local.db"
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=LostOnce()) as http:
                local = ReceiptStore(path)
                with pytest.raises(ReconciliationRequired):
                    await PeerClient(peers, local, http).submit(
                        "aerys", "hello", dispatch_id="lost"
                    )
                local.close()
                local = ReceiptStore(path)
                task = await PeerClient(peers, local, http).submit(
                    "aerys", "hello", dispatch_id="lost"
                )
                assert task.status.state == 3
                assert len(calls) == 1
                assert local.dispatch("lost")["remote_id"] == task.id
                local.close()

    asyncio.run(run())
