"""
NetProvision — VLAN Provisioning & Network Inventory API
=========================================================
Usage:
  python main.py                        # start API server
  python main.py --collect              # collect inventory then start
  python main.py --config myconfig.yaml

Environment overrides:
  NP_CONFIG, NP_DB_PATH, NP_HOST, NP_PORT, NP_LOG_FORMAT (text|json), NP_LOG_LEVEL
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# Windows consoles default to cp1252; log messages contain em dashes.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except ValueError:
            pass

from observability import configure_logging  # noqa: E402  (after sys.path setup)

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


def run(
    config_path: str = "config.yaml",
    collect_first: bool = False,
    db_path: str = "inventory.db",
    host: str | None = None,
    port: int | None = None,
) -> None:
    from api.server import build_app
    from auth import AuthConfig
    from inventory.store import InventoryStore

    raw = load_config(config_path)
    devices, device_map = load_devices(raw)

    try:
        auth_config = AuthConfig.from_env()
    except ValueError as exc:
        raise SystemExit(f"Authentication configuration error: {exc}") from exc

    if auth_config.enabled:
        log.info(f"Authentication: apikey ({len(auth_config.keys)} key(s) configured)")
    else:
        log.warning(
            "NP_AUTH_MODE=off — the API is unauthenticated. "
            "Set NP_AUTH_MODE=apikey and NP_API_KEYS outside a lab."
        )

    require_approval = os.getenv("NP_REQUIRE_APPROVAL", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    log.info(f"Approval workflow: {'required' if require_approval else 'not required'}")

    store = InventoryStore(path=db_path)
    log.info(f"Inventory store: {db_path}")
    log.info(f"Loaded {len(devices)} device(s) from {config_path}")

    try:
        if collect_first and devices:
            from inventory.collector import collect
            log.info("Running initial inventory collection...")
            for creds in devices:
                collect(creds, store)

        import uvicorn
        app = build_app(store, device_map, auth_config=auth_config)

        api_cfg = raw.get("api", {})
        host = host or os.getenv("NP_HOST") or api_cfg.get("host", "0.0.0.0")
        port = port or int(os.getenv("NP_PORT", 0)) or api_cfg.get("port", 8002)

        log.info(f"API server: http://{host}:{port}")
        log.info(f"Dashboard:  http://{host}:{port}/")
        log.info(f"API docs:   http://{host}:{port}/docs")

        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NetProvision")
    parser.add_argument("--config", default=os.getenv("NP_CONFIG", "config.yaml"))
    parser.add_argument("--db", default=os.getenv("NP_DB_PATH", "inventory.db"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-format", choices=["text", "json"],
                        default=os.getenv("NP_LOG_FORMAT", "text"))
    parser.add_argument("--collect", action="store_true",
                        help="Collect inventory from all devices before starting")
    args = parser.parse_args()

    configure_logging(os.getenv("NP_LOG_LEVEL", "INFO"), args.log_format)
    run(
        config_path   = args.config,
        collect_first = args.collect,
        db_path       = args.db,
        host          = args.host,
        port          = args.port,
    )
