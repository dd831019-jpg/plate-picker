#!/usr/bin/env python3
"""從 car16 取得全台目前可選的號碼（選牌）和還沒結束的標牌公告。

輸出兩個檔案：輸出資料夾/car.json（汽車）、輸出資料夾/moto.json（機車）。
用法：python3 update_plates.py 輸出資料夾 [前一份資料]
「前一份資料」可以是資料夾（含 car.json、moto.json）或舊版的單一 plates.json。

有前一份資料時，會替每個號碼記下第一次出現的日期（firstSeen），
當天新增的號碼另外記下是哪一次更新出現的時間（firstSeenTime，台灣時間 HH:MM）。
比前一份少掉的號碼記在 removed（當天累計，通常是被選走），頁面用來顯示「今天被選走」。
car16 沒有更新時（更新時間和前一份相同）不會重抓，也不會寫出檔案。
輸出為精簡格式（v2）：監理站、車種、日期改成索引，號碼資料分欄存放。
選牌資料有問題時以非 0 結束，不會寫出檔案，舊資料會保留。
標牌資料抓不到時沿用前一份還沒結束的公告，並在 warnings 裡說明。
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
GROUPS = {
    "car": ["電動自小客", "電動租賃車", "自用小客貨車", "租賃小客貨車",
            "營業小客車", "營業小貨車", "自用大貨車", "營業大貨車"],
    "moto": ["普通重型機車", "550cc以下重機", "550cc以上重機",
             "電動普通重型機車", "電動普通輕型機車", "微型電動二輪車"],
}
TYPES = [t for ts in GROUPS.values() for t in ts]
GROUP_OF = {t: g for g, ts in GROUPS.items() for t in ts}
PAGE_SIZE = 500
PLATE_RE = re.compile(r"^(?:[A-Z]{3}-\d{4}|[A-Z]{2}\d{5})$")


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
        headers={"Content-Type": "application/json", "User-Agent": "plate-picker/1.0"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def finder_body(car_type, page, size):
    return {
        "plate_number": "", "car_type": car_type, "motor_vehicles_office": "",
        "start_with_number": False, "end_with_number": False, "special": "不限",
        "alphabet_sort": "asc", "number_sort": "asc", "page": page,
        "page_size": size, "alphabet": "", "random_seed": 0,
    }


def normalize_office(name):
    return str(name).strip().replace("台", "臺")


def fetch_type(car_type):
    items, page, total, src_updated = [], 1, None, None
    while True:
        d = post(finder_body(car_type, page, PAGE_SIZE))
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
    for t in TYPES:
        n = sum(1 for i in items if i["type"] == t)
        if n != totals[t]:
            errors.append(f"{t} 筆數不符：抓到 {n}，來源標示 {totals[t]}")
    if sum(totals.values()) == 0:
        errors.append("所有車種都是 0 筆，來源可能異常")
    for i in items:
        if not PLATE_RE.match(i["plate"]):
            errors.append(f"號碼格式不對：{i['plate']}")
        if not i["office"]:
            errors.append(f"{i['plate']} 沒有監理站")
    return errors


def clean_duplicates(items, prev, warnings):
    """car16 偶爾會把某個監理站的同一批號碼重複列出很多次（爬取出錯），那個監理站的資料也就不可信。
    這種監理站這次沿用上一份資料；沒有上一份才只去掉重複。同一個號碼出現在不同監理站或車種則視為錯誤。"""
    by_plate = {}
    for i in items:
        by_plate.setdefault(i["plate"], []).append(i)
    conflicts = [p for p, v in by_plate.items() if len({(x["type"], x["office"]) for x in v}) > 1]
    if conflicts:
        return items, [f"號碼同時出現在不同監理站或車種：{p}" for p in conflicts]
    bad = {}
    for v in by_plate.values():
        if len(v) > 1:
            key = (v[0]["type"], v[0]["office"])
            n, extra = bad.get(key, (0, 0))
            bad[key] = (n + 1, extra + len(v) - 1)
    if not bad:
        return items, []
    out = [i for i in items if (i["type"], i["office"]) not in bad]
    for (t, o), (n, extra) in sorted(bad.items()):
        old = [{k: v for k, v in i.items() if k not in ("firstSeen", "firstSeenTime")}
               for i in (prev["items"] if prev else []) if i["type"] == t and i["office"] == o]
        if old:
            out += old
            warnings.append(f"car16 的{o}{t}資料有 {n} 個號碼重複出現（多了 {extra} 筆），這次沿用上一份的 {len(old)} 面")
        else:
            seen = set()
            for i in items:
                if (i["type"], i["office"]) == (t, o) and i["plate"] not in seen:
                    seen.add(i["plate"])
                    out.append(i)
            warnings.append(f"car16 的{o}{t}資料有 {n} 個號碼重複出現（多了 {extra} 筆），已去掉重複")
    return out, []


def parse_time(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))


def fetch_auctions(now):
    """抓所有車種的標牌公告，只留下還沒結束的。格式不對會丟出例外。"""
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
                "plates": str(a["plates"]).strip().rstrip(",").strip(),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "transferDeadline": parse_time(a["transfer_deadline"]).isoformat() if a.get("transfer_deadline") else None,
            })
        time.sleep(1)
    out.sort(key=lambda a: (a["start"], a["office"], a["plates"]))
    return out


def source_signature():
    """用兩次小查詢取得 car16 選牌和標牌的更新時間，判斷要不要重抓。"""
    d = post(finder_body(TYPES[0], 1, 1))
    a = post({"car_type": TYPES[0], "motor_vehicles_office": "", "page": 1, "page_size": 1}, AUCTION_API)
    return f'{d["data"].get("updatedTime")}|{a["data"].get("updatedTime")}'


def taipei_date(iso):
    return parse_time(iso).astimezone(TPE).date().isoformat()


def expand(doc):
    """把一份資料（v2 精簡格式或舊格式）還原成號碼清單和被選走清單。"""
    if doc.get("v") != 2:
        return doc["items"], []
    offices, types, dates = doc["offices"], doc["types"], doc["dates"]
    c = doc["items"]
    items = []
    for k, plate in enumerate(c["p"]):
        it = {"plate": plate, "office": offices[c["o"][k]], "type": types[c["t"][k]]}
        if c["f"][k] >= 0:
            it["firstSeen"] = dates[c["f"][k]]
        if c["h"][k]:
            it["firstSeenTime"] = c["h"][k]
        if c["g"][k]:
            it["tag"] = c["g"][k]
        items.append(it)
    r = doc.get("removed") or {"p": [], "o": [], "t": [], "h": []}
    removed = [{"plate": plate, "office": offices[r["o"][k]], "type": types[r["t"][k]], "time": r["h"][k]}
               for k, plate in enumerate(r["p"])]
    return items, removed


def compact(items, removed, types):
    """把號碼清單轉成 v2 精簡格式的欄位。"""
    offices, dates = [], []
    oi, di = {}, {}

    def idx(table, lookup, value):
        if value not in lookup:
            lookup[value] = len(table)
            table.append(value)
        return lookup[value]

    ti = {t: k for k, t in enumerate(types)}
    cols = {"p": [], "o": [], "t": [], "f": [], "h": [], "g": []}
    for i in items:
        cols["p"].append(i["plate"])
        cols["o"].append(idx(offices, oi, i["office"]))
        cols["t"].append(ti[i["type"]])
        cols["f"].append(idx(dates, di, i["firstSeen"]) if i.get("firstSeen") else -1)
        cols["h"].append(i.get("firstSeenTime", ""))
        cols["g"].append(i.get("tag", ""))
    rem = {"p": [], "o": [], "t": [], "h": []}
    for r in removed:
        rem["p"].append(r["plate"])
        rem["o"].append(idx(offices, oi, r["office"]))
        rem["t"].append(ti[r["type"]])
        rem["h"].append(r.get("time") or "")
    return {"offices": offices, "types": types, "dates": dates, "items": cols, "removed": rem}


def load_previous(path):
    """讀前一份資料（資料夾或單一檔案）；格式不對就當作沒有，回傳 None。"""
    if not path or not os.path.exists(path):
        return None
    try:
        files = ([os.path.join(path, f"{g}.json") for g in GROUPS] if os.path.isdir(path) else [path])
        docs = []
        for f in files:
            if os.path.exists(f):
                with open(f, encoding="utf-8") as fh:
                    docs.append(json.load(fh))
        if not docs:
            return None
        head = docs[0]
        items, removed = [], []
        for d in docs:
            its, rem = expand(d)
            items += its
            removed += rem
        prev_date = head.get("sourceDate") or taipei_date(head["sourceUpdatedAt"])
        return {
            "date": prev_date,
            "previousSourceDate": head.get("previousSourceDate"),
            "first": {str(i["plate"]): (i.get("firstSeen") or prev_date) for i in items},
            "times": {str(i["plate"]): i["firstSeenTime"] for i in items if i.get("firstSeenTime")},
            "types": {i.get("type") for i in items},
            "byPlate": {str(i["plate"]): (i["type"], i["office"]) for i in items},
            "items": items,
            "removed": removed,
            "auctions": [a for d in docs for a in (d.get("auctions") or [])],
            "signature": head.get("sourceSignature"),
            "complete": all(d.get("sourceSignature") == head.get("sourceSignature") for d in docs) and len(docs) == len(GROUPS),
        }
    except Exception as e:
        print(f"前一份資料無法使用，這次不標示新增：{e}", file=sys.stderr)
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
            # 前一份資料還沒有這個車種（剛加進來）：當作基準，不標成新增
            i["firstSeen"] = previous or source_date
            continue
        i["firstSeen"] = prev["first"].get(i["plate"], source_date)
        if previous and i["firstSeen"] == source_date:
            # 當天新增：沿用先前記下的時間；這次才出現的記成這次更新的時間
            t = prev["times"].get(i["plate"]) or source_time
            if t:
                i["firstSeenTime"] = t
    return previous


def compute_removed(items, prev, source_date, source_time, warnings):
    """當天被選走：前一份有、這一份沒有的號碼（同一天多次更新會累計）。"""
    if prev is None:
        return []
    current = {i["plate"] for i in items}
    kept = [r for r in (prev["removed"] if prev["date"] == source_date else []) if r["plate"] not in current]
    already = {r["plate"] for r in kept}
    newly = [{"plate": p, "type": t, "office": o, "time": source_time or ""}
             for p, (t, o) in prev["byPlate"].items()
             if p not in current and p not in already and t in GROUP_OF]
    # 某車種一次少掉超過一半，比較像來源異常，不當成被選走
    prev_counts, gone_counts = {}, {}
    for t, _ in prev["byPlate"].values():
        prev_counts[t] = prev_counts.get(t, 0) + 1
    for r in newly:
        gone_counts[r["type"]] = gone_counts.get(r["type"], 0) + 1
    odd = {t for t, n in gone_counts.items() if prev_counts.get(t, 0) > 20 and n > prev_counts[t] / 2}
    for t in odd:
        warnings.append(f"{t} 一次少了 {gone_counts[t]} 面（前一份 {prev_counts[t]} 面），可能是來源異常，這次不列入被選走")
    return kept + [r for r in newly if r["type"] not in odd]


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit("用法：python3 update_plates.py 輸出資料夾 [前一份資料]")
    out_dir = sys.argv[1]
    prev = load_previous(sys.argv[2] if len(sys.argv) == 3 else None)
    signature = source_signature()
    if prev and prev["complete"] and set(TYPES) <= prev["types"] and prev["signature"] == signature:
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

    warnings = []
    errors = validate(items, totals)
    if not errors:
        items, errors = clean_duplicates(items, prev, warnings)
    if errors:
        print("資料檢查失敗，不更新檔案：", file=sys.stderr)
        for e in errors[:30]:
            print(" -", e, file=sys.stderr)
        sys.exit(1)
    totals = {t: sum(1 for i in items if i["type"] == t) for t in TYPES}

    now = datetime.now(timezone.utc)
    try:
        auctions = fetch_auctions(now)
    except Exception as e:
        kept = [a for a in (prev["auctions"] if prev else []) if parse_time(a["end"]) > now]
        warnings.append(f"標牌資料抓取失敗，沿用前一份還沒結束的 {len(kept)} 筆公告：{e}")
        auctions = kept

    source_updated = max(src_times) if src_times else None
    source_date = taipei_date(source_updated) if source_updated else now.astimezone(TPE).date().isoformat()
    source_time = parse_time(source_updated).astimezone(TPE).strftime("%H:%M") if source_updated else None
    previous_date = mark_first_seen(items, source_date, prev, source_time)
    removed = compute_removed(items, prev, source_date, source_time, warnings)

    os.makedirs(out_dir, exist_ok=True)
    summary = {}
    for g, types in GROUPS.items():
        g_items = [i for i in items if GROUP_OF[i["type"]] == g]
        g_removed = [r for r in removed if GROUP_OF[r["type"]] == g]
        new_counts = {t: (sum(1 for i in g_items if i["type"] == t and i["firstSeen"] == source_date) if previous_date else 0)
                      for t in types}
        removed_counts = {t: sum(1 for r in g_removed if r["type"] == t) for t in types}
        write_json(os.path.join(out_dir, f"{g}.json"), {
            "v": 2,
            "updatedAt": now.isoformat(timespec="seconds"),
            "sourceUpdatedAt": source_updated,
            "sourceDate": source_date,
            "previousSourceDate": previous_date,
            "sourceSignature": signature,
            "source": "car16.com",
            "group": g,
            "totals": {t: totals[t] for t in types},
            "newCounts": new_counts,
            "removedCounts": removed_counts,
            "warnings": warnings,
            "auctions": [a for a in auctions if GROUP_OF[a["type"]] == g],
            **compact(g_items, g_removed, types),
        })
        summary[g] = {"totals": {t: totals[t] for t in types}, "newCounts": new_counts, "removedCounts": removed_counts}
    print(json.dumps({"sourceDate": source_date, "previousSourceDate": previous_date, "auctions": len(auctions),
                      "warnings": warnings, **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
