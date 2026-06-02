"""13F 보유 종목 검색 및 기관별 포트폴리오 비교 분석을 제공하는 Flask 웹 서버."""
import psycopg2
from flask import Flask, request, jsonify, render_template
from utils import DB_CONFIG, prev_quarter_date

app = Flask(__name__)


def fetch_top100():
    """top100_institutions 테이블에서 rank 순으로 상위 100개 기관을 조회한다."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rank, cik, institution, curr_amount_usd, prev_amount_usd
                FROM top100_institutions
                ORDER BY rank ASC
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


def fetch_search_results(q: str) -> list:
    """기관명·CIK로 all_institutions 테이블을 검색하여 운용금액 순 최대 10개를 반환한다."""
    conn = psycopg2.connect(**DB_CONFIG)
    q_stripped = q.lstrip("0") or "0"  # zero-padded CIK로 검색할 때도 숫자 부분으로 매칭
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cik, institution
                FROM all_institutions
                WHERE report_date = (SELECT MAX(report_date) FROM all_institutions)
                  AND (institution ILIKE %s OR cik::text LIKE %s OR cik::text LIKE %s)
                ORDER BY amount_usd DESC
                LIMIT 10
                """,
                (f"%{q}%", f"%{q}%", f"%{q_stripped}%"),
            )
            return cur.fetchall()
    finally:
        conn.close()


def fetch_report_dates(cik: str) -> list:
    """특정 CIK의 holdings 보고일자 목록을 최신순으로 반환한다."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT report_date FROM holdings WHERE cik = %s ORDER BY report_date DESC",
                (cik,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def quarter_label(d) -> str:
    """date 객체를 '2024 Q3' 형식 문자열로 변환한다."""
    q = (d.month - 1) // 3 + 1
    return f"{d.year} Q{q}"


def fetch_holdings(cik: str, report_date=None) -> list:
    """특정 CIK·보고일자의 보유 종목 전체를 평가금액 내림차순으로 반환한다."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT company_name, cusip, ticker, value, shares, report_date
                FROM holdings
                WHERE cik = %s AND report_date = %s
                  AND value > 0 AND cusip != '000000000'
                ORDER BY value DESC
                """,
                (cik, report_date),
            )
            return cur.fetchall()
    finally:
        conn.close()


def _fmt_value(v) -> str:
    """금액을 '$1,234,567' 형식으로 포맷한다. None이면 '-'를 반환한다."""
    if v is None:
        return "-"
    return f"${abs(v):,.0f}"


def _fmt_shares(v) -> str:
    """수량을 '1,234,567' 형식으로 포맷한다. None이면 '-'를 반환한다."""
    if v is None:
        return "-"
    return f"{abs(v):,}"


def change_html_value(current, previous) -> str:
    """전분기 대비 평가금액 변동을 ▲빨강/▼파랑 색상 HTML로 반환한다."""
    base = _fmt_value(current)
    if previous is None:
        diff = current or 0
        if diff > 0:
            return f'{base} <span style="color:red">▲ +{_fmt_value(diff)}</span>'
        return base
    diff = (current or 0) - (previous or 0)
    if diff > 0:
        return f'{base} <span style="color:red">▲ +{_fmt_value(diff)}</span>'
    if diff < 0:
        return f'{base} <span style="color:blue">▼ -{_fmt_value(diff)}</span>'
    return base


def change_html_shares(current, previous) -> str:
    """전분기 대비 보유수량 변동을 ▲빨강/▼파랑 색상 HTML로 반환한다."""
    base = _fmt_shares(current)
    if previous is None:
        diff = current or 0
        if diff > 0:
            return f'{base} <span style="color:red">▲ +{_fmt_shares(diff)}</span>'
        return base
    diff = (current or 0) - (previous or 0)
    if diff > 0:
        return f'{base} <span style="color:red">▲ +{_fmt_shares(diff)}</span>'
    if diff < 0:
        return f'{base} <span style="color:blue">▼ -{_fmt_shares(diff)}</span>'
    return base


@app.route("/api/search")
def api_search():
    """GET /api/search?q=… — 자동완성용 기관 검색 결과를 JSON으로 반환한다."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    rows = fetch_search_results(q)
    return jsonify([
        {"cik": cik, "institution": institution}
        for cik, institution in rows
    ])


@app.route("/")
def index():
    """GET / — 검색창과 운용금액 상위 100개 기관 목록을 렌더링한다."""
    rows = fetch_top100()

    table_data = [
        {
            'rank': rank,
            'cik': cik,
            'institution': institution,
            'aum_html': change_html_value(curr_amount_usd, prev_amount_usd),
        }
        for rank, cik, institution, curr_amount_usd, prev_amount_usd in rows
    ]
    return render_template('index.html', rows=table_data)


@app.route("/institution/<cik>")
def institution_detail(cik):
    """GET /institution/<cik> — 기관의 보유 종목을 5개 탭(전체·매수·매도·신규매수·전량매도)으로 렌더링한다."""
    cik_display = cik.zfill(10)
    cik_raw     = cik_display  # DB에 10자리 제로패딩으로 저장됨

    dates = fetch_report_dates(cik_raw)
    if not dates:
        return render_template('institution.html',
            cik_display=cik_display, has_dates=False,
            tabs=[], display_rows=[], latest_label='', prev_label='')

    latest_date = dates[0]
    prev_date   = dates[1] if len(dates) > 1 else None

    # (company_name, cusip, ticker, value, shares, report_date) — index 1 = cusip
    latest_by_cusip = {row[1]: row for row in fetch_holdings(cik_raw, latest_date)}
    prev_by_cusip   = {row[1]: row for row in (fetch_holdings(cik_raw, prev_date) if prev_date else [])}

    # prev 유효 데이터 없음 = CT(비공개처리) 또는 미수집 → 변화 비교 불가
    no_prev = not prev_by_cusip

    tab = request.args.get("tab", "all")
    TAB_DEFS = [
        ("all",       "전체"),
        ("buy",       "매수"),
        ("sell",      "매도"),
        ("new_buy",   "신규 매수"),
        ("full_sell", "전량 매도"),
    ]

    # (row_to_display, prev_row_or_None, is_full_sell)
    if tab == "all":
        display = [
            (row, None if no_prev else prev_by_cusip.get(row[1]), False)
            for row in sorted(latest_by_cusip.values(), key=lambda r: -(r[3] or 0))
        ]
    elif no_prev:
        display = []
    elif tab == "buy":
        display = sorted(
            [
                (row, prev_by_cusip[row[1]], False)
                for row in latest_by_cusip.values()
                if row[1] in prev_by_cusip and (row[4] or 0) > (prev_by_cusip[row[1]][4] or 0)
            ],
            key=lambda x: -(x[0][3] or 0),
        )
    elif tab == "sell":
        display = sorted(
            [
                (row, prev_by_cusip[row[1]], False)
                for row in latest_by_cusip.values()
                if row[1] in prev_by_cusip and (row[4] or 0) < (prev_by_cusip[row[1]][4] or 0)
            ],
            key=lambda x: -(x[0][3] or 0),
        )
    elif tab == "new_buy":
        display = sorted(
            [
                (row, None, False)
                for row in latest_by_cusip.values()
                if row[1] not in prev_by_cusip
            ],
            key=lambda x: -(x[0][3] or 0),
        )
    elif tab == "full_sell":
        # 이전 분기에는 있지만 최신 분기에 없는 종목 = 전량 매도; prev 기준으로 순회
        display = sorted(
            [
                (row, None, True)
                for row in prev_by_cusip.values()
                if row[1] not in latest_by_cusip
            ],
            key=lambda x: -(x[0][3] or 0),
        )
    else:
        display = []

    display_rows = []
    for rank, (row, prev_row, is_full_sell) in enumerate(display, 1):
        company_name, cusip, ticker, value, shares, _ = row
        if is_full_sell:
            val_html    = f'{_fmt_value(value)} <span style="color:blue">▼ -{_fmt_value(value)}</span>' if value else "-"
            shares_html = f'{_fmt_shares(shares)} <span style="color:blue">▼ -{_fmt_shares(shares)}</span>' if shares else "-"
        elif no_prev:
            val_html    = _fmt_value(value)
            shares_html = _fmt_shares(shares)
        else:
            prev_value  = prev_row[3] if prev_row else None
            prev_shares = prev_row[4] if prev_row else None
            val_html    = change_html_value(value, prev_value)
            shares_html = change_html_shares(shares, prev_shares)
        display_rows.append({
            'rank': rank,
            'company_name': company_name,
            'cusip': cusip,
            'ticker': ticker or '-',
            'val_html': val_html,
            'shares_html': shares_html,
        })

    tabs = [{'key': k, 'label': l, 'active': k == tab} for k, l in TAB_DEFS]

    return render_template('institution.html',
        cik_display=cik_display,
        has_dates=True,
        tabs=tabs,
        display_rows=display_rows,
        latest_label=quarter_label(latest_date),
        prev_label=quarter_label(prev_date) if prev_date else "없음",
    )


def fetch_consensus(tab: str) -> list:
    """consensus 테이블에서 탭에 맞는 순위 컬럼 기준으로 상위 500개를 반환한다."""
    rank_col = {
        "composite": "composite_rank",
        "inst":      "inst_rank",
        "buy":       "buy_rank",
        "weight":    "weight_rank",
    }.get(tab, "composite_rank")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT composite_rank, inst_rank, buy_rank, weight_rank,
                       ticker, company_name, cusip,
                       composite_score, institution_count, total_buy_amount, total_weight_change
                FROM consensus
                WHERE {rank_col} IS NOT NULL
                ORDER BY {rank_col} ASC
                LIMIT 500
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


def fetch_consensus_detail(cusip: str, tab: str) -> list:
    """특정 CUSIP에 대해 탭별 매수 기관 상세 정보를 반환한다. build_consensus.py와 동일한 매수 판정 로직 사용."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT report_date FROM consensus WHERE cusip = %s LIMIT 1", (cusip,))
            row = cur.fetchone()
            if not row:
                return []
            curr_date = row[0]
            p_date = prev_quarter_date(curr_date)

            cur.execute(
                """
                SELECT t.institution, t.cik, t.curr_amount_usd,
                       hc.value, hc.shares,
                       COALESCE(hp.shares, 0)
                FROM top100_institutions t
                JOIN holdings hc ON hc.cik = t.cik
                                 AND hc.cusip = %s
                                 AND hc.report_date = %s
                                 AND hc.value > 0
                LEFT JOIN holdings hp ON hp.cik = t.cik
                                      AND hp.cusip = %s
                                      AND hp.report_date = %s
                                      AND hp.value > 0
                WHERE EXISTS (
                    SELECT 1 FROM holdings
                    WHERE cik = t.cik AND report_date = %s
                      AND value > 0 AND cusip != '000000000'
                )
                  AND hc.shares > COALESCE(hp.shares, 0)
                """,
                (cusip, curr_date, cusip, p_date, p_date),
            )
            db_rows = cur.fetchall()
    finally:
        conn.close()

    result = []
    for institution, cik, curr_aum, curr_val, curr_shares, prev_shares in db_rows:
        diff = curr_shares - prev_shares
        if tab == "buy":
            raw = round(curr_val * diff / curr_shares) if curr_shares else 0
            result.append({"name": institution, "cik": cik, "display": _fmt_value(raw), "raw": raw})
        elif tab == "weight":
            raw = (curr_val * diff / curr_shares / curr_aum) if curr_aum and curr_shares else 0.0
            result.append({"name": institution, "cik": cik, "display": f"{raw*100:.4f}%", "raw": raw})
        else:  # inst
            result.append({"name": institution, "cik": cik, "display": None, "raw": curr_aum or 0})

    result.sort(key=lambda x: -x["raw"])

    for item in result:
        del item["raw"]
    return result


@app.route("/api/consensus/detail")
def api_consensus_detail():
    """GET /api/consensus/detail?cusip=…&tab=… — 통합분석 종목의 탭별 기관 상세 정보를 JSON으로 반환한다."""
    cusip = request.args.get("cusip", "").strip()
    tab   = request.args.get("tab", "inst").strip()
    if not cusip:
        return jsonify({"error": "cusip required"}), 400
    if tab not in ("inst", "buy", "weight"):
        return jsonify({"error": "invalid tab"}), 400
    return jsonify({"institutions": fetch_consensus_detail(cusip, tab)})


@app.route("/consensus")
def consensus():
    """GET /consensus — top100 기관 공통 매수 종목 통합분석 페이지."""
    tab = request.args.get("tab", "composite")
    TAB_DEFS = [
        ("composite", "종합 순위"),
        ("inst",      "기관 수"),
        ("buy",       "매수 금액"),
        ("weight",    "포트폴리오 비중 변화"),
    ]

    db_rows = fetch_consensus(tab)

    consensus_rows = []
    for r in db_rows:
        row = {'ticker': r[4] or '-', 'company_name': r[5], 'cusip': r[6]}
        if tab == "composite":
            row.update({
                'rank': r[0],
                'composite_score': f"{r[7]:.1f}",
                'inst_rank':   r[1] if r[1] else '-',
                'buy_rank':    r[2] if r[2] else '-',
                'weight_rank': r[3] if r[3] else '-',
            })
        elif tab == "inst":
            row.update({'rank': r[1], 'value': str(r[8])})
        elif tab == "buy":
            row.update({'rank': r[2], 'value': _fmt_value(r[9])})
        else:
            row.update({'rank': r[3], 'value': f"{r[10]*100:.4f}%"})
        consensus_rows.append(row)

    tabs = [{'key': k, 'label': l, 'active': k == tab} for k, l in TAB_DEFS]

    return render_template('consensus.html',
        tab=tab,
        tabs=tabs,
        rows=consensus_rows,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
