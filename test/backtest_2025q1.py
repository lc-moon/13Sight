"""
25Q1 컨센서스 백테스트: top100 기관의 2025-03-31 vs 2024-12-31 보유 비교
- DB는 읽기 전용 (top100 CIK 목록만 조회)
- 보유 데이터는 SEC에서 직접 수집 → holdings_cache.json 에 캐시
- 결과는 top10_consensus.json 으로 출력

실행 전 필요:
  pip install yfinance
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import math
import time
from datetime import date
from pathlib import Path

import psycopg2
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from xml.etree import ElementTree as ET

from utils import DB_CONFIG, sec_session, canonical_cusip, is_cusip_event

# ── 상수 ────────────────────────────────────────────────────────────
CURR_DATE = date(2025, 3, 31)
PREV_DATE = date(2024, 12, 31)

HERE      = Path(__file__).parent
CACHE_FILE = HERE / "holdings_cache.json"
OUT_FILE   = HERE / "top10_consensus.json"

# ── DB에서 top100 CIK 목록 조회 (읽기 전용) ─────────────────────────
def load_ciks() -> list:
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cik, curr_amount_usd FROM top100_institutions ORDER BY rank ASC")
            return cur.fetchall()
    finally:
        conn.close()


# ── SEC Filing 파싱 (collect_holdings.py 로직 재사용) ────────────────
def get_filing(session, cik, target_dates):
    url = f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json"
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    recent = resp.json()["filings"]["recent"]

    target_strs = {str(d) for d in target_dates}
    found = {}
    for i, form in enumerate(recent["form"]):
        if form == "13F-HR":
            rd = recent["reportDate"][i]
            if rd in target_strs and rd not in found:
                found[rd] = recent["accessionNumber"][i]
                if len(found) == len(target_strs):
                    break

    # 오래된 filing은 historyFiles에도 있을 수 있음
    if len(found) < len(target_strs):
        hist = resp.json()["filings"].get("files", [])
        for f in hist:
            if "submissions" in f.get("name", ""):
                try:
                    hr = session.get(f"https://data.sec.gov/submissions/{f['name']}", timeout=30)
                    hr.raise_for_status()
                    old = hr.json()
                    for i, form in enumerate(old["form"]):
                        if form == "13F-HR":
                            rd = old["reportDate"][i]
                            if rd in target_strs and rd not in found:
                                found[rd] = old["accessionNumber"][i]
                except Exception:
                    pass
                time.sleep(0.1)

    return found  # {report_date_str: accession}


def get_infotable(session, cik, accession):
    acc_nodash = accession.replace("-", "")
    cik_int = int(cik)
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/index.json"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    items = resp.json().get("directory", {}).get("item", [])

    filename = None
    for item in items:
        if item.get("type") == "13F-HR TABLE":
            filename = item["name"]; break
    if not filename:
        for item in items:
            if "infotable" in item["name"].lower():
                filename = item["name"]; break
    if not filename:
        xml_files = [i["name"] for i in items if i["name"].lower().endswith(".xml") and i["name"].lower() != "primary_doc.xml"]
        if len(xml_files) == 1:
            filename = xml_files[0]
    if not filename:
        raise ValueError(f"infotable 없음: CIK {cik}")

    multiplier = 1000
    try:
        pdoc = session.get(f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/primary_doc.xml", timeout=30)
        root = ET.fromstring(pdoc.content)
        for el in root.iter():
            if el.tag.split("}")[-1] == "schemaVersion" and el.text:
                if el.text.strip() == "X0202":
                    multiplier = 1
                break
    except Exception:
        pass

    return filename, multiplier


def parse_infotable(session, cik, accession, filename, multiplier):
    acc_nodash = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{filename}"
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    agg = {}
    for tbl in root.iter():
        if tbl.tag.split("}")[-1] != "infoTable":
            continue
        name = cusip = value_raw = shares = None
        for el in tbl:
            tag = el.tag.split("}")[-1]
            if tag == "nameOfIssuer" and el.text:   name = el.text.strip()
            elif tag == "cusip" and el.text:         cusip = el.text.strip()
            elif tag == "value" and el.text:         value_raw = int(el.text.strip())
            elif tag == "shrsOrPrnAmt":
                for ch in el:
                    if ch.tag.split("}")[-1] == "sshPrnamt" and ch.text:
                        shares = int(ch.text.strip())
        if name and cusip and value_raw is not None:
            c = canonical_cusip(cusip)
            if c not in agg:
                agg[c] = [name, 0, 0]
            agg[c][1] += value_raw * multiplier
            agg[c][2] += shares or 0

    return {cusip: (d[0], d[1], d[2]) for cusip, d in agg.items()}


# ── 보유 데이터 수집 (캐시 우선) ────────────────────────────────────
def collect_holdings(cik_aum_list):
    if CACHE_FILE.exists():
        print(f"캐시 로드: {CACHE_FILE}")
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)

    session = sec_session()
    # {cik: {"curr": {cusip: [name, val, shares]}, "prev": {...}}}
    data = {}

    for i, (cik, aum) in enumerate(cik_aum_list, 1):
        try:
            found = get_filing(session, cik, [CURR_DATE, PREV_DATE])
            time.sleep(0.2)
            entry = {}
            for date_str, accession in found.items():
                try:
                    fname, mult = get_infotable(session, cik, accession)
                    time.sleep(0.2)
                    holdings = parse_infotable(session, cik, accession, fname, mult)
                    time.sleep(0.2)
                    key = "curr" if date_str == str(CURR_DATE) else "prev"
                    # JSON 직렬화 위해 list로
                    entry[key] = {c: list(v) for c, v in holdings.items()}
                except Exception as e:
                    print(f"  [{i}] CIK {cik} ({date_str}) SKIP: {e}")
            if entry:
                data[cik] = entry
            print(f"[{i}/{len(cik_aum_list)}] CIK {cik}: curr={len(entry.get('curr',{}))}, prev={len(entry.get('prev',{}))}")
        except Exception as e:
            print(f"[{i}/{len(cik_aum_list)}] CIK {cik} SKIP: {e}")
        time.sleep(0.2)

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"캐시 저장: {CACHE_FILE}")
    return data


# ── 컨센서스 점수 계산 (build_consensus.py 동일 로직) ────────────────
def rank_desc(items):
    sorted_items = sorted(items, key=lambda x: -x[1])
    ranks = {}
    for i, (k, v) in enumerate(sorted_items):
        if i > 0 and v == sorted_items[i-1][1]:
            ranks[k] = ranks[sorted_items[i-1][0]]
        else:
            ranks[k] = i + 1
    return ranks


def build_consensus(holdings_data, cik_aum_list):
    aum_map = {cik: aum for cik, aum in cik_aum_list}

    cusip_meta = {}
    inst_count = {}
    buy_amount = {}
    weight_chg = {}

    for cik, entry in holdings_data.items():
        curr = entry.get("curr", {})
        prev = entry.get("prev", {})
        if not prev:
            continue

        curr_aum = aum_map.get(cik)

        for cusip, (name, curr_val, curr_shares) in curr.items():
            if is_cusip_event(cusip, PREV_DATE, CURR_DATE) and cusip in prev:
                continue

            prev_entry  = prev.get(cusip)
            prev_shares = prev_entry[2] if prev_entry else 0

            if cusip not in cusip_meta:
                cusip_meta[cusip] = name

            if curr_shares > prev_shares:
                inst_count[cusip] = inst_count.get(cusip, 0) + 1
                buy_amount[cusip] = buy_amount.get(cusip, 0) + round(curr_val * (curr_shares - prev_shares) / curr_shares)
                if curr_aum:
                    weight_chg[cusip] = weight_chg.get(cusip, 0) + curr_val * (curr_shares - prev_shares) / curr_shares / curr_aum

    inst_ranks   = rank_desc(inst_count.items())
    buy_ranks    = rank_desc(buy_amount.items())
    weight_ranks = rank_desc(weight_chg.items())

    n_i, n_b, n_w = len(inst_ranks), len(buy_ranks), len(weight_ranks)
    active = set(inst_count) | set(weight_chg)

    results = []
    for cusip in active:
        ir = inst_ranks.get(cusip)
        br = buy_ranks.get(cusip)
        wr = weight_ranks.get(cusip)
        i_s = 100 * (1 - math.log(ir) / math.log(n_i + 1)) if ir else 0.0
        b_s = 100 * (1 - math.log(br) / math.log(n_b + 1)) if br else 0.0
        w_s = 100 * (1 - math.log(wr) / math.log(n_w + 1)) if wr else 0.0
        comp = i_s * 0.5 + b_s * 0.3 + w_s * 0.2
        results.append({
            "cusip": cusip,
            "company_name": cusip_meta[cusip],
            "institution_count": inst_count.get(cusip, 0),
            "inst_score": round(i_s, 2),
            "buy_amount_usd": buy_amount.get(cusip, 0),
            "buy_score": round(b_s, 2),
            "weight_change": weight_chg.get(cusip, 0.0),
            "weight_score": round(w_s, 2),
            "composite_score": round(comp, 2),
        })

    results.sort(key=lambda x: -x["composite_score"])
    for i, r in enumerate(results, 1):
        r["composite_rank"] = i
    return results


# ── 주가 조회 (yfinance) ─────────────────────────────────────────────
def fetch_prices(tickers_cusips, as_of_date):
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance 없음. pip install yfinance 후 재실행")
        return {}, {}

    hist_prices = {}
    curr_prices = {}

    # as_of_date 다음 거래일 종가 조회 (start=date, end=date+5일)
    from datetime import timedelta
    end = as_of_date + timedelta(days=5)

    for ticker, cusip in tickers_cusips:
        if not ticker:
            continue
        try:
            t = yf.Ticker(ticker)
            hist = t.history(start=str(as_of_date), end=str(end))
            if not hist.empty:
                hist_prices[cusip] = round(float(hist["Close"].iloc[0]), 2)
            info = t.fast_info
            curr_prices[cusip] = round(float(info.last_price), 2) if info.last_price else None
            time.sleep(0.1)
        except Exception as e:
            print(f"  주가 조회 실패 ({ticker}): {e}")

    return hist_prices, curr_prices


# ── ticker 조회 (OpenFIGI, 간단 버전) ───────────────────────────────
def fetch_tickers(cusips):
    """holdings 데이터에 ticker가 없으므로 OpenFIGI로 조회 (무료 제한: 10개/2.5초)"""
    from utils import load_env
    load_env()
    api_key = os.environ.get("OPENFIGI_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    batch_size, sleep_sec = (100, 0.3) if api_key else (10, 2.5)
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key

    result = {}
    cusip_list = list(cusips)
    session = requests.Session()

    for i in range(0, len(cusip_list), batch_size):
        batch = cusip_list[i:i+batch_size]
        payload = [{"idType": "ID_CINS" if c[0].isalpha() else "ID_CUSIP", "idValue": c} for c in batch]
        try:
            resp = session.post("https://api.openfigi.com/v3/mapping", json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            for cusip, item in zip(batch, resp.json()):
                data = item.get("data")
                if data:
                    sec2 = data[0].get("securityType2", "")
                    ticker = data[0].get("ticker") or ""
                    if any(t in sec2 for t in {"Bond","Note","Fixed Income","Government","Municipal"}) or " " in ticker:
                        result[cusip] = None
                    else:
                        result[cusip] = ticker or None
                else:
                    result[cusip] = None
        except Exception as e:
            print(f"OpenFIGI 오류: {e}")
            for c in batch:
                result.setdefault(c, None)
        time.sleep(sleep_sec)

    return result


# ── 메인 ─────────────────────────────────────────────────────────────
def main():
    print("=== 25Q1 컨센서스 백테스트 ===")
    print(f"비교: {CURR_DATE} vs {PREV_DATE}\n")

    # 1. top100 CIK + AUM 로드
    cik_aum = load_ciks()
    print(f"top100 기관 {len(cik_aum)}개 로드")

    # 2. 보유 데이터 수집 (캐시 있으면 스킵)
    holdings = collect_holdings(cik_aum)
    print(f"\n데이터 있는 기관: {len(holdings)}개\n")

    # 3. 컨센서스 계산
    consensus = build_consensus(holdings, cik_aum)
    print(f"전체 활성 종목: {len(consensus)}개")

    top10 = consensus[:10]
    print(f"\n통합분석 1~10위:")
    for r in top10:
        print(f"  {r['composite_rank']:2d}위 {r['company_name'][:40]} (score={r['composite_score']:.1f}, 기관수={r['institution_count']})")

    # 4. ticker 조회
    top_cusips = [r["cusip"] for r in top10]
    print("\nticker 조회 중...")
    ticker_map = fetch_tickers(top_cusips)

    # 5. 주가 조회
    ticker_cusip_pairs = [(ticker_map.get(c), c) for c in top_cusips if ticker_map.get(c)]
    print("주가 조회 중 (as-of 2025-03-31 + 현재)...")
    hist_prices, curr_prices = fetch_prices(ticker_cusip_pairs, CURR_DATE)

    # 6. 결과 JSON 조합
    output = []
    for r in top10:
        cusip = r["cusip"]
        ticker = ticker_map.get(cusip)
        price_2025q1 = hist_prices.get(cusip)
        price_now    = curr_prices.get(cusip)
        ret_pct = None
        if price_2025q1 and price_now:
            ret_pct = round((price_now - price_2025q1) / price_2025q1 * 100, 2)

        output.append({
            "rank":              r["composite_rank"],
            "cusip":             cusip,
            "ticker":            ticker,
            "company_name":      r["company_name"],
            "composite_score":   r["composite_score"],
            "institution_count": r["institution_count"],
            "buy_amount_usd":    r["buy_amount_usd"],
            "price_2025q1_end":  price_2025q1,
            "price_current":     price_now,
            "return_pct":        ret_pct,
        })

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n결과 저장: {OUT_FILE}")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
