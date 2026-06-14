"""13F 보유 종목 검색 및 기관별 포트폴리오 비교 분석 — v2 리디자인 버전 (포트 5001)."""
import json
import os
import sys

# 상위 디렉토리에서 utils.py 임포트
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from flask import Flask, request, jsonify, render_template
from utils import (DB_CONFIG, prev_quarter_date, canonical_cusip, CUSIP_ALIAS,
                   is_cusip_event, cusip_event_label)

# institution_names_kr.json 은 프로젝트 루트에 있음
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KR_JSON_PATH = os.path.join(_ROOT, "institution_names_kr.json")

# 디자인 토큰 색상 (한국 주식 컨벤션: 상승=빨강, 하락=파랑)
_COLOR_UP   = "#d23f3f"
_COLOR_DOWN = "#1e58c8"


def _load_kr_names() -> dict:
    if not os.path.exists(_KR_JSON_PATH):
        return {}
    with open(_KR_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {cik: v["name_kr"] for cik, v in data.get("institutions", {}).items()}


app = Flask(__name__)


def fetch_institution_meta(cik: str):
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rank, institution, curr_amount_usd, prev_amount_usd FROM top100_institutions WHERE cik = %s",
                (cik,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def fetch_top100():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rank, cik, institution, curr_amount_usd, prev_amount_usd FROM top100_institutions ORDER BY rank ASC"
            )
            return cur.fetchall()
    finally:
        conn.close()


def fetch_search_results(q: str) -> list:
    conn = psycopg2.connect(**DB_CONFIG)
    # CIK는 leading zero를 무시한 유효숫자 접두(prefix) 매칭 — "914208"·"0000914208" 모두
    # 0000914208에 매칭. 과거 substring 매칭은 "00020"→"20"이 CIK 어디든 포함되면 잡혀
    # 무관한 기관이 운용금액순으로 다수 노출되는 문제가 있었음.
    q_stripped = q.lstrip("0") or "0"
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cik, institution
                FROM top100_institutions
                WHERE institution ILIKE %s OR ltrim(cik, '0') LIKE %s
                ORDER BY curr_amount_usd DESC
                LIMIT 10
                """,
                (f"%{q}%", f"{q_stripped}%"),
            )
            return cur.fetchall()
    finally:
        conn.close()


def fetch_report_dates(cik: str) -> list:
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
    q = (d.month - 1) // 3 + 1
    return f"{d.year} Q{q}"


def fetch_holdings(cik: str, report_date=None) -> list:
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
            # 구 CUSIP을 정본으로 정규화해 분기 비교가 동일 종목으로 인식되게 한다.
            return [
                (cn, canonical_cusip(cusip), tk, val, sh, rd)
                for cn, cusip, tk, val, sh, rd in cur.fetchall()
            ]
    finally:
        conn.close()


def fetch_consensus(tab: str) -> list:
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
                       composite_score, institution_count, total_buy_amount, total_weight_change,
                       inst_score, buy_score, weight_score
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
        else:
            result.append({"name": institution, "cik": cik, "display": None, "raw": curr_aum or 0})
    result.sort(key=lambda x: -x["raw"])
    return result


def fetch_stock(cusip: str):
    """종목(cusip)의 현재·직전 분기 보유 기관 목록과 통합분석 점수를 반환한다."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(report_date) FROM top100_institutions")
            curr_date = cur.fetchone()[0]
            if not curr_date:
                return None
            prev_date = prev_quarter_date(curr_date)
            # 직전 분기는 구 CUSIP(별칭)으로 신고됐을 수 있어 정본+구 CUSIP을 모두 매칭한다.
            prev_cusips = [cusip] + [old for old, new in CUSIP_ALIAS.items() if new == cusip]
            cur.execute(
                """
                SELECT t.institution, t.cik,
                       hc.value, hc.shares,
                       hp.value, hp.shares,
                       COALESCE(hc.company_name, hp.company_name),
                       COALESCE(hc.ticker, hp.ticker),
                       t.prev_amount_usd
                FROM top100_institutions t
                LEFT JOIN holdings hc ON hc.cik = t.cik AND hc.cusip = %s
                                      AND hc.report_date = %s AND hc.value > 0
                LEFT JOIN holdings hp ON hp.cik = t.cik AND hp.cusip = ANY(%s)
                                      AND hp.report_date = %s AND hp.value > 0
                WHERE hc.cik IS NOT NULL OR hp.cik IS NOT NULL
                ORDER BY hc.value DESC NULLS LAST
                """,
                (cusip, curr_date, prev_cusips, prev_date),
            )
            rows = cur.fetchall()
            cur.execute(
                """
                SELECT composite_rank, composite_score, inst_rank, institution_count,
                       buy_rank, total_buy_amount, weight_rank, total_weight_change
                FROM consensus WHERE cusip = %s
                """,
                (cusip,),
            )
            cons = cur.fetchone()
    finally:
        conn.close()
    return curr_date, prev_date, rows, cons


def fetch_stock_search(q: str) -> list:
    """최근 분기 holdings에서 ticker·종목명으로 종목을 검색해 (cusip, ticker, company_name)을 반환한다."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cusip, MAX(ticker), MAX(company_name)
                FROM holdings
                WHERE report_date = (SELECT MAX(report_date) FROM holdings)
                  AND value > 0 AND cusip != '000000000'
                  AND (ticker ILIKE %s OR company_name ILIKE %s)
                GROUP BY cusip
                -- 90/95 오신고 변종(전부 ticker 없음·실재하지 않는 번호)은 검색에서 숨긴다.
                -- 일반형식 무티커 종목(CSG 등 실재 종목)은 그대로 유지된다.
                HAVING NOT (MAX(ticker) IS NULL AND substring(cusip FROM 7 FOR 2) IN ('90', '95'))
                ORDER BY MAX(value) DESC
                LIMIT 6
                """,
                (f"%{q}%", f"%{q}%"),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ── Formatting helpers ────────────────────────────────────────────────────────

def _radar_geom(s_inst, s_buy, s_weight) -> dict:
    """종합 점수 3지표를 삼각형 레이더 좌표로 변환 (viewBox 200x165, 중심 100,100, R=80)."""
    fi, fb, fw = s_inst / 100, s_buy / 100, s_weight / 100
    ax, ay = 100, 100 - fi * 80
    bx, by = 100 + fb * 69.3, 100 + fb * 40
    cx, cy = 100 - fw * 69.3, 100 + fw * 40
    return {
        "poly": f"{ax:.1f},{ay:.1f} {bx:.1f},{by:.1f} {cx:.1f},{cy:.1f}",
        "dots": [(round(ax, 1), round(ay, 1)),
                 (round(bx, 1), round(by, 1)),
                 (round(cx, 1), round(cy, 1))],
    }


def _fmt_value(v) -> str:
    if v is None:
        return "-"
    return f"${abs(v):,.0f}"


def _fmt_shares(v) -> str:
    if v is None:
        return "-"
    return f"{abs(v):,}"


def _fmt_aum(v) -> str:
    if v is None:
        return "-"
    if v >= 1_000_000_000_000:
        return f"${v / 1_000_000_000_000:.1f}조"
    if v >= 100_000_000:
        return f"${round(v / 100_000_000):,}억"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    return f"${v:,.0f}"


def _aum_delta_html(curr, prev) -> str:
    if curr is None or prev is None or prev == 0:
        return ""
    diff = curr - prev
    if diff == 0:
        return ""
    sign  = "▲ +" if diff > 0 else "▼ -"
    color = _COLOR_UP if diff > 0 else _COLOR_DOWN
    pct   = abs(diff) / prev * 100
    return f'<span style="color:{color}">{sign}{_fmt_aum(abs(diff))} ({pct:.1f}%)</span>'


def _index_aum_html(curr, prev) -> str:
    base = _fmt_value(curr)
    if prev is None:
        return f'{base} <span class="no-compare">직전 분기 없음</span>'
    return change_html_value(curr, prev)


def change_html_value(current, previous) -> str:
    base = _fmt_value(current)
    if previous is None:
        diff = current or 0
        if diff > 0:
            return f'{base} <span style="color:{_COLOR_UP}">▲ +{_fmt_value(diff)}</span>'
        return base
    diff = (current or 0) - (previous or 0)
    if diff > 0:
        return f'{base} <span style="color:{_COLOR_UP}">▲ +{_fmt_value(diff)}</span>'
    if diff < 0:
        return f'{base} <span style="color:{_COLOR_DOWN}">▼ -{_fmt_value(diff)}</span>'
    return base


def change_html_shares(current, previous) -> str:
    base = _fmt_shares(current)
    if previous is None:
        diff = current or 0
        if diff > 0:
            return f'{base} <span style="color:{_COLOR_UP}">▲ +{_fmt_shares(diff)}</span>'
        return base
    diff = (current or 0) - (previous or 0)
    if diff > 0:
        return f'{base} <span style="color:{_COLOR_UP}">▲ +{_fmt_shares(diff)}</span>'
    if diff < 0:
        return f'{base} <span style="color:{_COLOR_DOWN}">▼ -{_fmt_shares(diff)}</span>'
    return base


def trade_amount_html(value, shares, prev_shares) -> str:
    """이번 분기 추정 매매금액 = Δ수량 × 기준 분기말 종가(value/shares).
    실제 체결가는 13F에 없어 분기말 종가로 환산한 추정치. 매수=빨강 ▲, 매도=파랑 ▼."""
    if not value or not shares:
        return "—"
    if prev_shares is None:          # 신규 매수: 전량 신규 취득
        amount = value
    else:
        delta = shares - prev_shares
        if delta == 0:
            return "—"
        amount = round(value * delta / shares)
    if amount > 0:
        return f'<span style="color:{_COLOR_UP}">▲ +{_fmt_value(amount)}</span>'
    if amount < 0:
        return f'<span style="color:{_COLOR_DOWN}">▼ -{_fmt_value(amount)}</span>'
    return "—"


# ── Treemap (포트폴리오 구성) ──────────────────────────────────────────────────

# 증감 방향 → 사각형 색 (한국 주식 컨벤션: 신규/매수=빨강, 매도=파랑)
_TM_FILL = {
    "new":  "#b91c1c",  # 신규 (up-strong)
    "up":   _COLOR_UP,  # 비중 확대
    "down": _COLOR_DOWN, # 비중 축소
    "flat": "#888888",  # 변화 없음
    "other": "#c8ccd2", # 기타 (꼬리 종목 묶음)
}


def _tm_layout(sizes, x, y, dx, dy):
    """현재 빈 사각형(dx,dy)에 sizes를 한 줄(행 또는 열)로 배치한 좌표 목록."""
    covered = sum(sizes)
    rects = []
    if dx >= dy:  # 세로로 쌓는 행 (폭 고정)
        width = covered / dy
        for s in sizes:
            rects.append((x, y, width, s / width))
            y += s / width
    else:         # 가로로 쌓는 열 (높이 고정)
        height = covered / dx
        for s in sizes:
            rects.append((x, y, s / height, height))
            x += s / height
    return rects


def _tm_worst(sizes, dx, dy):
    """한 줄 배치 시 가장 나쁜(1에서 먼) 종횡비."""
    return max(max(w / h, h / w) for (_, _, w, h) in _tm_layout(sizes, 0, 0, dx, dy))


def _tm_leftover(sizes, x, y, dx, dy):
    covered = sum(sizes)
    if dx >= dy:
        width = covered / dy
        return x + width, y, dx - width, dy
    height = covered / dx
    return x, y + height, dx, dy - height


def _tm_squarify(sizes, x, y, dx, dy):
    """squarified treemap — sizes(내림차순, 면적)로 좌표 계산."""
    if not sizes or dx <= 0 or dy <= 0:
        return []
    if len(sizes) == 1:
        return _tm_layout(sizes, x, y, dx, dy)
    i = 1
    while i < len(sizes) and _tm_worst(sizes[:i], dx, dy) >= _tm_worst(sizes[: i + 1], dx, dy):
        i += 1
    current, remaining = sizes[:i], sizes[i:]
    lx, ly, ldx, ldy = _tm_leftover(current, x, y, dx, dy)
    return _tm_layout(current, x, y, dx, dy) + _tm_squarify(remaining, lx, ly, ldx, ldy)


def _tm_block(it, x, y, w, h):
    """squarify 좌표 + 색·라벨·툴팁을 합쳐 최종 블록 dict 생성."""
    is_new = it["direction"] == "new"
    if it["direction"] == "other":
        tooltip = f'기타 {it.get("count", 0)}개 종목 · {it["pct"]}%'
    else:
        cn = it.get("company_name") or ""
        tooltip = f'{it["label"]} · {it["pct"]}%' + (f' · {cn}' if cn else "")
    return {
        **it,
        "x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2),
        "fill": _TM_FILL[it["direction"]],
        "text_fill": "#4d4d4d" if it["direction"] == "other" else "#ffffff",
        "show_label": w >= 46 and h >= 28,
        "show_pct": (not is_new) and w >= 52 and h >= 46,
        "show_new": is_new and w >= 46 and h >= 44,  # 신규: 티커 아래 NEW 라벨
        "tooltip": tooltip,
    }


def build_treemap(latest_by_cusip, prev_by_cusip, no_prev, event_cusips=(),
                  width=1000.0, height=380.0, top_n=24):
    """기관 최신 분기 보유 종목 → 비중 트리맵 블록 목록.

    크기 = 평가금액, 색 = 직전 분기 대비 보유수량 증감. 상위 top_n개만 개별 표시하고
    나머지 꼬리 종목은 '기타' 한 블록으로 묶는다.
    """
    rows = sorted(latest_by_cusip.values(), key=lambda r: -(r[3] or 0))
    rows = [r for r in rows if (r[3] or 0) > 0]
    total = sum((r[3] or 0) for r in rows)
    if not rows or total <= 0:
        return []

    items = []
    for company_name, cusip, ticker, value, shares, _ in rows[:top_n]:
        if no_prev or cusip in event_cusips:
            direction = "flat"
        elif cusip not in prev_by_cusip:
            direction = "new"
        else:
            ps = prev_by_cusip[cusip][4] or 0
            cs = shares or 0
            direction = "up" if cs > ps else "down" if cs < ps else "flat"
        items.append({
            "label": ticker or cusip, "cusip": cusip, "value": value or 0,
            "company_name": company_name or "",
            "pct": round((value or 0) / total * 100, 1),
            "direction": direction, "label_class": "tm-mono",
        })

    tail = rows[top_n:]
    other_val = sum((r[3] or 0) for r in tail) if tail else 0

    # 기타는 비중에 비례한 폭의 '오른쪽 전체높이 칼럼'으로 고정 배치.
    # (크기와 무관하게 항상 우측 — squarify 정렬에 휩쓸려 좌측으로 가지 않게)
    scale  = (width * height) / total
    other_w = (other_val / total) * width if other_val > 0 else 0.0
    main_w  = width - other_w

    items.sort(key=lambda it: -it["value"])  # squarify는 내림차순 가정
    rects = _tm_squarify([it["value"] * scale for it in items], 0.0, 0.0, main_w, height)

    blocks = [_tm_block(it, x, y, w, h) for it, (x, y, w, h) in zip(items, rects)]
    if other_val > 0:
        blocks.append(_tm_block({
            "label": "기타", "cusip": "", "value": other_val,
            "pct": round(other_val / total * 100, 1),
            "direction": "other", "label_class": "tm-sans", "count": len(tail),
        }, main_w, 0.0, other_w, height))
    return blocks


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    kr_names = _load_kr_names()
    institutions = [
        {"type": "institution", "cik": cik, "institution": institution,
         "institution_ko": kr_names.get(cik, "")}
        for cik, institution in fetch_search_results(q)
    ]
    stocks = [
        {"type": "stock", "cusip": cusip, "ticker": ticker or "",
         "company_name": company_name or ""}
        for cusip, ticker, company_name in fetch_stock_search(q)
    ]
    return jsonify(institutions + stocks)


@app.route("/api/consensus/detail")
def api_consensus_detail():
    cusip = request.args.get("cusip", "").strip()
    tab   = request.args.get("tab", "inst").strip()
    if not cusip:
        return jsonify({"error": "cusip required"}), 400
    if tab not in ("inst", "buy", "weight"):
        return jsonify({"error": "invalid tab"}), 400
    return jsonify({"institutions": fetch_consensus_detail(cusip, tab)})


def _fetch_latest_quarter() -> str:
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(report_date) FROM top100_institutions")
            d = cur.fetchone()[0]
            return quarter_label(d) if d else '—'
    finally:
        conn.close()


@app.route("/")
def index():
    rows = fetch_top100()
    kr_names = _load_kr_names()
    table_data = [
        {
            'rank':           rank,
            'cik':            cik,
            'institution':    institution,
            'institution_ko': kr_names.get(cik, ''),
            'is_new_report':  prev_amount_usd is None,
            'aum_html':       _index_aum_html(curr_amount_usd, prev_amount_usd),
        }
        for rank, cik, institution, curr_amount_usd, prev_amount_usd in rows
    ]
    return render_template('index.html', rows=table_data, quarter=_fetch_latest_quarter())


@app.route("/institution/<cik>")
def institution_detail(cik):
    cik_display = cik.zfill(10)
    cik_raw     = cik_display

    meta = fetch_institution_meta(cik_raw)
    kr_names = _load_kr_names()
    institution    = meta[1] if meta else ''
    institution_ko = kr_names.get(cik_raw, '')
    rank           = meta[0] if meta else None
    curr_aum       = meta[2] if meta else None
    prev_aum       = meta[3] if meta else None

    if curr_aum and prev_aum:
        hi = max(curr_aum, prev_aum)
        aum_curr_pct = round(curr_aum / hi * 100)
        aum_prev_pct = round(prev_aum / hi * 100)
        aum_direction = 'up' if curr_aum > prev_aum else 'down' if curr_aum < prev_aum else 'flat'
    else:
        aum_curr_pct, aum_prev_pct = 100, 0
        aum_direction = 'flat'

    dates = fetch_report_dates(cik_raw)
    if not dates:
        return render_template('institution.html',
            cik_display=cik_display, has_dates=False,
            institution=institution, institution_ko=institution_ko,
            rank=rank,
            aum_current=_fmt_aum(curr_aum), aum_prev=_fmt_aum(prev_aum),
            aum_delta_html=_aum_delta_html(curr_aum, prev_aum),
            aum_curr_pct=aum_curr_pct, aum_prev_pct=aum_prev_pct,
            aum_direction=aum_direction,
            holdings_count=0,
            tabs=[], display_rows=[], latest_label='', prev_label='')

    latest_date = dates[0]
    prev_date   = dates[1] if len(dates) > 1 else None

    latest_by_cusip = {row[1]: row for row in fetch_holdings(cik_raw, latest_date)}
    prev_by_cusip   = {row[1]: row for row in (fetch_holdings(cik_raw, prev_date) if prev_date else [])}
    no_prev = not prev_by_cusip

    # 이번 비교 구간(prev_date→latest_date)에서 식별자 전환 경계에 걸리는 종목
    event_cusips = {c for c in latest_by_cusip if is_cusip_event(c, prev_date, latest_date)}

    treemap = build_treemap(latest_by_cusip, prev_by_cusip, no_prev, event_cusips)

    tab = request.args.get("tab", "all")
    TAB_DEFS = [
        ("all",       "전체"),
        ("buy",       "매수"),
        ("sell",      "매도"),
        ("new_buy",   "신규 매수"),
        ("full_sell", "전량 매도"),
    ]

    if tab == "all":
        display = [
            (row, None if no_prev else prev_by_cusip.get(row[1]), False)
            for row in sorted(latest_by_cusip.values(), key=lambda r: -(r[3] or 0))
        ]
    elif no_prev:
        display = []
    elif tab == "buy":
        display = sorted(
            [(row, prev_by_cusip[row[1]], False)
             for row in latest_by_cusip.values()
             if row[1] in prev_by_cusip and row[1] not in event_cusips
             and (row[4] or 0) > (prev_by_cusip[row[1]][4] or 0)],
            key=lambda x: -(x[0][3] or 0),
        )
    elif tab == "sell":
        display = sorted(
            [(row, prev_by_cusip[row[1]], False)
             for row in latest_by_cusip.values()
             if row[1] in prev_by_cusip and row[1] not in event_cusips
             and (row[4] or 0) < (prev_by_cusip[row[1]][4] or 0)],
            key=lambda x: -(x[0][3] or 0),
        )
    elif tab == "new_buy":
        display = sorted(
            [(row, None, False) for row in latest_by_cusip.values() if row[1] not in prev_by_cusip],
            key=lambda x: -(x[0][3] or 0),
        )
    elif tab == "full_sell":
        display = sorted(
            [(row, None, True) for row in prev_by_cusip.values() if row[1] not in latest_by_cusip],
            key=lambda x: -(x[0][3] or 0),
        )
    else:
        display = []

    display_rows = []
    for row_rank, (row, prev_row, is_full_sell) in enumerate(display, 1):
        company_name, cusip, ticker, value, shares, _ = row
        if is_full_sell:
            val_html    = f'{_fmt_value(value)} <span style="color:{_COLOR_DOWN}">▼ -{_fmt_value(value)}</span>' if value else "-"
            shares_html = f'{_fmt_shares(shares)} <span style="color:{_COLOR_DOWN}">▼ -{_fmt_shares(shares)}</span>' if shares else "-"
            # 전량 매도: 직전 보유 전량을 직전 분기말 종가로 매도
            trade_html  = f'<span style="color:{_COLOR_DOWN}">▼ -{_fmt_value(value)}</span>' if value else "—"
        elif cusip in event_cusips and prev_row:
            # 식별자 변경(ADR→보통주) 캐리오버: 환산비가 달라 변화 비교 불가.
            label       = f'<span style="color:var(--color-mute)">{cusip_event_label(cusip)}</span>'
            val_html    = f'{_fmt_value(value)} {label}'
            shares_html = _fmt_shares(shares)
            trade_html  = label
        elif no_prev:
            val_html    = _fmt_value(value)
            shares_html = _fmt_shares(shares)
            trade_html  = "—"   # 직전 분기 없음 → 매매 판단 불가
        else:
            prev_value  = prev_row[3] if prev_row else None
            prev_shares = prev_row[4] if prev_row else None
            val_html    = change_html_value(value, prev_value)
            shares_html = change_html_shares(shares, prev_shares)
            trade_html  = trade_amount_html(value, shares, prev_shares)
        display_rows.append({
            'rank':         row_rank,
            'company_name': company_name,
            'cusip':        cusip,
            'ticker':       ticker or '-',
            'val_html':     val_html,
            'trade_html':   trade_html,
            'shares_html':  shares_html,
        })

    tabs = [{'key': k, 'label': l, 'active': k == tab} for k, l in TAB_DEFS]

    return render_template('institution.html',
        cik_display=cik_display,
        has_dates=True,
        institution=institution,
        institution_ko=institution_ko,
        rank=rank,
        aum_current=_fmt_aum(curr_aum),
        aum_prev=_fmt_aum(prev_aum),
        aum_delta_html=_aum_delta_html(curr_aum, prev_aum),
        aum_curr_pct=aum_curr_pct,
        aum_prev_pct=aum_prev_pct,
        aum_direction=aum_direction,
        holdings_count=len(latest_by_cusip),
        treemap=treemap,
        treemap_has_prev=not no_prev,
        tabs=tabs,
        display_rows=display_rows,
        latest_label=quarter_label(latest_date),
        prev_label=quarter_label(prev_date) if prev_date else "없음",
    )


@app.route("/consensus")
def consensus():
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
            i_s, b_s, w_s = r[11], r[12], r[13]
            # 표시 종합점수는 저장된 정수 점수로 재계산해 기여합과 정확히 일치시킴
            # (DB의 composite_score는 반올림 전 실수로 계산돼 0.1 오차가 생김)
            comp = i_s * 0.5 + b_s * 0.3 + w_s * 0.2
            row.update({
                'rank':            r[0],
                'composite_score': f"{comp:.1f}",
                'inst_rank':       r[1] if r[1] else '-',
                'buy_rank':        r[2] if r[2] else '-',
                'weight_rank':     r[3] if r[3] else '-',
                'radar':           _radar_geom(i_s, b_s, w_s),
                'metrics': [
                    {'key': 'inst',   'label': '기관 수',   'score': i_s,
                     'weight': 50, 'contrib': f"{i_s * 0.5:.1f}",
                     'raw': f"{r[8]}개 기관", 'rank': r[1] if r[1] else '-'},
                    {'key': 'buy',    'label': '매수 금액', 'score': b_s,
                     'weight': 30, 'contrib': f"{b_s * 0.3:.1f}",
                     'raw': _fmt_value(r[9]), 'rank': r[2] if r[2] else '-'},
                    {'key': 'weight', 'label': '비중 변화', 'score': w_s,
                     'weight': 20, 'contrib': f"{w_s * 0.2:.1f}",
                     'raw': f"{r[10] * 100:.4f}%", 'rank': r[3] if r[3] else '-'},
                ],
            })
        elif tab == "inst":
            row.update({'rank': r[1], 'value': str(r[8])})
        elif tab == "buy":
            row.update({'rank': r[2], 'value': _fmt_value(r[9])})
        else:
            row.update({'rank': r[3], 'value': f"{r[10]*100:.4f}%"})
        consensus_rows.append(row)

    tabs = [{'key': k, 'label': l, 'active': k == tab} for k, l in TAB_DEFS]
    return render_template('consensus.html', tab=tab, tabs=tabs, rows=consensus_rows)


@app.route("/stock/<cusip>")
def stock_detail(cusip):
    cusip = canonical_cusip(cusip)   # 구 CUSIP 링크는 정본으로 정규화
    data = fetch_stock(cusip)
    if not data:
        return render_template('stock.html', found=False, cusip=cusip)

    curr_date, prev_date, rows, cons = data
    if not rows:
        return render_template('stock.html', found=False, cusip=cusip)

    is_event = is_cusip_event(cusip, prev_date, curr_date)   # 전환 경계 분기에만 True
    kr_names = _load_kr_names()
    company_name = ticker = None
    holders = []
    n_new = n_buy = n_sell = n_hold = n_full_sell = n_new_inst = n_event = 0

    for institution, cik, cval, cshares, pval, pshares, cname, tkr, inst_prev_aum in rows:
        company_name = company_name or cname
        ticker = ticker or tkr
        if not cval:
            # 직전 분기 대비 전량 매도(현재 보유 0). 필터링 대상으로 표에 포함하되
            # 기본 보기(보유 기관)에서는 숨긴다.
            n_full_sell += 1
            holders.append({
                'institution':    institution,
                'institution_ko': kr_names.get(cik, ''),
                'cik':            cik,
                'value':          0,
                'cat':            'full_sell',
                'tag':            'full_sell',
                'val_html':       change_html_value(0, pval),
                'shares_html':    change_html_shares(0, pshares),
                # 전량 매도: 직전 보유 전량을 직전 분기말 종가로 매도
                'trade_html':     f'<span style="color:{_COLOR_DOWN}">▼ -{_fmt_value(pval)}</span>' if pval else "—",
            })
            continue

        # 직전 분기 13F가 없는 신규 보고 기관(prev_amount_usd IS NULL)은
        # 비교 기준이 없어 매수/매도·신규 매수로 판단하지 않는다.
        no_prior = inst_prev_aum is None
        if no_prior:
            n_new_inst += 1
            cat = tag   = 'new_inst'
            na = '<span class="no-compare">직전 분기 없음</span>'
            val_html    = f'{_fmt_value(cval)} {na}'
            shares_html = f'{_fmt_shares(cshares)} {na}'
            trade_html  = "—"   # 직전 분기 없음 → 매매 판단 불가
        elif is_event and pval:
            # 식별자 변경(ADR→보통주) 캐리오버: 주식수 환산비가 달라 변화 비교 불가.
            # 현재 스냅샷만 보이고 분기 델타·거래추정액은 기업 액션 라벨로 대체.
            n_event += 1
            cat = tag = 'event'
            label       = f'<span style="color:var(--color-mute)">{cusip_event_label(cusip)}</span>'
            val_html    = f'{_fmt_value(cval)} {label}'
            shares_html = _fmt_shares(cshares)
            trade_html  = label
        else:
            if not pval:
                n_new += 1
                cat = tag = 'new'
            elif (cshares or 0) > (pshares or 0):
                n_buy += 1
                cat, tag = 'buy', None
            elif (cshares or 0) < (pshares or 0):
                n_sell += 1
                cat, tag = 'sell', None
            else:
                n_hold += 1
                cat, tag = 'hold', None
            val_html    = change_html_value(cval, pval)
            shares_html = change_html_shares(cshares, pshares)
            trade_html  = trade_amount_html(cval, cshares, pshares)

        holders.append({
            'institution':    institution,
            'institution_ko': kr_names.get(cik, ''),
            'cik':            cik,
            'value':          cval or 0,
            'cat':            cat,
            'tag':            tag,
            'val_html':       val_html,
            'trade_html':     trade_html,
            'shares_html':    shares_html,
        })

    # 현재 보유 기관만 평가금액순으로 순위 부여, 전량 매도 기관은 뒤에 배치(순위 없음)
    current  = [h for h in holders if h['cat'] != 'full_sell']
    sold_out = [h for h in holders if h['cat'] == 'full_sell']
    current.sort(key=lambda h: -h['value'])
    for i, h in enumerate(current, 1):
        h['rank'] = i
    for h in sold_out:
        h['rank'] = None
    holders = current + sold_out

    cons_vm = None
    if cons:
        comp_rank, comp_score, inst_rank, inst_cnt, buy_rank, buy_amt, weight_rank, weight_chg = cons
        cons_vm = {
            'composite_rank':  comp_rank,
            'composite_score': f"{comp_score:.1f}",
            'inst_rank':       inst_rank,
            'inst_count':      inst_cnt,
            'buy_rank':        buy_rank,
            'buy_amount':      _fmt_value(buy_amt),
            'weight_rank':     weight_rank,
            'weight_change':   f"{weight_chg*100:.4f}%",
        }

    # 매수·매도 분포 100% 누적 막대 세그먼트. 신규 보고 기관은 직전 분기 비교 기준이
    # 없어 매수·매도 판단 불가 → 그래프에서 제외. 5개 카테고리는 0이어도 항상 범례에 표시.
    # 가장 강한 신호(신규 매수·전량 매도)에 가장 선명한 색을 배치.
    seg_total = n_new + n_buy + n_hold + n_sell + n_full_sell
    seg_defs = [
        ('신규 매수', n_new,       '#d23f3f'),
        ('매수',      n_buy,       '#db5a5a'),
        ('변동 없음', n_hold,      '#cfcfcf'),
        ('매도',      n_sell,      '#4276d4'),
        ('전량 매도', n_full_sell, '#1e58c8'),
    ]
    sentiment = [
        {'label': l, 'count': c, 'color': col, 'pct': round(c / seg_total * 100, 2)}
        for l, c, col in seg_defs
    ] if seg_total else []

    return render_template('stock.html',
        found=True,
        cusip=cusip,
        ticker=ticker,
        company_name=company_name,
        holders_count=len(current),
        n_new=n_new,
        n_buy=n_buy,
        n_sell=n_sell,
        n_hold=n_hold,
        n_full_sell=n_full_sell,
        n_new_inst=n_new_inst,
        n_event=n_event,
        holders=holders,
        consensus=cons_vm,
        sentiment=sentiment,
        latest_label=quarter_label(curr_date),
        prev_label=quarter_label(prev_date),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
