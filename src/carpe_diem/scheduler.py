from datetime import datetime, timedelta

from carpe_diem.models import CalendarEventSchema, ItemType, Task, TimelineItem


def resolve_timeline(
    events: list[CalendarEventSchema],
    all_tasks: list[Task],
    cushion_mins: int = 5,
    default_task_duration: int = 15,
) -> list[TimelineItem]:
    """
    Orchestrates the day by nesting tasks into events and filling gaps
    between meetings with standalone tasks.
    """
    timeline: list[TimelineItem] = []

    standalone_tasks = [
        t for t in all_tasks if t.parent_event_id is None and not t.completed
    ]
    sorted_events = sorted(events, key=lambda x: x.start_time)

    # Use current time as the starting point for scheduling
    # Ensure current_time is timezone-aware to match events
    current_time: datetime = datetime.now().astimezone()
    cushion = timedelta(minutes=cushion_mins)
    task_dur = timedelta(minutes=default_task_duration)

    for event in sorted_events:
        # Fill Gaps Before This Event
        # We check if there's enough space for at least one task + cushion
        while (
            standalone_tasks and (current_time + task_dur + cushion) <= event.start_time
        ):
            task = standalone_tasks.pop(0)
            timeline.append(
                TimelineItem(
                    item_type=ItemType.GAP_TASK,
                    title=task.title,
                    start_time=current_time,
                    end_time=current_time + task_dur,
                    nested_tasks=[task],
                )
            )
            current_time += task_dur + cushion

        # Add the Event Folder
        nested = [t for t in all_tasks if t.parent_event_id == event.id]
        timeline.append(
            TimelineItem(
                item_type=ItemType.EVENT_FOLDER,
                title=event.title,
                start_time=event.start_time,
                end_time=event.end_time,
                nested_tasks=nested,
                source_id=event.id,
            )
        )

        # Advance time to the end of the meeting + cushion
        current_time = max(current_time, event.end_time + cushion)

    # Append remaining standalone tasks at the end of the day
    for task in standalone_tasks:
        timeline.append(
            TimelineItem(
                item_type=ItemType.GAP_TASK,
                title=task.title,
                start_time=current_time,
                end_time=current_time + task_dur,
                nested_tasks=[task],
            )
        )
        current_time += task_dur + cushion

    return timeline
