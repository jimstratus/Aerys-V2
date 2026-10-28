import asyncio
import json

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from test_peer_server import rpc, send

from aerys_v2.a2a.config import PeerSettings, create_app
from aerys_v2.a2a.task_store import ReceiptStore
from aerys_v2.coordination.ledger import LedgerReference, LedgerView


def test_portable_config_runs_real_assistant_and_health_without_owner_settings(
    tmp_path,
):
    config = json.loads(
        __import__("pathlib").Path("deploy/a2a/config.example.json").read_text()
    )

    async def run():
        config.update(
            public_url="http://localhost/",
            receipt_path=str(tmp_path / "receipts.db"),
            model="fixture",
        )
        config["peers"] = [
            {
                "id": "alice",
                "url": "http://localhost/alice/",
                "tenant": "alice",
                "card_name": "Alice",
                "token_env": "ALICE",
                "outbound_token_env": "ALICE_OUT",
            }
        ]
        settings = PeerSettings.model_validate(config)
        app = create_app(
            settings,
            secrets={"ALICE": "alice-token", "ALICE_OUT": "outbound-only"},
            model=FakeListChatModel(responses=["restricted response"]),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http:
            assert (await http.get("/healthz")).status_code == 503
            async with app.router.lifespan_context(app):
                assert (await http.get("/healthz")).status_code == 200
                assert (
                    await rpc(http, "SendMessage", send(), token="outbound-only")
                ).status_code == 401
                task = (await rpc(http, "SendMessage", send())).json()["result"]["task"]
                assert task["artifacts"][0]["parts"][0]["text"] == "restricted response"
                assert (await http.post("/ask", json={"text": "hi"})).status_code != 200

    asyncio.run(run())


def test_ledger_reference_is_durable_not_a_second_assignable_task(tmp_path):
    path = tmp_path / "receipts.db"
    store = ReceiptStore(path)
    store.prepare_dispatch("dispatch-1", "peer", "text")
    reference = LedgerReference("native-owning-ledger", "original-id")
    store.bind_work("outbound", "dispatch-1", reference)
    store.close()
    store = ReceiptStore(path)
    assert store.work_reference("outbound", "dispatch-1") == reference
    with pytest.raises(ValueError):
        store.bind_work("outbound", "dispatch-1", LedgerReference("other", "different"))
    calls = []

    def read(work_id):
        calls.append(work_id)
        return {"id": work_id, "status": "review"}

    view = LedgerView("native-owning-ledger", read)
    assert view.observe(reference)["status"] == "review"
    assert calls == ["original-id"]
    with pytest.raises(PermissionError):
        view.observe(LedgerReference("other", "original-id"))
    assert not hasattr(view, "complete") and not hasattr(view, "reassign")
    store.close()
