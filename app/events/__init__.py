"""In-process typed event bus. See docs/specs/01 §"Boundary rules" #3."""

from app.events.bus import EventBus, bus
from app.events.registry import (
    Event,
    EventAlreadyRegistered,
    EventNotFound,
    get_event_type,
    register,
    registered_events,
)
from app.events.types import (
    ExpenseApproved,
    ShiftChanged,
    ShiftChangedAction,
    ShiftEnded,
    StayUpcoming,
    TaskCompleted,
    TaskCreated,
    TaskOverdue,
)

__all__ = [
    "Event",
    "EventAlreadyRegistered",
    "EventBus",
    "EventNotFound",
    "ExpenseApproved",
    "ShiftChanged",
    "ShiftChangedAction",
    "ShiftEnded",
    "StayUpcoming",
    "TaskCompleted",
    "TaskCreated",
    "TaskOverdue",
    "bus",
    "get_event_type",
    "register",
    "registered_events",
]
