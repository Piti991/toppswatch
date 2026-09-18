# toppswatch

Restock monitor for **es.topps.com**. Polls every Topps Chrome product, pushes an
alert to your phone the moment one flips from *Agotado* to *Añadir a la cesta*.

Auto-discovers new Chrome products as they get listed, so it catches drops you
didn't know were coming.

---

## Read this first

The storefront is a Next.js app. Product pages are server-rendered, so stock
state is readable with a plain HTTP request — no browser, no Selenium, low
resource use. But **the exact markup can change without notice**, so the
detector tries four strategies in confidence order and tells you which one fired:

| # | Strategy | Signal |
|---|----------|--------|
| 1 | `json-ld` | `<script type="application/ld+json">` → `offers.availability` |
| 2 | `meta` | `og:availability` / `product:availability` |
| 3 | `embedded-json` | `"available": true/false` near the product handle |
| 4 | `text` | `Añadir a la cesta` vs `Agotado` |

If Topps restructures the page, one strategy breaking still leaves three.

**I could not test against the live site** — my sandbox can't reach
`es.topps.com`. The logic is fully tested against fixtures and an end-to-end mock,
but step 3 below is your 30-second confirmation that detection is right on the
real thing. Do it before you trust the monitor.

---

## Setup

### 1. Install

```bash
git clone <wherever you put this> /opt/toppswatch
cd /opt/toppswatch
cp config.example.yaml config.yaml
cp .env.example .env
```

### 2. Pick an alert channel

**ntfy is the fastest path** — free, no account, push notifications on iOS/Android.

1. Install the *ntfy* app.
2. Choose a long random topic name (it's the only thing protecting your alerts).
3. Put it in `.env` as `NTFY_TOPIC=`, subscribe to the same topic in the app.

Telegram, email, a generic webhook, and Home Assistant are all supported — see
`config.example.yaml`. Enable as many as you like; they fire independently.

Confirm delivery:

```bash
python -m toppswatch.main --config config.yaml test-notify
```

### 3. Verify detection against the live site ← don't skip

```bash
python -m toppswatch.main --config config.yaml discover
python -m toppswatch.main --config config.yaml once --debug
```

`discover` prints every Chrome product it found. `once` checks each one and
prints its verdict with the evidence:

```
[debug] 2026-topps-chrome®-baseball-hobby-box -> in_stock=False via json-ld
        (offers.availability=https://schema.org/OutOfStock) price=120.00
```

Open two or three of those product pages in a browser and confirm the verdicts
match. If any are wrong or show `via none`, send me the `--debug` output and I'll
adjust the markers — that's a config/parse tweak, not a rebuild.

### 4. Run it

**Docker (recommended on Proxmox):**
```bash
docker compose up -d
docker compose logs -f
```

**systemd (LXC or bare metal):**
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo useradd -r -s /usr/sbin/nologin toppswatch
sudo chown -R toppswatch /opt/toppswatch
sudo cp toppswatch.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now toppswatch
journalctl -u toppswatch -f
```

Check what it knows any time:

```bash
python -m toppswatch.main --config config.yaml status
```

---

## Home Assistant integration

You already have HA running, so this is the nicest option — alerts arrive as
native mobile push with an actionable "Open store" button.

Enable the `home_assistant` channel in `config.yaml`, then add this automation:

```yaml
alias: Topps restock alert
trigger:
  - platform: webhook
    webhook_id: topps_restock
    allowed_methods: [POST]
    local_only: true
action:
  - service: notify.mobile_app_YOUR_PHONE
    data:
      title: "{{ trigger.json.title }}"
      message: "{{ trigger.json.message }}"
      data:
        url: "{{ trigger.json.url }}"
        push:
          sound:
            name: default
            critical: 1        # iOS: breaks through silent mode
            volume: 1.0
        actions:
          - action: URI
            title: Open store
            uri: "{{ trigger.json.url }}"
```

Point `webhook_url` at `http://192.168.86.50:8123/api/webhook/topps_restock`
(adjust if HA sits on a different IP than the Proxmox host).

---

## How alerting behaves

- Alerts fire on the **transition** out-of-stock → in-stock, not continuously.
- The very first check records a baseline silently, so starting the monitor
  doesn't dump an alert for everything currently in stock. Set
  `alerts.alert_on_first_sight: true` if you'd rather know immediately.
- `min_alert_interval_seconds` (default 30 min) stops a flapping listing from
  spamming you.
- A failed fetch never counts as "out of stock", so a network blip can't
  manufacture a fake restock on the next successful check.
- New matching products found by discovery get a low-priority heads-up.

## Tuning the polling rate

Default is one sweep every 60s with ±20% jitter and 3 parallel requests. For
~10 Chrome products that's roughly 10 requests/minute — light enough to be
unremarkable, fast enough that you're in the first wave.

Config rejects anything under 20 seconds. Going faster doesn't get you the box
sooner; it gets your IP blocked, and then you find out about restocks last.
If you ever see `rate limited (HTTP 403)` in the logs, the monitor already backs
off for 15 minutes on its own — raise `interval_seconds` rather than fighting it.

### If Topps adds bot protection

```bash
pip install curl_cffi
```
then set `polling.impersonate: chrome124`. That mimics a real Chrome TLS
fingerprint. A residential proxy can go in `polling.proxy` if it comes to that.

## What this does *not* do

It doesn't add to cart or check out. It tells you fast; you buy. Automated
checkout is against Topps' terms and is how accounts get banned — and the alert
is the part that was actually missing.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q      # 31 tests
```

Covers stock detection across all four strategies, contradictory-marker
handling, recommendation-carousel poisoning, discovery filtering, alert
deduplication, flap muting, and state persistence across restarts.
