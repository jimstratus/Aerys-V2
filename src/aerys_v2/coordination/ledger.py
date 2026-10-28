"""Non-authoritative protocol-to-work reference seam.

No native runtime DB is opened here and no local assignment/status/lease exists.
The integration owner supplies a supported, scoped ledger reader. Transport success
cannot complete a work item or authorize reassignment. Those operations are not
exposed until remote ownership/fencing qualifies independently.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LedgerReference:
    authority: str
    work_id: str

    def __post_init__(self):
        if not self.authority.strip() or not self.work_id.strip():
            raise ValueError("Both authority and original work ID are required")


class LedgerView:
    """Read through the owning runtime's supported interface; never infer closure."""

    def __init__(self, authority: str, read: Callable[[str], Mapping[str, Any]]):
        self.authority, self._read = authority, read

    def observe(self, reference: LedgerReference) -> Mapping[str, Any]:
        if reference.authority != self.authority:
            raise PermissionError("Wrong ledger authority")
        record = self._read(reference.work_id)
        if record.get("id") != reference.work_id:
            raise ValueError("Ledger returned a different work item")
        return record
