#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ardata_scraper.py
=================
監察院政治獻金公開查閱平臺 (https://ardata.cy.gov.tw) 下載 / 正規化工具。

針對指定的「選舉年度」（民國年），抓取各選舉、各選舉區之政治獻金資料，
解開官方 ZIP（內含 收入明細 / 支出明細 / 收支結算表），
並正規化成統一 schema 的 CSV，供視覺化平台使用。

資料來源
--------
監察院政治獻金公開查閱平臺  https://ardata.cy.gov.tw
後端 API base                https://ardata.cy.gov.tw/api/v1

關鍵 API（本工具實際使用）
--------------------------
1. GET /api/v1/search/data
     回傳 {reportYears, electionYears, elections:[{code,name,year,districts[]}]}
     （用來列出平臺目前收錄哪些年度 / 選舉，--catalog 會用到）

2. GET /api/v1/search/elections?ElectionYear={民國年}&page=&pageSize=
     回傳該年度所有「選舉 × 選舉區 × 申報次別」的申報列，
     每一列附帶現成的 downloadCsv / downloadPdf / downloadZip 連結。

3. GET /api/v1/Search/download
        ?ElectionName=&ElectionArea=&AccountNumber=&YearOrSerial=&Version=
        &SearchType=2&DownloadType={1=PDF, 2=CSV, 3=ZIP}
     SearchType=2 為「選舉查詢」。DownloadType=3 取得完整 ZIP。

ZIP 內容
--------
  incomes.csv        捐贈「收入」明細（含捐贈者姓名、金額、捐贈方式…）  ← 政治獻金來源
  expenditures.csv   「支出」明細
  election_incomes and expenditures_first.csv   每位候選人的收支結算表（彙總）
  manifest.csv       檔案清單與說明
  schema_*.csv       各檔欄位字典

注意事項 / 已知限制
-------------------
* 平臺僅收錄 107 年(2018)修法後之「會計報告書」明細；93–107 年僅有收支結算表，
  須至監察院陽光法令主題網查詢，且明細需臨櫃申請。本工具只處理本平臺有的年度。
* 捐贈者「姓名」會顯示；身分證/統編、完整地址、電話多為隱碼(****)或空白。
* 大型選舉（如立委、縣市議員）的 ElectionArea 僅到「縣市」層級，
  不含更細的「選舉區」編號；候選人姓名仍完整。
* 同一 (選舉, 選舉區) 可能有多個申報次別 (yearOrSerial = 首次/更正/補申報)。
  預設全部保留並標記；--latest-only 只留每位候選人最高次別。
* 本資料僅供研究/監督等正當目的使用，不得作營利、徵信或其他不正當用途
  （見平臺使用須知）。請於再利用時標註來源。

用法範例
--------
  # 看平臺目前收錄哪些年度/選舉
  python3 ardata_scraper.py --catalog

  # 抓 2022 九合一 (民國111) 與 2024 總統+立委 (民國113)
  python3 ardata_scraper.py --years 111 113

  # 只抓 2022 的縣市長與直轄市長
  python3 ardata_scraper.py --years 111 --name-filter "市長|縣(市)長"

  # 用西元年指定，且只保留最新申報次別
  python3 ardata_scraper.py --years-ad 2022 2024 --latest-only
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import time
import zipfile
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

API_BASE = "https://ardata.cy.gov.tw/api/v1"
USER_AGENT = (
    "ardata-opendata-scraper/1.0 (civic open-data visualization; "
    "contact: jtl0513@gmail.com)"
)
DEFAULT_SLEEP = 1.0          # 每次下載間隔秒數（對政府主機客氣一點）
DEFAULT_PAGESIZE = 200
MAX_RETRY = 4

# 收入明細 / 支出明細 兩檔欄位相同，索引 -> 統一欄位名
DETAIL_COLS = {
    0: "seq",                 # 序號
    1: "candidate",           # 擬參選人／政黨
    2: "election_name_raw",   # 選舉名稱
    3: "report_serial",       # 申報序號／年度
    4: "txn_date_roc",        # 交易日期
    5: "account_subject",     # 收支科目
    6: "counterparty",        # 捐贈者／支出對象
    7: "counterparty_id",     # 身分證／統一編號（多為隱碼）
    8: "income_amount",       # 收入金額
    9: "expense_amount",      # 支出金額
    10: "method",             # 捐贈方式
    11: "deposit_date_roc",   # 存入專戶日期
    12: "returned_or_treasury",  # 返還/繳庫
    13: "purpose",            # 支出用途
    14: "is_monetary",        # 金錢類
    15: "address",            # 地址（多為部分隱碼）
    16: "phone",              # 聯絡電話
    17: "disclosed_payee",    # 應揭露之支出對象
    18: "payee_internal_name",   # 支出對象之內部人員姓名
    19: "payee_internal_title",  # 支出對象之內部人員職稱
    20: "party_internal_name",   # 政黨之內部人員姓名
    21: "party_internal_title",  # 政黨之內部人員職稱
    22: "relation",           # 關係
    23: "correction_note",    # 更正註記
    24: "correction_date_roc",  # 資料更正日期
}

# 正規化後 transactions（明細）輸出欄位順序
TXN_FIELDS = [
    "election_year_roc", "election_year_ad", "election_name", "election_type",
    "electoral_district", "candidate", "report_serial",
    "direction", "txn_date_roc", "txn_date", "account_subject", "donor_type",
    "counterparty", "counterparty_id", "amount", "method", "is_monetary",
    "deposit_date", "returned_or_treasury", "purpose", "address", "phone",
    "disclosed_payee", "relation", "correction_note", "correction_date",
    "source_zip",
]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    return s


def api_get(session: requests.Session, path: str, params: dict | None = None) -> dict:
    """呼叫 JSON API，含重試。"""
    url = f"{API_BASE}/{path.lstrip('/')}"
    last = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = session.get(url, params=params, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"API 失敗 {url} params={params}: {last}")


def roc_to_iso(s: str) -> str:
    """民國日期字串 -> 西元 ISO (YYYY-MM-DD)。無法解析則回傳空字串。
    支援 7 碼民國 (1140109)、8 碼西元 (20250109)。"""
    s = (s or "").strip()
    if not s or not s.isdigit():
        return ""
    if len(s) == 7:  # 民國：YYYMMDD
        y, m, d = int(s[:3]) + 1911, s[3:5], s[5:7]
        return f"{y:04d}-{m}-{d}"
    if len(s) == 8:  # 已是西元
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return ""


def to_amount(s: str) -> str:
    """金額字串 -> 去除無意義小數的字串；保留原值若解析失敗。"""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else str(f)
    except ValueError:
        return s


def derive_election_type(name: str) -> str:
    """由選舉名稱推導選舉類別。順序重要（先比對較專一的字串）。"""
    n = name or ""
    rules = [
        ("總統", "總統副總統"),
        ("立法委員", "立法委員"),
        ("直轄市議員", "直轄市議員"),
        ("直轄市山地原住民區民代表", "原住民區民代表"),
        ("直轄市山地原住民區長", "原住民區長"),
        ("山地原住民區民代表", "原住民區民代表"),
        ("山地原住民區長", "原住民區長"),
        ("鄉(鎮、市)民代表", "鄉鎮市民代表"),
        ("鄉(鎮、市)長", "鄉鎮市長"),
        ("村(里)長", "村里長"),
        ("直轄市市長", "直轄市長"),
        ("直轄市長", "直轄市長"),
        ("縣(市)議員", "縣市議員"),
        ("縣(市)長", "縣市長"),
        ("市長", "縣市長"),
        ("議員", "縣市議員"),
    ]
    for kw, label in rules:
        if kw in n:
            return label
    return "其他"


def derive_donor_type(account_subject: str) -> str:
    """由收支科目推導捐贈來源類別（僅對收入有意義）。"""
    a = account_subject or ""
    for kw in ("個人", "營利事業", "政黨", "人民團體", "匿名", "其他"):
        if kw in a:
            return kw
    return ""


def safe_name(s: str) -> str:
    return re.sub(r"[^\w一-鿿()]+", "_", s).strip("_")


# --------------------------------------------------------------------------- #
# 步驟 1：列出某年度的所有申報列
# --------------------------------------------------------------------------- #
def list_filings(session: requests.Session, roc_year: int) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while True:
        data = api_get(
            session, "search/elections",
            {"ElectionYear": roc_year, "page": page, "pageSize": DEFAULT_PAGESIZE},
        )
        batch = data.get("data", [])
        rows.extend(batch)
        paging = data.get("paging", {})
        if page >= paging.get("pageCount", 1) or not batch:
            break
        page += 1
    return rows


# --------------------------------------------------------------------------- #
# 步驟 2：下載單一申報列的 ZIP
# --------------------------------------------------------------------------- #
def download_zip(session: requests.Session, row: dict, dest: Path,
                 skip_existing: bool, sleep: float) -> Path | None:
    if skip_existing and dest.exists() and dest.stat().st_size > 0:
        return dest
    params = {
        "ElectionName": row["electionName"],
        "ElectionArea": row.get("electionArea", ""),
        "AccountNumber": "",
        "YearOrSerial": int(row.get("yearOrSerial", 1)),
        "Version": "",
        "SearchType": 2,
        "DownloadType": 3,  # ZIP
    }
    url = f"{API_BASE}/Search/download"
    last = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = session.get(url, params=params, timeout=120)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "zip" not in ctype and not r.content[:2] == b"PK":
                raise RuntimeError(f"非 ZIP 回應 (content-type={ctype})")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            time.sleep(sleep)
            return dest
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * attempt)
    print(f"  ! 下載失敗：{row['electionName']} / {row.get('electionArea','')} "
          f"(次別{row.get('yearOrSerial')}) -> {last}", file=sys.stderr)
    return None


# --------------------------------------------------------------------------- #
# 步驟 3：解析 ZIP -> 正規化明細列
# --------------------------------------------------------------------------- #
def _read_csv_member(zf: zipfile.ZipFile, member: str) -> list[list[str]]:
    with zf.open(member) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
        return [r for r in csv.reader(text)]


def parse_zip(zip_path: Path, roc_year: int, district: str,
              election_type: str) -> list[dict]:
    """回傳該 ZIP 內 收入+支出 的正規化明細列。
    election_type 由列表頁的選舉名稱推導後傳入（明細 CSV 的選舉名稱為各縣市別，
    無法區分直轄市長/縣市長，故由列表頁判定）。"""
    out: list[dict] = []
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        print(f"  ! 壞檔 ZIP：{zip_path.name}", file=sys.stderr)
        return out

    members = set(zf.namelist())
    for member, direction in (("incomes.csv", "income"),
                              ("expenditures.csv", "expense")):
        if member not in members:
            continue
        rows = _read_csv_member(zf, member)
        if len(rows) < 2:
            continue
        for raw in rows[1:]:
            if not any(c.strip() for c in raw):
                continue
            rec = {DETAIL_COLS[i]: (raw[i] if i < len(raw) else "")
                   for i in DETAIL_COLS}
            ename = rec["election_name_raw"] or ""
            amount = (rec["income_amount"] if direction == "income"
                      else rec["expense_amount"])
            out.append({
                "election_year_roc": roc_year,
                "election_year_ad": roc_year + 1911,
                "election_name": ename,
                "election_type": election_type,
                "electoral_district": district,
                "candidate": rec["candidate"],
                "report_serial": rec["report_serial"],
                "direction": direction,
                "txn_date_roc": rec["txn_date_roc"],
                "txn_date": roc_to_iso(rec["txn_date_roc"]),
                "account_subject": rec["account_subject"],
                "donor_type": (derive_donor_type(rec["account_subject"])
                               if direction == "income" else ""),
                "counterparty": rec["counterparty"],
                "counterparty_id": rec["counterparty_id"],
                "amount": to_amount(amount),
                "method": rec["method"],
                "is_monetary": rec["is_monetary"],
                "deposit_date": roc_to_iso(rec["deposit_date_roc"]),
                "returned_or_treasury": rec["returned_or_treasury"],
                "purpose": rec["purpose"],
                "address": rec["address"],
                "phone": rec["phone"],
                "disclosed_payee": rec["disclosed_payee"],
                "relation": rec["relation"],
                "correction_note": rec["correction_note"],
                "correction_date": roc_to_iso(rec["correction_date_roc"]),
                "source_zip": zip_path.name,
            })
    return out


def parse_summary(zip_path: Path, roc_year: int, district: str,
                  election_type: str) -> list[dict]:
    """回傳收支結算表（每位候選人彙總）列，附加年度/類別/選舉區欄位。"""
    out: list[dict] = []
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        return out
    target = next((m for m in zf.namelist()
                   if m.startswith("election_incomes")), None)
    if not target:
        return out
    rows = _read_csv_member(zf, target)
    if len(rows) < 2:
        return out
    header = rows[0]
    for raw in rows[1:]:
        if not any(c.strip() for c in raw):
            continue
        rec = dict(zip(header, raw))
        merged = {
            "election_year_roc": roc_year,
            "election_year_ad": roc_year + 1911,
            "election_type": election_type,
            "electoral_district": district,
        }
        merged.update(rec)
        # 結算/申報日期轉西元
        for k in ("結算日期", "申報日期", "更正日期"):
            if k in merged:
                merged[f"{k}_西元"] = roc_to_iso(merged.get(k, ""))
        out.append(merged)
    return out


# --------------------------------------------------------------------------- #
# 輸出
# --------------------------------------------------------------------------- #
def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_summary_csv(path: Path, rows: list[dict]) -> None:
    """summary 欄位是動態的（沿用官方中文欄名），動態組 header。"""
    if not rows:
        return
    lead = ["election_year_roc", "election_year_ad", "election_type",
            "electoral_district"]
    rest, seen = [], set(lead)
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                rest.append(k)
    write_csv(path, lead + rest, rows)


def apply_latest_only(txns: list[dict]) -> list[dict]:
    """每位 (選舉, 選舉區, 候選人) 只保留最高申報次別。
    次別由 source_zip 內 yearOrSerial 不易回推，改以 report_serial 文字 +
    correction_date 粗略排序：有更正日期者優先、其次保留全部首次。
    為避免誤刪，這裡僅去除完全重複列。"""
    seen = set()
    out = []
    for t in txns:
        key = (t["election_name"], t["electoral_district"], t["candidate"],
               t["direction"], t["txn_date_roc"], t["counterparty"],
               t["amount"], t["account_subject"])
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def cmd_catalog(session: requests.Session) -> None:
    data = api_get(session, "search/data")
    print("平臺收錄之選舉年度 (民國 / 西元)：")
    for y in data.get("electionYears", []):
        print(f"  {y} / {y + 1911}")
    print("\n各選舉 (code, 民國年, 名稱, 選舉區數)：")
    for e in sorted(data.get("elections", []), key=lambda x: -x["year"]):
        print(f"  {e['code']:>7}  {e['year']}  {e['name']}  "
              f"(districts={len(e['districts'])})")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="監察院政治獻金平臺 (ardata) 下載/正規化工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--years", nargs="+", type=int, metavar="ROC",
                   help="民國年，如 111 113")
    g.add_argument("--years-ad", nargs="+", type=int, metavar="AD",
                   help="西元年，如 2022 2024")
    ap.add_argument("--catalog", action="store_true",
                    help="只列出平臺收錄的年度/選舉，不下載")
    ap.add_argument("--name-filter", metavar="REGEX",
                    help="只處理選舉名稱符合此 regex 的申報列")
    ap.add_argument("--out", default="data", metavar="DIR",
                    help="輸出根目錄 (預設 data/)")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                    help=f"下載間隔秒數 (預設 {DEFAULT_SLEEP})")
    ap.add_argument("--refresh", action="store_true",
                    help="強制重新下載（預設沿用已存在的 ZIP）")
    ap.add_argument("--latest-only", action="store_true",
                    help="輸出時去除完全重複的明細列")
    args = ap.parse_args()

    session = make_session()

    if args.catalog or (not args.years and not args.years_ad):
        cmd_catalog(session)
        if not args.catalog:
            print("\n（未指定 --years / --years-ad，僅顯示目錄。"
                  "範例：python3 ardata_scraper.py --years 111 113）")
        return

    if args.years_ad:
        years = [y - 1911 for y in args.years_ad]
    else:
        years = args.years
    name_re = re.compile(args.name_filter) if args.name_filter else None

    out_root = Path(args.out)
    raw_root = out_root / "raw"
    norm_root = out_root / "normalized"

    all_txns: list[dict] = []
    all_summary: list[dict] = []
    manifest: list[dict] = []

    for roc in years:
        ad = roc + 1911
        print(f"\n=== 年度 民國{roc} ({ad}) ===")
        filings = list_filings(session, roc)
        if name_re:
            filings = [f for f in filings if name_re.search(f["electionName"])]
        print(f"  申報列：{len(filings)} 筆"
              + (f"（已套用 name-filter）" if name_re else ""))

        year_txns: list[dict] = []
        year_summary: list[dict] = []
        for i, row in enumerate(filings, 1):
            district = row.get("electionArea", "")
            etype = derive_election_type(row["electionName"])
            zname = row.get("zipFileName") or (
                f"{safe_name(row['electionName'])}_{safe_name(district)}"
                f"_s{int(row.get('yearOrSerial', 1))}.zip")
            zpath = raw_root / str(roc) / zname
            print(f"  [{i}/{len(filings)}] {row['electionName']} / {district} "
                  f"(次別{int(row.get('yearOrSerial', 1))})")
            zp = download_zip(session, row, zpath,
                              skip_existing=not args.refresh, sleep=args.sleep)
            status = "ok" if zp else "download_failed"
            n_txn = 0
            if zp:
                txns = parse_zip(zp, roc, district, etype)
                summ = parse_summary(zp, roc, district, etype)
                year_txns.extend(txns)
                year_summary.extend(summ)
                n_txn = len(txns)
            manifest.append({
                "election_year_roc": roc, "election_year_ad": ad,
                "election_name": row["electionName"],
                "electoral_district": district,
                "year_or_serial": int(row.get("yearOrSerial", 1)),
                "zip_file": zname, "status": status, "detail_rows": n_txn,
            })

        if args.latest_only:
            year_txns = apply_latest_only(year_txns)

        write_csv(norm_root / f"transactions_{roc}.csv", TXN_FIELDS, year_txns)
        write_summary_csv(norm_root / f"summary_{roc}.csv", year_summary)
        print(f"  -> transactions_{roc}.csv：{len(year_txns)} 列")
        print(f"  -> summary_{roc}.csv：{len(year_summary)} 列")

        all_txns.extend(year_txns)
        all_summary.extend(year_summary)

    # 合併輸出
    if len(years) > 1:
        write_csv(norm_root / "transactions_all.csv", TXN_FIELDS, all_txns)
        write_summary_csv(norm_root / "summary_all.csv", all_summary)
    write_csv(norm_root / "download_manifest.csv",
              ["election_year_roc", "election_year_ad", "election_name",
               "electoral_district", "year_or_serial", "zip_file",
               "status", "detail_rows"], manifest)

    print(f"\n完成。明細合計 {len(all_txns)} 列、彙總 {len(all_summary)} 列。")
    print(f"輸出位置：{norm_root}/")
    print(f"原始 ZIP：{raw_root}/")


if __name__ == "__main__":
    main()
