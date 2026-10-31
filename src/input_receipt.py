"""Native submission evidence; a receipt never proves that the game accepted it."""
from dataclasses import asdict, dataclass, field
import time
from uuid import uuid4


STATUSES = ('not_sent', 'partial', 'unknown', 'native_submitted')


def receipt_status(result):
    status = getattr(result, 'status', None)
    return status if type(status) is str and status in STATUSES else 'unknown'


@dataclass
class InputReceipt:
    action: str
    target_hwnd: int
    client_point: tuple
    requested: int
    transport: str = 'SendInput'
    attempt_id: str = field(default_factory=lambda: str(uuid4()))
    clock: str = 'perf_counter_ns'
    started_monotonic_ns: int = field(default_factory=lambda: time.perf_counter_ns())
    finished_monotonic_ns: int | None = None
    target_pid: int | None = None
    status: str = 'not_sent'
    reason: str = ''
    initial_context: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)
    stages: list = field(default_factory=list)
    game_accepted: None = None

    def __bool__(self):
        return self.status == 'native_submitted'

    def finish(self, status=None, reason=''):
        if status is None:
            counts = [stage.get('sent') for stage in self.stages if not stage.get('cleanup')]
            if any(type(count) is not int or count not in (0, 1) for count in counts):
                status = 'unknown'
            elif sum(counts) == self.requested and self.requested:
                status = 'native_submitted'
            elif any(counts):
                status = 'partial'
            else:
                status = 'not_sent'
        self.status = status
        self.reason = reason or self.reason
        self.finished_monotonic_ns = time.perf_counter_ns()
        return self

    def as_dict(self):
        return asdict(self)
