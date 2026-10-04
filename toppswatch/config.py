"""YAML config loading, with env-var expansion and sane defaults."""

from __future__ import annotations

import os
import re
from copy import deepcopy

import yaml

DEFAULTS = {
    "site": {
        "base_url": "https://es.topps.com",
        "product_path": "/products/{handle}",
        "product_segment": None,
        "collections": ["/collections/all-products"],
        "sitemaps": ["/sitemap.xml"],
    },
    "watch": {
        "patterns": ["chrome"],
        "exclude_patterns": ["sleeve", "top ?loader", "album", "binder"],
        "handles": [],
        "auto_discover": True,
        "discover_every_minutes": 30,
        "max_products": 60,
    },
    "polling": {
        "interval_seconds": 60,
        "jitter_percent": 20,
        "concurrency": 3,
        "per_request_delay_ms": 250,
        "timeout_seconds": 20,
        "retries": 3,
        "backoff": 2.0,
        "rate_limit_cooldown_seconds": 900,
        "impersonate": None,
        "proxy": None,
        "strategies": ["json-ld", "meta", "embedded-json", "text"],
    },
    "alerts": {
        "min_alert_interval_seconds": 1800,
        "alert_on_first_sight": False,
        "heartbeat_hours": 0,
        "notify_on_new_product": True,
    },
    "state_file": "./data/state.json",
    "log_level": "INFO",
    "notifications": {},
}

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value):
    """Replace ${VAR} and ${VAR:-default} anywhere in the config."""
    if isinstance(value, str):
        def replace(match):
            return os.environ.get(match.group(1), match.group(2) or "")
        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    merged = _deep_merge(DEFAULTS, _expand_env(raw))
    validate(merged)
    return merged


def validate(config: dict) -> None:
    interval = config["polling"]["interval_seconds"]
    if interval < 20:
        raise ValueError(
            f"polling.interval_seconds is {interval}; use 20 or more. "
            "Hammering the store faster gets you blocked, not served."
        )
    if not config["watch"]["patterns"] and not config["watch"]["handles"]:
        raise ValueError("nothing to watch: set watch.patterns or watch.handles")
    if not config.get("notifications"):
        raise ValueError("no notifications configured — the monitor would be silent")


def product_url(config: dict, handle: str) -> str:
    base = config["site"]["base_url"].rstrip("/")
    path = config["site"]["product_path"].format(handle=handle)
    return f"{base}{path}"
