"""toppswatch — restock monitor for es.topps.com.

    python -m toppswatch.main --config config.yaml run
    python -m toppswatch.main --config config.yaml once --debug
    python -m toppswatch.main --config config.yaml discover
    python -m toppswatch.main --config config.yaml test-notify
    python -m toppswatch.main --config config.yaml status
"""

from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .config import load_config, product_url
from .fetch import Fetcher, RateLimited
from .notify import Alert, Notifier
from .parse import detect_stock, discover_handles, handle_matches, normalize
from .state import Store

log = logging.getLogger("toppswatch")

_shutdown = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _shutdown
    _shutdown = True
    log.info("signal %s received — finishing this cycle then exiting", signum)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def pretty_handle(handle: str) -> str:
    from urllib.parse import unquote
    return unquote(handle).replace("-", " ").title()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def discover(config: dict, fetcher: Fetcher) -> list[str]:
    """Find every product handle matching the watch patterns."""
    base = config["site"]["base_url"].rstrip("/")
    segment = config["site"].get("product_segment")
    patterns = config["watch"]["patterns"]
    excludes = config["watch"]["exclude_patterns"]

    pages: list[str] = []
    for path in config["site"]["collections"]:
        pages.append(path if path.startswith("http") else f"{base}{path}")
    for path in config["site"]["sitemaps"]:
        pages.append(path if path.startswith("http") else f"{base}{path}")

    found: dict[str, None] = {}
    nested_sitemaps: list[str] = []

    for url in pages:
        try:
            result = fetcher.get(url)
        except RateLimited:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("discovery source %s unavailable: %s", url, exc)
            continue
        if result.status >= 400:
            log.debug("discovery source %s returned HTTP %s", url, result.status)
            continue
        for handle in discover_handles(result.text, segment):
            found.setdefault(handle, None)
        if url.endswith(".xml"):
            import re
            for match in re.finditer(r"<loc>\s*([^<]+sitemap[^<]*\.xml)\s*</loc>", result.text, re.I):
                nested_sitemaps.append(match.group(1).strip())

    for url in nested_sitemaps[:10]:
        try:
            result = fetcher.get(url)
            for handle in discover_handles(result.text, segment):
                found.setdefault(handle, None)
        except Exception as exc:  # noqa: BLE001
            log.debug("nested sitemap %s failed: %s", url, exc)

    matched = [
        handle
        for handle in found
        if handle_matches(handle, patterns) and not handle_matches(handle, excludes)
    ]

    log.info(
        "discovery: %d product links seen, %d match %s",
        len(found), len(matched), patterns,
    )
    return matched[: config["watch"]["max_products"]]


def resolve_watchlist(config: dict, fetcher: Fetcher, cached: list[str] | None = None) -> list[str]:
    manual = list(config["watch"]["handles"])
    if not config["watch"]["auto_discover"]:
        return manual
    try:
        discovered = discover(config, fetcher)
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("discovery failed (%s) — keeping previous watchlist", exc)
        discovered = cached or []
    merged: dict[str, None] = {}
    for handle in manual + discovered + (cached or []):
        merged.setdefault(handle, None)
    return list(merged)


# --------------------------------------------------------------------------
# One polling cycle
# --------------------------------------------------------------------------


def check_product(config: dict, fetcher: Fetcher, handle: str, debug: bool = False):
    url = product_url(config, handle)
    result = fetcher.get(url)
    if result.status == 404:
        return handle, url, None, "delisted (404)", None, None
    verdict = detect_stock(
        result.text,
        handle=handle,
        strategies=config["polling"]["strategies"],
    )
    if debug:
        log.info(
            "[debug] %s -> in_stock=%s via %s (%s) title=%r price=%s notes=%s",
            handle, verdict.in_stock, verdict.method, verdict.evidence,
            verdict.title, verdict.price, verdict.notes,
        )
    return handle, url, verdict.in_stock, verdict.method, verdict.title, verdict.price


def run_cycle(config: dict, fetcher: Fetcher, store: Store, notifier: Notifier,
              watchlist: list[str], debug: bool = False) -> dict:
    stats = {"checked": 0, "in_stock": 0, "errors": 0, "alerts": 0}
    delay = config["polling"]["per_request_delay_ms"] / 1000.0
    min_interval = config["alerts"]["min_alert_interval_seconds"]
    first_sight = config["alerts"]["alert_on_first_sight"]

    def worker(handle: str):
        time.sleep(random.uniform(0, delay))
        return check_product(config, fetcher, handle, debug)

    with ThreadPoolExecutor(max_workers=config["polling"]["concurrency"]) as pool:
        futures = {pool.submit(worker, h): h for h in watchlist}
        for future, handle in futures.items():
            try:
                handle, url, in_stock, method, title, price = future.result()
            except RateLimited as exc:
                raise
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                store.record(handle, None, error=True)
                log.warning("check failed for %s: %s", handle, exc)
                continue

            stats["checked"] += 1
            if in_stock is None:
                log.debug("%s: inconclusive (%s)", handle, method)
                store.record(handle, None, title=title, price=price)
                continue
            if in_stock:
                stats["in_stock"] += 1

            should, reason = store.should_alert(handle, in_stock, min_interval, first_sight)
            log.debug("%s: in_stock=%s (%s) — %s", handle, in_stock, method, reason)

            if should:
                display = title or pretty_handle(handle)
                price_str = f" — {price}" if price else ""
                alert = Alert(
                    title=f"IN STOCK: {display}",
                    message=(
                        f"{display}{price_str}\n"
                        f"Just came back in stock on {site_name(config)}.\n"
                        f"Detected {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} via {method}."
                    ),
                    url=url,
                    priority="high",
                    tags=["rotating_light", "shopping_cart"],
                )
                delivered = notifier.send(alert)
                stats["alerts"] += 1
                log.info("ALERT %s (%d channels)", display, delivered)
                store.record(handle, in_stock, title=title, price=price, alerted=True)
            else:
                store.record(handle, in_stock, title=title, price=price)

    store.save()
    return stats


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def site_name(config: dict) -> str:
    name = config["site"].get("name")
    if name:
        return name
    from urllib.parse import urlparse
    host = urlparse(config["site"]["base_url"]).netloc
    return host[4:] if host.startswith("www.") else host


def announce_new(config: dict, notifier, added: list, known: set) -> int:
    sent = 0
    for handle in added:
        if handle in known:
            continue
        notifier.send(Alert(
            title=f"NEW on {site_name(config)}: {pretty_handle(handle)}",
            message=f"A new matching product appeared on {site_name(config)}. Now being monitored.",
            url=product_url(config, handle),
            priority="default",
            tags=["new"],
        ))
        sent += 1
    return sent


def cmd_run(config: dict, args) -> int:
    fetcher = _make_fetcher(config)
    store = Store(config["state_file"])
    notifier = Notifier(config["notifications"])
    log.info("notification channels: %s", ", ".join(notifier.channel_names) or "NONE")

    watchlist = []
    for attempt in range(1, 7):
        try:
            watchlist = resolve_watchlist(config, fetcher, cached=list(store.products))
            break
        except RateLimited as exc:
            cached = list(store.products)
            if cached:
                log.warning("discovery blocked (HTTP %s) — using %d cached products",
                            exc.status, len(cached))
                watchlist = cached
                break
            wait = min(900, 60 * attempt)
            log.warning("discovery blocked (HTTP %s), attempt %d — retrying in %ds",
                        exc.status, attempt, wait)
            _sleep_interruptible(wait)
            if _shutdown:
                return 0
    if not watchlist:
        log.error("could not build a watchlist — exiting")
        return 1
    log.info("watching %d products", len(watchlist))
    for handle in watchlist:
        log.debug("  - %s", handle)

    known = set(store.products)
    last_discovery = time.time()
    discovery_interval = config["watch"]["discover_every_minutes"] * 60
    base_interval = config["polling"]["interval_seconds"]
    jitter = config["polling"]["jitter_percent"] / 100.0

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not _shutdown:
        cycle_started = time.monotonic()
        try:
            stats = run_cycle(config, fetcher, store, notifier, watchlist, args.debug)
            log.info(
                "cycle: %d checked, %d in stock, %d errors, %d alerts (%.1fs)",
                stats["checked"], stats["in_stock"], stats["errors"], stats["alerts"],
                time.monotonic() - cycle_started,
            )
        except RateLimited as exc:
            cooldown = exc.retry_after or config["polling"]["rate_limit_cooldown_seconds"]
            log.error("rate limited (HTTP %s) — cooling down %.0fs", exc.status, cooldown)
            _sleep_interruptible(cooldown)
            continue

        if time.time() - last_discovery > discovery_interval:
            last_discovery = time.time()
            refreshed = resolve_watchlist(config, fetcher, cached=watchlist)
            added = [h for h in refreshed if h not in watchlist]
            if added:
                log.info("discovery added %d new product(s): %s", len(added), added)
                if config["alerts"]["notify_on_new_product"]:
                    announce_new(config, notifier, added, known)
            watchlist = refreshed
            known |= set(watchlist)

        wait = base_interval * (1 + random.uniform(-jitter, jitter))
        wait = max(5.0, wait - (time.monotonic() - cycle_started))
        _sleep_interruptible(wait)

    log.info("shut down cleanly")
    fetcher.close()
    return 0


def _sleep_interruptible(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _shutdown and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def cmd_once(config: dict, args) -> int:
    fetcher = _make_fetcher(config)
    store = Store(config["state_file"])
    notifier = Notifier(config["notifications"])
    watchlist = []
    for attempt in range(1, 7):
        try:
            watchlist = resolve_watchlist(config, fetcher, cached=list(store.products))
            break
        except RateLimited as exc:
            cached = list(store.products)
            if cached:
                log.warning("discovery blocked (HTTP %s) — using %d cached products",
                            exc.status, len(cached))
                watchlist = cached
                break
            wait = min(900, 60 * attempt)
            log.warning("discovery blocked (HTTP %s), attempt %d — retrying in %ds",
                        exc.status, attempt, wait)
            _sleep_interruptible(wait)
            if _shutdown:
                return 0
    if not watchlist:
        log.error("could not build a watchlist — exiting")
        return 1
    log.info("watching %d products", len(watchlist))
    stats = run_cycle(config, fetcher, store, notifier, watchlist, debug=True)
    log.info("done: %s", stats)
    fetcher.close()
    return 0


def cmd_discover(config: dict, args) -> int:
    fetcher = _make_fetcher(config)
    handles = discover(config, fetcher)
    print(f"\n{len(handles)} matching product(s):\n")
    for handle in handles:
        print(f"  - {handle}")
        print(f"    {product_url(config, handle)}")
    print("\nPaste any of these under watch.handles in config.yaml to pin them.\n")
    fetcher.close()
    return 0


def cmd_status(config: dict, args) -> int:
    store = Store(config["state_file"])
    if not store.products:
        print("No state recorded yet — run `once` first.")
        return 0
    rows = sorted(store.products.values(), key=lambda p: (p.in_stock is not True, p.handle))
    print(f"\n{'STOCK':<10} {'PRICE':<10} PRODUCT")
    print("-" * 78)
    for state in rows:
        mark = {True: "IN STOCK", False: "sold out", None: "unknown"}[state.in_stock]
        seen = (
            datetime.fromtimestamp(state.last_seen_at).strftime("%d %b %H:%M")
            if state.last_seen_at else "never"
        )
        print(f"{mark:<10} {(state.last_price or '-'):<10} {state.title or state.handle}")
        print(f"{'':<21} last checked {seen}")
    print()
    return 0


def cmd_test_notify(config: dict, args) -> int:
    notifier = Notifier(config["notifications"])
    if not notifier.channels:
        print("No channels configured.")
        return 1
    print(f"Sending test alert via: {', '.join(notifier.channel_names)}")
    delivered = notifier.send(Alert(
        title="toppswatch test alert",
        message="If you can read this, restock alerts will reach you.",
        url="https://es.topps.com/collections/all-products",
        priority="default",
        tags=["white_check_mark"],
    ))
    print(f"Delivered to {delivered}/{len(notifier.channels)} channel(s).")
    return 0 if delivered else 1


def _make_fetcher(config: dict) -> Fetcher:
    polling = config["polling"]
    return Fetcher(
        timeout=polling["timeout_seconds"],
        retries=polling["retries"],
        backoff=polling["backoff"],
        impersonate=polling["impersonate"],
        proxy=polling["proxy"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toppswatch", description="Topps España restock monitor")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--debug", action="store_true", help="verbose per-product detection output")
    parser.add_argument(
        "command",
        choices=["run", "once", "discover", "status", "test-notify"],
        nargs="?",
        default="run",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    setup_logging("DEBUG" if args.debug else config["log_level"])

    commands = {
        "run": cmd_run,
        "once": cmd_once,
        "discover": cmd_discover,
        "status": cmd_status,
        "test-notify": cmd_test_notify,
    }
    return commands[args.command](config, args)


if __name__ == "__main__":
    sys.exit(main())
