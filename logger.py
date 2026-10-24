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
TZ_OFFSET = float(os.environ.get("SIGNAL_TZ_OFFSET", "6"))  # இயல்பு நேர மண்டலம் (மணி). வங்கதேசம்=6, இலங்கை=5.5
FIRST_RUN_LIMIT = 300


def _channel_list():
    """CHANNELS="name1,name2:5.5"  (name:offset -> அந்த சேனலுக்கு மட்டும் தனி நேர மண்டலம்)"""
    raw = os.environ.get("CHANNELS") or os.environ.get("CHANNEL") or "Secret_Quotex_Bot"
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key, off = item, TZ_OFFSET
        if ":" in item:
            k, o = item.rsplit(":", 1)
            try:
                key, off = k.strip(), float(o)
            except ValueError:
                pass
        out.append((key.lstrip("@"), off))
    return out


CHANNEL_LIST = _channel_list()
CHANNEL = CHANNEL_LIST[0][0]   # trader_demo பழைய பெயருக்காக

STATE = "data/state.json"
SIGNALS = "data/signals.csv"
RAW = "data/raw_messages.csv"
FIELDS = ["channel", "msg_id", "msg_utc", "fmt", "pair", "entry_local", "entry_utc",
          "lead_sec", "expiry", "direction", "price", "channel_rate_pct",
          "result_raw", "result_class", "result_utc"]
RAW_FIELDS = ["channel", "msg_id", "msg_utc", "text"]

# result_class: DW = முதல் ட்ரேடிலேயே வெற்றி, MW = முதல் தோல்வி, MTG-ல் வெற்றி,
#               L = MTG-க்குப் பிறகும் தோல்வி, D = doji/draw


def find(pattern, text):
    m = re.search(pattern, text, re.I)
    return m.group(1) if m else None


OUTCOME_RE = r"(DIRECT WIN|WIN \d MTG|LOSS \d MTG|MTG LOSS|MTG WIN|DOJI)"


def outcome_class(o):
    o = o.upper()
    if o == "DIRECT WIN":
        return "DW"
    if o == "DOJI":
        return "D"
    if "WIN" in o:
        return "MW"
    return "L"


def norm_pair(p):
    return p.upper().replace("-OTCQ", "-OTC")


def _entry(local_msg, hhmm, future):
    h, mi = map(int, hhmm.split(":"))
    e = local_msg.replace(hour=h, minute=mi, second=0, microsecond=0)
    if future:      # சிக்னல்: entry நேரம் மெசேஜுக்குப் பின்
        if e < local_msg - timedelta(hours=12):
            e += timedelta(days=1)
        elif e > local_msg + timedelta(hours=12):
            e -= timedelta(days=1)
    else:           # முடிவு: entry நேரம் மெசேஜுக்கு முன்
        if e > local_msg + timedelta(minutes=2):
            e -= timedelta(days=1)
    return e


def parse(text, msg_dt, msg_id, tz_offset=None):
    """சிக்னல் (வடிவம் A அல்லது B) என்றால் dict, இல்லையென்றால் None"""
    fmt = pair = tm = direction = expiry = price = rate = None
    a = find(r"PAIR\W+([A-Za-z0-9/_\-]+)", text)
    b = find(r"TIME\W+(\d{1,2}:\d{2})", text)
    d = find(r"DIRECTION\W+(PUT|CALL)", text)
    if a and b and d:                                   # வடிவம் A (PAIR/TIME/...)
        fmt, pair, tm, direction = "A", a, b, d
        expiry = find(r"EXPIRY\W+([A-Za-z]\d+)", text)
        price = find(r"PRICE\W+(\d+(?:\.\d+)?)", text)
        rate = find(r"Success Rate\W+([\d.]+)\s*%", text)
    elif "\U0001F4CA" in text and "\U0001F553" in text and not re.search(OUTCOME_RE, text, re.I):
        p = find(r"\U0001F4CA\s*([A-Za-z0-9/_\-]+)", text)           # வடிவம் B (NonSTOP)
        t = find(r"\U0001F553\s*(\d{1,2}:\d{2})", text)
        dd = find(r"\b(PUT|CALL)\b", text)
        ex = find(r"\u23F3\s*([A-Za-z]\d+)", text)
        if p and t and dd and ex:
            fmt, pair, tm, direction, expiry = "B", p, t, dd, ex
    if not fmt:
        return None
    tz = timezone(timedelta(hours=TZ_OFFSET if tz_offset is None else tz_offset))
    local_msg = msg_dt.astimezone(tz)
    entry = _entry(local_msg, tm, future=True)
    return {
        "msg_id": msg_id,
        "msg_utc": msg_dt.astimezone(timezone.utc),
        "fmt": fmt,
        "pair": pair.upper(),
        "entry_local": entry.strftime("%Y-%m-%d %H:%M"),
        "entry_utc": entry.astimezone(timezone.utc),
        "lead_sec": round((entry - local_msg).total_seconds(), 1),
        "expiry": (expiry or "").upper(),
        "direction": direction.upper(),
        "price": price,
        "channel_rate_pct": rate,
    }


def parse_result(text, msg_dt, tz_offset=None):
    """சேனல் வெளியிடும் முடிவு மெசேஜ் -> dict அல்லது None"""
    o = find(OUTCOME_RE, text)
    if not o:
        return None
    m = re.search(r"\U0001F432\s*([A-Za-z0-9/_\-]+)\s*\u2503\s*\U0001F553\s*(\d{1,2}:\d{2})", text)  # வடிவம் A
    if m:
        pair, tm = m.group(1), m.group(2)
    else:                                                                                   # வடிவம் B
        pair = find(r"\U0001F4CA\s*([A-Za-z0-9/_\-]+)", text)
        tm = find(r"\U0001F553\s*(\d{1,2}:\d{2})", text)
    if not (pair and tm):
        return None
    tz = timezone(timedelta(hours=TZ_OFFSET if tz_offset is None else tz_offset))
    entry = _entry(msg_dt.astimezone(tz), tm, future=False)
    return {"pair": norm_pair(pair), "entry_utc": entry.astimezone(timezone.utc),
            "result_raw": o.upper(), "result_class": outcome_class(o),
            "result_utc": msg_dt.astimezone(timezone.utc)}


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
    raise LookupError(f"சேனல் '{key}' கிடைக்கவில்லை. அந்த சேனலில் Join ஆகியுள்ளீர்களா? list_channels.py ஓட்டிப் பெயரைச் சரிபாருங்கள்.")


def rebuild():
    """raw_messages.csv-ல் இருந்து signals.csv-ஐ (முடிவுகளுடன்) முழுவதும் மீண்டும் உருவாக்கு.
    Parser மேம்பட்டால் பழைய தரவுக்கும் தானாகப் பொருந்தும்."""
    if not os.path.exists(RAW):
        return 0, 0
    offs = {k: o for k, o in CHANNEL_LIST}
    sigs, results = [], {}
    with open(RAW, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ch, text = r["channel"], r["text"] or ""
            dt = datetime.fromisoformat(r["msg_utc"])
            off = offs.get(ch, TZ_OFFSET)
            row = parse(text, dt, int(r["msg_id"]), off)
            if row:
                row["channel"] = ch
                sigs.append(row)
                continue
            res = parse_result(text, dt, off)
            if res:
                key = (ch, res["pair"], res["entry_utc"])
                results.setdefault(key, res)          # முதல் முடிவு மட்டும்
    matched = 0
    for row in sigs:
        res = results.get((row["channel"], norm_pair(row["pair"]), row["entry_utc"]))
        if res:
            row["result_raw"], row["result_class"], row["result_utc"] = \
                res["result_raw"], res["result_class"], res["result_utc"]
            matched += 1
    with open(SIGNALS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for row in sigs:
            w.writerow({k: (v.isoformat() if isinstance(v, datetime) else v)
                        for k, v in row.items()})
    return len(sigs), matched


def load_state(first_key):
    if not os.path.exists(STATE):
        return {}
    d = json.load(open(STATE))
    if "channels" in d:
        return d["channels"]
    if "last_id" in d:                      # பழைய வடிவம் -> முதல் சேனலுக்கு
        return {first_key: d["last_id"]}
    return {}


def migrate(path, fields, default):
    """பழைய CSV-ல் 'channel' நெடுவரிசை இல்லையென்றால் சேர்"""
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        if "channel" in (r.fieldnames or []):
            return
        rows = list(r)
    for row in rows:
        row["channel"] = default
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


async def main():
    if not (API_ID and API_HASH and SESSION):
        raise SystemExit("API_ID / API_HASH / SESSION_STRING secrets இல்லை")
    os.makedirs("data", exist_ok=True)
    first_key = CHANNEL_LIST[0][0]
    migrate(RAW, RAW_FIELDS, first_key)
    state = load_state(first_key)

    fetched = {}
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Session string செல்லாது. gen_session.py மூலம் புதியது உருவாக்கி secret-ஐ மாற்றுங்கள்.")
        for key, off in CHANNEL_LIST:
            last_id = state.get(key, 0)
            try:
                ent = await resolve(client, key)
                msgs = [m async for m in client.iter_messages(
                    ent, min_id=last_id, limit=None if last_id else FIRST_RUN_LIMIT)]
            except Exception as e:
                print(f"[{key}] தவிர்க்கப்பட்டது: {e}")
                continue
            msgs.reverse()  # பழையது முதலில்
            fetched[key] = (off, msgs)
    finally:
        await client.disconnect()

    for key, (off, msgs) in fetched.items():
        raw_rows = [{"channel": key, "msg_id": m.id, "msg_utc": m.date.isoformat(), "text": m.message}
                    for m in msgs if m.message]
        if raw_rows:
            append_csv(RAW, RAW_FIELDS, raw_rows)
        if msgs:
            state[key] = max(m.id for m in msgs)
        print(f"[{key}] புதிய மெசேஜ்கள்={len(raw_rows)}")
    json.dump({"channels": state}, open(STATE, "w"))
    n, matched = rebuild()
    print(f"signals.csv: மொத்த சிக்னல்கள்={n}, முடிவுடன் பொருந்தியவை={matched}")


if __name__ == "__main__":
    asyncio.run(main())
