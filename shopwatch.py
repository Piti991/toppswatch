#!/usr/bin/env python3
"""Restock monitor for Shopify stores exposing /collections/<h>/products.json.
One request per collection per cycle; stock from variant availability."""
import json, logging, random, signal, sys, time
sys.path.insert(0, "/opt/toppswatch")
from curl_cffi import requests as cr
from toppswatch.config import load_config
from toppswatch.notify import Notifier, Alert
from toppswatch.state import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("shopwatch")
_stop = False
def _sig(*a):
    global _stop; _stop = True; log.info("signal received, finishing cycle")

def fetch(cfg):
    out = {}
    for h in cfg["collections"]:
        for page in range(1, cfg.get("max_pages", 10) + 1):
            url = f'{cfg["base_url"]}/collections/{h}/products.json?limit=250&page={page}'
            try:
                x = cr.get(url, impersonate=cfg.get("impersonate", "safari17_0"), timeout=30)
            except Exception as exc:
                log.warning("%s p%d: %s", h, page, str(exc)[:60]); break
            if x.status_code != 200:
                log.warning("%s p%d -> HTTP %s", h, page, x.status_code); break
            ps = json.loads(x.text).get("products", [])
            if not ps:
                break
            for p in ps:
                vs = p.get("variants", [])
                avail = any(v.get("available") for v in vs)
                prices = [v["price"] for v in vs if v.get("price")]
                price = min(prices, key=lambda s: float(s)) if prices else None
                out[p["handle"]] = (p.get("title", ""), avail, price)
            time.sleep(cfg.get("delay", 1.5))
    return out

def main(path):
    cfg = json.load(open(path))
    store = Store(cfg["state_file"])
    notifier = Notifier(load_config("/opt/toppswatch/config.yaml")["notifications"])
    mute = cfg.get("min_alert_interval", 1800)
    signal.signal(signal.SIGINT, _sig); signal.signal(signal.SIGTERM, _sig)
    log.info("shopwatch %s: %d collections, channels: %s",
             cfg["name"], len(cfg["collections"]), ", ".join(notifier.channel_names))
    known = set(store.products)
    while not _stop:
        t0 = time.time()
        found = fetch(cfg)
        if not found:
            log.error("no products returned — backing off"); time.sleep(300); continue
        alerts = 0
        for handle, (title, avail, price) in found.items():
            should, reason = store.should_alert(handle, avail, mute)
            url = f'{cfg["base_url"]}/products/{handle}'
            fired = False
            if should:
                notifier.send(Alert(title=f"IN STOCK: {title or handle}",
                    message=f'{title}{" - €"+price if price else ""}\nBack in stock on {cfg["name"]}.',
                    url=url, priority="high", tags=["rotating_light"]))
                fired = True; alerts += 1
            elif known and handle not in known and cfg.get("notify_new", True):
                notifier.send(Alert(title=f'NEW on {cfg["name"]}: {title or handle}',
                    message=f'New product listed on {cfg["name"]}. Now monitored.',
                    url=url, priority="default", tags=["new"]))
                alerts += 1
            store.record(handle, in_stock=avail, title=title,
                         price=(f"\u20ac{price}" if price else None), alerted=fired)
        known |= set(found)
        store.save()
        log.info("cycle: %d products, %d in stock, %d alerts (%.1fs)",
                 len(found), sum(1 for v in found.values() if v[1]), alerts, time.time()-t0)
        wait = cfg.get("interval", 300) * random.uniform(0.8, 1.2)
        for _ in range(int(wait)):
            if _stop: break
            time.sleep(1)
    log.info("shut down cleanly")

if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
