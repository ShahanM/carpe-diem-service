import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field, field_validator


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
    """Calendar Event schema.

    Note: Calendar events are considered equal based only on their title, start time, and end time.
    This aligns with the project goal to minimize clutter. We do not want to keep any information regarding
    the calendar the event came from.
    """

    id: str
    title: str
    start_time: dt.datetime
    end_time: dt.datetime
    source_id: str

    @field_validator("start_time", "end_time")
    @classmethod
    def fix_lying_outlook_timezone(cls, v: dt.datetime) -> dt.datetime:
        """
        If the time claims to be UTC but we know it's actually Local
        (common with broken Exchange/Outlook syncs), we strip the TZ
        and force Local.
        """
        # Get system local timezone
        local_tz = dt.datetime.now().astimezone().tzinfo

        # Check if it looks like the bug (U)
        # You might want to flag this via a specific check if possible
        if v.tzinfo == dt.UTC:
            return v.replace(tzinfo=local_tz)

        # Normal behavior for other times
        return v.astimezone(local_tz)

    def __eq__(self, other: object) -> bool:
        """Overridden equality method to treat duplicate event from multiple sources as equal."""
        if isinstance(other, CalendarEventSchema):
            title_eq = self.title == other.title
            start_eq = self.start_time == other.start_time
            end_eq = self.end_time == other.end_time

            return title_eq and start_eq and end_eq
        return NotImplemented

    def __hash__(self):
        """Overridden hash to ensure that events are compared based on their title and start-end time."""
        time_fmt = "%d%m%Y%H:%M:%S"
        start_str = self.start_time.strftime(time_fmt)
        end_str = self.end_time.strftime(time_fmt)

        return hash(f"{self.title}_{start_str}_{end_str}")
