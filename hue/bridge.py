#!/usr/bin/env python3
"""Philips Hue bridge discovery and application registration."""

import json
import ssl
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DISCOVERY_URL = "https://discovery.meethue.com"
CONFIG_PATH = Path(__file__).parent / ".hue_credentials.json"


def _no_verify_ctx() -> ssl.SSLContext:
    """Create an SSL context that skips certificate verification (bridge uses self-signed certs)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def discover_bridges() -> list[dict]:
    """Discover Hue bridges on the network via meethue.com cloud endpoint."""
    req = urllib.request.Request(DISCOVERY_URL)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def register_app(
    bridge_ip: str,
    device_type: str = "homeautomation#hue",
    timeout: int = 30,
    poll_interval: int = 2,
) -> dict:
    """Register a new application with the bridge.

    Press the link button on the bridge, then call this function.
    It polls until the button is pressed or the timeout expires.

    Returns dict with 'username' and optionally 'clientkey'.
    """
    url = f"https://{bridge_ip}/api"
    body = json.dumps({"devicetype": device_type, "generateclientkey": True}).encode()
    ctx = _no_verify_ctx()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=ctx) as resp:
            result = json.loads(resp.read())

        if "success" in result[0]:
            return result[0]["success"]

        error = result[0].get("error", {})
        if error.get("type") == 101:
            remaining = int(deadline - time.monotonic())
            print(f"  Waiting for link button press... ({remaining}s remaining)")
            time.sleep(poll_interval)
            continue

        raise RuntimeError(f"Unexpected error from bridge: {error}")

    raise TimeoutError("Timed out waiting for link button press")


def save_credentials(bridge_ip: str, credentials: dict, path: Path = CONFIG_PATH) -> None:
    """Save bridge credentials to a JSON file."""
    config = {}
    if path.exists():
        config = json.loads(path.read_text())
    config[bridge_ip] = credentials
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"Credentials saved to {path}")


def load_credentials(bridge_ip: str, path: Path = CONFIG_PATH) -> dict | None:
    """Load saved credentials for a bridge, or return None."""
    if not path.exists():
        return None
    config = json.loads(path.read_text())
    return config.get(bridge_ip)


def get_api(bridge_ip: str, endpoint: str, app_key: str) -> dict:
    """Make a GET request to the Hue CLIP v2 API."""
    url = f"https://{bridge_ip}/clip/v2/resource/{endpoint}"
    req = urllib.request.Request(url, headers={"hue-application-key": app_key})
    with urllib.request.urlopen(req, context=_no_verify_ctx()) as resp:
        return json.loads(resp.read())


def put_api(bridge_ip: str, endpoint: str, app_key: str, body: dict) -> dict:
    """Make a PUT request to the Hue CLIP v2 API."""
    url = f"https://{bridge_ip}/clip/v2/resource/{endpoint}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"hue-application-key": app_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, context=_no_verify_ctx()) as resp:
        return json.loads(resp.read())


@dataclass
class Light:
    id: str
    name: str
    on: bool | None
    brightness: float | None

    def state_str(self) -> str:
        if self.on is None:
            return ""
        if not self.on:
            return "off"
        return "on" + (f" {self.brightness:.0f}%" if self.brightness is not None else "")


@dataclass
class LightGroup:
    id: str
    name: str
    on: bool | None
    brightness: float | None
    lights: list[Light] = field(default_factory=list)

    def state_str(self) -> str:
        if self.on is None:
            return ""
        if not self.on:
            return "off"
        return "on" + (f" {self.brightness:.0f}%" if self.brightness is not None else "")


def fetch_light_info(bridge_ip: str, app_key: str) -> tuple[list[LightGroup], list[Light]]:
    """Return (groups, ungrouped_lights) fetched from the bridge."""
    raw_lights = get_api(bridge_ip, "light", app_key).get("data", [])
    grouped_lights_raw = get_api(bridge_ip, "grouped_light", app_key).get("data", [])
    rooms_raw = get_api(bridge_ip, "room", app_key).get("data", [])
    zones_raw = get_api(bridge_ip, "zone", app_key).get("data", [])

    lights = [
        Light(
            id=r["id"],
            name=r.get("metadata", {}).get("name", "(unnamed)"),
            on=r.get("on", {}).get("on"),
            brightness=r.get("dimming", {}).get("brightness"),
        )
        for r in raw_lights
    ]
    light_by_id = {l.id: l for l in lights}

    lights_by_device: dict[str, list[Light]] = {}
    for raw, light in zip(raw_lights, lights):
        device_id = raw.get("owner", {}).get("rid")
        if device_id:
            lights_by_device.setdefault(device_id, []).append(light)

    grouped_light_by_id = {gl["id"]: gl for gl in grouped_lights_raw}

    # Build grouped_light_id -> (name, member_lights) from rooms then zones
    groups_map: dict[str, tuple[str, list[Light]]] = {}

    for room in rooms_raw:
        gl_id = next((svc["rid"] for svc in room.get("services", [])
                      if svc.get("rtype") == "grouped_light"), None)
        if gl_id is None:
            continue
        name = room.get("metadata", {}).get("name", "(unnamed)")
        member_lights = [
            light
            for child in room.get("children", [])
            if child.get("rtype") == "device"
            for light in lights_by_device.get(child["rid"], [])
        ]
        groups_map[gl_id] = (name, member_lights)

    for zone in zones_raw:
        gl_id = next((svc["rid"] for svc in zone.get("services", [])
                      if svc.get("rtype") == "grouped_light"), None)
        if gl_id is None:
            continue
        name = zone.get("metadata", {}).get("name", "(unnamed)")
        member_lights = [
            light_by_id[child["rid"]]
            for child in zone.get("children", [])
            if child.get("rtype") == "light" and child["rid"] in light_by_id
        ] + [
            light
            for child in zone.get("children", [])
            if child.get("rtype") == "device"
            for light in lights_by_device.get(child["rid"], [])
        ]
        groups_map[gl_id] = (name, member_lights)

    groups: list[LightGroup] = []
    seen_light_ids: set[str] = set()
    for gl_id, (name, member_lights) in groups_map.items():
        if not member_lights:
            continue
        raw_gl = grouped_light_by_id.get(gl_id, {})
        groups.append(LightGroup(
            id=gl_id,
            name=name,
            on=raw_gl.get("on", {}).get("on"),
            brightness=raw_gl.get("dimming", {}).get("brightness"),
            lights=member_lights,
        ))
        for light in member_lights:
            seen_light_ids.add(light.id)

    ungrouped = [l for l in lights if l.id not in seen_light_ids]
    return groups, ungrouped


def main() -> None:
    verb = sys.argv[1] if len(sys.argv) > 1 else "list"
    target_name = sys.argv[2] if len(sys.argv) > 2 else None

    if verb not in ("list", "list_groups", "on", "off"):
        print(f"Usage: {sys.argv[0]} [list|list_groups|on|off] [name]", file=sys.stderr)
        sys.exit(1)

    print("Discovering Hue bridges...")
    bridges = discover_bridges()
    if not bridges:
        print("No bridges found.")
        return

    for i, b in enumerate(bridges):
        print(f"  [{i}] {b['id']} @ {b['internalipaddress']}")

    bridge = bridges[0] if len(bridges) == 1 else bridges[int(input("Select bridge: "))]
    ip = bridge["internalipaddress"]

    creds = load_credentials(ip)
    if creds:
        print(f"Found existing credentials for {ip}")
    else:
        print(f"\nPress the link button on your bridge ({ip}), then press Enter...")
        input()
        print("Registering application...")
        creds = register_app(ip)
        save_credentials(ip, creds)
        print(f"Registered! App key: {creds['username'][:8]}...")

    groups, ungrouped = fetch_light_info(ip, creds["username"])

    if verb in ("on", "off"):
        turn_on = verb == "on"
        if target_name:
            matched_group = next((g for g in groups if g.name.lower() == target_name.lower()), None)
            if matched_group:
                put_api(ip, f"grouped_light/{matched_group.id}", creds["username"], {"on": {"on": turn_on}})
                print(f"  {matched_group.name}: turned {verb}")
                return
            all_lights = [l for g in groups for l in g.lights] + ungrouped
            matched_light = next((l for l in all_lights if l.name.lower() == target_name.lower()), None)
            if matched_light:
                put_api(ip, f"light/{matched_light.id}", creds["username"], {"on": {"on": turn_on}})
                print(f"  {matched_light.name}: turned {verb}")
                return
            print(f"No light or group named '{target_name}' found.", file=sys.stderr)
            sys.exit(1)
        for group in groups:
            put_api(ip, f"grouped_light/{group.id}", creds["username"], {"on": {"on": turn_on}})
            print(f"  {group.name}: turned {verb}")
        for light in ungrouped:
            put_api(ip, f"light/{light.id}", creds["username"], {"on": {"on": turn_on}})
            print(f"  {light.name}: turned {verb}")
        return

    if verb == "list_groups":
        print("\nGroups:")
        for group in groups:
            state = group.state_str()
            print(f"  {group.name}" + (f": {state}" if state else ""))
        return

    print("\nLights:")
    for group in groups:
        state = group.state_str()
        print(f"  {group.name}" + (f": {state}" if state else ""))
        for light in group.lights:
            state = light.state_str()
            print(f"    - {light.name}" + (f": {state}" if state else ""))
    if ungrouped:
        if groups:
            print("  (ungrouped)")
        for light in ungrouped:
            state = light.state_str()
            print(f"  - {light.name}" + (f": {state}" if state else ""))


if __name__ == "__main__":
    main()
