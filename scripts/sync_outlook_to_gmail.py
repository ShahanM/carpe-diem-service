import base64
import datetime as dt
import json
import os
from typing import Any

import icalendar
import requests
import structlog
from dateutil import parser as dt_parser
from dateutil import rrule, tz
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from horadric_lib.logging import configure_logging

log_path = configure_logging("runtime/log")
logger = structlog.getLogger()

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/tasks.readonly"]

LOCAL_TZ = tz.gettz("America/New_York")
CENTRAL_TZ = tz.gettz("America/Chicago")


def get_env_var(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise ValueError(f"Environment variable {key} is missing!")
    return value


def get_google_service():
    token_b64 = get_env_var("GCP_TOKEN_B64")
    token_b64 += "=" * ((4 - len(token_b64) % 4) % 4)

    token_json = base64.b64decode(token_b64).decode("utf-8")
    token_data = json.loads(token_json)

    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    return build("calendar", "v3", credentials=creds)


def fetch_ics_data() -> str:
    url = get_env_var("ICS_URL")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    if response.text.strip().lower().startswith("<!doctype html>"):
        raise ValueError("CRITICAL: Microsoft returned an HTML webpage. Your ICS link is dead.")

    return response.text


def parse_ical_dt(dt_prop):
    """Extracts datetime from ical property and enforces correct timezone based on Outlook hints."""
    dt_val = getattr(dt_prop, "dt", dt_prop)

    if type(dt_val) is dt.date:
        return dt_val

    if dt_val.tzinfo is not None:
        return dt_val

    tzid = ""
    if hasattr(dt_prop, "params"):
        tzid = str(dt_prop.params.get("TZID", "")).upper()

    if "CENTRAL" in tzid or "CHICAGO" in tzid:
        return dt_val.replace(tzinfo=CENTRAL_TZ)

    return dt_val.replace(tzinfo=LOCAL_TZ)


def get_utc_timestamp(dt_val):
    """Converts any datetime, date, or ISO string to a strict, comparable UTC signature."""
    if isinstance(dt_val, str):
        dt_val = dt_parser.parse(dt_val)

    if type(dt_val) is dt.date:
        return dt_val.strftime("%Y%m%d_ALLDAY")

    if dt_val.tzinfo is None:
        dt_val = dt_val.replace(tzinfo=LOCAL_TZ)

    dt_utc = dt_val.astimezone(dt.UTC)

    if dt_utc.hour == 0 and dt_utc.minute == 0 and dt_utc.second == 0:
        return dt_utc.strftime("%Y%m%d_ALLDAY")

    return dt_utc.strftime("%Y%m%d_%H%M%S")


def parse_ics_to_google_payloads(ics_text, past_limit, future_limit):
    cal = icalendar.Calendar.from_ical(ics_text)
    cancellations = set()

    for component in cal.walk("VEVENT"):
        exdates = component.get("EXDATE")
        if exdates:
            if not isinstance(exdates, list):
                exdates = [exdates]
            for exdate in exdates:
                if hasattr(exdate, "dts"):
                    for d_prop in exdate.dts:
                        dt_aware = parse_ical_dt(d_prop)
                        cancellations.add(get_utc_timestamp(dt_aware))

        status = str(component.get("STATUS", "")).upper()
        summary = str(component.get("SUMMARY", "")).upper()
        if status == "CANCELLED" or "CANCELLED:" in summary or "CANCELED:" in summary:
            rec_id = component.get("RECURRENCE-ID")
            if rec_id:
                dt_aware = parse_ical_dt(rec_id)
                cancellations.add(get_utc_timestamp(dt_aware))

    parsed_events = []

    for component in cal.walk("VEVENT"):
        if component.get("RECURRENCE-ID") or str(component.get("STATUS", "")).upper() == "CANCELLED":
            continue

        summary = str(component.get("summary", "No Title"))
        summary = summary.replace("Canceled: ", "").replace("Cancelled: ", "").strip()

        description = str(component.get("description", ""))
        location = str(component.get("location", ""))

        if "SYNCED_FROM_CLEMSON_OUTLOOK" not in description:
            description = "SYNCED_FROM_CLEMSON_OUTLOOK\n\n" + description

        dtstart_prop = component.get("dtstart")
        dtend_prop = component.get("dtend")

        base_start = parse_ical_dt(dtstart_prop)
        base_end = parse_ical_dt(dtend_prop)

        is_all_day = type(base_start) is dt.date
        duration = base_end - base_start

        rrule_str = component.get("RRULE")
        instances = []

        if is_all_day:
            rrule_start = dt.datetime.combine(base_start, dt.datetime.min.time())
            rrule_past = dt.datetime.combine(past_limit.date(), dt.datetime.min.time())
            rrule_future = dt.datetime.combine(future_limit.date(), dt.datetime.min.time())
        else:
            rrule_start = base_start
            rrule_past = past_limit
            rrule_future = future_limit

        if rrule_str:
            try:
                rule_string = rrule_str.to_ical().decode("utf-8")
                rule = rrule.rrulestr(rule_string, dtstart=rrule_start)
                recurrences = rule.between(rrule_past, rrule_future, inc=True)

                for rec_start in recurrences:
                    if is_all_day:
                        final_rec_start = rec_start.date()
                    else:
                        final_rec_start = rec_start

                    final_rec_end = final_rec_start + duration

                    if get_utc_timestamp(final_rec_start) in cancellations:
                        continue
                    instances.append((final_rec_start, final_rec_end))
            except Exception as e:
                logger.info("[!] Failed to parse RRULE.", summary=summary, error=e)
                if get_utc_timestamp(base_start) not in cancellations:
                    instances.append((base_start, base_end))
        else:
            if get_utc_timestamp(base_start) not in cancellations:
                instances.append((base_start, base_end))

        for final_start, final_end in instances:
            if is_all_day:
                if final_start > future_limit.date() or final_end < past_limit.date():
                    continue
            else:
                if final_start > future_limit or final_end < past_limit:
                    continue

            payload: dict[str, Any] = {
                "summary": summary,
                "description": description,
                "location": location,
            }

            if is_all_day:
                payload["start"] = {"date": final_start.strftime("%Y-%m-%d")}
                payload["end"] = {"date": final_end.strftime("%Y-%m-%d")}
            else:
                payload["start"] = {"dateTime": final_start.isoformat()}
                payload["end"] = {"dateTime": final_end.isoformat()}

            parsed_events.append(payload)

    return parsed_events


def sync_events(service, calendar_id, ics_events, google_events):
    logger.info("STARTING SYNC")

    google_map = {}
    for g_ev in google_events:
        start_raw = g_ev["start"].get("dateTime", g_ev["start"].get("date"))
        start_sig = get_utc_timestamp(start_raw)
        title = g_ev.get("summary", "No Title")
        google_map[(title, start_sig)] = g_ev

    ics_map = {}
    for i_ev in ics_events:
        start_raw = i_ev["start"].get("dateTime", i_ev["start"].get("date"))
        start_sig = get_utc_timestamp(start_raw)
        title = i_ev.get("summary", "No Title")
        ics_map[(title, start_sig)] = i_ev

    google_keys = set(google_map.keys())
    ics_keys = set(ics_map.keys())

    to_delete = google_keys - ics_keys
    to_create = ics_keys - google_keys
    to_check = google_keys & ics_keys

    logger.info("Events", to_delete=len(to_delete), to_create=len(to_create), to_check=len(to_check))

    for key in to_delete:
        event_id = google_map[key]["id"]
        try:
            service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
            logger.info("[-] Deleted", key=key[0])
        except Exception as e:
            logger.error("[!] Failed to delete", key=key[0], error=e)

    for key in to_create:
        body = ics_map[key]
        try:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            logger.info("[+] Created", key=key[0])
        except Exception as e:
            logger.error("[!] Failed to create", key=key[0], error=e)

    for key in to_check:
        g_ev = google_map[key]
        i_ev = ics_map[key]

        desc_changed = g_ev.get("description", "") != i_ev.get("description", "")
        loc_changed = g_ev.get("location", "") != i_ev.get("location", "")

        if desc_changed or loc_changed:
            g_ev["description"] = i_ev.get("description", "")
            g_ev["location"] = i_ev.get("location", "")
            try:
                service.events().update(calendarId=calendar_id, eventId=g_ev["id"], body=g_ev).execute()
                logger.info("[*] Updated", key=key[0])
            except Exception as e:
                logger.error("[!] Failed to update", key=key[0], error=e)

    logger.info("SYNC COMPLETE")


def main():
    logger.info("Initializing sync engine...")
    calendar_id = get_env_var("TARGET_CALENDAR_ID")
    service = get_google_service()

    ics_text = fetch_ics_data()
    logger.info("Fetched data", total_bytes=len(ics_text))

    now = dt.datetime.now(dt.UTC)
    past_limit = now - dt.timedelta(days=15)
    future_limit = now + dt.timedelta(days=15)

    parsed_ics_events = parse_ics_to_google_payloads(ics_text, past_limit, future_limit)
    logger.info("Parsed and flattened", total_instances=len(parsed_ics_events))

    logger.info("Fetching existing events from Google Calendar...")
    google_events_result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=past_limit.isoformat(),
            timeMax=future_limit.isoformat(),
            singleEvents=True,
            q="SYNCED_FROM_CLEMSON_OUTLOOK",
        )
        .execute()
    )
    existing_google_events = google_events_result.get("items", [])

    if parsed_ics_events:
        sync_events(service, calendar_id, parsed_ics_events, existing_google_events)
    else:
        logger.warn("Parsed ICS events list is empty. Skipping sync to prevent wiping calendar.")


if __name__ == "__main__":
    main()
