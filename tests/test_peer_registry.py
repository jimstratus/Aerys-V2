import pytest

from aerys_v2.a2a.registry import Peer, Registry


def test_distinct_machine_principals_and_configuration_only_enrollment():
    registry = Registry(
        [
            Peer(
                id="vector",
                url="https://example.test/vector/",
                tenant="vector",
                card_name="Vector",
                token_env="VECTOR_TOKEN",
            ),
            Peer(
                id="new-agent",
                url="https://example.test/new/",
                tenant="new",
                card_name="New",
                token_env="NEW_TOKEN",
            ),
        ],
        {"VECTOR_TOKEN": "vector-secret", "NEW_TOKEN": "new-secret"},
    )
    assert registry.authenticate("Bearer new-secret").id == "new-agent"
    assert registry.authenticate("Bearer vector-secret").id == "vector"
    with pytest.raises(PermissionError):
        registry.authenticate("Bearer owner-token")
    with pytest.raises(ValueError):
        Registry(
            [registry.peer("vector"), registry.peer("new-agent")],
            {"VECTOR_TOKEN": "same", "NEW_TOKEN": "same"},
        )


def test_disabled_and_draining_peers_cannot_submit_but_can_reconcile():
    registry = Registry(
        [
            Peer(
                id="vector",
                url="https://example.test/vector/",
                tenant="vector",
                card_name="Vector",
                token_env="TOKEN",
                state="draining",
            )
        ],
        {"TOKEN": "secret"},
    )
    peer = registry.authenticate("Bearer secret")
    registry.authorize(peer.id, "get")
    with pytest.raises(PermissionError):
        registry.authorize(peer.id, "send")
    with pytest.raises(ValueError):
        Peer(
            id="bad",
            url="https://user:pass@example.test/",
            card_name="Bad",
            token_env="TOKEN",
        )
