"""Cooperative suspension, not an execution failure."""
class WorkflowPause(Exception):
    def __init__(self, status, **details):
        if status not in {'waiting_input', 'awaiting_confirmation', 'awaiting_review', 'waiting_dependencies'}:
            raise ValueError(status)
        super().__init__(status)
        self.details = {'status': status, **details}
