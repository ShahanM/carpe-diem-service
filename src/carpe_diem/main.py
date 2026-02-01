import datetime as dt

import uvicorn
from fastapi import FastAPI

from carpe_diem.eds import EDSCalendarFetcher
from carpe_diem.models import Task, TimelineItem
from carpe_diem.scheduler import resolve_timeline

app = FastAPI(title="Carpe Diem Sidecar")


@app.get("/timeline", response_model=list[TimelineItem])
async def get_timeline(date: dt.date | None = None):
    if date is None:
        date = dt.datetime.now().date()
    print(date)
    fetcher = EDSCalendarFetcher()
    events = await fetcher.fetch_events(date)

    tasks: list[Task] = []

    timeline = resolve_timeline(events, tasks)

    return timeline


if __name__ == "__main__":
    uvicorn.run("carpe_diem.main:app", host="127.0.0.1", port=8090, reload=True)
