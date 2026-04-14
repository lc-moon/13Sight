import os
import psycopg2
from flask import Flask

app = Flask(__name__)


def _load_env_file() -> None:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

DB_CONFIG = {
    "host":     os.environ.get("DB_HOST",     "localhost"),
    "port":     os.environ.get("DB_PORT",     "5432"),
    "dbname":   os.environ.get("DB_NAME",     "postgres"),
    "user":     os.environ.get("DB_USER",     "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
}


def fetch_top100():
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


@app.route("/")
def index():
    rows = fetch_top100()

    table_rows = ""
    for rank, (cik, institution, amount_usd) in enumerate(rows, 1):
        formatted = f"${amount_usd:,.0f}"
        padded_cik = str(cik).zfill(10)
        table_rows += (
            f"<tr>"
            f"<td>{rank}</td>"
            f"<td>{padded_cik}</td>"
            f"<td>{institution}</td>"
            f"<td>{formatted}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>Top 100 기관 AUM</title>
</head>
<body>
  <h2>운용금액(AUM) 상위 100개 기관</h2>
  <table border="1" cellpadding="4" cellspacing="0">
    <thead>
      <tr>
        <th>순위</th>
        <th>CIK</th>
        <th>기관명</th>
        <th>운용금액 (AUM)</th>
      </tr>
    </thead>
    <tbody>
{table_rows}    </tbody>
  </table>
</body>
</html>"""
    return html


if __name__ == "__main__":
    app.run(debug=True, port=5000)
