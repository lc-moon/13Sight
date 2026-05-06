"""공통 유틸리티: 환경변수 로드, DB 설정, SEC HTTP 세션."""
import os
from datetime import date
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def load_env() -> None:
    """.env 파일을 읽어 환경변수로 등록한다. 이미 설정된 키는 덮어쓰지 않는다."""
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


load_env()  # 임포트 시 자동 실행

DB_CONFIG = {
    "host":     os.environ.get("DB_HOST",     "localhost"),
    "port":     os.environ.get("DB_PORT",     "5432"),
    "dbname":   os.environ.get("DB_NAME",     "postgres"),
    "user":     os.environ.get("DB_USER",     "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

SEC_HEADERS = {
    "User-Agent": "NPS-13F-Tracker lcmoony1@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}


def prev_quarter_date(d: date) -> date:
    """분기말 날짜를 받아 직전 분기말 날짜를 반환한다."""
    if d.month == 3:  return date(d.year - 1, 12, 31)
    if d.month == 6:  return date(d.year, 3, 31)
    if d.month == 9:  return date(d.year, 6, 30)
    if d.month == 12: return date(d.year, 9, 30)
    raise ValueError(f"분기말 날짜가 아님: {d}")


def sec_session() -> requests.Session:
    """타임아웃·재시도 설정이 적용된 requests Session을 반환한다."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(SEC_HEADERS)
    return session
