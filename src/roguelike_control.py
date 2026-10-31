"""Shared task lifecycle; temporary pauses never become cancellations."""
import time
from threading import Condition, Event, RLock

from src.workflow import SafetyStop


class Reobserve(RuntimeError):
    """Unwind a prepared action, then classify a fresh frame in the runner loop."""


class RoguelikeControl:
    TERMINAL = frozenset(('STOPPED', 'CANCELLED', 'ERROR', 'SUCCESS', 'PARTIAL'))

    def __init__(self, clock=time.monotonic):
        self.raw_clock = clock
        self.cancel_event = Event()
        self.condition = Condition(RLock())
        self.state = 'RUNNING'
        self.stop_source = None
        self.revision = 0
        self.pauses = set()
        self.pause_started = None
        self.paused_seconds = 0

    def clock(self):
        with self.condition:
            return (self.pause_started if self.pause_started is not None else self.raw_clock()) - self.paused_seconds

    def set_paused(self, source, paused):
        with self.condition:
            if self.state in self.TERMINAL:return
            if paused:
                if not self.pauses:
                    self.pause_started = self.raw_clock()
                    self.revision += 1
                self.pauses.add(source)
                self.state = 'PAUSED'
            else:
                self.pauses.discard(source)
                if not self.pauses:
                    if self.pause_started is not None:
                        self.paused_seconds += self.raw_clock() - self.pause_started
                    self.pause_started = None
                    self.state = 'RUNNING'
            self.condition.notify_all()

    def finish(self, state, source=None):
        with self.condition:
            if self.state in self.TERMINAL:return
            if state not in self.TERMINAL:raise ValueError(state)
            self.state = state
            self.stop_source = source
            if state not in ('SUCCESS','PARTIAL'):self.cancel_event.set()
            self.pauses.clear()
            self.condition.notify_all()

    def resume(self):
        """A deliberate resume acknowledges all pause sources and reobserves."""
        with self.condition:
            if self.state != 'PAUSED':return False
            for source in tuple(self.pauses):self.set_paused(source,False)
            return True

    def toggle_pause(self, source='user'):
        with self.condition:
            if self.state in self.TERMINAL:return
            if self.state == 'PAUSED':self.resume()
            else:self.set_paused(source,True)

    def checkpoint(self):
        with self.condition:
            while self.state == 'PAUSED':self.condition.wait(.05)
            if self.state in self.TERMINAL:
                raise SafetyStop('肉鸽任务已终止：'+self.state+('；来源：'+self.stop_source if self.stop_source else ''))

    def input(self, revision, callback):
        # Pause/Stop and dispatch share a lock. An already dispatched mouse-up
        # may finish; no new input starts after a pause/cancellation is accepted.
        with self.condition:
            if self.state in self.TERMINAL:raise SafetyStop('肉鸽任务已终止：'+self.state)
            if self.state != 'RUNNING' or self.revision != revision:raise Reobserve()
            return callback()
