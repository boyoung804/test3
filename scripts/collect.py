"""
korea.kr(정책브리핑) 보도자료 수집 스크립트 (5분 간격 증분 수집)
--------------------------------------------------
GitHub Actions에서 5분마다 자동 실행됩니다 (.github/workflows/daily-collect.yml 참고).

동작 방식:
1. korea.kr 보도자료 목록 페이지를 여러 쪽 가져온다.
2. 감시 대상 기관(AGENCIES) 목록에 포함된 기관의, 오늘 날짜 보도자료만 골라낸다.
3. 오늘자 기존 저장 파일(data/YYYY-MM-DD.json)을 불러와서, 이미 저장된 링크(newsId)는
   건너뛰고 새로 올라온 기사만 앞쪽에 병합한다. → 5분마다 돌아도 서버에 큰 부담 없이
   새 기사를 놓치지 않고 누적할 수 있다.
4. data/YYYY-MM-DD.json 으로 저장하고, data/latest.json 도 같이 갱신한다.
5. data/dates.json 에 "지금까지 수집된 날짜 목록"을 갱신한다.

5분 간격 관련 주의:
- 매 실행마다 여전히 최대 MAX_PAGES 페이지까지 훑지만, 이미 저장된 기사를 만나는
  페이지에서 조기 종료하므로(EARLY_STOP_ON_SEEN) 실제 요청 수는 대부분 1~2페이지로 끝난다.
- 자정 근처(날짜가 바뀌는 시점)에는 전날 항목이 여전히 목록 앞쪽에 섞여 나올 수 있어
  약간의 페이지를 더 훑을 수 있다.

주의:
- 이 스크립트는 korea.kr의 현재 HTML 구조를 기준으로 작성되었습니다.
  사이트 구조가 바뀌면 파싱이 깨질 수 있으니, 정기적으로 결과를 확인하세요.
- 목록 페이지의 페이지네이션은 브라우저에서는 자바스크립트로 동작하지만,
  대부분의 전자정부 게시판(eGovFrame) 계열은 서버 GET 파라미터로도
  페이지 이동이 가능한 경우가 많아 pageIndex 파라미터로 시도합니다.
  만약 이 방식이 막혀 있다면 PAGINATION 관련 함수만 사이트 구조에 맞게
  교체하면 됩니다.
"""

import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.korea.kr/briefing/pressReleaseList.do"
DETAIL_URL = "https://www.korea.kr/briefing/pressReleaseView.do"
LIST_FALLBACK = BASE_URL

# 감시 대상 기관 (대통령실 + 19개 부·처). 여기만 고치면 수집 대상이 바뀝니다.
AGENCIES = [
    "대통령실", "국무조정실",
    "재정경제부", "교육부", "과학기술정보통신부", "외교부", "통일부",
    "법무부", "국방부", "행정안전부", "국가보훈부", "문화체육관광부",
    "농림축산식품부", "산업통상부", "보건복지부", "기후에너지환경부",
    "고용노동부", "성평등가족부", "국토교통부", "해양수산부",
    "중소벤처기업부", "국가데이터처", "인사혁신처", "법제처",
    "식품의약품안전처",
]

KST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PressReleaseTracker/1.0; +https://github.com/)"
}

MAX_PAGES = 15          # 한 번 실행에서 넘겨볼 최대 페이지 수 (과도한 요청 방지, 안전장치)
REQUEST_DELAY_SEC = 0.6  # 요청 간 최소 대기시간 (서버 부담 완화)
EARLY_STOP_ON_SEEN = True  # 이미 저장된 기사를 만나면 그 즉시 스캔 중단 (증분 수집 최적화)

# korea.kr이 본문 요약 없이 접근성용으로 붙이는 상투적 안내문구.
# 목록 링크의 title/aria-label에 "{제목} 관련 보도자료 내용입니다. 자세한 내용은 첨부파일을
# 참고하시기 바랍니다." 형태로 섞여 들어와, 제목 파싱이 깨지는 원인이 된다.
_BOILERPLATE_SUFFIX_RE = re.compile(
    r"\s*관련\s*보도자료(?:\s*내용입니다)?\.?\s*자세한\s*내용은\s*첨부파일을\s*참고하시기\s*바랍니다\.?\s*$"
)


def clean_title(raw: str) -> str:
    """목록에서 뽑은 원시 텍스트에서 상투적 안내문구를 제거하고,
    제목이 그대로 두 번 반복 삽입된 경우(접근성 텍스트 중복) 한 번만 남긴다."""
    core = _BOILERPLATE_SUFFIX_RE.sub("", raw).strip()

    n = len(core)
    if n >= 4:
        # "제목 제목" (공백 하나 사이) 형태의 정확한 중복 탐지
        half = n // 2
        first, second = core[:half].strip(), core[half + (n % 2):].strip()
        if first and first == second:
            core = first
        else:
            # 공백 2칸 이상으로 구분된 반복 패턴도 확인 (기존 방식과의 호환)
            parts = re.split(r"\s{2,}", core)
            if len(parts) >= 2 and parts[0].strip() == parts[1].strip():
                core = parts[0].strip()

    return core.strip()


def today_str():
    return datetime.now(KST).strftime("%Y-%m-%d")


# 상세 페이지의 meta description 끝에 붙는 사이트명 꼬리표
# (예: "... - 정책브리핑 | 브리핑룸 | 대한민국 정책브리핑")
_DETAIL_DESC_SUFFIX_RE = re.compile(r"\s*-\s*정책브리핑\s*\|.*$")


def fetch_detail_meta(link: str):
    """상세 페이지(pressReleaseView.do)를 열어 og:title/og:description에서
    신뢰할 수 있는 제목과 요약을 가져온다. 목록 페이지 텍스트를 정규식으로
    잘라내는 것보다 훨씬 안정적이다 (사이트 쪽 목록 마크업이 바뀌어도 영향 없음).
    실패하면 None을 반환하고, 호출한 쪽에서 목록 기반 제목을 그대로 쓴다."""
    try:
        resp = requests.get(link, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[경고] 상세 페이지 요청 실패 ({link}): {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    title = None
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content", "").strip():
        title = og_title["content"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title = h1.get_text(" ", strip=True)
    if not title:
        return None

    summary = ""
    og_desc = soup.find("meta", attrs={"property": "og:description"})
    if og_desc and og_desc.get("content", "").strip():
        summary = _DETAIL_DESC_SUFFIX_RE.sub("", og_desc["content"].strip()).strip()

    return {"title": title[:200], "summary": summary[:500]}


def enrich_with_detail(items):
    """새로 발견된 항목마다 상세 페이지를 열어 제목/요약을 보강한다.
    이미 알고 있던(known_links) 항목은 여기 들어오지 않으므로, 5분 간격
    실행 기준으로도 보통 몇 건 안 되는 추가 요청만 발생한다."""
    for it in items:
        detail = fetch_detail_meta(it["link"])
        if detail:
            it["title"] = detail["title"]
            if detail["summary"]:
                it["summary"] = detail["summary"]
        else:
            it["unverified"] = True  # 상세 조회 실패: 목록 기반 제목이라는 표시
        time.sleep(REQUEST_DELAY_SEC)
    return items


def fetch_page(page_index: int) -> str:
    """목록 페이지 HTML을 가져온다. pageIndex 파라미터로 페이지 이동을 시도한다."""
    params = {"pageIndex": page_index}
    resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def parse_items(html: str):
    """목록 HTML에서 (제목, 기관, 날짜, 링크) 리스트를 뽑아낸다."""
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 목록의 각 보도자료 링크는 pressReleaseView.do?newsId=... 형태를 가진다.
    for a in soup.select("a[href*='pressReleaseView.do']"):
        href = a.get("href", "")
        m = re.search(r"newsId=(\d+)", href)
        if not m:
            continue
        news_id = m.group(1)
        text = a.get_text(" ", strip=True)

        # 항목 텍스트 끝부분에 보통 "YYYY.MM.DD 기관명" 형태가 붙어 있다.
        date_agency = re.search(r"(\d{4}\.\d{2}\.\d{2})\s+(\S+)\s*$", text)
        if not date_agency:
            continue
        date_raw, agency = date_agency.groups()
        date_iso = date_raw.replace(".", "-")

        # 제목 추출: korea.kr 목록 항목은 보통 <strong>(또는 <b>) 태그로 "진짜 제목"을
        # 감싸고, 그 바로 뒤에 제목이 다시 한 번 반복되며 본문 미리보기나 상투적
        # 안내문구("...관련 보도자료 내용입니다...")가 공백 없이 이어 붙는 구조다.
        # 굵은 글씨 태그의 텍스트가 있으면 그걸 신뢰할 수 있는 제목으로 우선 사용하고,
        # 없는 경우에만 기존 방식(전체 텍스트에서 추출 + 정리)으로 대체한다.
        strong_el = a.find(["strong", "b"])
        if strong_el and strong_el.get_text(strip=True):
            title = strong_el.get_text(" ", strip=True)
        else:
            title = text.split(date_raw)[0].strip()
            title = clean_title(title) if title else text[:200]
        title = title[:200]

        items.append({
            "date": date_iso,
            "agency": agency,
            "title": title or "(제목 확인 필요)",
            "summary": "",  # 상세 페이지를 별도로 열어야 본문 요약이 가능 (부하 고려해 기본은 비움)
            "link": f"{DETAIL_URL}?newsId={news_id}",
            "unverified": False,
        })
    return items


def collect_for_date(target_date: str, known_links=None):
    """target_date 기준 보도자료를 수집한다.

    known_links가 주어지면(직전 실행까지 이미 저장된 링크 집합), 목록을 최신순으로
    훑다가 이미 알고 있는 링크를 만나는 순간 그 뒤는 전부 이전에 수집한 범위이므로
    더 넘길 필요가 없어 그 자리에서 멈춘다. 이 덕분에 5분마다 실행해도 대부분
    1~2페이지만 요청하고 끝난다.
    """
    known_links = known_links or set()
    collected = []
    seen_ids = set()

    for page in range(1, MAX_PAGES + 1):
        try:
            html = fetch_page(page)
        except requests.RequestException as e:
            print(f"[경고] {page}페이지 요청 실패: {e}", file=sys.stderr)
            break

        items = parse_items(html)
        if not items:
            break

        stop = False
        for it in items:
            key = it["link"]
            if key in seen_ids:
                continue
            seen_ids.add(key)

            if key in known_links:
                # 이전 실행에서 이미 저장한 기사에 도달 = 그 이후(더 과거)는 다 아는 내용.
                stop = True
                continue

            if it["date"] < target_date:
                # 최신순 정렬이 유지된다는 전제 하에, 목표 날짜보다 과거 항목이
                # 나오기 시작하면 더 넘길 필요가 없다.
                stop = True
                continue
            if it["date"] != target_date:
                continue
            if it["agency"] not in AGENCIES:
                continue
            collected.append(it)

        if stop:
            break
        time.sleep(REQUEST_DELAY_SEC)

    return collected


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target_date = today_str()

    day_path = DATA_DIR / f"{target_date}.json"
    existing_items = load_json(day_path, [])
    known_links = {it["link"] for it in existing_items}

    print(f"[수집 시작] {target_date} (기존 저장 {len(existing_items)}건, known_links={len(known_links)})")
    new_items = collect_for_date(target_date, known_links=known_links)
    print(f"[신규 발견] {len(new_items)}건 (감시 대상 {len(AGENCIES)}개 기관 기준)")

    if new_items:
        print(f"[상세 페이지 보강] 신규 {len(new_items)}건의 정확한 제목/요약 조회 중...")
        new_items = enrich_with_detail(new_items)

    # 새 기사를 앞쪽(최신순)에 붙이고, 링크 기준으로 중복 제거.
    merged = []
    merged_seen = set()
    for it in new_items + existing_items:
        if it["link"] in merged_seen:
            continue
        merged_seen.add(it["link"])
        merged.append(it)

    items = merged
    if new_items:
        print(f"[병합 완료] 총 {len(items)}건 (신규 {len(new_items)}건 추가)")
    else:
        print(f"[변경 없음] 총 {len(items)}건 (신규 기사 없음)")

    day_path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    latest_path = DATA_DIR / "latest.json"
    latest_path.write_text(
        json.dumps(
            {
                "date": target_date,
                "collected_at": datetime.now(KST).isoformat(),
                "agencies": AGENCIES,
                "items": items,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    dates_path = DATA_DIR / "dates.json"
    dates = load_json(dates_path, [])
    if target_date not in dates:
        dates.append(target_date)
        dates.sort(reverse=True)
    dates_path.write_text(json.dumps(dates, ensure_ascii=False, indent=2), encoding="utf-8")

    print("[저장 완료]", day_path, latest_path, dates_path)


if __name__ == "__main__":
    main()
