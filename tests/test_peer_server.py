import asyncio

import httpx

from aerys_v2.a2a.registry import Peer, Registry
from aerys_v2.a2a.task_store import ReceiptStore
from aerys_v2.transports.a2a_server import build_peer_app


def registry():
    return Registry(
        [
            Peer(
                id=p,
                url=f"http://localhost/{p}/",
                tenant=p,
                card_name=p,
                token_env=p.upper(),
            )
            for p in ("alice", "bob")
        ],
        {"ALICE": "alice-token", "BOB": "bob-token"},
    )


def send(mid="m1", context="ctx1", **extra):
    return {
        "message": {
            "messageId": mid,
            "contextId": context,
            "role": "ROLE_USER",
            "parts": [{"text": "hello"}],
            **extra,
        },
        "tenant": "aerys",
    }


async def rpc(client, method, params, token="alice-token"):
    r = await client.post(
        "/",
        headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
        json={"jsonrpc": "2.0", "id": "rpc", "method": method, "params": params},
    )
    return r


def test_peer_roundtrip_dedup_and_cross_peer_denial(tmp_path):
    async def run():
        calls = []

        async def execute(request):
            calls.append(request)
            return "answer"

        store = ReceiptStore(tmp_path / "receipts.db")
        app = build_peer_app(
            registry(), store, execute, url="http://localhost/", tenant="aerys"
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                assert (
                    await rpc(client, "SendMessage", send(), token="owner-token")
                ).status_code == 401
                r = (await rpc(client, "SendMessage", send())).json()
                task = r["result"]["task"]
                assert task["status"]["state"] == "TASK_STATE_COMPLETED"
                assert (await rpc(client, "SendMessage", send())).json()["result"][
                    "task"
                ]["id"] == task["id"]
                assert len(calls) == 1
                assert calls[0].principal == "alice"
                assert calls[0].thread_id.startswith("a2a:")
                assert "owner" not in calls[0].thread_id
                params = {"id": task["id"], "tenant": "aerys"}
                for method in ("GetTask", "CancelTask"):
                    assert (
                        "error"
                        in (await rpc(client, method, params, token="bob-token")).json()
                    )
                assert (
                    await rpc(
                        client, "ListTasks", {"tenant": "aerys"}, token="bob-token"
                    )
                ).json()["result"]["tasks"] == []
                assert (
                    "error"
                    in (
                        await rpc(
                            client, "SendMessage", send("m2", "ctx1"), token="bob-token"
                        )
                    ).json()
                )
                assert (
                    "error"
                    in (
                        await rpc(
                            client,
                            "SendMessage",
                            send("m3", "owner", taskId="owner-thread"),
                        )
                    ).json()
                )
                assert (
                    "error"
                    in (
                        await rpc(
                            client, "SendMessage", {**send("m4"), "tenant": "wrong"}
                        )
                    ).json()
                )

    asyncio.run(run())
