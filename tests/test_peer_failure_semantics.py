import asyncio
import json
import threading

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from test_peer_server import registry, rpc

from aerys_v2.a2a.client import PeerClient
from aerys_v2.a2a.contracts import PeerRequest, ReconciliationRequired
from aerys_v2.a2a.executor import PeerAssistant
from aerys_v2.a2a.registry import Peer, Registry
from aerys_v2.a2a.task_store import ReceiptStore
from aerys_v2.transports.a2a_server import build_peer_app


def test_pending_cancellation_is_idempotent_and_never_runs_executor(tmp_path):
    async def run():
        calls = []

        async def execute(req):
            calls.append(req)
            return "unexpected"

        store = ReceiptStore(tmp_path / "receipts.db")
        app = build_peer_app(registry(), store, execute, url="http://localhost/")
        async with app.router.lifespan_context(app):
            task, _ = store.reserve(
                "alice", "pending", "ctx-pending", "pending-digest", {}
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as http:
                for _ in range(2):
                    response = (
                        await rpc(
                            http, "CancelTask", {"id": task.id, "tenant": "aerys"}
                        )
                    ).json()
                    assert (
                        response["result"]["status"]["state"] == "TASK_STATE_CANCELED"
                    )
                assert not calls

    asyncio.run(run())


def test_default_sync_assistant_drains_worker_before_cancel_returns():
    async def run():
        started, release = threading.Event(), threading.Event()

        class SlowModel(FakeListChatModel):
            def _call(self, *args, **kwargs):
                started.set()
                release.wait(5)
                return super()._call(*args, **kwargs)

        assistant = PeerAssistant(SlowModel(responses=["finished"]))
        task = asyncio.create_task(
            assistant(PeerRequest("alice", "task", "context", "a2a:thread", "hello"))
        )
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_redirect_card_does_not_leak_credentials_or_send(tmp_path):
    async def run():
        seen = []

        def transport(req):
            seen.append(str(req.url))
            return httpx.Response(
                302, headers={"Location": "https://attacker.invalid/"}
            )

        peers = Registry(
            [
                Peer(
                    id="peer",
                    url="https://peer.invalid/",
                    card_name="Peer",
                    token_env="TOKEN",
                )
            ],
            {"TOKEN": "private-token"},
        )
        store = ReceiptStore(tmp_path / "receipts.db")
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            with pytest.raises(httpx.HTTPStatusError):
                await PeerClient(peers, store, http).submit(
                    "peer", "hello", dispatch_id="redirect"
                )
        assert seen == ["https://peer.invalid/.well-known/agent-card.json"]
        assert store.dispatch("redirect")["state"] == "prepared"
        store.close()

    asyncio.run(run())


def test_timeout_before_acceptance_remains_uncertain_without_replay(tmp_path):
    async def run():
        posts = []

        def transport(req):
            if req.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "name": "Peer",
                        "supportedInterfaces": [
                            {
                                "url": "https://peer.invalid/",
                                "protocolBinding": "JSONRPC",
                                "protocolVersion": "1.0",
                                "tenant": "peer",
                            }
                        ],
                    },
                )
            body = json.loads(req.content)
            posts.append(body["method"])
            if body["method"] == "SendMessage":
                raise httpx.ConnectTimeout("uncertain connectivity")
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tasks": [], "totalSize": 0},
                },
            )

        peers = Registry(
            [
                Peer(
                    id="peer",
                    url="https://peer.invalid/",
                    tenant="peer",
                    card_name="Peer",
                    token_env="TOKEN",
                )
            ],
            {"TOKEN": "private-token"},
        )
        store = ReceiptStore(tmp_path / "receipts.db")
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            client = PeerClient(peers, store, http)
            for _ in range(2):
                with pytest.raises(ReconciliationRequired):
                    await client.submit("peer", "hello", dispatch_id="lost")
        assert posts == ["SendMessage", "ListTasks"]
        assert store.dispatch("lost")["state"] == "uncertain"
        store.close()

    asyncio.run(run())


def test_concurrent_clients_use_one_durable_dispatch_claim(tmp_path):
    path = tmp_path / "receipts.db"
    one, two = ReceiptStore(path), ReceiptStore(path)
    first, _ = one.prepare_dispatch("one", "peer", "text")
    second, _ = two.prepare_dispatch("one", "peer", "text")
    assert (
        first["message"] == second["message"] and first["context"] == second["context"]
    )
    assert one.claim_dispatch("one")
    assert not two.claim_dispatch("one")
    one.close()
    two.close()


def test_cancel_refusal_cannot_authorize_replacement(tmp_path):
    async def run():
        async def execute(req):
            return "done"

        store = ReceiptStore(tmp_path / "remote.db")
        app = build_peer_app(registry(), store, execute, url="http://localhost/")
        peers = Registry(
            [
                Peer(
                    id="peer",
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
                original = await client.submit("peer", "hello", dispatch_id="once")
                with pytest.raises(ReconciliationRequired):
                    await client.cancel("once")
                assert (await client.status("once")).id == original.id
                assert (await client.status("once")).status.state == 3
        local.close()

    asyncio.run(run())
