import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field


class TaskSource(str, Enum):
    LOCAL = "Local"
    EVOLUTION = "Evolution"
    GITHUB = "GitHub"


class Task(BaseModel):
    id: str
    title: str
    completed: bool = False
    source: TaskSource
    parent_event_id: str | None = None
    time_spent_seconds: int = 0
    is_active: bool = False


class ItemType(str, Enum):
    EVENT_FOLDER = "EventFolder"
    GAP_TASK = "GapTask"


class TimelineItem(BaseModel):
    item_type: ItemType
    title: str
    start_time: dt.datetime
    end_time: dt.datetime | None = None
    # For 'EventFolder', these are tasks dragged into the event
    # For 'GapTask', this will typically be empty or contain the specific task
    nested_tasks: list[Task] = Field(default_factory=list[Task])
    source_id: str | None = None  # Original EDS or GitHub ID


class AppSettings(BaseModel):
    cushion_mins: int = 5
    default_task_duration_mins: int = 30
    enabled_calendars: list[str] = Field(default_factory=list)
    github_token_set: bool = False


import datetime as dt

from pydantic import BaseModel


class CalendarSchema(BaseModel):
    id: str
    name: str
    color: str
    enabled: bool


class CalendarEventSchema(BaseModel):
    id: str
    title: str
    start_time: dt.datetime
    end_time: dt.datetime
    source_id: str
