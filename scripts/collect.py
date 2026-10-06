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
- 새로 발견된 기사마다 네이버 뉴스 "검색 웹페이지"(search.naver.com, 사람이 브라우저로
  보는 그 검색결과 페이지 그대로)를 가져와서, 거기 뜬 언론사명이 연합뉴스/뉴시스/뉴스1
  중 하나인 결과들만 골라 제목 유사도로 가장 비슷한 기사를 고른다. 우선순위는
  연합뉴스 > 뉴시스 > 뉴스1. 네이버 공식 Open API가 아니라 공개 검색결과 페이지를
  읽어오는 것이라 API 키나 가입, 신용카드 등록이 전혀 필요 없다.
- 매칭 실패 시 media_* 필드는 빈 문자열로 남고, 나머지 데이터는 그대로 저장된다.
- 주의: 이 방식은 네이버가 검색결과 페이지의 HTML 구조를 바꾸거나, GitHub Actions의
  공유 IP를 차단하면 똑같이 막힐 수 있다(DuckDuckGo에서 실제로 겪었던 문제). 실행 로그의
  [검색차단?]/[검색실패] 메시지로 그런 상황인지 확인할 수 있다.

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


def summarize_lines(raw: str, max_lines: int = 2, maybe_truncated: bool = True) -> str:
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
    """새로 발견된 항목마다 상세 페이지의 부제를 읽어 요약(최대 2줄)으로 만든다.
    부제가 2줄 미만이면 목록 미리보기에서 뽑은 줄로 채운다. 실패하면 목록 미리보기 요약을 그대로 둔다."""
    for it in items:
        sub = fetch_detail_subtitle(it["link"])
        if sub:
            lines = summarize_lines(sub, maybe_truncated=False).split("\n")
            for extra in (it.get("summary") or "").split("\n"):
                if len(lines) >= 2:
                    break
                if extra and not any(_norm(extra) in _norm(l) or _norm(l) in _norm(extra) for l in lines):
                    lines.append(extra)
            it["summary"] = "\n".join(l for l in lines if l)[:300]
        time.sleep(REQUEST_DELAY_SEC)
    return items


def today_str():
    return datetime.now(KST).strftime("%Y-%m-%d")


# 확인 대상 매체 우선순위: 연합뉴스 > 뉴시스 > 뉴스1. 네이버 검색결과에 찍히는
# 언론사명 문자열로 판별한다 (네이버는 보통 "연합뉴스", "뉴시스", "뉴스1"로 표기).
MEDIA_PRIORITY = ["연합뉴스", "뉴시스", "뉴스1"]

# 네이버 뉴스 검색 "웹페이지" 요청용 헤더. API가 아니라 사람이 브라우저로 보는
# search.naver.com 검색결과 페이지를 그대로 가져오는 것이라 키/가입이 필요 없다.
_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://search.naver.com/",
}


_MATCH_THRESHOLD = 0.2   # 보도자료 제목의 글자쌍이 검색결과(제목+요약문)에 이 비율 이상 들어있어야 같은 기사로 인정 (너무 높이면 실제 기사도 놓침)


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
    return q[:80]


def _naver_news_search(query: str):
    """네이버 뉴스 검색결과 웹페이지(search.naver.com)를 그대로 가져와
    [(언론사명, 기사제목, 링크, 미리보기문구), ...]로 돌려준다.
    차단/오류면 None을 반환해서 '결과 없음'과 구분한다.

    네이버가 검색결과 디자인을 새 컴포넌트 체계(SDS)로 바꾸면서 class 이름이
    전부 해시값처럼 랜덤화되어(news_tit, news_wrap 같은 예전 class는 더 이상
    존재하지 않음) class 기반 선택자로는 결과를 하나도 못 찾는다. 대신 각
    블록의 역할을 나타내는 data-heatmap-target 속성(".tit"=제목 링크,
    ".prof"=언론사명 링크, ".body"=본문 미리보기 링크)은 의미 기반이라 class보다
    안정적이라 이걸로 찾는다. (실제 네이버 검색결과 페이지 소스를 받아서 구조를
    확인하고 검증한 선택자다.)"""
    try:
        resp = requests.get(
            "https://search.naver.com/search.naver",
            params={"where": "news", "query": query},
            headers=_SEARCH_HEADERS,
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"    [검색오류] 네이버: {e}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"    [검색차단?] 네이버: HTTP {resp.status_code}", file=sys.stderr)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    # "새 창 열림" 같은 접근성 전용 안내문구가 텍스트에 섞여 들어오므로 미리 제거.
    for span in soup.select("span.fender-ui_0cb57fb2"):
        span.decompose()

    title_anchors = soup.select('a[data-heatmap-target=".tit"]')
    if not title_anchors:
        # 선택자가 지금 네이버 페이지 구조와 안 맞거나, 봇 탐지로 다른 페이지를 받은 경우.
        # 실제로 뭘 받았는지 다음 실행 로그에서 바로 보이도록 진단 정보를 남긴다.
        body_snippet = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:200]
        title_tag = soup.title.get_text(strip=True) if soup.title else "(없음)"
        print(
            f"    [검색진단] 네이버: 제목링크 0개 / 최종URL={resp.url} / "
            f"응답길이={len(resp.text)}자 / <title>={title_tag} / 본문일부=\"{body_snippet}\"",
            file=sys.stderr,
        )

    out = []
    for title_a in title_anchors:
        title = title_a.get_text(" ", strip=True)
        link = title_a.get("href", "")
        if not title or not link:
            continue

        # 제목 링크의 조상을 위로 올라가며, 같은 항목에 속한 언론사명(.prof)과
        # 본문 미리보기(.body) 링크를 찾는다. (제목·본문은 형제 관계, 언론사명은
        # 그보다 위쪽 조상 아래에 있는 구조라 몇 단계 넉넉히 올라가 본다.)
        press = ""
        desc = ""
        node = title_a
        for _ in range(8):
            node = node.parent
            if node is None:
                break
            if not press:
                for p in node.select('a[data-heatmap-target=".prof"]'):
                    t = p.get_text(" ", strip=True)
                    if t:
                        press = t
                        break
            if not desc:
                body_a = node.select_one('a[data-heatmap-target=".body"]')
                if body_a:
                    desc = body_a.get_text(" ", strip=True)
            if press and desc:
                break

        out.append((press, title, link, desc))
    return out


def find_media_coverage(title: str):
    """연합뉴스 > 뉴시스 > 뉴스1 순서로 실제 보도 기사를 찾는다.
    네이버 뉴스 검색을 한 번만 호출해서(검색어 1개로 모든 언론사 결과가 같이 나오므로)
    그 안에서 매체명으로 걸러 우선순위대로 확인하고, 제목 유사도가 기준
    (_MATCH_THRESHOLD)에 못 미치면 '이 매체엔 없음'으로 보고 다음 매체로 넘어간다."""
    query = _clean_query(title)
    results = _naver_news_search(query)
    time.sleep(1.0)  # 검색엔진 부담/차단 방지
    if results is None:
        print("    - 네이버 검색 실패(차단/오류)")
        return None
    if not results:
        print("    - 네이버 검색결과 없음")
        return None

    for press_name in MEDIA_PRIORITY:
        candidates = [r for r in results if press_name in r[0]]
        if not candidates:
            print(f"    - {press_name}: 검색결과 없음")
            continue
        best = max(candidates, key=lambda r: _match_score(title, r[1] + " " + r[3]))
        score = _match_score(title, best[1] + " " + best[3])
        print(f"    - {press_name}: 후보 {len(candidates)}건, 최고 유사도 {score:.2f}")
        if score >= _MATCH_THRESHOLD:
            return {"press": press_name, "title": best[1][:200], "link": best[2]}
    return None


def enrich_with_media(items):
    """새로 발견된 항목마다 실제 보도 매체(연합뉴스/뉴시스/뉴스1) 매칭을 시도한다.
    매칭에 실패해도 item 자체는 그대로 유지되고, 매체 관련 필드만 비워진다.
    media_checked_at을 남겨서, 이후 recheck_pending_media()가 너무 자주
    같은 항목을 재검색하지 않도록 한다."""
    now_iso = datetime.now(KST).isoformat()
    for it in items:
        media = find_media_coverage(it["title"])
        it["media_checked_at"] = now_iso
        if media:
            it["media_press"] = media["press"]
            it["media_title"] = media["title"]
            it["media_link"] = media["link"]
        else:
            it["media_press"] = ""
            it["media_title"] = ""
            it["media_link"] = ""
    return items


# 보도자료는 당일 올라와도 언론 보도는 하루이틀 늦게 나오는 경우가 많다.
# 그래서 한 번 매칭에 실패했다고 끝내지 않고, 최근 며칠치를 주기적으로 다시 확인한다.
RECHECK_WINDOW_DAYS = 3        # 오늘 포함, 최근 며칠치까지 재확인 대상으로 볼지
RECHECK_MIN_INTERVAL_HOURS = 3  # 같은 항목을 다시 확인하기까지 최소 대기 시간
RECHECK_BATCH_LIMIT = 8         # 한 번 실행(5분)에서 재확인할 최대 건수 (검색엔진 부하 제한)


def recheck_pending_media(current_date: str) -> bool:
    """최근 RECHECK_WINDOW_DAYS일치 데이터 파일들을 훑어서, 아직 매체 매칭이
    안 됐고 마지막 확인 후 RECHECK_MIN_INTERVAL_HOURS시간이 지난 항목을 최대
    RECHECK_BATCH_LIMIT건까지 다시 검색해본다. 오늘(current_date)자 파일이
    바뀌었으면 True를 반환한다 (호출한 쪽에서 latest.json을 다시 써야 하므로)."""
    now = datetime.now(KST)
    base_date = datetime.strptime(current_date, "%Y-%m-%d")

    day_items = {}    # date_str -> 그 날짜 파일의 items 리스트(그대로 수정해서 재사용)
    candidates = []   # (date_str, idx)

    for delta in range(RECHECK_WINDOW_DAYS):
        d = (base_date - timedelta(days=delta)).strftime("%Y-%m-%d")
        path = DATA_DIR / f"{d}.json"
        items = load_json(path, [])
        if not items:
            continue
        day_items[d] = items
        for idx, it in enumerate(items):
            if it.get("media_press"):
                continue
            checked_at = it.get("media_checked_at")
            if checked_at:
                try:
                    last = datetime.fromisoformat(checked_at)
                    if (now - last).total_seconds() < RECHECK_MIN_INTERVAL_HOURS * 3600:
                        continue
                except ValueError:
                    pass
            candidates.append((d, idx))

    if not candidates:
        return False

    picked = candidates[:RECHECK_BATCH_LIMIT]
    print(f"[매체 재확인] 최근 {RECHECK_WINDOW_DAYS}일치 중 아직 매칭 안 된 {len(candidates)}건 "
          f"중 {len(picked)}건 재검색...")

    touched_dates = set()
    for d, idx in picked:
        it = day_items[d][idx]
        print(f"  - ({d}) {it['title'][:40]}")
        media = find_media_coverage(it["title"])
        it["media_checked_at"] = now.isoformat()
        if media:
            it["media_press"] = media["press"]
            it["media_title"] = media["title"]
            it["media_link"] = media["link"]
            print(f"    -> 매칭됨: {media['press']}")
        touched_dates.add(d)

    for d in touched_dates:
        path = DATA_DIR / f"{d}.json"
        path.write_text(json.dumps(day_items[d], ensure_ascii=False, indent=2), encoding="utf-8")

    return current_date in touched_dates


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

    # 보도자료보다 뉴스가 늦게 나오는 경우를 위해, 최근 며칠치 중 아직 매체
    # 매칭이 안 된 항목을 이 시점에 추가로 재검색한다.
    today_touched = recheck_pending_media(target_date)
    if today_touched:
        refreshed_items = load_json(day_path, items)
        latest_path.write_text(
            json.dumps(
                {
                    "date": target_date,
                    "collected_at": datetime.now(KST).isoformat(),
                    "agencies": AGENCIES,
                    "items": refreshed_items,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[재확인 반영] {day_path}, {latest_path} 갱신 완료")


if __name__ == "__main__":
    main()
