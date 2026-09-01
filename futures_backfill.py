from __future__ import annotations

import argparse
import csv
import io
import os
import tempfile
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from db import db, fetchall_dict, USING_POSTGRES
from futures_similarity import ensure_tables

TZ = ZoneInfo("Asia/Taipei")
PRODUCT = "TXF_CONT"
PUBLIC_SQL = "https://raw.githubusercontent.com/jason43314-crypto/taiwan-futures-1min-ohlc/main/data_FITXN_{year}.sql"
OFFICIAL_ZIP = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{ymd}.zip"


def session_info(dt_local: datetime):
    mins = dt_local.hour * 60 + dt_local.minute
    if mins >= 15 * 60:
        idx = (mins - 15 * 60) // 3
        return "night", idx
    if mins <= 5 * 60:
        idx = (9 * 60 + mins) // 3
        return "night", idx
    if 8 * 60 + 45 <= mins <= 13 * 60 + 45:
        idx = (mins - (8 * 60 + 45)) // 3
        return "day", idx
    return None


def bar_start(dt_local: datetime, session: str, idx: int):
    if session == "day":
        s = dt_local.replace(hour=8, minute=45, second=0, microsecond=0)
    elif dt_local.hour >= 15:
        s = dt_local.replace(hour=15, minute=0, second=0, microsecond=0)
    else:
        prev = dt_local - timedelta(days=1)
        s = prev.replace(hour=15, minute=0, second=0, microsecond=0)
    return s + timedelta(minutes=3 * idx)


def add_bar(store, key, dt_local, o, h, l, c, v):
    sec = int(dt_local.replace(tzinfo=TZ).astimezone(timezone.utc).timestamp())
    session, idx = key[-2], key[-1]
    start = int(bar_start(dt_local.replace(tzinfo=TZ), session, idx).astimezone(timezone.utc).timestamp())
    b = store.get(key)
    if b is None:
        store[key] = {
            "ts_utc": start, "open": o, "high": h, "low": l, "close": c,
            "volume": v, "first": sec, "last": sec,
        }
    else:
        b["high"] = max(b["high"], h)
        b["low"] = min(b["low"], l)
        b["volume"] += v
        if sec < b["first"]:
            b["first"] = sec
            b["open"] = o
        if sec >= b["last"]:
            b["last"] = sec
            b["close"] = c


def upsert(store, source):
    if not store:
        return 0
    ensure_tables()
    now = datetime.now(timezone.utc).isoformat()
    vals = []
    for key, b in store.items():
        trading_date, session, idx = key[-3], key[-2], key[-1]
        vals.append((
            PRODUCT, trading_date, session, idx, b["ts_utc"],
            b["open"], b["high"], b["low"], b["close"], b["volume"],
            source, now,
        ))
    if USING_POSTGRES:
        sql = """
        INSERT INTO futures_3m
        (product,trading_date,session,bar_index,ts_utc,open,high,low,close,volume,source,imported_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(product,trading_date,session,bar_index) DO UPDATE SET
          ts_utc=EXCLUDED.ts_utc, open=EXCLUDED.open, high=EXCLUDED.high,
          low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume,
          source=EXCLUDED.source, imported_at=EXCLUDED.imported_at
        """
    else:
        sql = """
        INSERT INTO futures_3m
        (product,trading_date,session,bar_index,ts_utc,open,high,low,close,volume,source,imported_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(product,trading_date,session,bar_index) DO UPDATE SET
          ts_utc=excluded.ts_utc, open=excluded.open, high=excluded.high,
          low=excluded.low, close=excluded.close, volume=excluded.volume,
          source=excluded.source, imported_at=excluded.imported_at
        """
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.executemany(sql, vals)
        else:
            con.executemany(sql, vals)
    return len(vals)


def download_to(url, path):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    with httpx.stream("GET", url, headers=headers, timeout=60, follow_redirects=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)


def import_public_year(year):
    url = PUBLIC_SQL.format(year=year)
    print(f"[public] download {url}")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / f"fitxn_{year}.sql"
        download_to(url, path)
        store = {}
        rows = 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line or line.startswith("--") or line.startswith("COPY ") or line.startswith("BEGIN") or line.startswith("COMMIT") or line.startswith("\\."):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 9:
                    continue
                dt_s, product_id, o, h, l, c, vol, trading_date, synthetic = parts
                if product_id.strip() != "FITXN*1" or synthetic.strip().lower() in ("t", "true", "1"):
                    continue
                try:
                    dt_local = datetime.strptime(dt_s, "%Y-%m-%d %H:%M:%S")
                    info = session_info(dt_local)
                    if not info:
                        continue
                    session, idx = info
                    key = (trading_date.strip(), session, idx)
                    add_bar(store, key, dt_local, float(o), float(h), float(l), float(c), float(vol or 0))
                    rows += 1
                except Exception:
                    continue
        n = upsert(store, f"github-fitxn-{year}")
        print({"source": "public_sql", "year": year, "minute_rows": rows, "bars_3m": n})
        return n


def _norm_headers(fieldnames):
    return {str(x).strip().replace("\ufeff", ""): x for x in (fieldnames or [])}


def import_official_day(d: date):
    ymd = d.strftime("%Y_%m_%d")
    url = OFFICIAL_ZIP.format(ymd=ymd)
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Referer": "https://www.taifex.com.tw/"}
    try:
        r = httpx.get(url, headers=headers, timeout=45, follow_redirects=True)
        if r.status_code != 200 or len(r.content) < 200:
            print({"date": str(d), "status": r.status_code, "skip": "no_official_zip"})
            return 0
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception as e:
        print({"date": str(d), "skip": "download_or_zip_error", "error": repr(e)})
        return 0

    by_exp = {}
    exp_volume = defaultdict(float)
    found = 0
    for name in z.namelist():
        if not name.lower().endswith((".csv", ".txt")):
            continue
        with z.open(name) as raw:
            text = io.TextIOWrapper(raw, encoding="cp950", errors="replace", newline="")
            reader = csv.DictReader(text)
            hm = _norm_headers(reader.fieldnames)
            needed = ["成交日期", "商品代號", "到期月份(週別)", "成交時間", "成交價格", "成交數量(B+S)"]
            if any(k not in hm for k in needed):
                continue
            for row in reader:
                if str(row.get(hm["商品代號"], "")).strip() != "TX":
                    continue
                exp = str(row.get(hm["到期月份(週別)"], "")).strip()
                try:
                    dt_local = datetime.strptime(
                        str(row[hm["成交日期"]]).strip() + " " + str(row[hm["成交時間"]]).strip().zfill(6),
                        "%Y%m%d %H%M%S",
                    )
                    info = session_info(dt_local)
                    if not info:
                        continue
                    session, idx = info
                    price = float(str(row[hm["成交價格"]]).strip())
                    volume = float(str(row[hm["成交數量(B+S)"]]).strip() or 0) / 2.0
                except Exception:
                    continue
                store = by_exp.setdefault(exp, {})
                key = (str(d), session, idx)
                add_bar(store, key, dt_local, price, price, price, price, volume)
                exp_volume[exp] += volume
                found += 1

    if not exp_volume:
        print({"date": str(d), "skip": "no_TX_rows"})
        return 0
    active = max(exp_volume, key=exp_volume.get)
    n = upsert(by_exp[active], f"taifex-daily-{active}")
    print({"date": str(d), "TX_trades": found, "active_expiry": active, "bars_3m": n})
    return n


def import_official_range(start, end):
    d = start
    total = 0
    attempted = 0
    while d <= end:
        if d.weekday() < 5:
            attempted += 1
            total += import_official_day(d)
        d += timedelta(days=1)
    print({"official_attempted_weekdays": attempted, "bars_3m_written": total})
    return total


def coverage():
    ensure_tables()
    ph = "%s" if USING_POSTGRES else "?"
    with db() as con:
        rs = fetchall_dict(
            con,
            f"""SELECT session,MIN(trading_date) first_date,MAX(trading_date) last_date,
            COUNT(DISTINCT trading_date) trading_days,COUNT(*) bars
            FROM futures_3m WHERE product={ph}
            GROUP BY session ORDER BY session""",
            (PRODUCT,),
        )
        src = fetchall_dict(
            con,
            f"""SELECT source,COUNT(*) bars,COUNT(DISTINCT trading_date) trading_days
            FROM futures_3m WHERE product={ph}
            GROUP BY source ORDER BY bars DESC""",
            (PRODUCT,),
        )
    print({"coverage": rs, "sources": src})
    return rs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["public", "official", "bootstrap", "coverage"], default="coverage")
    ap.add_argument("--years", nargs="*", type=int, default=[2024, 2025, 2026])
    ap.add_argument("--start", default="2026-05-18")
    ap.add_argument("--end", default="2026-08-31")
    args = ap.parse_args()

    if args.mode in ("public", "bootstrap"):
        for y in args.years:
            try:
                import_public_year(y)
            except Exception as e:
                print({"year": y, "public_error": repr(e)})
                if args.mode == "public":
                    raise
    if args.mode in ("official", "bootstrap"):
        import_official_range(date.fromisoformat(args.start), date.fromisoformat(args.end))
    coverage()


if __name__ == "__main__":
    main()
