import configparser
import datetime as dt
from typing import Any, TypeGuard, cast

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

    @dbus_method_async(
        input_signature="s", result_signature="ss", method_name="OpenCalendar"
    )
    async def open_calendar(self, source_uid: str) -> str:
        """Returns (object_path, bus_name)."""
        ...


class CalendarInterface(
    DbusInterfaceCommonAsync, interface_name="org.gnome.evolution.dataserver.Calendar"
):
    """
    org.gnome.evolution.dataserver.Calendar interface.
    Represents an open calendar instance.
    """

    @dbus_method_async(
        input_signature="s", result_signature="as", method_name="GetObjectList"
    )
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
                    config = configparser.ConfigParser(
                        allow_no_value=True, interpolation=None
                    )
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

    async def fetch_events(self, target_date: dt.date) -> list[CalendarEventSchema]:
        """Query events for a specific date (24-hour range)."""

        import icalendar

        calendars = await self.get_calendars()
        print("calendars", calendars)
        if not calendars:
            return []

        # Convert to UTC for the query to be safe and standard
        dt_start = (
            dt.datetime.combine(target_date, dt.datetime.min.time())
            .replace(tzinfo=None)
            .astimezone(dt.UTC)
        )
        dt_end = dt_start + dt.timedelta(days=1)

        # EDS sexp expects simple ISO usually
        start_str = dt_start.strftime("%Y%m%dT%H%M%SZ")
        end_str = dt_end.strftime("%Y%m%dT%H%M%SZ")

        sexp = (
            f'(occur-in-time-range? (make-time "{start_str}") (make-time "{end_str}"))'
        )

        all_events: list[CalendarEventSchema] = []

        for cal_info in calendars:
            try:
                # OpenCalendar returns (object_path, bus_name)
                cal_path, bus_name = await self.factory.open_calendar(cal_info.id)

                # Connect to the specific subprocess bus_name
                cal_proxy = CalendarInterface.new_proxy(bus_name, cal_path)

                ical_strings = await cal_proxy.get_object_list(sexp)

                for ical_data in ical_strings:
                    try:
                        cal_obj = icalendar.Calendar.from_ical(ical_data)
                        for component in cal_obj.walk():
                            if component.name == "VEVENT":
                                # Extract details
                                summary = str(component.get("summary", "Untitled"))
                                uid = str(component.get("uid", ""))

                                from datetime import (
                                    date,
                                )  # validation check needs date class

                                start_dt = component.get("dtstart").dt
                                end_dt = (
                                    component.get("dtend").dt
                                    if component.get("dtend")
                                    else start_dt
                                )

                                # Handle all-day events (date objects)
                                if type(start_dt) is date:
                                    start_dt = dt.datetime.combine(
                                        start_dt, dt.datetime.min.time()
                                    )
                                    # Use local tz for all day events usually, or UTC.
                                    # If we assume service local time is relevant:
                                    start_dt = start_dt.replace(
                                        tzinfo=dt.datetime.now().astimezone().tzinfo
                                    )

                                if type(end_dt) is date:
                                    end_dt = dt.datetime.combine(
                                        end_dt, dt.datetime.min.time()
                                    )
                                    end_dt = end_dt.replace(
                                        tzinfo=dt.datetime.now().astimezone().tzinfo
                                    )

                                # Ensure timezone awareness for naive datetimes
                                if (
                                    isinstance(start_dt, dt.datetime)
                                    and start_dt.tzinfo is None
                                ):
                                    start_dt = start_dt.replace(tzinfo=dt.UTC)
                                if (
                                    isinstance(end_dt, dt.datetime)
                                    and end_dt.tzinfo is None
                                ):
                                    end_dt = end_dt.replace(tzinfo=dt.UTC)
                                cal_event = CalendarEventSchema(
                                    id=uid,
                                    title=summary,
                                    start_time=start_dt,
                                    end_time=end_dt,
                                    source_id=cal_info.id,
                                )
                                all_events.append(cal_event)
                    except Exception as parse_err:
                        print(f"Error parsing ical: {parse_err}")

            except Exception as e:
                print(f"Error fetching from calendar {cal_info.id}: {e}")

        return all_events


fetcher = EDSCalendarFetcher()
