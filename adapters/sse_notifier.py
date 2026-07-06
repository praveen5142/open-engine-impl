from ports.notifier import NotifierPort
import json

class SSENotifier(NotifierPort):
    def __init__(self, broadcast_fn):
        self.broadcast_fn = broadcast_fn
        
    def notify_hold(self, task_id: int, role: str, reason: str) -> None:
        self.broadcast_fn("hold_declared", {
            "task_id": task_id,
            "role": role,
            "reason": reason
        })
