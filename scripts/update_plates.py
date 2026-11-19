#!/usr/bin/env python3
"""從 car16 取得全台目前可選的號碼（選牌）和還沒結束的標牌公告，輸出 plates.json。

車種：電動自小客、電動租賃車、自用小客貨車、租賃小客貨車、營業小客車、營業小貨車、自用大貨車、營業大貨車。
car16 沒有更新時（更新時間和前一份相同）不會重抓，也不會寫出檔案。
用法：python3 update_plates.py 輸出路徑 [前一天的 plates.json]
有提供前一天的檔案時，會替每個號碼記下第一次出現的日期（firstSeen），
當天新增的號碼另外記下是哪一次更新出現的時間（firstSeenTime，台灣時間 HH:MM）。
頁面用它們標出比前一天新增的號碼與新增時間。
選牌資料有問題時以非 0 結束，不會寫出檔案，舊的 plates.json 會保留。
標牌資料抓不到時沿用前一天還沒結束的公告，並在輸出的 warnings 裡說明。
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

TPE = timezone(timedelta(hours=8))
API = "https://strapi.car16.com/api/car-plate-finder"
AUCTION_API = "https://strapi.car16.com/api/car-plate-ann"
TYPES = ["電動自小客", "電動租賃車", "自用小客貨車", "租賃小客貨車",
         "營業小客車", "營業小貨車", "自用大貨車", "營業大貨車"]
PAGE_SIZE = 500
PLATE_RE = re.compile(r"^[A-Z]{3}-\d{4}$")


def ssl_context():
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        path = os.environ.get(key)
        if path and os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


CTX = ssl_context()


def post(body, url=API):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "ev-plates-daily/1.0"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40, context=CTX) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def normalize_office(name):
    return str(name).strip().replace("台", "臺")


def fetch_type(car_type):
    items, page, total, src_updated = [], 1, None, None
    while True:
        body = {
            "plate_number": "", "car_type": car_type, "motor_vehicles_office": "",
            "start_with_number": False, "end_with_number": False, "special": "不限",
            "alphabet_sort": "asc", "number_sort": "asc", "page": page,
            "page_size": PAGE_SIZE, "alphabet": "", "random_seed": 0,
        }
        d = post(body)
        meta = d["meta"]
        total = meta["total"]
        src_updated = d["data"].get("updatedTime")
        for x in d["data"]["carPlates"]:
            page_no = str(x.get("page_number") or "").strip()
            item = {
                "plate": str(x["plate_number"]).strip().upper(),
                "office": normalize_office(x["motor_vehicles_office"]),
                "type": car_type,
            }
            if page_no and not page_no.isdigit():
                item["tag"] = page_no  # 例如「流標牌」
            items.append(item)
        if page >= meta["pageCount"]:
            break
        page += 1
        time.sleep(1)
    return items, total, src_updated


def validate(items, totals):
    errors = []
    by_type = {t: [i for i in items if i["type"] == t] for t in TYPES}
    for t in TYPES:
        if len(by_type[t]) != totals[t]:
            errors.append(f"{t} 筆數不符：抓到 {len(by_type[t])}，來源標示 {totals[t]}")
    if sum(totals.values()) == 0:
        errors.append("所有車種都是 0 筆，來源可能異常")
    seen = set()
    for i in items:
        if not PLATE_RE.match(i["plate"]):
            errors.append(f"號碼格式不對：{i['plate']}")
        if not i["office"]:
            errors.append(f"{i['plate']} 沒有監理站")
        if i["plate"] in seen:
            errors.append(f"號碼重複：{i['plate']}")
        seen.add(i["plate"])
    return errors


def parse_time(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))


def fetch_auctions(now):
    """抓三種車種的標牌公告，只留下還沒結束的。格式不對會丟出例外。"""
    out = []
    for car_type in TYPES:
        page, rows = 1, []
        while True:
            d = post({"car_type": car_type, "motor_vehicles_office": "", "page": page, "page_size": PAGE_SIZE}, AUCTION_API)
            rows += d["data"]["carPlateAnns"]
            if page >= max(d["meta"]["pageCount"], 1):
                break
            page += 1
            time.sleep(1)
        if len(rows) != d["meta"]["total"]:
            raise ValueError(f"{car_type} 標牌筆數不符：抓到 {len(rows)}，來源標示 {d['meta']['total']}")
        for a in rows:
            start, end = parse_time(a["start_time"]), parse_time(a["end_time"])
            if end <= now:
                continue
            out.append({
                "type": car_type,
                "office": normalize_office(a["motor_vehicles_office"]),
                "plates": str(a["plates"]).strip(),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "transferDeadline": parse_time(a["transfer_deadline"]).isoformat() if a.get("transfer_deadline") else None,
            })
        time.sleep(1)
    out.sort(key=lambda a: (a["start"], a["office"], a["plates"]))
    return out


def source_signature():
    """用兩次小查詢取得 car16 選牌和標牌的更新時間，判斷要不要重抓。"""
    d = post({"plate_number": "", "car_type": TYPES[0], "motor_vehicles_office": "",
              "start_with_number": False, "end_with_number": False, "special": "不限",
              "alphabet_sort": "asc", "number_sort": "asc", "page": 1, "page_size": 1,
              "alphabet": "", "random_seed": 0})
    a = post({"car_type": TYPES[0], "motor_vehicles_office": "", "page": 1, "page_size": 1}, AUCTION_API)
    return f'{d["data"].get("updatedTime")}|{a["data"].get("updatedTime")}'


def taipei_date(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TPE).date().isoformat()


def load_previous(path):
    """讀前一天的 plates.json；格式不對就當作沒有，回傳 None。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
        items = prev["items"]
        prev_date = prev.get("sourceDate") or taipei_date(prev["sourceUpdatedAt"])
        first = {str(i["plate"]): (i.get("firstSeen") or prev_date) for i in items}
        times = {str(i["plate"]): i["firstSeenTime"] for i in items if i.get("firstSeenTime")}
        types = {i.get("type") for i in items}
        return {"date": prev_date, "previousSourceDate": prev.get("previousSourceDate"), "first": first, "times": times,
                "types": types, "auctions": prev.get("auctions") or [],
                "signature": prev.get("sourceSignature"), "allTypes": set(TYPES) <= types}
    except Exception as e:
        print(f"前一天的檔案無法使用，這次不標示新增：{e}", file=sys.stderr)
        return None


def mark_first_seen(items, source_date, prev, source_time=None):
    """回傳 previousSourceDate（比較基準日；None 表示沒有可比較的資料）。"""
    if prev is None:
        for i in items:
            i["firstSeen"] = source_date
        return None
    # 來源還沒換新的一天（例如同一天重跑）：沿用原本的比較基準，標示不會被洗掉
    previous = prev["previousSourceDate"] if prev["date"] == source_date else prev["date"]
    for i in items:
        if i["type"] not in prev["types"]:
            # 前一份資料還沒有這個車種（剛加進來）：當作基準，記成比較基準日，不標成新增
            i["firstSeen"] = previous or source_date
        else:
            i["firstSeen"] = prev["first"].get(i["plate"], source_date)
            if previous and i["firstSeen"] == source_date:
                # 當天新增：沿用先前記下的時間；這次才出現的記成這次更新的時間
                t = prev["times"].get(i["plate"]) or source_time
                if t:
                    i["firstSeenTime"] = t
    return previous


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit("用法：python3 update_plates.py 輸出路徑 [前一天的 plates.json]")
    out_path = sys.argv[1]
    prev = load_previous(sys.argv[2] if len(sys.argv) == 3 else None)
    signature = source_signature()
    if prev and prev["allTypes"] and prev["signature"] == signature:
        print(json.dumps({"unchanged": True, "sourceSignature": signature}, ensure_ascii=False))
        return
    items, totals, src_times = [], {}, []
    for t in TYPES:
        got, total, src_updated = fetch_type(t)
        items += got
        totals[t] = total
        if src_updated:
            src_times.append(src_updated)
        time.sleep(1)

    errors = validate(items, totals)
    if errors:
        print("資料檢查失敗，不更新檔案：", file=sys.stderr)
        for e in errors[:30]:
            print(" -", e, file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    warnings = []
    try:
        auctions = fetch_auctions(now)
    except Exception as e:
        kept = [a for a in (prev["auctions"] if prev else []) if parse_time(a["end"]) > now]
        warnings.append(f"標牌資料抓取失敗，沿用前一天還沒結束的 {len(kept)} 筆公告：{e}")
        auctions = kept

    source_updated = max(src_times) if src_times else None
    source_date = taipei_date(source_updated) if source_updated else now.astimezone(TPE).date().isoformat()
    source_time = (datetime.fromisoformat(source_updated.replace("Z", "+00:00")).astimezone(TPE).strftime("%H:%M")
                   if source_updated else None)
    previous_date = mark_first_seen(items, source_date, prev, source_time)
    new_counts = {t: (sum(1 for i in items if i["type"] == t and i["firstSeen"] == source_date) if previous_date else 0) for t in TYPES}

    result = {
        "updatedAt": now.isoformat(timespec="seconds"),
        "sourceUpdatedAt": source_updated,
        "sourceDate": source_date,
        "previousSourceDate": previous_date,
        "sourceSignature": signature,
        "source": "car16.com",
        "totals": totals,
        "newCounts": new_counts,
        "warnings": warnings,
        "auctions": auctions,
        "items": items,
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_path)
    print(json.dumps({"totals": totals, "newCounts": new_counts, "auctions": len(auctions),
                      "sourceDate": source_date, "previousSourceDate": previous_date,
                      "warnings": warnings}, ensure_ascii=False))


if __name__ == "__main__":
    main()
