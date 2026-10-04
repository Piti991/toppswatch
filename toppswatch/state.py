"""Persistent per-product state.

The whole point of this file is that you get alerted on the *transition* into
stock, not once a minute for as long as it stays in stock.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger("toppswatch.state")


@dataclass
class ProductState:
    handle: str
    title: str | None = None
    in_stock: bool | None = None
    last_seen_at: float = 0.0
    last_alert_at: float = 0.0
    last_price: str | None = None
    consecutive_errors: int = 0
    first_seen_at: float = field(default_factory=time.time)


class Store:
    def __init__(self, path: str):
        self.path = path
        self.products: dict[str, ProductState] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            for key, value in raw.get("products", {}).items():
                known = {f for f in ProductState.__dataclass_fields__}
                self.products[key] = ProductState(**{k: v for k, v in value.items() if k in known})
            log.debug("loaded state for %d products", len(self.products))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read state file %s (%s) — starting fresh", self.path, exc)

    def save(self) -> None:
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "products": {k: asdict(v) for k, v in self.products.items()},
        }
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get(self, handle: str) -> ProductState:
        if handle not in self.products:
            self.products[handle] = ProductState(handle=handle)
        return self.products[handle]

    def should_alert(
        self,
        handle: str,
        now_in_stock: bool,
        min_alert_interval: float,
        alert_on_first_sight: bool = False,
    ) -> tuple[bool, str]:
        """Decide whether this observation deserves a push.

        Returns (should_alert, reason).
        """
        state = self.get(handle)
        previous = state.in_stock

        if not now_in_stock:
            return False, "out of stock"

        if previous is None:
            if alert_on_first_sight:
                return True, "in stock on first check"
            return False, "first observation — baseline recorded, no alert"

        if previous is False:
            since_last = time.time() - state.last_alert_at
            if state.last_alert_at and since_last < min_alert_interval:
                return False, f"restock but muted ({since_last:.0f}s < {min_alert_interval:.0f}s)"
            return True, "RESTOCK: out of stock -> in stock"

        return False, "still in stock (already alerted)"

    def record(
        self,
        handle: str,
        in_stock: bool | None,
        title: str | None = None,
        price: str | None = None,
        alerted: bool = False,
        error: bool = False,
    ) -> None:
        state = self.get(handle)
        if title:
            state.title = title
        if price:
            state.last_price = price
        if error:
            state.consecutive_errors += 1
            return
        state.consecutive_errors = 0
        if in_stock is not None:
            state.in_stock = in_stock
        state.last_seen_at = time.time()
        if alerted:
            state.last_alert_at = time.time()
