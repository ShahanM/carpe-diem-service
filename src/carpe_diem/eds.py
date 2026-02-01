import configparser
import datetime as dt
from typing import Any, Literal, TypeGuard, cast

import icalendar
from dateutil import rrule
from sdbus import (
    DbusInterfaceCommonAsync,
    DbusObjectManagerInterfaceAsync,
    dbus_method_async,
)

from .models import CalendarEventSchema, CalendarSchema

EXPOSED_CALENDAR_URI = "org.gnome.evolution.dataserver.Source.Calendar"
EVOLUTION_DS_SOURCE_URI = "org.gnome.evolution.dataserver.Source"


def is_sdbus_variant(val: Any) -> TypeGuard[tuple[str, Any]]:
    # sdbus might return variants as tuples (signature, value) if not auto-unwrapped
    if not isinstance(val, tuple):
        return False

    v = cast(tuple[Any, ...], val)
    return len(v) == 2 and isinstance(v[0], str)


class CalendarFactoryInterface(
    DbusInterfaceCommonAsync,
    interface_name="org.gnome.evolution.dataserver.CalendarFactory",
):
    """
    org.gnome.evolution.dataserver.CalendarFactory interface.
    Used to open a calendar using its source UID.
    """

    @dbus_method_async(input_signature="s", result_signature="ss", method_name="OpenCalendar")
    async def open_calendar(self, source_uid: str) -> str:
        """Returns (object_path, bus_name)."""
        ...


class CalendarInterface(DbusInterfaceCommonAsync, interface_name="org.gnome.evolution.dataserver.Calendar"):
    """
    org.gnome.evolution.dataserver.Calendar interface.
    Represents an open calendar instance.
    """

    @dbus_method_async(input_signature="s", result_signature="as", method_name="GetObjectList")
    async def get_object_list(self, query: str) -> list[str]: ...


class EDSCalendarFetcher:
    def __init__(self):
        # socket connection is handled implicitly by sdbus
        pass

    async def _init_connection(self):
        # Create interfaces on demand using new_proxy factory
        self.registry = DbusObjectManagerInterfaceAsync.new_proxy(
            "org.gnome.evolution.dataserver.Sources5",
            "/org/gnome/evolution/dataserver/SourceManager",
        )
        self.factory = CalendarFactoryInterface.new_proxy(
            "org.gnome.evolution.dataserver.Calendar8",
            "/org/gnome/evolution/dataserver/CalendarFactory",
        )

    def _unwrap(self, val: tuple[str, Any] | Any) -> Any:
        if is_sdbus_variant(val):
            return val[1]
        return val

    async def get_calendars(self) -> list[CalendarSchema]:
        """
        Finds all calendar sources in Evolution that are enabled.
        Returns a list of dicts with 'id', 'name', 'color', 'enabled'.
        """

        if not hasattr(self, "registry"):
            await self._init_connection()

        try:
            objects = await self.registry.get_managed_objects()
        except Exception as e:
            print(f"Error fetching managed objects: {e}")
            return []

        calendars: list[CalendarSchema] = []

        for path, interfaces in objects.items():
            source_iface = interfaces.get(EVOLUTION_DS_SOURCE_URI, {})

            # Check for explicitly exposed Calendar interface first
            has_calendar_interface = EXPOSED_CALENDAR_URI in interfaces

            # Fallback/Primary: Parse the 'Data' property which contains the raw key-file content
            data_raw = self._unwrap(source_iface.get("Data", ""))

            # Try to get UID from interface properties first (most reliable)
            uid = self._unwrap(source_iface.get("UID", source_iface.get("Uid", "")))

            name = "Unknown"
            color = "#000000"
            enabled = True
            is_calendar = has_calendar_interface

            if data_raw:
                try:
                    config = configparser.ConfigParser(allow_no_value=True, interpolation=None)
                    config.read_string(data_raw)

                    if config.has_section("Calendar"):
                        is_calendar = True
                        if config.has_option("Calendar", "Color"):
                            color = config.get("Calendar", "Color")

                    if config.has_section("Data Source"):
                        if config.has_option("Data Source", "DisplayName"):
                            name = config.get("Data Source", "DisplayName")
                        if config.has_option("Data Source", "Enabled"):
                            enabled = config.getboolean("Data Source", "Enabled")
                        # If we didn't get UID from interface, try config
                        if not uid and config.has_option("Data Source", "Uid"):
                            uid = config.get("Data Source", "Uid")

                except Exception as parse_err:
                    print(f"Error parsing source data for {path}: {parse_err}")

            # Last resort fallback to path
            if not uid:
                uid = path.split("/")[-1]

            if is_calendar and enabled:
                cal = CalendarSchema(id=uid, name=name, color=color, enabled=True)
                calendars.append(cal)

        return calendars

    async def fetch_events(self, target_date: dt.date) -> set[CalendarEventSchema]:
        """Query events for a specific date (24-hour range)."""

        WEEKDAY_MAP: dict[str, rrule.weekday] = {
            "MO": rrule.MO,
            "TU": rrule.TU,
            "WE": rrule.WE,
            "TH": rrule.TH,
            "FR": rrule.FR,
            "SA": rrule.SA,
            "SU": rrule.SU,
        }

        calendars = await self.get_calendars()
        if not calendars:
            return set()

        # Convert to UTC for the query to be safe and standard
        dt_start_query = (
            dt.datetime.combine(target_date, dt.datetime.min.time()).replace(tzinfo=None).astimezone(dt.UTC)
        )
        dt_end_query = dt_start_query + dt.timedelta(days=1)

        # EDS sexp expects simple ISO usually
        start_str = dt_start_query.strftime("%Y%m%dT%H%M%SZ")
        end_str = dt_end_query.strftime("%Y%m%dT%H%M%SZ")
        sexp = f'(occur-in-time-range? (make-time "{start_str}") (make-time "{end_str}"))'

        all_events: set[CalendarEventSchema] = set()

        for cal_info in calendars:
            try:
                # OpenCalendar returns (object_path, bus_name)
                cal_path, bus_name = await self.factory.open_calendar(cal_info.id)
                cal_proxy = CalendarInterface.new_proxy(bus_name, cal_path)
                ical_strings = await cal_proxy.get_object_list(sexp)

                for ical_data in ical_strings:
                    try:
                        cal_obj = icalendar.Calendar.from_ical(ical_data)

                        for component in cal_obj.walk():
                            if component.name == "VEVENT":
                                summary = str(component.get("summary", "Untitled"))  # Title of the event
                                uid = str(component.get("uid", ""))

                                # Extract base times
                                raw_start = component.get("dtstart").dt
                                raw_end = component.get("dtend").dt

                                # Normalize base times to UTC/Aware for calculations
                                if type(raw_start) is dt.date:
                                    base_start = dt.datetime.combine(raw_start, dt.datetime.min.time()).replace(
                                        tzinfo=dt.UTC
                                    )
                                    base_end = dt.datetime.combine(raw_end, dt.datetime.min.time()).replace(
                                        tzinfo=dt.UTC
                                    )
                                else:
                                    base_start = raw_start if raw_start.tzinfo else raw_start.replace(tzinfo=dt.UTC)
                                    base_end = raw_end if raw_end.tzinfo else raw_end.replace(tzinfo=dt.UTC)

                                duration = base_end - base_start

                                # Recurrence rule
                                rrule_component = component.get("RRULE")
                                event_instances: list[tuple[dt.datetime, dt.datetime]] = []

                                if rrule_component:
                                    try:
                                        freq_map: dict[str, Literal[0, 1, 2, 3, 4, 5, 6]] = {
                                            "WEEKLY": rrule.WEEKLY,
                                            "DAILY": rrule.DAILY,
                                            "MONTHLY": rrule.MONTHLY,
                                            "YEARLY": rrule.YEARLY,
                                        }

                                        # Extract freq (required)
                                        freq_str = rrule_component.get("FREQ", ["WEEKLY"])[0]
                                        freq: Literal[0, 1, 2, 3, 4, 5, 6] = freq_map.get(freq_str, rrule.WEEKLY)

                                        # Extract byday (optional)
                                        byweekday: list[rrule.weekday] = []
                                        if "BYDAY" in rrule_component:
                                            # Handle both single string or list of strings
                                            days: list[str] | str = rrule_component["BYDAY"]
                                            if not isinstance(days, list):
                                                days = [days]
                                            assert isinstance(days, list)
                                            byweekday = [WEEKDAY_MAP[d] for d in days if d in WEEKDAY_MAP]

                                        # Extract until (optional)
                                        until = None
                                        if "UNTIL" in rrule_component:
                                            until = rrule_component["UNTIL"][0]
                                            if isinstance(until, dt.datetime) and until.tzinfo is None:
                                                until = until.replace(tzinfo=dt.UTC)

                                        rule = rrule.rrule(
                                            freq,
                                            dtstart=base_start,
                                            until=until,
                                            byweekday=byweekday if byweekday else None,
                                        )

                                        # Find instances of events within the target time window
                                        search_start = dt_start_query - dt.timedelta(hours=24)
                                        search_end = dt_end_query + dt.timedelta(hours=24)

                                        recurrences = rule.between(search_start, search_end, inc=True)

                                        for rec_start in recurrences:
                                            # We only care if the event actually overlaps our target day
                                            rec_end = rec_start + duration
                                            if (rec_start < dt_end_query) and (rec_end > dt_start_query):
                                                event_instances.append((rec_start, rec_end))

                                    except Exception as rrule_err:
                                        print(f"Failed to expand RRULE for {uid}: {rrule_err}")
                                        # Fallback: Just add the base event if parsing fails
                                        event_instances.append((base_start, base_end))

                                else:
                                    # Non-recurring event
                                    event_instances.append((base_start, base_end))

                                for final_start, final_end in event_instances:
                                    print(
                                        cal_info.name,
                                        component.get("summary"),
                                        final_start,
                                        component.get("dtstart"),
                                        component.get("dtend"),
                                    )
                                    cal_event = CalendarEventSchema(
                                        id=uid,
                                        title=summary,
                                        start_time=final_start,
                                        end_time=final_end,
                                        source_id=cal_info.id,
                                    )
                                    all_events.add(cal_event)
                    except Exception as parse_err:
                        print(f"Error parsing ical: {parse_err}")

            except Exception as e:
                print(f"Error fetching from calendar {cal_info.id}: {e}")

        return all_events


fetcher = EDSCalendarFetcher()
