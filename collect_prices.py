"""Polygon(Massive) 일봉으로 consensus 종목의 보고 분기 가격대와 현재가를 계산해 price_summary 테이블에 저장한다.

보고 분기(예: Q2 = 4/1~6/30) 중 장중 최저가보다 현재가가 낮으면, 그 분기에 장내에서 매수한
어떤 기관보다도 지금 더 싸게 살 수 있다는 뜻이다. 일봉은 분할 미반영(raw)으로 저장하고
계산 시 분할 이력(stock_splits)으로 현재 기준 가격으로 환산한다.

사용법: python collect_prices.py   (보고 분기 시작일 ~ 오늘 중 미수집 거래일만 받아 증분 갱신)
"""
import os
import time
from datetime import date, datetime, timedelta, timezone
import psycopg2
import requests
from psycopg2.extras import execute_values
from utils import DB_CONFIG, prev_quarter_date

POLYGON_URL = "https://api.polygon.io"
# 무료 요금제 분당 5회. 유료 전환 시 .env의 POLYGON_CALLS_PER_MIN만 올리면 됨.
CALLS_PER_MIN = int(os.environ.get("POLYGON_CALLS_PER_MIN", "5"))
# 보고 분기 시작 후 이 일수 안에 첫 거래가 없으면 '분기 중 신규 상장'으로 본다 (공모가 < 장내 최저가 가능).
IPO_GRACE_DAYS = 5

_last_call = [0.0]


def polygon_get(session: requests.Session, url: str, params: dict = None):
    """레이트 리밋을 지켜 Polygon GET을 호출한다. 429는 잠시 후 재시도, 403(권한 없음)은 None 반환."""
    params = dict(params or {}, apiKey=os.environ.get("POLYGON_API_KEY", ""))
    for _ in range(5):
        wait = 60 / CALLS_PER_MIN - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        resp = session.get(url, params=params, timeout=60)
        if resp.status_code == 429:
            time.sleep(60 / CALLS_PER_MIN)
            continue
        if resp.status_code == 403:
            return None
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Polygon 레이트 리밋 초과 반복: {url}")


def create_tables(conn) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_prices (
                    ticker VARCHAR(20) NOT NULL,
                    date   DATE        NOT NULL,
                    open   DOUBLE PRECISION, high DOUBLE PRECISION,
                    low    DOUBLE PRECISION, close DOUBLE PRECISION,
                    volume DOUBLE PRECISION, vwap DOUBLE PRECISION,
                    PRIMARY KEY (ticker, date)
                )
                """
            )
            # 수집 완료된 거래일 (휴장일은 n_tickers=0으로 기록해 재요청 방지)
            cur.execute(
                "CREATE TABLE IF NOT EXISTS price_days (date DATE PRIMARY KEY, n_tickers INT NOT NULL)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_splits (
                    ticker         VARCHAR(20) NOT NULL,
                    execution_date DATE        NOT NULL,
                    split_from     DOUBLE PRECISION NOT NULL,
                    split_to       DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (ticker, execution_date)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS price_summary (
                    cusip         VARCHAR(20) PRIMARY KEY,
                    ticker        VARCHAR(20) NOT NULL,
                    report_date   DATE NOT NULL,
                    q_low         DOUBLE PRECISION,
                    q_low_date    DATE,
                    q_high        DOUBLE PRECISION,
                    q_vwap        DOUBLE PRECISION,
                    q_close       DOUBLE PRECISION,
                    last_close    DOUBLE PRECISION,
                    last_date     DATE,
                    listed_in_qtr BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at    TIMESTAMP DEFAULT NOW()
                )
                """
            )


def polygon_ticker(ticker):
    """OpenFIGI 표기(BRK/B)를 Polygon 표기(BRK.B)로 변환."""
    return ticker.replace("/", ".") if ticker else None


def quarter_window(report_date: date) -> tuple:
    """보고 분기의 (시작일, 종료일). 예: 2026-06-30 → (2026-04-01, 2026-06-30)."""
    return prev_quarter_date(report_date) + timedelta(days=1), report_date


def fetch_daily(session, conn, start: date, end: date) -> int:
    """start~end 평일 중 아직 수집하지 않은 날의 전 종목 일봉(분할 미반영)을 받아 저장한다."""
    with conn.cursor() as cur:
        cur.execute("SELECT date FROM price_days WHERE date BETWEEN %s AND %s", (start, end))
        done = {r[0] for r in cur.fetchall()}
    today = datetime.now(timezone.utc).date()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    todo = [d for d in days if d.weekday() < 5 and d not in done]
    print(f"일봉 수집 대상 {len(todo)}일 (예상 {len(todo) * 60 / CALLS_PER_MIN / 60:.0f}분)")

    fetched = 0
    for i, d in enumerate(todo, 1):
        data = polygon_get(
            session, f"{POLYGON_URL}/v2/aggs/grouped/locale/us/market/stocks/{d}",
            {"adjusted": "false"},
        )
        results = (data or {}).get("results") or []
        if not results and (data is None or d >= today):
            # 권한 없음(아직 공개 전) 또는 당일 데이터 미확정 → 기록하지 않고 다음 실행에서 재시도
            print(f"  [{i}/{len(todo)}] {d}: 아직 데이터 없음 — 다음 실행에서 재시도")
            continue
        rows = [
            (r["T"], d, r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v"), r.get("vw"))
            for r in results if r.get("T")
        ]
        with conn:
            with conn.cursor() as cur:
                if rows:
                    execute_values(
                        cur,
                        """
                        INSERT INTO daily_prices (ticker, date, open, high, low, close, volume, vwap)
                        VALUES %s
                        ON CONFLICT (ticker, date) DO UPDATE SET
                            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                            close = EXCLUDED.close, volume = EXCLUDED.volume, vwap = EXCLUDED.vwap
                        """,
                        rows,
                    )
                cur.execute(
                    "INSERT INTO price_days (date, n_tickers) VALUES (%s, %s) "
                    "ON CONFLICT (date) DO UPDATE SET n_tickers = EXCLUDED.n_tickers",
                    (d, len(rows)),
                )
        fetched += 1
        print(f"  [{i}/{len(todo)}] {d}: {len(rows)}개 종목" + (" (휴장)" if not rows else ""))
    return fetched


def fetch_splits(session, conn, since: date) -> int:
    """since 이후 실행된 주식 분할 이력을 받아 저장한다 (가격 환산용)."""
    url, params, rows = f"{POLYGON_URL}/v3/reference/splits", {"execution_date.gte": str(since), "limit": 1000}, []
    while url:
        data = polygon_get(session, url, params) or {}
        rows += [
            (s["ticker"], s["execution_date"], s["split_from"], s["split_to"])
            for s in data.get("results", [])
            if s.get("ticker") and s.get("split_from") and s.get("split_to")
        ]
        url, params = data.get("next_url"), None  # next_url에 커서 포함, apiKey는 polygon_get이 붙임
    if rows:
        with conn:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO stock_splits (ticker, execution_date, split_from, split_to) VALUES %s
                    ON CONFLICT (ticker, execution_date) DO UPDATE SET
                        split_from = EXCLUDED.split_from, split_to = EXCLUDED.split_to
                    """,
                    rows,
                )
    return len(rows)


def load_splits(conn, tickers=None) -> dict:
    """{ticker: [(execution_date, 비율=split_to/split_from), ...]}"""
    with conn.cursor() as cur:
        if tickers is None:
            cur.execute("SELECT ticker, execution_date, split_from, split_to FROM stock_splits")
        else:
            cur.execute(
                "SELECT ticker, execution_date, split_from, split_to FROM stock_splits WHERE ticker = ANY(%s)",
                (list(tickers),),
            )
        out: dict = {}
        for tk, d, f, t in cur.fetchall():
            out.setdefault(tk, []).append((d, t / f))
    return out


def split_factor(splits, d: date) -> float:
    """d 시점 가격을 현재 기준으로 환산할 때 나눌 값 (d 이후 실행된 분할 비율의 곱)."""
    k = 1.0
    for exec_date, ratio in splits or ():
        if exec_date > d:
            k *= ratio
    return k


def adjust_bar(splits, d, lo, hi, cl, vw, vol) -> tuple:
    """일봉 하나를 현재 기준 가격으로 환산한 (date, low, high, close, vwap, volume)."""
    k = split_factor(splits, d)
    return (d, *(p / k if p else p for p in (lo, hi, cl, vw)), vol)


def load_adjusted_bars(conn, ticker: str, start: date) -> list:
    """ticker의 start 이후 일봉을 분할 환산해 [(date, low, high, close, vwap, volume), ...]로 반환한다."""
    tk = polygon_ticker(ticker)
    splits = load_splits(conn, [tk]).get(tk)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT date, low, high, close, vwap, volume FROM daily_prices "
            "WHERE ticker = %s AND date >= %s ORDER BY date",
            (tk, start),
        )
        return [adjust_bar(splits, *r) for r in cur.fetchall()]


def summarize(bars: list, q_start: date, q_end: date) -> dict:
    """분할 환산된 일봉 목록으로 보고 분기 가격대·최신 종가를 계산한다. 분기 내 거래가 없으면 None."""
    qbars = [b for b in bars if q_start <= b[0] <= q_end and b[1] and b[2]]
    if not qbars or not bars:
        return None
    low_bar = min(qbars, key=lambda b: b[1])
    vol = sum(b[5] or 0 for b in qbars if b[4])
    last = bars[-1]
    return {
        "q_low": low_bar[1], "q_low_date": low_bar[0],
        "q_high": max(b[2] for b in qbars),
        "q_vwap": sum(b[4] * (b[5] or 0) for b in qbars if b[4]) / vol if vol else None,
        "q_close": qbars[-1][3],
        "last_close": last[3], "last_date": last[0],
        "listed_in_qtr": qbars[0][0] > q_start + timedelta(days=IPO_GRACE_DAYS),
    }


def build_summary(conn) -> int:
    """consensus 전 종목의 price_summary를 다시 계산한다 (TRUNCATE 후 삽입)."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(report_date) FROM consensus")
        report_date = cur.fetchone()[0]
        cur.execute("SELECT cusip, ticker FROM consensus WHERE ticker IS NOT NULL")
        targets = cur.fetchall()
    q_start, q_end = quarter_window(report_date)

    all_splits = load_splits(conn)
    tickers = {polygon_ticker(t) for _, t in targets}
    bars_by: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticker, date, low, high, close, vwap, volume FROM daily_prices "
            "WHERE date >= %s AND ticker = ANY(%s) ORDER BY ticker, date",
            (q_start, list(tickers)),
        )
        for tk, *bar in cur:
            bars_by.setdefault(tk, []).append(adjust_bar(all_splits.get(tk), *bar))

    rows = []
    for cusip, ticker in targets:
        s = summarize(bars_by.get(polygon_ticker(ticker), []), q_start, q_end)
        if s:
            rows.append((cusip, polygon_ticker(ticker), report_date, s["q_low"], s["q_low_date"], s["q_high"],
                         s["q_vwap"], s["q_close"], s["last_close"], s["last_date"], s["listed_in_qtr"]))
    with conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE price_summary")
            if rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO price_summary (cusip, ticker, report_date, q_low, q_low_date, q_high,
                                               q_vwap, q_close, last_close, last_date, listed_in_qtr)
                    VALUES %s
                    """,
                    rows,
                )
    return len(rows)


def main() -> None:
    if not os.environ.get("POLYGON_API_KEY"):
        raise SystemExit(".env에 POLYGON_API_KEY가 없습니다.")
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        create_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(report_date) FROM consensus")
            report_date = cur.fetchone()[0]
        q_start, _ = quarter_window(report_date)
        today = datetime.now(timezone.utc).date()
        print(f"보고 분기: {report_date} (가격 구간 {q_start} ~ {today})")

        session = requests.Session()
        fetch_daily(session, conn, q_start, today)
        n_splits = fetch_splits(session, conn, q_start)
        print(f"분할 이력 {n_splits}건 갱신")
        n = build_summary(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FILTER (WHERE last_close < q_low) FROM price_summary")
            below = cur.fetchone()[0]
        print(f"price_summary 저장 완료: {n}개 종목 (분기 최저가 아래 {below}개)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
