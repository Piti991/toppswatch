#!/usr/bin/env python3
"""Daily digest built from the watcher's state file. Makes zero web requests."""
import html as _html, json, os, sys, time, urllib.parse, urllib.request

STATE = "/opt/toppswatch/data/state.json"
BASE = "https://es.topps.com"
CHUNK = 3500
STALE = 3600

def load_env(path="/opt/toppswatch/.env"):
    env = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env

def send(token, chat, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=20) as r:
            r.read()
    except Exception as exc:
        print(f"send failed: {exc}", file=sys.stderr)

def send_chunked(token, chat, lines):
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > CHUNK:
            send(token, chat, buf); time.sleep(1); buf = ""
        buf += line + "\n"
    if buf.strip():
        send(token, chat, buf)

def main():
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("missing telegram credentials", file=sys.stderr); return 1
    try:
        with open(STATE) as fh:
            raw = json.load(fh)
    except Exception as exc:
        send(token, chat, f"Digest failed: cannot read state file ({exc})"); return 1

    saved_at = raw.get("saved_at", 0)
    age = time.time() - saved_at
    prods = list(raw.get("products", {}).values())
    live = [p for p in prods if p.get("in_stock") is True]
    out  = [p for p in prods if p.get("in_stock") is False]
    unk  = [p for p in prods if p.get("in_stock") is None]

    lines = [f"<b>Topps digest - {time.strftime('%d %b %Y')}</b>",
             f"{len(live)} in stock / {len(out)} sold out / {len(unk)} unknown", ""]
    if age > STALE:
        lines.append(f"WARNING: monitor data is {int(age/60)} min old - check the service")
        lines.append("")
    for label, group in (("IN STOCK", live), ("SOLD OUT", out), ("UNKNOWN", unk)):
        if not group:
            continue
        lines.append(f"--- {label} ---")
        for p in sorted(group, key=lambda r: (r.get("title") or r["handle"]).lower()):
            name = _html.escape((p.get("title") or p["handle"]).split("|")[0].strip())
            lines.append(f'<a href="{BASE}/products/{p["handle"]}">{name}</a>')
        lines.append("")
    send_chunked(token, chat, lines)
    print(f"{len(prods)} products from state ({int(age)}s old)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
