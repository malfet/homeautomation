#!/usr/bin/env python3
"""WeatherLink v2 cloud client with local SQLite cache."""

import datetime
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import zoneinfo
from pathlib import Path

API_BASE = "https://api.weatherlink.com/v2"
HERE = Path(__file__).parent
CREDS_PATH = HERE / "weatherlink_credentials.json"
DB_PATH = HERE / "weatherlink.db"

# v2 historic endpoint accepts at most 24h per call.
HISTORIC_WINDOW = 24 * 3600


def load_creds(path: Path = CREDS_PATH) -> tuple[str, str]:
    c = json.loads(path.read_text())
    return c["key_v2"], c["secret"]


def api_get(endpoint: str, params: dict | None = None) -> dict:
    key, secret = load_creds()
    q = dict(params or {})
    q["api-key"] = key
    url = f"{API_BASE}{endpoint}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"X-Api-Secret": secret})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def db_connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stations (
            station_id INTEGER PRIMARY KEY,
            name TEXT,
            product_number TEXT,
            gateway_hex TEXT,
            city TEXT,
            region TEXT,
            time_zone TEXT,
            recording_interval INTEGER,
            firmware_version TEXT,
            raw_json TEXT,
            updated_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS readings (
            station_id INTEGER NOT NULL,
            lsid INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            sensor_type INTEGER,
            data_structure_type INTEGER,
            data_json TEXT NOT NULL,
            PRIMARY KEY (station_id, lsid, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_readings_station_ts
            ON readings (station_id, ts);
    """)
    return conn


def upsert_stations(conn: sqlite3.Connection, stations: list[dict]) -> int:
    now = int(time.time())
    rows = [
        (
            s["station_id"],
            s.get("station_name"),
            s.get("product_number"),
            s.get("gateway_id_hex"),
            s.get("city"),
            s.get("region"),
            s.get("time_zone"),
            s.get("recording_interval"),
            s.get("firmware_version"),
            json.dumps(s),
            now,
        )
        for s in stations
    ]
    conn.executemany(
        """INSERT INTO stations (station_id, name, product_number, gateway_hex,
               city, region, time_zone, recording_interval, firmware_version,
               raw_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(station_id) DO UPDATE SET
               name=excluded.name,
               product_number=excluded.product_number,
               gateway_hex=excluded.gateway_hex,
               city=excluded.city,
               region=excluded.region,
               time_zone=excluded.time_zone,
               recording_interval=excluded.recording_interval,
               firmware_version=excluded.firmware_version,
               raw_json=excluded.raw_json,
               updated_at=excluded.updated_at""",
        rows,
    )
    conn.commit()
    return len(rows)


def insert_sensor_payload(conn: sqlite3.Connection, station_id: int, payload: dict) -> int:
    """Insert sensor records from /current or /historic response. Returns row count."""
    rows = []
    for sensor in payload.get("sensors", []):
        lsid = sensor.get("lsid")
        st = sensor.get("sensor_type")
        dst = sensor.get("data_structure_type")
        for d in sensor.get("data") or []:
            ts = d.get("ts") or d.get("last_packet_received_timestamp")
            if ts is None:
                continue
            rows.append((station_id, lsid, ts, st, dst, json.dumps(d)))
    if rows:
        conn.executemany(
            """INSERT OR REPLACE INTO readings
               (station_id, lsid, ts, sensor_type, data_structure_type, data_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    return len(rows)


def sync_stations(conn: sqlite3.Connection) -> None:
    payload = api_get("/stations")
    n = upsert_stations(conn, payload.get("stations", []))
    print(f"synced {n} station(s)")


def list_station_ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT station_id FROM stations ORDER BY station_id")]


def sync_current(conn: sqlite3.Connection, station_ids: list[int]) -> None:
    for sid in station_ids:
        payload = api_get(f"/current/{sid}")
        n = insert_sensor_payload(conn, sid, payload)
        print(f"station {sid}: stored {n} sensor record(s) from /current")


def sync_historic(conn: sqlite3.Connection, station_id: int, days: int) -> None:
    end = int(time.time())
    start = end - days * 86400
    total = 0
    cur = start
    while cur < end:
        win_end = min(cur + HISTORIC_WINDOW, end)
        payload = api_get(
            f"/historic/{station_id}",
            {"start-timestamp": cur, "end-timestamp": win_end},
        )
        n = insert_sensor_payload(conn, station_id, payload)
        total += n
        t0 = datetime.datetime.fromtimestamp(cur, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
        t1 = datetime.datetime.fromtimestamp(win_end, tz=datetime.timezone.utc).strftime("%H:%M")
        print(f"  {t0}–{t1} UTC: {n} record(s)")
        cur = win_end
    print(f"station {station_id}: stored {total} historic record(s) over {days} day(s)")


def _fmt_ts(ts: int | None, tz: str) -> str:
    if ts is None:
        return "n/a"
    return datetime.datetime.fromtimestamp(ts, zoneinfo.ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S %Z")


STATUS_FRESH_SECS = 3600  # auto-refresh /current if local data is older than this


def status(conn: sqlite3.Connection) -> None:
    """Print last-known reception/battery state for each station.

    If a station's most recent local reading is older than STATUS_FRESH_SECS,
    pull /current for it before printing.
    """
    rows = conn.execute(
        "SELECT station_id, name, time_zone FROM stations ORDER BY station_id"
    ).fetchall()
    if not rows:
        print("no stations in DB; run: weatherlink.py sync-stations")
        return
    now = int(time.time())
    stale = []
    for sid, _, _ in rows:
        latest = conn.execute(
            "SELECT MAX(ts) FROM readings WHERE station_id = ?", (sid,)
        ).fetchone()[0]
        if latest is None or now - latest > STATUS_FRESH_SECS:
            stale.append(sid)
    if stale:
        print(f"refreshing {len(stale)} stale station(s): {stale}")
        sync_current(conn, stale)
    for sid, name, tz in rows:
        tz = tz or "UTC"
        print(f"\n[{sid}] {name}  ({tz})")
        # Latest record per (lsid, data_structure_type) for this station.
        sensors = conn.execute(
            """SELECT lsid, data_structure_type, ts, data_json
               FROM readings r
               WHERE station_id = ? AND ts = (
                   SELECT MAX(ts) FROM readings
                   WHERE station_id = r.station_id AND lsid = r.lsid
               )
               ORDER BY data_structure_type, lsid""",
            (sid,),
        ).fetchall()
        if not sensors:
            print("  (no readings — try: sync-current or sync-historic)")
            continue
        for lsid, dst, ts, data_json in sensors:
            d = json.loads(data_json)
            label = {
                23: "ISS (outdoor)",
                27: "console health",
                19: "barometer",
                21: "indoor",
            }.get(dst, f"data_structure_type={dst}")
            print(f"  lsid {lsid}  {label}  reading@ {_fmt_ts(ts, tz)}")
            if dst == 23:  # ISS — what the user cares about
                last = d.get("last_packet_received_timestamp")
                age_min = (now - last) / 60 if last else None
                age = f"{age_min:.1f} min ago" if age_min is not None and age_min < 1440 else (
                    f"{age_min/60:.1f} h ago" if age_min is not None else "n/a")
                print(f"      last packet:    {_fmt_ts(last, tz)}  ({age})")
                print(f"      rx_state:       {d.get('rx_state')}  (0=ok, 1=lost, 2=syncing)")
                print(f"      battery_volt:   {d.get('trans_battery_volt')} V"
                      f"   battery_flag={d.get('trans_battery_flag')}  (0=ok, 1=low)")
                print(f"      supercap:       {d.get('supercap_volt')} V"
                      f"   solar_panel={d.get('solar_panel_volt')} V")
                print(f"      rssi:           {d.get('rssi_last')} dB"
                      f"   reception_day={d.get('reception_day')}%"
                      f"   resyncs_day={d.get('resyncs_day')}")
                print(f"      temp:           {d.get('temp')}F"
                      f"   hum={d.get('hum')}%"
                      f"   wind={d.get('wind_speed_last')} mph @ {d.get('wind_dir_last')}°")
            elif dst == 27:  # console
                print(f"      console sw:     {d.get('console_sw_version')}"
                      f"   wifi_rssi={d.get('wifi_rssi')} dB"
                      f"   ip={d.get('ip_v4_address')}")
                print(f"      battery:        {d.get('battery_percent')}%"
                      f"   charger={d.get('charger_plugged')}"
                      f"   uptime={d.get('os_uptime', 0)//3600}h")


def reception_history(conn: sqlite3.Connection, station_id: int, hours: int = 48) -> None:
    """Show ISS reception state over the last N hours from historic readings."""
    rows = conn.execute(
        """SELECT ts, data_json FROM readings
           WHERE station_id = ? AND data_structure_type = 23
             AND ts > ?
           ORDER BY ts""",
        (station_id, int(time.time()) - hours * 3600),
    ).fetchall()
    tz_row = conn.execute("SELECT time_zone FROM stations WHERE station_id=?", (station_id,)).fetchone()
    tz = (tz_row and tz_row[0]) or "UTC"
    if not rows:
        print(f"no ISS records for station {station_id} in the last {hours}h")
        return
    print(f"reception history for station {station_id} (last {hours}h, {len(rows)} records):")
    print(f"{'time':<26}{'rx':>4}{'reception%':>13}{'rssi':>8}{'batt_v':>9}")
    prev_rx = None
    for ts, dj in rows:
        d = json.loads(dj)
        rx = d.get("rx_state")
        marker = "  *" if prev_rx is not None and rx != prev_rx else ""
        print(f"{_fmt_ts(ts, tz):<26}{str(rx):>4}{str(d.get('reception_day')):>13}"
              f"{str(d.get('rssi_last')):>8}{str(d.get('trans_battery_volt')):>9}{marker}")
        prev_rx = rx


def usage() -> None:
    print(f"""usage: {sys.argv[0]} <verb> [args]

verbs:
  sync-stations              fetch station list from cloud, store in DB
  sync-current [station_id]  fetch /current for one or all stations
  sync-historic <id> [days]  backfill historic readings (default 1 day)
  status                     print last-known battery + reception per station
  reception [id] [hours]     show ISS reception history (default 48h, all stations)
  list                       print stations in DB
""", file=sys.stderr)


def main() -> None:
    if len(sys.argv) < 2:
        usage()
        sys.exit(2)
    verb = sys.argv[1]
    conn = db_connect()
    if verb == "sync-stations":
        sync_stations(conn)
    elif verb == "sync-current":
        ids = [int(sys.argv[2])] if len(sys.argv) > 2 else list_station_ids(conn)
        if not ids:
            print("no stations — run sync-stations first", file=sys.stderr)
            sys.exit(1)
        sync_current(conn, ids)
    elif verb == "sync-historic":
        if len(sys.argv) < 3:
            usage(); sys.exit(2)
        sid = int(sys.argv[2])
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        sync_historic(conn, sid, days)
    elif verb == "status":
        status(conn)
    elif verb == "reception":
        # Args: [station_id] [hours]. If the first arg isn't a known station
        # in the DB, treat it as `hours` and run for all stations.
        known = set(list_station_ids(conn))
        sid: int | None = None
        hours = 48
        args = sys.argv[2:]
        if args:
            try:
                first = int(args[0])
            except ValueError:
                usage(); sys.exit(2)
            if first in known:
                sid = first
                if len(args) > 1:
                    hours = int(args[1])
            else:
                hours = first
        targets = [sid] if sid is not None else sorted(known)
        if not targets:
            print("no stations — run sync-stations first", file=sys.stderr)
            sys.exit(1)
        for i, t in enumerate(targets):
            if i > 0:
                print()
            reception_history(conn, t, hours)
    elif verb == "list":
        for sid, name, city, fw in conn.execute(
            "SELECT station_id, name, city, firmware_version FROM stations ORDER BY station_id"
        ):
            print(f"  {sid}  {name}  ({city})  fw {fw}")
    else:
        usage()
        sys.exit(2)


if __name__ == "__main__":
    main()
