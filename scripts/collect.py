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

매체 매칭 (연합뉴스/뉴시스/뉴스1):
- 새로 발견된 기사마다 DuckDuckGo 사이트 한정 검색(site:도메인)으로 제목을 검색해서,
  연합뉴스(yna.co.kr) > 뉴시스(newsis.com) > 뉴스1(news1.kr) 우선순위로 가장 먼저
  매칭되는 실제 보도 기사를 찾는다. API 키나 별도 가입이 필요 없다.
- 매칭 실패 시 media_* 필드는 빈 문자열로 남고, 나머지 데이터는 그대로 저장된다.

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
from urllib.parse import urlparse, parse_qs, unquote

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


def _consume_common_prefix(text: str, prefix: str):
    """공백을 무시하고 text가 prefix로 시작하는 만큼 소비한다.
    (소비한 text 위치, 일치한 글자 수, prefix의 공백 제외 글자 수)를 반환."""
    i = j = matched = 0
    n, m = len(text), len(prefix)
    while i < n and j < m:
        if text[i].isspace():
            i += 1
            continue
        if prefix[j].isspace():
            j += 1
            continue
        if text[i] != prefix[j]:
            break
        i += 1
        j += 1
        matched += 1
    total = sum(1 for c in prefix if not c.isspace())
    return i, matched, total


# 요약 줄 구분: "- " 처럼 하이픈 뒤에 공백이 오는 경우 (K-배터리, 한-아세안 같은 단어 내부 하이픈은 제외)
_BULLET_SPLIT_RE = re.compile(r"\s*[-–—]\s+")
# 문장/구절이 끝난 것으로 볼 수 있는 어미·부호 (미리보기가 중간에 잘렸는지 판단용)
_TERMINAL_RE = re.compile(
    r"(다|함|음|임|됨|등|예정|계획|개최|추진|발표|실시|마련|확대|강화|지원|운영|시행|선정|체결|출범|착수)\.?$"
    r"|[)\]」』”’\"'.!?]$|\d\s*(건|명|개|곳|원|억|조|%|년|월|일)$"
)
_SUBTITLE_CHROME = ("이전다음기사", "정책 NOW", "오늘의 멀티미디어", "정책포커스", "하단 배너",
                    "콘텐츠 영역", "사이트 이동경로", "사실은 이렇습니다", "공지사항", "실시간 인기뉴스")


def summarize_lines(raw: str, max_lines: int = 3, maybe_truncated: bool = True) -> str:
    """부제/미리보기 문장을 '- ' 기준으로 나눠 최대 max_lines줄의 요약(줄바꿈 구분)으로 만든다.
    미리보기가 중간에 잘렸을 가능성이 있으면(maybe_truncated) 마지막의 미완성 줄은 버린다."""
    parts = [p.lstrip("▷□○▲◇※·•ㆍ ").rstrip(" -–—").strip() for p in _BULLET_SPLIT_RE.split(raw.strip())]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if maybe_truncated and len(raw.strip()) >= 90 and not _TERMINAL_RE.search(parts[-1]):
        if len(parts) >= 2:
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1].rstrip(" ,·") + "…"
    return "\n".join(parts[:max_lines])[:300]


def extract_summary(full: str, title: str) -> str:
    """목록 링크 텍스트(제목 + 제목 반복 + 본문 미리보기)에서 본문 미리보기만 뽑아 요약 줄로 만든다.
    안내문구뿐이거나 너무 짧으면 빈 문자열을 반환한다."""
    rest = full.strip()
    for _ in range(2):
        i, matched, total = _consume_common_prefix(rest, title)
        if total and matched >= max(6, int(total * 0.6)):
            rest = rest[i:].strip()
        else:
            break
    rest = _BOILERPLATE_SUFFIX_RE.sub("", rest).strip()
    if re.search(r"관련\s*보도자료\s*내용입니다", rest) or len(rest) < 15:
        return ""
    return summarize_lines(rest, maybe_truncated=True)


def fetch_detail_subtitle(link: str) -> str:
    """상세 페이지에서 제목(h1) 바로 아래 부제(h2)를 가져온다. 보도자료의 부제는
    보통 핵심 내용을 요약한 문장들이라, 잘림 없는 요약으로 쓰기에 가장 좋다.
    실패하거나 부제가 없으면 빈 문자열."""
    try:
        resp = requests.get(link, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[경고] 상세 페이지(부제) 요청 실패: {e}", file=sys.stderr)
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    h1 = soup.find("h1")
    h2 = h1.find_next("h2") if h1 else None
    if not h2:
        return ""
    txt = h2.get_text("\n", strip=True)
    if not txt or any(txt.startswith(c) for c in _SUBTITLE_CHROME):
        return ""
    return txt


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", t)


def enrich_with_summary(items):
    """새로 발견된 항목마다 상세 페이지의 부제를 읽어 요약(최대 3줄)으로 만든다.
    부제가 3줄 미만이면 목록 미리보기에서 뽑은 줄로 채운다. 실패하면 목록 미리보기 요약을 그대로 둔다."""
    for it in items:
        sub = fetch_detail_subtitle(it["link"])
        if sub:
            lines = summarize_lines(sub, maybe_truncated=False).split("\n")
            for extra in (it.get("summary") or "").split("\n"):
                if len(lines) >= 3:
                    break
                if extra and not any(_norm(extra) in _norm(l) or _norm(l) in _norm(extra) for l in lines):
                    lines.append(extra)
            it["summary"] = "\n".join(l for l in lines if l)[:300]
        time.sleep(REQUEST_DELAY_SEC)
    return items


def today_str():
    return datetime.now(KST).strftime("%Y-%m-%d")


# 확인 대상 매체 우선순위: 연합뉴스 > 뉴시스 > 뉴스1. 도메인으로 판별한다.
MEDIA_PRIORITY = [
    ("연합뉴스", "yna.co.kr"),
    ("뉴시스", "newsis.com"),
    ("뉴스1", "news1.kr"),
]

# DuckDuckGo html 검색 전용 헤더. korea.kr용 HEADERS와 분리해 둔다
# (일반 브라우저처럼 보이는 UA를 써야 차단 확률이 낮다).
_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://duckduckgo.com/",
}


def _unwrap_ddg_link(href: str) -> str:
    """DuckDuckGo html 버전은 실제 링크를 //duckduckgo.com/l/?uddg=... 형태의
    리다이렉트 링크로 감싸서 내려주는 경우가 있어, 그 안의 진짜 URL을 꺼낸다."""
    if "duckduckgo.com/l/" in href:
        parsed = urlparse(href if href.startswith("http") else "https:" + href)
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target)
    return href


_MATCH_THRESHOLD = 0.4   # 보도자료 제목의 글자쌍이 검색결과(제목+요약문)에 이 비율 이상 들어있어야 같은 기사로 인정


def _bigrams(text: str):
    t = re.sub(r"[\W_]+", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _match_score(gov_title: str, result_text: str) -> float:
    """보도자료 제목과 검색결과(기사 제목+요약문)가 얼마나 겹치는지 0~1로 계산한다."""
    g = _bigrams(gov_title)
    if not g:
        return 0.0
    return len(g & _bigrams(result_text)) / len(g)


def _clean_query(title: str) -> str:
    q = re.sub(r"[\[\(][^\]\)]{0,15}[\]\)]", " ", title)   # [보도자료], (참고) 같은 꼬리표 제거
    q = re.sub(r"[「」『』“”\"'‘’]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q[:60]


def _search_domain(query: str, domain: str):
    """DuckDuckGo에서 site:도메인 검색 결과를 [(제목, 링크, 요약문), ...]로 돌려준다.
    차단/오류면 (None)을 반환해서 '결과 없음'과 구분한다."""
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"{query} site:{domain}"},
            headers=_SEARCH_HEADERS,
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"    [검색오류] {domain}: {e}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"    [검색차단?] {domain}: HTTP {resp.status_code}", file=sys.stderr)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for res in soup.select(".result"):
        a = res.select_one("a.result__a")
        if not a:
            continue
        link = _unwrap_ddg_link(a.get("href", ""))
        if domain not in link:
            continue
        snip = res.select_one(".result__snippet")
        out.append((a.get_text(" ", strip=True), link, snip.get_text(" ", strip=True) if snip else ""))
    return out


def find_media_coverage(title: str):
    """연합뉴스 > 뉴시스 > 뉴스1 순서로 실제 보도 기사를 찾는다.
    각 매체마다 상위 검색결과 중 보도자료 제목과 가장 비슷한 기사를 고르고,
    유사도가 기준(_MATCH_THRESHOLD)에 못 미치면 '이 매체엔 없음'으로 보고 다음 매체로 넘어간다."""
    query = _clean_query(title)
    for press_name, domain in MEDIA_PRIORITY:
        results = _search_domain(query, domain)
        time.sleep(1.0)  # 검색엔진 부담/차단 방지
        if not results:
            print(f"    - {press_name}: 검색결과 없음")
            continue
        best = max(results, key=lambda r: _match_score(title, r[0] + " " + r[2]))
        score = _match_score(title, best[0] + " " + best[2])
        print(f"    - {press_name}: 후보 {len(results)}건, 최고 유사도 {score:.2f}")
        if score >= _MATCH_THRESHOLD:
            return {"press": press_name, "title": best[0][:200], "link": best[1]}
    return None


def enrich_with_media(items):
    """새로 발견된 항목마다 실제 보도 매체(연합뉴스/뉴시스/뉴스1) 매칭을 시도한다.
    매칭에 실패해도 item 자체는 그대로 유지되고, 매체 관련 필드만 비워진다."""
    for it in items:
        media = find_media_coverage(it["title"])
        if media:
            it["media_press"] = media["press"]
            it["media_title"] = media["title"]
            it["media_link"] = media["link"]
        else:
            it["media_press"] = ""
            it["media_title"] = ""
            it["media_link"] = ""
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
        summary = extract_summary(text.split(date_raw)[0], title)

        items.append({
            "date": date_iso,
            "agency": agency,
            "title": title or "(제목 확인 필요)",
            "summary": summary,  # 목록의 본문 미리보기에서 추출 (없으면 빈 문자열)
            "link": f"{DETAIL_URL}?newsId={news_id}",
            "unverified": False,
            "media_press": "",   # 연합뉴스/뉴시스/뉴스1 중 매칭된 매체명 (없으면 빈 문자열)
            "media_title": "",   # 그 매체의 실제 기사 제목
            "media_link": "",    # 그 매체의 실제 기사 링크
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
        print(f"[요약] 신규 {len(new_items)}건의 부제/미리보기로 요약 생성 중...")
        new_items = enrich_with_summary(new_items)
        print(f"[매체 매칭] 신규 {len(new_items)}건의 연합뉴스/뉴시스/뉴스1 보도 여부 확인 중...")
        new_items = enrich_with_media(new_items)

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
