#!/usr/bin/env python3
"""Philips Hue bridge discovery and application registration."""

import json
import ssl
import sys
import time
import urllib.request
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


def main() -> None:
    verb = sys.argv[1] if len(sys.argv) > 1 else "list"
    if verb not in ("list", "on", "off"):
        print(f"Usage: {sys.argv[0]} [list|on|off]", file=sys.stderr)
        sys.exit(1)

    # Step 1: Discover bridges
    print("Discovering Hue bridges...")
    bridges = discover_bridges()
    if not bridges:
        print("No bridges found.")
        return

    for i, b in enumerate(bridges):
        print(f"  [{i}] {b['id']} @ {b['internalipaddress']}")

    if len(bridges) == 1:
        bridge = bridges[0]
    else:
        idx = int(input("Select bridge: "))
        bridge = bridges[idx]

    ip = bridge["internalipaddress"]

    # Step 2: Check for existing credentials
    creds = load_credentials(ip)
    if creds:
        print(f"Found existing credentials for {ip}")
    else:
        # Step 3: Register
        print(f"\nPress the link button on your bridge ({ip}), then press Enter...")
        input()
        print("Registering application...")
        creds = register_app(ip)
        save_credentials(ip, creds)
        print(f"Registered! App key: {creds['username'][:8]}...")

    # Step 4: Fetch lights
    lights_data = get_api(ip, "light", creds["username"])
    lights = lights_data.get("data", [])

    if verb in ("on", "off"):
        turn_on = verb == "on"
        for light in lights:
            light_id = light["id"]
            name = light.get("metadata", {}).get("name", light_id)
            put_api(ip, f"light/{light_id}", creds["username"], {"on": {"on": turn_on}})
            print(f"  {name}: turned {verb}")
        return

    # List devices with state
    print("\nDevices:")
    data = get_api(ip, "device", creds["username"])

    # Map device id -> list of light resources owned by that device
    lights_by_device: dict[str, list[dict]] = {}
    for light in lights:
        owner_id = light.get("owner", {}).get("rid")
        if owner_id:
            lights_by_device.setdefault(owner_id, []).append(light)

    for device in data.get("data", []):
        name = device.get("metadata", {}).get("name", "(unnamed)")
        product = device.get("product_data", {}).get("product_name", "")
        device_id = device.get("id", "")

        state_parts = []
        for light in lights_by_device.get(device_id, []):
            on = light.get("on", {}).get("on")
            if on is None:
                continue
            if not on:
                state_parts.append("off")
            else:
                brightness = light.get("dimming", {}).get("brightness")
                s = "on"
                if brightness is not None:
                    s += f" {brightness:.0f}%"
                state_parts.append(s)

        state_str = ", ".join(state_parts) if state_parts else None
        suffix = f": {state_str}" if state_str else ""
        print(f"  - {name} ({product}){suffix}")


if __name__ == "__main__":
    main()
