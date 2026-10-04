#!/usr/bin/env python3
"""Daily digest from a watcher's state file. Zero web requests.
Usage: digest.py <state.json> <label> <product_url_template>"""
import html as _html, json, os, sys, time, urllib.parse, urllib.request
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

def fmt_price(p):
    p = str(p or "").strip()
    if not p:
        return ""
    return p if p.startswith("\u20ac") else f"\u20ac{p}"

def main(argv):
    if len(argv) != 4:
        print(__doc__, file=sys.stderr); return 2
    state_path, label, url_tpl = argv[1], argv[2], argv[3]
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("missing telegram credentials", file=sys.stderr); return 1
    try:
        with open(state_path) as fh:
            raw = json.load(fh)
    except Exception as exc:
        send(token, chat, f"{_html.escape(label)} digest failed: cannot read state ({_html.escape(str(exc))})")
        return 1
    age = time.time() - raw.get("saved_at", 0)
    prods = list(raw.get("products", {}).values())
    live = [p for p in prods if p.get("in_stock") is True]
    out  = [p for p in prods if p.get("in_stock") is False]
    unk  = [p for p in prods if p.get("in_stock") is None]
    lines = [f"<b>{_html.escape(label)} digest - {time.strftime('%d %b %Y')}</b>",
             f"{len(live)} in stock / {len(out)} sold out / {len(unk)} unknown", ""]
    if age > STALE:
        lines += [f"WARNING: monitor data is {int(age/60)} min old - check the service", ""]
    for name, group in (("IN STOCK", live), ("SOLD OUT", out), ("UNKNOWN", unk)):
        if not group:
            continue
        lines.append(f"--- {name} ---")
        for p in sorted(group, key=lambda r: (r.get("title") or r["handle"]).lower()):
            title = _html.escape((p.get("title") or p["handle"]).split("|")[0].strip())
            url = url_tpl.format(handle=p["handle"])
            lines.append(f'<a href="{url}">{title}</a> {_html.escape(fmt_price(p.get("price")))}'.rstrip())
        lines.append("")
    send_chunked(token, chat, lines)
    print(f"{label}: {len(prods)} products ({int(age)}s old)")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
