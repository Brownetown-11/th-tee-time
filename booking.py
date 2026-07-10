#!/usr/bin/env python3
"""
Terrey Hills Golf Club — Automated Tee Time Booker
=======================================================
Reads bookings.json to determine if today has a booking to run.
Sleeps until 6:10 PM Sydney time, then logs in and books the
optimal slot the instant the 6:30 PM window opens.

Environment variables (set as GitHub Secrets):
  TH_USERNAME  — member login number (default: 10635)
  TH_PASSWORD  — member password (required)
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

# ─── Config ───────────────────────────────────────────────────────────────────
SYDNEY      = ZoneInfo("Australia/Sydney")
LOGIN_URL   = "https://www.terreyhillsgolf.com.au/security/login.msp"
EVENTS_URL  = "https://www.terreyhillsgolf.com.au/views/members/booking/eventList.xhtml"

TH_USERNAME = os.environ.get("TH_USERNAME", "10635")
TH_PASSWORD = os.environ.get("TH_PASSWORD", "")

if not TH_PASSWORD:
    print("❌ ERROR: TH_PASSWORD environment variable not set")
    sys.exit(1)


# ─── Bookings config ──────────────────────────────────────────────────────────
def load_bookings(path="bookings.json"):
    with open(path) as f:
        return json.load(f)


def save_bookings(data, path="bookings.json"):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def get_today_tasks(bookings):
    """Return list of booking tasks to run today (Sydney time)."""
    now      = datetime.now(SYDNEY)
    today    = now.strftime("%Y-%m-%d")
    weekday  = now.strftime("%A")  # e.g. "Friday"
    tasks    = []

    # ── Default recurring booking ──────────────────────────────────────────
    default = bookings.get("default", {})
    if default.get("enabled", True):
        target_day = default.get("targetDay", "Friday")
        if weekday == target_day:
            # Check if this fire date is in the skip list
            skip_dates = default.get("skipDates", [])
            if today in skip_dates:
                print(f"ℹ️  Default {target_day} booking is skipped for {today}")
            else:
                target = now + timedelta(days=14)
                tasks.append({
                    "id":           "default",
                    "targetDate":   target.strftime("%Y-%m-%d"),
                    "targetDOW":    target.strftime("%a"),   # "Fri"
                    "targetDay":    str(target.day),          # "24"
                    "targetMonth":  target.strftime("%b"),   # "Jul"
                    "preferredHour": default.get("preferredHour", 8),
                    "note": f"Default {target_day} booking → {target.strftime('%a %d %b %Y')}"
                })

    # ── One-off bookings ───────────────────────────────────────────────────
    for booking in bookings.get("oneoffs", []):
        if booking.get("fireDate") == today and booking.get("status") == "pending":
            tasks.append(booking)

    return tasks


# ─── Timing ───────────────────────────────────────────────────────────────────
async def sleep_until(hour, minute, label):
    """Sleep until the given local Sydney hour:minute."""
    now    = datetime.now(SYDNEY)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        print(f"✓ Already past {label} Sydney ({now.strftime('%H:%M:%S')}), continuing immediately")
        return
    delta = (target - now).total_seconds()
    print(f"⏳ Sleeping {delta/60:.1f} min until {label} Sydney time …")
    await asyncio.sleep(delta)


# ─── Booking flow ─────────────────────────────────────────────────────────────
async def book_tee_time(page, task):
    """Execute the full booking flow for one task. Returns booked slot string."""
    dow   = task["targetDOW"]    # "Fri"
    day   = task["targetDay"]    # "24"
    month = task["targetMonth"]  # "Jul"
    pref  = task["preferredHour"]

    print(f"\n▶ Booking: {dow} {day} {month}  (preferred ~{pref}:00)")

    # ── Step 1: Login ──────────────────────────────────────────────────────
    print("  → Logging in …")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.fill('input[name="user"]', TH_USERNAME)
    await page.fill('input[type="password"]', TH_PASSWORD)
    await page.click('input[type="submit"]')
    await page.wait_for_load_state("domcontentloaded", timeout=15_000)

    if "login" in page.url.lower():
        raise RuntimeError("Login failed — check TH_USERNAME / TH_PASSWORD secrets")
    print("  ✓ Logged in")

    # ── Step 2: Navigate to event list ────────────────────────────────────
    await page.goto(EVENTS_URL, wait_until="domcontentloaded", timeout=30_000)
    print("  ✓ On event list")

    # ── Step 3: Background 300ms poller + wait for 6:30 PM ────────────────
    await page.evaluate(f"""
        window.alert = msg => {{ window._lastAlert = msg; }};
        window._openClicked = false;
        window._clickedAt   = null;
        window._T_DOW   = '{dow}';
        window._T_DAY   = '{day}';
        window._T_MONTH = '{month}';

        if (window._bgPoll) clearInterval(window._bgPoll);
        window._bgPoll = setInterval(() => {{
            if (window._openClicked) {{ clearInterval(window._bgPoll); return; }}
            const containers = [...document.querySelectorAll('div.left-content-container')];
            for (const c of containers) {{
                const span = (c.querySelector('span')?.textContent || '').trim();
                // span format: "Fri24 Jul"
                if (span.includes(window._T_DOW) &&
                    span.includes(window._T_DAY) &&
                    span.includes(window._T_MONTH)) {{
                    const link = c.querySelector('a');
                    if (link && link.textContent.trim() === 'OPEN') {{
                        window._openClicked = true;
                        window._clickedAt = new Date().toLocaleTimeString('en-AU');
                        clearInterval(window._bgPoll);
                        link.click();
                    }}
                }}
            }}
        }}, 300);
        'poller started';
    """)
    print(f"  ✓ 300ms poller running — waiting for 6:30 PM …")

    # Sleep until just before 6:30 PM
    await sleep_until(18, 28, "6:28 PM")

    # Poll every 2 seconds for up to 20 minutes
    for attempt in range(600):
        open_clicked = await page.evaluate("window._openClicked")
        if open_clicked or "eventList" not in page.url:
            clicked_at = await page.evaluate("window._clickedAt || 'now'")
            print(f"  ✓ OPEN link clicked at {clicked_at}")
            break

        if attempt % 15 == 0:
            ts = datetime.now(SYDNEY).strftime("%H:%M:%S")
            print(f"  … polling [{ts}] attempt {attempt+1}")

        await asyncio.sleep(2)

        # Reload every 2 minutes if still locked (belt+suspenders)
        if attempt > 0 and attempt % 60 == 0:
            print("  ↻ Reloading event list and restarting poller …")
            await page.goto(EVENTS_URL, wait_until="domcontentloaded", timeout=20_000)
            await page.evaluate(f"""
                window.alert = msg => {{ window._lastAlert = msg; }};
                window._openClicked = false;
                if (window._bgPoll) clearInterval(window._bgPoll);
                window._bgPoll = setInterval(() => {{
                    if (window._openClicked) {{ clearInterval(window._bgPoll); return; }}
                    const containers = [...document.querySelectorAll('div.left-content-container')];
                    for (const c of containers) {{
                        const span = (c.querySelector('span')?.textContent || '').trim();
                        if (span.includes('{dow}') && span.includes('{day}') && span.includes('{month}')) {{
                            const link = c.querySelector('a');
                            if (link && link.textContent.trim() === 'OPEN') {{
                                window._openClicked = true;
                                window._clickedAt = new Date().toLocaleTimeString('en-AU');
                                clearInterval(window._bgPoll);
                                link.click();
                            }}
                        }}
                    }}
                }}, 300);
            """)
    else:
        raise RuntimeError(f"Timed out waiting for OPEN link for {dow} {day} {month} (20 min elapsed)")

    await page.wait_for_load_state("domcontentloaded", timeout=15_000)

    # ── Step 4: Find and click best BOOK GROUP slot ────────────────────────
    print("  → Selecting slot …")
    slot_summary = await page.evaluate(f"""
        window.alert = msg => {{ window._lastAlert = msg; }};
        window._lastAlert = null;
        window._PREF = {pref};

        const allBtns  = [...document.querySelectorAll('button')].filter(b => b.textContent.trim() === 'BOOK GROUP');
        const timeRx   = /(\d{{1,2}}):(\d{{2}})\s*(AM|PM)/g;
        const allTimes = [...document.body.innerText.matchAll(timeRx)].map(m => ({{
            hour: parseInt(m[1]) + (m[3]==='PM' && m[1]!=='12' ? 12 : 0),
            str: m[0].trim()
        }}));

        window._slots = allBtns.map((btn, i) => {{
            const t = allTimes[i] || {{ hour: 99, str: '?' }};
            return {{ btn, hour: t.hour, str: t.str, diff: Math.abs(t.hour - window._PREF) }};
        }});

        // AM slots sorted by proximity to preferred hour
        window._queue = [...window._slots]
            .filter(s => s.hour < 12)
            .sort((a, b) => a.diff - b.diff || a.hour - b.hour);

        JSON.stringify({{ total: window._slots.length, queue: window._queue.slice(0,5).map(s => s.str) }});
    """)
    print(f"  ✓ Slots found: {slot_summary}")

    booked_slot = None
    queue_len = await page.evaluate("window._queue.length")

    for i in range(min(queue_len, 10)):
        slot_str = await page.evaluate(f"window._queue[{i}].str")
        await page.evaluate(f"""
            window._lastAlert = null;
            window._queue[{i}].btn.click();
        """)
        await asyncio.sleep(1.2)

        result = json.loads(await page.evaluate(
            "JSON.stringify({ url: location.pathname, alert: window._lastAlert })"
        ))

        if "makeBooking" in result.get("url", ""):
            booked_slot = slot_str
            print(f"  ✓ Slot claimed: {slot_str}")
            break
        elif result.get("alert"):
            print(f"  ↷ Slot {slot_str} taken ({result['alert'][:40]}…), trying next")
    else:
        raise RuntimeError("All AM slots exhausted — booking may have been missed")

    # ── Step 5: Add 3 guest placeholders ──────────────────────────────────
    print("  → Adding 3 guest placeholders …")
    for g in range(1, 4):
        # Click Add Guest
        await page.evaluate("""
            const btn = [...document.querySelectorAll('button')].find(b => /add\\s*guest/i.test(b.textContent));
            if (btn) btn.click();
        """)
        await asyncio.sleep(0.6)

        # Fill First Name = G, Surname = Guest
        await page.evaluate("""
            const tds    = [...document.querySelectorAll('td')];
            const fnTd   = tds.find(td => td.textContent.trim() === 'First Name');
            const snTd   = tds.find(td => td.textContent.trim() === 'Surname');
            const fnIn   = fnTd?.nextElementSibling?.querySelector('input') || document.querySelector('input[id*="first" i]');
            const snIn   = snTd?.nextElementSibling?.querySelector('input') || document.querySelector('input[id*="surname" i]');
            const fire   = (el, v) => { el.value = v; el.dispatchEvent(new Event('change',{bubbles:true})); el.dispatchEvent(new Event('blur',{bubbles:true})); };
            if (fnIn) fire(fnIn, 'G');
            if (snIn) fire(snIn, 'Guest');
            const add = [...document.querySelectorAll('button')].find(b => /^\\+?\\s*ADD$/i.test(b.textContent.trim()));
            if (add) add.click();
        """)
        await asyncio.sleep(0.5)
        print(f"    Guest {g} added")

    # Verify
    guest_count = await page.evaluate("(document.body.innerText.match(/Guest/gi)||[]).length")
    print(f"  ✓ Guest mentions on page: {guest_count} (expect ≥ 3)")

    # ── Step 6: Confirm booking ────────────────────────────────────────────
    print("  → Confirming …")
    await page.evaluate("""
        window.alert = msg => { window._lastAlert = msg; };
        window._lastAlert = null;
        const btn = [...document.querySelectorAll('button')].find(b => /confirm\\s*booking/i.test(b.textContent));
        if (btn) btn.click();
    """)
    await asyncio.sleep(3)

    final = json.loads(await page.evaluate("""
        JSON.stringify({ url: location.pathname, snippet: document.body.innerText.substring(0,300), alert: window._lastAlert })
    """))

    print(f"  ✓ Final URL: {final['url']}")
    print(f"  ✓ Page: {final['snippet'][:120].strip()}")
    if final.get("alert"):
        print(f"  ⚠️  Alert: {final['alert']}")

    return booked_slot


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    bookings = load_bookings()
    tasks    = get_today_tasks(bookings)
    now_syd  = datetime.now(SYDNEY)

    if not tasks:
        print(f"ℹ️  No bookings for today ({now_syd.strftime('%A %d %b %Y')} Sydney). Exiting.")
        return

    print(f"📋 {len(tasks)} task(s) for {now_syd.strftime('%A %d %b %Y')}:")
    for t in tasks:
        print(f"   • {t.get('note', t.get('targetDate'))} @ {t.get('preferredHour', 8)}:00")

    # Sleep until 6:10 PM
    await sleep_until(18, 10, "6:10 PM")

    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        ctx  = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await ctx.new_page()

        for task in tasks:
            try:
                slot = await book_tee_time(page, task)
                results.append({"task": task, "status": "booked", "slot": slot})
                print(f"\n✅ BOOKED: {task.get('note', task['targetDate'])} → {slot}")

                # Update oneoff status
                for b in bookings.get("oneoffs", []):
                    if b.get("id") == task.get("id"):
                        b["status"]      = "booked"
                        b["bookedSlot"]  = slot
                        b["bookedAt"]    = now_syd.isoformat()

            except Exception as e:
                results.append({"task": task, "status": "failed", "error": str(e)})
                print(f"\n❌ FAILED: {task.get('note', task['targetDate'])} — {e}")

                for b in bookings.get("oneoffs", []):
                    if b.get("id") == task.get("id"):
                        b["status"]     = "failed"
                        b["failReason"] = str(e)

        await browser.close()

    # Persist updated statuses for oneoffs
    save_bookings(bookings)

    # GitHub Actions job summary
    lines = [f"# Terrey Hills Booking — {now_syd.strftime('%a %d %b %Y')}\n"]
    for r in results:
        icon = "✅" if r["status"] == "booked" else "❌"
        detail = r.get("slot") or r.get("error", "")
        lines.append(f"{icon} **{r['task'].get('note', r['task'].get('targetDate', '?'))}** — {detail}")

    summary = "\n".join(lines)
    print("\n" + summary)

    gss = os.environ.get("GITHUB_STEP_SUMMARY")
    if gss:
        with open(gss, "a") as f:
            f.write(summary + "\n")

    if any(r["status"] == "failed" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
