"""Provider document generation is separate from order and instance writes."""

from dataclasses import dataclass, field
from typing import Callable, ContextManager, Protocol

from .contracts import OzonCredentials
from .reading import AccountSource

MAX_LABEL_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class LabelTask:
    state: str
    url: str | None = field(default=None, repr=False)
    identity_digest: str | None = None


class LabelClient(Protocol):
    def create(self, posting_number: str, size: str) -> str: ...
    def poll(self, task_id: str, posting_number: str) -> LabelTask: ...
    def download(self, url: str) -> bytes: ...


LabelFactory = Callable[[AccountSource, OzonCredentials], ContextManager[LabelClient]]
