"""Telegram Quotex signal logger -> data/signals.csv + data/raw_messages.csv
GitHub Actions-ல் ஓடும். Secrets: API_ID, API_HASH, SESSION_STRING
"""
import os, re, csv, json, asyncio
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
SESSION = os.environ.get("SESSION_STRING", "")
CHANNEL = os.environ.get("CHANNEL", "Secret_Quotex_Bot")
TZ_OFFSET = float(os.environ.get("SIGNAL_TZ_OFFSET", "6"))  # சிக்னல் நேர மண்டலம் (மணி). வங்கதேசம்=6, இலங்கை=5.5
FIRST_RUN_LIMIT = 300

STATE = "data/state.json"
SIGNALS = "data/signals.csv"
RAW = "data/raw_messages.csv"
FIELDS = ["msg_id", "msg_utc", "pair", "entry_local", "entry_utc",
          "lead_sec", "expiry", "direction", "price", "channel_rate_pct"]


def find(pattern, text):
    m = re.search(pattern, text, re.I)
    return m.group(1) if m else None


def parse(text, msg_dt, msg_id):
    """சிக்னல் மெசேஜ் என்றால் dict, இல்லையென்றால் None"""
    pair = find(r"PAIR\W+([A-Za-z0-9/_\-]+)", text)
    tm = find(r"TIME\W+(\d{1,2}:\d{2})", text)
    direction = find(r"DIRECTION\W+(PUT|CALL)", text)
    if not (pair and tm and direction):
        return None
    expiry = find(r"EXPIRY\W+([A-Za-z]\d+)", text)
    price = find(r"PRICE\W+(\d+(?:\.\d+)?)", text)
    rate = find(r"Success Rate\W+([\d.]+)\s*%", text)

    tz = timezone(timedelta(hours=TZ_OFFSET))
    local_msg = msg_dt.astimezone(tz)
    h, mi = map(int, tm.split(":"))
    entry = local_msg.replace(hour=h, minute=mi, second=0, microsecond=0)
    if entry < local_msg - timedelta(hours=12):
        entry += timedelta(days=1)
    elif entry > local_msg + timedelta(hours=12):
        entry -= timedelta(days=1)

    return {
        "msg_id": msg_id,
        "msg_utc": msg_dt.astimezone(timezone.utc),
        "pair": pair.upper(),
        "entry_local": entry.strftime("%Y-%m-%d %H:%M"),
        "entry_utc": entry.astimezone(timezone.utc),
        "lead_sec": round((entry - local_msg).total_seconds(), 1),
        "expiry": (expiry or "").upper(),
        "direction": direction.upper(),
        "price": price,
        "channel_rate_pct": rate,
    }


def append_csv(path, fields, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: (v.isoformat() if isinstance(v, datetime) else v)
                        for k, v in r.items()})


async def resolve(client, key):
    """சேனலை username / பெயர் / id மூலம் கண்டுபிடி"""
    try:
        return await client.get_entity(int(key) if key.lstrip("-").isdigit() else key)
    except Exception:
        pass
    async for d in client.iter_dialogs():
        if str(d.id) == key or key.lower().lstrip("@") in (d.name or "").lower():
            return d.entity
    raise SystemExit(f"சேனல் '{key}' கிடைக்கவில்லை. அந்த சேனலில் Join ஆகியுள்ளீர்களா? list_channels.py ஓட்டிப் பெயரைச் சரிபாருங்கள்.")


async def main():
    if not (API_ID and API_HASH and SESSION):
        raise SystemExit("API_ID / API_HASH / SESSION_STRING secrets இல்லை")
    os.makedirs("data", exist_ok=True)
    last_id = 0
    if os.path.exists(STATE):
        last_id = json.load(open(STATE))["last_id"]

    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Session string செல்லாது. gen_session.py மூலம் புதியது உருவாக்கி secret-ஐ மாற்றுங்கள்.")
        ent = await resolve(client, CHANNEL)
        msgs = [m async for m in client.iter_messages(
            ent, min_id=last_id, limit=None if last_id else FIRST_RUN_LIMIT)]
    finally:
        await client.disconnect()
    msgs.reverse()  # பழையது முதலில்

    sig_rows, raw_rows = [], []
    for m in msgs:
        if not m.message:
            continue
        raw_rows.append({"msg_id": m.id, "msg_utc": m.date.isoformat(), "text": m.message})
        row = parse(m.message, m.date, m.id)
        if row:
            sig_rows.append(row)

    if raw_rows:
        append_csv(RAW, ["msg_id", "msg_utc", "text"], raw_rows)
    if sig_rows:
        append_csv(SIGNALS, FIELDS, sig_rows)
    if msgs:
        json.dump({"last_id": max(m.id for m in msgs)}, open(STATE, "w"))
    print(f"messages={len(raw_rows)} signals={len(sig_rows)}")


if __name__ == "__main__":
    asyncio.run(main())
