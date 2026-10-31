"""Observe real mouse input during crafting; injected automation is excluded."""
import time


MOUSE_ACTIONS={0x0201:'left_down',0x0202:'left_up',0x0204:'right_down',0x0205:'right_up',
               0x0207:'middle_down',0x0208:'middle_up',0x020b:'x_down',0x020c:'x_up',
               0x020a:'wheel',0x020e:'horizontal_wheel'}


class CraftManualInputMonitor:
    def __init__(self,task,emit,listener_factory=None):
        self.task=task;self.emit=emit;self.listener=None;self.active=False
        self.listener_factory=listener_factory
        task.craft_manual_inputs=[]
        task.craft_manual_monitor='unavailable'
        task.craft_manual_input_monitor=self

    @property
    def available(self):
        try:return self.active and self.listener is not None and bool(self.listener.is_alive())
        except Exception:return False

    def event_filter(self,message,data):
        # LLMHF_INJECTED and LLMHF_LOWER_IL_INJECTED identify native SendInput.
        # The hook observes, never consumes any user's Windows mouse input.
        flags=int(data.flags)
        if self.active and message in MOUSE_ACTIONS and not flags & 3:
            event={'monotonic_ns':time.perf_counter_ns(),'clock':'perf_counter_ns','kind':MOUSE_ACTIONS[message],
                   'message':message,'flags':flags,'screen':[int(data.pt.x),int(data.pt.y)],
                   'reason':'non_injected_mouse_input_during_crafting'}
            self.task.craft_manual_inputs.append(event)
            try:self.emit('craft_manual_intervention',**event)
            except Exception as error:
                # Retain the event even if diagnostics persistence fails.
                self.task.craft_manual_monitor_error=str(error)
        return True

    def start(self):
        try:
            if self.listener_factory is None:
                from pynput import mouse
                self.listener_factory=mouse.Listener
            self.listener=self.listener_factory(win32_event_filter=self.event_filter)
            self.active=True
            self.listener.start()
            self.task.craft_manual_monitor='active'
        except Exception as error:
            self.active=False
            self.task.craft_manual_monitor='unavailable'
            self.task.craft_manual_monitor_error=str(error)
        return self

    def close(self):
        self.active=False
        if self.listener is not None:
            try:self.listener.stop();self.listener.join(timeout=1)
            except Exception as error:
                self.task.craft_manual_monitor='unavailable'
                self.task.craft_manual_monitor_error=str(error)
            finally:
                if self.task.craft_manual_monitor=='active':self.task.craft_manual_monitor='stopped'

