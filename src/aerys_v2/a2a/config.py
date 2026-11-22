"""Standalone opt-in peer listener configuration; never reads owner .env/settings."""

import argparse
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from aerys_v2.a2a.registry import Peer, Registry
from aerys_v2.a2a.task_store import ReceiptStore


class PeerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_url: str
    tenant: str = Field(default="aerys", min_length=1, max_length=128)
    host: str = "127.0.0.1"
    port: int = Field(default=9901, ge=1, le=65535)
    receipt_path: Path
    peers: list[Peer] = Field(min_length=1)
    capacity: int = Field(default=8, ge=1, le=128)
    max_body: int = Field(default=65536, ge=1, le=1048576)
    model: str
    model_base_url: str = "https://api.openai.com/v1"
    model_key_env: str = Field(
        default="AERYS_PEER_MODEL_KEY", pattern=r"^[A-Z][A-Z0-9_]*$"
    )
    allow_private_http: bool = False

    @model_validator(mode="after")
    def validate_paths(self):
        Peer(
            id="listener",
            url=self.public_url,
            card_name="Aerys",
            token_env="UNUSED",
            allow_insecure_http=self.allow_private_http,
        )
        if not self.receipt_path.is_absolute():
            raise ValueError(
                "receipt path must be absolute and on a persistent private volume"
            )
        return self


def create_app(settings: PeerSettings, *, secrets=None, model=None):
    from aerys_v2.a2a.executor import PeerAssistant
    from aerys_v2.transports.a2a_server import build_peer_app

    env = os.environ if secrets is None else secrets
    registry = Registry(
        settings.peers, env, redact_values=(env.get(settings.model_key_env, ""),)
    )
    if model is None:
        from langchain_openai import ChatOpenAI

        key = env.get(settings.model_key_env)
        if not key:
            raise ValueError("peer model credential missing")
        # Dedicated model, not Claude CLI/owner Settings. No builtin tools or
        # filesystem process can be activated by registry/capability metadata.
        model = ChatOpenAI(
            model=settings.model,
            base_url=settings.model_base_url,
            api_key=SecretStr(key),
            timeout=60,
            max_retries=0,
            max_completion_tokens=2048,
        )
    store = ReceiptStore(settings.receipt_path)
    return build_peer_app(
        registry,
        store,
        PeerAssistant(model),
        url=settings.public_url,
        tenant=settings.tenant,
        capacity=settings.capacity,
        max_body=settings.max_body,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Isolated Aerys A2A peer listener (not owner /ask)"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate enrollment and credentials without binding or calling a model",
    )
    args = parser.parse_args()
    try:
        settings = PeerSettings.model_validate_json(args.config.read_text())
        Registry(settings.peers)
        if args.check:
            if not os.environ.get(settings.model_key_env):
                raise ValueError("peer model credential missing")
            print("Peer configuration valid; no listener or model invocation started.")
            return
        # SDK automatic exception events are not a payload privacy boundary.
        # Disable them before importing server/client decorators in this process;
        # our explicit correlation-only spans remain available.
        os.environ["OTEL_INSTRUMENTATION_A2A_SDK_ENABLED"] = "false"
        import uvicorn

        uvicorn.run(
            create_app(settings), host=settings.host, port=settings.port, workers=1
        )
    except (ValueError, OSError):
        # Pydantic validation errors may quote field values. Do not dump secrets.
        parser.exit(2, "Peer configuration/credential/storage validation failed.\n")


if __name__ == "__main__":
    main()
