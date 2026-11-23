"""Validated enrollment, explicit grants, and isolated machine credentials."""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Peer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    url: str
    tenant: str = ""
    card_name: str = Field(min_length=1)
    token_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    outbound_token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    allow_insecure_http: bool = False
    state: Literal["active", "draining", "disabled"] = "active"
    grants: frozenset[Literal["send", "get", "list", "cancel"]] = frozenset(
        {"send", "get", "list", "cancel"}
    )
    capabilities: frozenset[str] = frozenset()
    timeout: float = Field(default=30, gt=0, le=300)

    @model_validator(mode="after")
    def endpoint(self):
        value = self.url
        u = urlsplit(value)
        if (
            u.scheme not in {"https", "http"}
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError("endpoint must be an absolute credential-free URL")
        if (
            u.scheme == "http"
            and u.hostname not in {"localhost", "127.0.0.1", "::1"}
            and not self.allow_insecure_http
        ):
            raise ValueError(
                "non-loopback endpoint requires HTTPS or explicit private-network opt-in"
            )
        return self


class Registry:
    def __init__(
        self,
        peers: list[Peer],
        secrets: Mapping[str, str] | None = None,
        *,
        redact_values: tuple[str, ...] = (),
    ):
        self._peers = {p.id: p for p in peers}
        if len(self._peers) != len(peers):
            raise ValueError("duplicate peer ID")
        source = os.environ if secrets is None else secrets
        self._secrets = {
            ref: source.get(ref, "")
            for p in peers
            for ref in (p.token_env, p.outbound_token_env)
            if ref
        }
        self._redact_values = redact_values
        tokens = [
            self._secrets.get(p.token_env, "") for p in peers if p.state != "disabled"
        ]
        for p in peers:
            if (
                p.state != "disabled"
                and p.outbound_token_env
                and not self._secrets.get(p.outbound_token_env, "").strip()
            ):
                raise ValueError("outbound peer credential missing")
        if any(not token.strip() for token in tokens) or len(set(tokens)) != len(
            tokens
        ):
            raise ValueError("active peer credentials must be present and distinct")

    def peer(self, peer_id: str) -> Peer:
        return self._peers[peer_id]

    def token(self, peer_id: str) -> str:
        peer = self.peer(peer_id)
        if peer.state == "disabled":
            raise PermissionError("peer disabled")
        return self._secrets[peer.outbound_token_env or peer.token_env]

    def redactions(self) -> tuple[str, ...]:
        return self._redact_values + tuple(
            self._secrets.get(ref, "")
            for p in self._peers.values()
            for ref in (p.token_env, p.outbound_token_env)
            if ref
        )

    def authenticate(self, authorization: str) -> Peer:
        for p in self._peers.values():
            if p.state != "disabled" and hmac.compare_digest(
                authorization.encode(),
                ("Bearer " + self._secrets[p.token_env]).encode(),
            ):
                return p
        raise PermissionError("invalid peer credential")

    def authorize(self, peer_id: str, operation: str):
        p = self.peer(peer_id)
        if (
            p.state == "disabled"
            or operation not in p.grants
            or (operation == "send" and p.state != "active")
        ):
            raise PermissionError("operation not granted")
