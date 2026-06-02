"""Gemini API를 통해 top100 기관의 영문명을 한국어로 번역하여 institution_names_kr.json을 생성·갱신한다."""
import os
import json
import re
from datetime import datetime
import psycopg2
from google import genai
from utils import DB_CONFIG

JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "institution_names_kr.json")


def _load_existing() -> dict:
    if not os.path.exists(JSON_PATH):
        return {}
    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("institutions", {})


def _fetch_top100() -> list[tuple[str, str]]:
    """DB에서 top100_institutions의 (cik, institution) 목록을 반환한다."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cik, institution FROM top100_institutions ORDER BY rank")
            return cur.fetchall()
    finally:
        conn.close()


def _translate_via_gemini(institutions: dict[str, str]) -> dict[str, str]:
    """{cik: name_en} 딕셔너리를 Gemini API에 전달하고 {cik: name_kr} 딕셔너리를 반환한다."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    client = genai.Client(api_key=api_key)

    items = "\n".join(f'{cik}: "{name}"' for cik, name in institutions.items())
    prompt = f"""다음은 미국 SEC에 13F 보고서를 제출한 주요 기관투자자들의 영문 공식 명칭입니다.
각 기관의 한국어 공식 표기 또는 통용 표기명을 반환해주세요.

규칙:
- 한국에 공식 법인명이 있는 경우 그 명칭 사용 (예: BlackRock → 블랙록)
- 공식 한국어명이 없는 경우 통용 한국어 표기 사용
- 약어·축약형은 풀어서 번역 (예: MGMT → 매니지먼트)
- Inc., LLC, L.P. 등 법인 형태 표시는 생략

입력 형식 — CIK: "영문명":
{items}

응답은 반드시 아래와 같이 순수 JSON 객체만 반환하세요 (마크다운 코드블록 없이):
{{"cik": "한국어명", ...}}"""

    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    raw = response.text.strip()
    # 마크다운 코드블록이 포함된 경우 제거
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw.strip())


def translate_top100() -> None:
    """top100 기관명 번역 JSON을 생성·갱신한다. 기존 번역은 보존된다."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("\n[번역 스킵] .env에 GEMINI_API_KEY가 없습니다. institution_names_kr.json을 갱신하려면 키를 추가하세요.")
        return

    existing = _load_existing()
    top100 = _fetch_top100()

    # 신규 CIK만 번역 대상으로 추출
    new_institutions = {cik: name for cik, name in top100 if cik not in existing}

    if not new_institutions:
        print(f"\n[번역] 신규 번역 0개, 기존 {len(existing)}개 유지 → institution_names_kr.json 변경 없음")
        return

    print(f"\n[번역] 신규 {len(new_institutions)}개 기관명을 Gemini API로 번역 중...")
    kr_map = _translate_via_gemini(new_institutions)

    # 기존 항목 보존 + 신규 항목 추가
    merged = dict(existing)
    added = 0
    for cik, name_en in new_institutions.items():
        name_kr = kr_map.get(cik, "")
        if name_kr:
            merged[cik] = {"name_en": name_en, "name_kr": name_kr}
            added += 1
        else:
            print(f"  [경고] CIK {cik} ({name_en}) 번역 결과 없음")

    output = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "institutions": merged,
    }
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[번역 완료] 신규 {added}개 추가, 기존 {len(existing)}개 유지 → institution_names_kr.json")


if __name__ == "__main__":
    translate_top100()
