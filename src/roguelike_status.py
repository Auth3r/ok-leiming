"""Bounded atomic metadata replacement shared by the runner and Qt panel."""
import time

def replace_status(temp,path):
    for attempt in range(3):
        try:
            temp.replace(path);return
        except PermissionError:
            if attempt==2:raise
            time.sleep(.01)
