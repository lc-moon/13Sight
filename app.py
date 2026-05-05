"""13F 보유 종목 검색 및 기관별 포트폴리오 비교 분석을 제공하는 Flask 웹 서버."""
import psycopg2
from flask import Flask, request, jsonify
from utils import DB_CONFIG

app = Flask(__name__)


def fetch_top100():
    """institution 테이블에서 운용금액 상위 100개 기관을 조회한다."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cik, institution, amount_usd
                FROM institution
                ORDER BY amount_usd DESC
                LIMIT 100
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


def fetch_search_results(q: str) -> list:
    """기관명·CIK로 institution 테이블을 검색하여 운용금액 순 최대 10개를 반환한다."""
    conn = psycopg2.connect(**DB_CONFIG)
    q_stripped = q.lstrip("0") or "0"  # zero-padded CIK로 검색할 때도 숫자 부분으로 매칭
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cik, institution
                FROM institution
                WHERE institution ILIKE %s
                   OR cik::text LIKE %s
                   OR cik::text LIKE %s
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
        {"cik": str(cik).zfill(10), "institution": institution}
        for cik, institution in rows
    ])


@app.route("/")
def index():
    """GET / — 검색창과 운용금액 상위 100개 기관 목록을 렌더링한다."""
    rows = fetch_top100()

    table_rows = ""
    for rank, (cik, institution, amount_usd) in enumerate(rows, 1):
        padded_cik = str(cik).zfill(10)
        table_rows += (
            f"<tr>"
            f"<td>{rank}</td>"
            f"<td>{padded_cik}</td>"
            f'<td><a href="/institution/{padded_cik}">{institution}</a></td>'
            f"<td>${amount_usd:,.0f}</td>"
            f"</tr>\n"
        )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>13Sight</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Arial, sans-serif; background: #fff; color: #202124; }}
    .container {{ max-width: 620px; margin: 0 auto; padding: 60px 20px 60px; }}
    .logo {{ text-align: center; font-size: 52px; font-weight: bold; color: #4285f4;
             letter-spacing: -1px; margin-bottom: 28px; }}
    .search-wrapper {{ position: relative; margin-bottom: 14px; }}
    .search-input {{
      width: 100%; border-radius: 24px; border: 1px solid #dfe1e5;
      padding: 12px 20px; font-size: 16px; outline: none;
    }}
    .search-input:focus {{ border-color: #4285f4; box-shadow: 0 1px 6px rgba(32,33,36,.28); }}
    .dropdown {{
      display: none; position: absolute; top: calc(100% + 4px); left: 0; right: 0;
      background: #fff; border: 1px solid #dfe1e5; border-radius: 8px;
      box-shadow: 0 4px 6px rgba(0,0,0,.1); z-index: 100; overflow: hidden;
    }}
    .dropdown-item {{ padding: 10px 20px; cursor: pointer; font-size: 15px; }}
    .dropdown-item:hover {{ background: #f8f9fa; }}
    .dropdown-no-result {{ padding: 10px 20px; font-size: 15px; color: #d93025; }}
    .search-input.error {{ border-color: #d93025 !important; box-shadow: 0 1px 6px rgba(217,48,37,.28) !important; }}
    .action-btn {{
      display: block; width: 100%; padding: 12px; border-radius: 24px;
      background: #f8f9fa; border: 1px solid #dfe1e5; font-size: 15px;
      cursor: default; text-align: center; margin-bottom: 36px;
    }}
    .list-title {{ font-size: 15px; color: #5f6368; margin-bottom: 8px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ border: 1px solid #dfe1e5; padding: 8px 10px; text-align: left; }}
    th {{ background: #f8f9fa; }}
    tr:hover td {{ background: #f8f9fa; }}
    a {{ color: #1a0dab; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="logo">13Sight</div>

    <div class="search-wrapper">
      <input type="text" class="search-input" id="searchInput"
             placeholder="기관명 또는 CIK 검색" autocomplete="off">
      <div class="dropdown" id="dropdown"></div>
    </div>

    <button class="action-btn">상위 100개 기관 투자자들의 13F 포트폴리오 분석</button>

    <div class="list-title">운용금액(AUM) 상위 100개 기관</div>
    <table>
      <thead>
        <tr><th>순위</th><th>CIK</th><th>기관명</th><th>운용금액 (AUM)</th></tr>
      </thead>
      <tbody>
{table_rows}      </tbody>
    </table>
  </div>

  <script>
    const input    = document.getElementById('searchInput');
    const dropdown = document.getElementById('dropdown');
    let timer;

    function resetError() {{
      input.classList.remove('error');
      dropdown.style.display = 'none';
      dropdown.innerHTML = '';
    }}

    input.addEventListener('input', () => {{
      input.classList.remove('error');
      clearTimeout(timer);
      timer = setTimeout(() => {{
        const q = input.value.trim();
        if (!q) {{ resetError(); return; }}
        fetch('/api/search?q=' + encodeURIComponent(q))
          .then(r => r.json())
          .then(results => {{
            if (!results.length) {{ resetError(); return; }}
            dropdown.innerHTML = results
              .map(r => `<div class="dropdown-item" data-cik="${{r.cik}}">${{r.institution}}</div>`)
              .join('');
            dropdown.style.display = 'block';
          }});
      }}, 300);
    }});

    dropdown.addEventListener('click', e => {{
      const item = e.target.closest('.dropdown-item');
      if (item) location.href = '/institution/' + item.dataset.cik;
    }});

    input.addEventListener('keydown', e => {{
      if (e.key === 'Enter') {{
        const first = dropdown.querySelector('.dropdown-item');
        if (first) {{
          location.href = '/institution/' + first.dataset.cik;
        }} else if (input.value.trim()) {{
          dropdown.innerHTML = '<div class="dropdown-no-result">존재하지 않는 기관 명입니다.</div>';
          dropdown.style.display = 'block';
          input.classList.add('error');
        }}
      }}
    }});

    document.addEventListener('click', e => {{
      if (!e.target.closest('.search-wrapper')) resetError();
    }});
  </script>
</body>
</html>"""


@app.route("/institution/<cik>")
def institution_detail(cik):
    """GET /institution/<cik> — 기관의 보유 종목을 5개 탭(전체·매수·매도·신규매수·전량매도)으로 렌더링한다."""
    cik_display = cik.zfill(10)
    cik_raw     = str(int(cik))  # DB는 leading zero 없이 저장되므로 정수 변환 후 문자열화

    dates = fetch_report_dates(cik_raw)
    if not dates:
        return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"><title>보유 종목 - CIK {cik_display}</title></head>
<body>
  <h2>CIK {cik_display} 보유 종목</h2>
  <p><a href="/">← 목록으로</a></p>
  <p>데이터 없음 — collect_holdings.py를 먼저 실행해 holdings 데이터를 수집하세요.</p>
</body></html>"""

    latest_date = dates[0]
    prev_date   = dates[1] if len(dates) > 1 else None

    # (company_name, cusip, ticker, value, shares, report_date) — index 1 = cusip
    latest_by_cusip = {row[1]: row for row in fetch_holdings(cik_raw, latest_date)}
    prev_by_cusip   = {row[1]: row for row in (fetch_holdings(cik_raw, prev_date) if prev_date else [])}

    tab = request.args.get("tab", "all")
    TAB_DEFS = [
        ("all",       "전체"),
        ("buy",       "매수"),
        ("sell",      "매도"),
        ("new_buy",   "신규 매수"),
        ("full_sell", "전량 매도"),
    ]

    tabs_html = ""
    for key, label in TAB_DEFS:
        if key == tab:
            tabs_html += f'<strong style="margin-right:14px; border-bottom:2px solid #333; padding-bottom:2px">{label}</strong>'
        else:
            tabs_html += f'<a href="/institution/{cik_display}?tab={key}" style="margin-right:14px">{label}</a>'

    # (row_to_display, prev_row_or_None, is_full_sell)
    if tab == "all":
        display = [
            (row, prev_by_cusip.get(row[1]), False)
            for row in sorted(latest_by_cusip.values(), key=lambda r: -(r[3] or 0))
        ]
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

    table_rows = ""
    for rank, (row, prev_row, is_full_sell) in enumerate(display, 1):
        company_name, cusip, ticker, value, shares, _ = row
        if is_full_sell:
            val_html    = f'{_fmt_value(value)} <span style="color:blue">▼ -{_fmt_value(value)}</span>' if value else "-"
            shares_html = f'{_fmt_shares(shares)} <span style="color:blue">▼ -{_fmt_shares(shares)}</span>' if shares else "-"
        else:
            prev_value  = prev_row[3] if prev_row else None
            prev_shares = prev_row[4] if prev_row else None
            val_html    = change_html_value(value, prev_value)
            shares_html = change_html_shares(shares, prev_shares)

        table_rows += (
            f"<tr>"
            f"<td>{rank}</td>"
            f"<td>{company_name}</td>"
            f"<td>{cusip}</td>"
            f"<td>{ticker or '-'}</td>"
            f"<td>{val_html}</td>"
            f"<td>{shares_html}</td>"
            f"</tr>\n"
        )

    latest_label = quarter_label(latest_date)
    prev_label   = quarter_label(prev_date) if prev_date else "없음"

    if display:
        body = f"""
  <p style="color:#666;font-size:0.9em">기준 분기: {latest_label} &nbsp;|&nbsp; 비교 분기: {prev_label}</p>
  <table border="1" cellpadding="4" cellspacing="0">
    <thead>
      <tr>
        <th>순위</th><th>종목명</th><th>CUSIP</th><th>티커</th>
        <th>평가금액</th><th>보유수량</th>
      </tr>
    </thead>
    <tbody>
{table_rows}    </tbody>
  </table>"""
    else:
        body = "<p>해당 조건의 종목이 없습니다.</p>"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>보유 종목 - CIK {cik_display}</title>
</head>
<body>
  <h2>CIK {cik_display} 보유 종목</h2>
  <p><a href="/">← 목록으로</a></p>
  <p>{tabs_html}</p>
{body}
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)
