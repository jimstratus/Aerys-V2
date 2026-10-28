"""Execution context is minted by the transport, never supplied as authority."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PeerRequest:
    principal: str
    task_id: str
    context_id: str
    thread_id: str
    text: str


class ReconciliationRequired(RuntimeError):
    """An earlier attempt may still have effects; do not submit a replacement."""
