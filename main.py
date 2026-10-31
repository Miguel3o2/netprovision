"""
NetProvision — VLAN Provisioning & Network Inventory API
=========================================================
Usage:
  python main.py                        # start API server
  python main.py --collect              # collect inventory then start
  python main.py --config myconfig.yaml
"""

from __future__ import annotations
import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# Windows consoles default to cp1252; logs contain em dashes.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("netprovision")

CONFIG_HINT = (
    "Copy the example config and fill in your device credentials:\n"
    "    cp configs/config.example.yaml config.yaml"
)


def load_config(config_path: str) -> dict:
    """Read and parse the YAML config, with a helpful error if it is missing."""
    import yaml

    if not os.path.isfile(config_path):
        raise SystemExit(f"Config file '{config_path}' not found.\n{CONFIG_HINT}")

    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_devices(raw: dict) -> tuple[list, dict]:
    """Build the device list and the hostname → credentials map from parsed config."""
    from models import DeviceCredentials, DeviceType

    devices = []
    device_map = {}

    for d in raw.get("devices", []):
        try:
            dtype = DeviceType(d.get("device_type", "cisco_ios"))
        except ValueError:
            dtype = DeviceType.CISCO_IOS

        creds = DeviceCredentials(
            hostname      = d["hostname"],
            ip            = d["ip"],
            device_type   = dtype,
            username      = d.get("username", ""),
            password      = d.get("password", ""),
            enable_secret = d.get("enable_secret", ""),
            port          = d.get("port", 22),
            timeout       = d.get("timeout", 30),
            tags          = d.get("tags", {}),
        )
        devices.append(creds)
        device_map[creds.hostname] = creds

    return devices, device_map


def run(config_path: str = "config.yaml", collect_first: bool = False) -> None:
    from inventory.store import InventoryStore
    from api.server import build_app

    raw = load_config(config_path)
    devices, device_map = load_devices(raw)

    db_path = "inventory.db"
    store   = InventoryStore(path=db_path)
    log.info(f"Inventory store: {db_path}")
    log.info(f"Loaded {len(devices)} device(s) from {config_path}")

    if collect_first and devices:
        from inventory.collector import collect
        log.info("Running initial inventory collection...")
        for creds in devices:
            collect(creds, store)

    import uvicorn
    app = build_app(store, device_map)

    api_cfg = raw.get("api", {})
    host = api_cfg.get("host", "0.0.0.0")
    port = api_cfg.get("port", 8002)

    log.info(f"API server: http://{host}:{port}")
    log.info(f"Dashboard:  http://{host}:{port}/")
    log.info(f"API docs:   http://{host}:{port}/docs")

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NetProvision")
    parser.add_argument("--config",  default="config.yaml")
    parser.add_argument("--collect", action="store_true",
                        help="Collect inventory from all devices before starting")
    args = parser.parse_args()
    run(args.config, collect_first=args.collect)
