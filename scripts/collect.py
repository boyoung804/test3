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
import random
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
# 주의: Referer를 "https://search.naver.com/"처럼 자기 자신으로 채워서 보내면
# (실제 브라우저는 검색창에 처음 검색어를 칠 때 이런 self-referer를 보내지 않는다)
# 오히려 조작된 요청이라는 신호로 보여 차단(HTTP 403) 확률을 높일 수 있다.
# 실제로 잘 동작하는 다른 네이버 검색 스크래퍼도 Referer 없이 UA/Accept-Language만
# 보내길래 동일하게 맞췄다.
_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


_MATCH_TITLE_MIN = 0.08  # 제목만 비교했을 때 최소 이 정도는 겹쳐야 검증 단계로 넘어감 (완전 무관한 후보를 1차로 거름)
_MATCH_BODY_MIN = 0.12   # 기사 원문(본문)까지 가져와서 보도자료 제목+요약문과 비교한 유사도 기준 (주 판정 기준)
_MATCH_BODY_MIN_OVERLAP = 6  # 위 비율 기준과 별도로, 실제로 겹친 글자쌍 최소 개수. 제목이 짧으면
                              # 전체 글자쌍 수가 적어 우연히 몇 개만 겹쳐도 비율 기준을 넘기 쉬워서,
                              # "최소 이 정도는 겹쳐야 한다"는 절대 기준도 같이 둔다.
_MATCH_THRESHOLD = 0.18  # 기사 원문을 못 가져왔을 때(차단/오류)만 쓰는 대체 기준 — 제목+미리보기 문구만으로 판단
# (제목만 비교하면 "출범 1년" vs "후보부지 내년 공모"처럼 같은 사안인데 기자가 전혀
# 다른 포인트로 제목을 뽑아 유사도가 9.7%까지 떨어지는 경우를 놓치고, 반대로 네이버
# 미리보기 문구(desc)만 보고 판단하면 날짜·조사 같은 공통 조각만으로 전혀 무관한
# 기사가 잘못 걸리는 경우(주한외교단↔중동뉴스, 근로복지공단↔9/21 기사)가 있었다.
# 그래서 제목만으로 1차로 "아예 무관한 건 아닌지"만 걸러내고(_MATCH_TITLE_MIN),
# 통과한 후보는 실제 기사 본문까지 가져와서(_fetch_article_text) 보도자료 요약문과
# 비교한다 — 같은 사안을 다룬 기사라면 제목 표현이 달라도 본문 내용은 많이
# 겹치고, 전혀 무관한 기사라면 본문까지 가져와도 거의 안 겹치므로 훨씬 믿을 만하다.
# 기사 원문 접속이 막히거나 실패하면 어쩔 수 없이 기존 방식(제목+미리보기)으로
# 대체 판단한다.)


_ARTICLE_DATE_META_ATTRS = [
    {"property": "article:published_time"},
    {"property": "og:article:published_time"},
    {"name": "article:published_time"},
    {"property": "og:regDate"},
    {"name": "regDate"},
    {"name": "PUBLISH_DATETIME"},  # 연합뉴스가 실제로 쓰는 meta 중 하나
]
_ARTICLE_DATE_TEXT_PATTERNS = [
    # "입력 2026.09.13. 15:30", "송고시간 2026-09-13 15:30:00" 등 기사 상단에
    # 흔히 쓰는 표기. 연도-월-일 사이 구분자(.  또는 -)와 끝맺는 마침표 유무가
    # 언론사마다 달라서 폭넓게 잡는다.
    re.compile(r"(?:입력|송고)[^0-9]{0,6}(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})"),
]


def _extract_article_page_date(soup, resp_text: str):
    """기사 원문 페이지 자체에서 실제 게재 날짜를 뽑는다. 네이버 검색결과 페이지의
    "N일 전" 같은 표시(_extract_result_date)보다 훨씬 믿을 만하다 — 검색결과
    스니펫은 선택자가 살짝만 어긋나도 날짜를 통째로 못 읽어오지만, 기사 원문
    페이지에는 거의 항상 meta 태그나 "입력/송고" 날짜 표기가 박혀있기 때문이다.
    1) <meta property="article:published_time"> 등 표준/언론사별 meta 태그
    2) JSON-LD(application/ld+json)의 "datePublished"
    3) 본문 텍스트 속 "입력 YYYY.MM.DD." / "송고 YYYY-MM-DD" 패턴
    순서로 시도하고, 다 실패하면 None을 돌려준다(호출하는 쪽에서 '모른다'로 처리)."""
    for attrs in _ARTICLE_DATE_META_ATTRS:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            content = tag["content"]
            m = re.search(r"(\d{4})[-.]?(\d{2})[-.]?(\d{2})", content)
            if m:
                try:
                    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
                except ValueError:
                    pass

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text() or ""
        m = re.search(r'"datePublished"\s*:\s*"(\d{4})-(\d{2})-(\d{2})', text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            except ValueError:
                pass

    body_text = soup.get_text(" ", strip=True)
    for pat in _ARTICLE_DATE_TEXT_PATTERNS:
        m = pat.search(body_text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            except ValueError:
                continue
    return None


def _fetch_article_text(link: str, timeout: int = 8):
    """기사 원문 페이지를 가져와 (본문 비교용 텍스트, 실제 게재 날짜)를 뽑는다.
    텍스트는 meta description(또는 og:description)과 본문 앞쪽 문단 몇 개를
    합쳐서 쓴다 — 언론사마다 본문 class가 달라서 정교하게 긁기보다, 이 정도면
    보도자료 요약문과 비교하기에 충분하다. 날짜는 _extract_article_page_date 참고.
    접속 실패/차단이면 ("", None)을 돌려주고, 호출하는 쪽에서 기존 방식으로 대체한다."""
    try:
        resp = requests.get(link, headers=_SEARCH_HEADERS, timeout=timeout)
    except requests.RequestException:
        return "", None
    if resp.status_code != 200:
        return "", None
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return "", None
    parts = []
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            parts.append(tag["content"])
    paragraphs = soup.find_all("p")
    parts.append(" ".join(p.get_text(" ", strip=True) for p in paragraphs[:15]))
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()[:1500]
    article_date = _extract_article_page_date(soup, resp.text)
    return text, article_date


def _bigrams(text: str):
    t = re.sub(r"[\W_]+", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _match_score(gov_title: str, result_text: str) -> float:
    """보도자료 제목과 검색결과(기사 제목+요약문)가 얼마나 겹치는지 0~1로 계산한다."""
    g = _bigrams(gov_title)
    if not g:
        return 0.0
    return len(g & _bigrams(result_text)) / len(g)


def _match_score_with_overlap(gov_title: str, result_text: str):
    """_match_score와 같은 비율(ratio)에 더해, 실제로 겹친 글자쌍 개수(overlap)도
    같이 돌려준다. 보도자료 제목이 짧으면(예: 요약문이 비어있고 제목도 짧은 경우)
    전체 글자쌍 수 자체가 적어서, 우연히 2~3개만 겹쳐도 비율이 쉽게 기준을 넘는
    문제가 있다 — 실제 사례: "적극행정 우수공무원 시상"처럼 짧은 제목이, 완전히
    무관한 기사 본문(긴 텍스트라 글자쌍 종류가 많음)과 우연한 글자쌍 몇 개만
    겹쳤는데도 비율 기준(_MATCH_BODY_MIN)을 넘겨버렸다. 비율뿐 아니라 "최소 이만큼은
    겹쳐야 한다"는 절대 개수 기준도 같이 둬서, 짧은 제목에서 비율만으로 통과되는
    걸 막는다."""
    g = _bigrams(gov_title)
    if not g:
        return 0.0, 0
    overlap = len(g & _bigrams(result_text))
    return overlap / len(g), overlap


# 기관명/소속기관명 필터: 정부 보도자료를 다룬 기사라면 보통 그 부처나 소속기관
# 이름이 어딘가에 등장한다. 반대로 기관명이 전혀 안 나오는 기사는 아예 다른 사안을
# 다룬 기사일 가능성이 높다. 완전히 무관한 기사(예: "적극행정 우수공무원 시상"
# 보도자료에 "빅테크 거물 할리우드 악당 캐릭터" 기사가 매칭된 사례)를 걸러내는
# 추가 안전장치로 쓴다.
#
# "agency" 필드(예: 고용노동부)는 보도자료를 낸 상위 부처인데, 실제 보도자료는
# 소속기관(예: 근로복지공단) 이름으로 다뤄지는 경우가 많아서, agency 하나만으로
# 걸러내면 오히려 멀쩡한 매칭까지 막을 수 있다. 그래서 제목 안에서 "~부/~처/~청/
# ~공단/~공사/~진흥원/~연구원/~위원회/~재단/~센터/~협회/~본부" 등으로 끝나는
# 기관명 같은 단어도 같이 뽑아서 후보 집합에 넣는다.
#
# "공무원", "병원", "학원"처럼 기관명이 아닌데 우연히 같은 접미사로 끝나는 흔한
# 단어가 섞이면 필터가 오히려 느슨해지므로(이 단어들은 아무 기사에나 나올 수
# 있어서), 알려진 오탐 단어는 블록리스트로 제외한다.
_ORG_NAME_PATTERN = re.compile(
    r"[가-힣]{2,10}(?:부|처|청|원|공단|공사|진흥원|연구원|위원회|재단|센터|협회|본부|관리단|협의회|조합)"
)
_ORG_NAME_BLOCKLIST = {
    "공무원", "병원", "학원", "내부", "외부", "정원", "지원", "자원", "원인", "원래",
    "경우", "지난해", "올해", "전부", "일부", "전체", "자체", "구체", "대부분",
}

# 기사 제목은 보통 부처 정식명칭을 다 쓰지 않고 언론에서 통용되는 약칭을 쓴다
# (예: "문화체육관광부" 대신 "문체부"). agency 필드는 항상 정식명칭이라, 정식명칭만
# 기준으로 필터링하면 약칭을 쓴 멀쩡한 기사까지 "기관명이 안 보인다"며 걸러질 수
# 있어서, 실제로 언론이 쓰는 약칭을 같이 등록해둔다. 약칭이 따로 없는 부처(예:
# 외교부, 법무부처럼 이미 짧은 경우)는 정식명칭 하나로도 충분해서 목록에 없다.
_AGENCY_ABBREVIATIONS = {
    "국무조정실": {"국조실"},
    "재정경제부": {"재경부"},
    "과학기술정보통신부": {"과기정통부", "과기부"},
    "행정안전부": {"행안부"},
    "국가보훈부": {"보훈부"},
    "문화체육관광부": {"문체부"},
    "농림축산식품부": {"농식품부"},
    "산업통상부": {"산업부", "산통부"},
    "보건복지부": {"복지부"},
    "기후에너지환경부": {"기후부", "환경부"},
    "고용노동부": {"고용부", "노동부"},
    "성평등가족부": {"여가부", "성가부"},
    "국토교통부": {"국토부"},
    "해양수산부": {"해수부"},
    "중소벤처기업부": {"중기부"},
    "인사혁신처": {"인사처"},
    "식품의약품안전처": {"식약처"},
}


def _extract_org_terms(title: str, agency: str, summary: str = ""):
    """보도자료 제목과 기관명에서, 실제 기사에 등장할 법한 '기관명 후보' 문자열
    집합을 뽑는다. 빈 집합이면(뽑을 게 없으면) 호출하는 쪽에서 필터를 건너뛴다.

    제목만 보면 안 되는 이유: 보도자료 제목은 "~한다/~나선다" 같은 홍보성 문구만
    있고, 실제로 그 일을 하는 구체적인 기관명(예: "노사발전재단", "공무원연금공단")은
    summary(본문 요약) 쪽에만 적혀있는 경우가 많다. 실제 뉴스 기사는 보통 그 구체적인
    기관명으로 쓰이지, 상위 부처(agency) 이름만 쓰는 게 아니라서, 제목만 보고 뽑으면
    진짜 멀쩡한 매칭까지 "기관명이 안 보인다"며 걸러버리는 문제가 있었다."""
    terms = set()
    if agency:
        terms.add(agency)
        terms.update(_AGENCY_ABBREVIATIONS.get(agency, set()))
    for text in (title or "", summary or ""):
        for m in _ORG_NAME_PATTERN.finditer(text):
            token = m.group()
            if token in _ORG_NAME_BLOCKLIST:
                continue
            terms.add(token)
    return terms


def _clean_query(title: str) -> str:
    q = re.sub(r"[\[\(][^\]\)]{0,15}[\]\)]", " ", title)   # [보도자료], (참고) 같은 꼬리표 제거
    q = re.sub(r"[「」『』“”\"'‘’]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q[:80]


def _build_queries(title: str, agency: str, summary: str = ""):
    """보도자료 제목 전체를 그대로 검색어로 쓰면 실제로 사람이 검색하는 방식과 달라서
    (문장 전체 대 핵심어 몇 개) 네이버가 관련 기사를 놓치는 경우가 많다. 실제로
    "기후에너지환경부 가습기살균제 참사 배상재원, 기후에너지환경부와 기업이 함께
    책임진다"라는 긴 제목 그대로는 못 찾았던 연합뉴스 기사를, 사람이 쓴 "기후에너지환경부
    가습기"라는 짧은 키워드 검색으로는 바로 찾은 사례가 있어 이렇게 바꿨다.

    보도자료 제목은 보통 "[길게 수식하는 앞부분] + [핵심 결과를 나타내는 뒷부분]" 구조인데,
    실제 언론 제목은 뒷부분(결과)만 가져다 쓰는 경우가 많다. 예: 보도자료 "고온에도 속 꽉
    찬 배추 '청명가을' 대한민국 최고 품종 선정" vs 실제 뉴시스 제목 "올해 대한민국 최고
    품종은…'청명가을' 등 8개 선정" — "고온에도 속 꽉 찬 배추" 같은 앞부분 수식어는 기사
    제목에 전혀 없다.

    처음엔 "따옴표(''"") 안 고유명사"만 이 "제목 끝부분"을 끌어오는 조건으로 삼았는데,
    실제로는 "AI의 위로가 사람을 대신할 수 없습니다 「정신건강 목적 생성형 AI 이용
    가이드라인 발표」"처럼 전각괄호(「」『』)로 핵심 결과 어구를 감싸는 제목도 많고,
    아예 괄호/따옴표가 하나도 없는 제목도 많다. 따옴표 유무와 상관없이 "제목 끝부분
    (핵심 결과 어구)"은 항상 유용한 검색어라, 이제 따옴표가 있든 없든 매번 시도한다.

    0순위: 기관명 + 요약문(summary) 속 따옴표/전각괄호 안 사업명(있으면) — 제목이 "반려동물과의
    마지막 동행까지 함께합니다"처럼 감성적인 문구라 핵심 사업명이 제목에 아예 없고 본문
    요약에만 "「펫로스 심리지원 프로그램」"처럼 박혀있는 경우가 실제로 있었다. 기사 제목은
    보통 이런 "진짜 사업명"을 쓰지, 보도자료의 홍보성 제목을 그대로 쓰지 않는다.
    1순위: 기관명 + 제목 속 따옴표/전각괄호 안 고유명사(있으면) + 제목 끝부분
    2순위: 기관명 + 제목 끝부분(마지막 5단어 안팎) — 따옴표/괄호가 없어도 항상 시도.
    3순위: 기관명 + 제목의 첫 구절(쉼표·콜론·가운뎃점 앞부분) — 사람이 검색하는 방식과 비슷.
    4순위: 정제된 전체 제목 — 앞선 시도로 못 찾았을 때 보강용으로 그대로 둔다.
    최대 3개 검색어로 제한해 요청 수가 지나치게 늘지 않게 한다(우선순위 순으로 3개 채움)."""
    # 원문(제목/요약문)에서 따옴표나 전각괄호로 묶인 고유명사(사업명·브랜드명 등)를 뽑는다.
    # _clean_query는 이 문자들을 전부 지워버리므로, 지우기 전 원문에서 뽑아야 한다.
    # 길이 상한을 20자에서 40자로 늘렸다: "데이터 기반 연근해어업 관리 체계 혁신 방안"처럼
    # 정책/사업명이 20자를 넘는 경우가 실제로 있어서, 20자로 제한하면 이런 핵심 문구가
    # 통째로 추출 대상에서 빠지는 문제가 있었다.
    #
    # 괄호/따옴표 종류를 섞어서 매칭하면(예: 여는 ' 과 닫는 」를 한 쌍으로 착각) 전혀
    # 엉뚱한 범위가 뽑히는 버그가 있었다(외교부 사례: "...재구성(...)'이라는 주제로
    # 「제8차 한-아세안..." 에서 '...'가 길어서(40자 넘음) 매칭 실패하자, 그 닫는 '를
    # 엉뚱하게 뒤에 나오는 「...」의 여는 따옴표로 착각해 "이라는 주제로 「제8차..."처럼
    # 짝이 안 맞는 텍스트를 뽑아버렸다). 그래서 종류별로 짝을 맞춰 따로 매칭한다.
    _QUOTE_PAIRS = [("'", "'"), ('"', '"'), ("‘", "’"), ("“", "”"), ("「", "」"), ("『", "』")]

    def _extract_quoted(text: str):
        text = text or ""
        matches = []  # (start, end, content)
        for open_c, close_c in _QUOTE_PAIRS:
            pat = re.escape(open_c) + f"([^{re.escape(open_c)}{re.escape(close_c)}]{{2,40}})" + re.escape(close_c)
            for m in re.finditer(pat, text):
                # "(이하 '농식품부')", "(이하, '시범사업')"처럼 공식 문서에서 흔한
                # "약칭 정의"용 따옴표는 진짜 사업명이 아니라 그냥 줄임말 정의라서
                # 검색어로 뽑으면 오히려 핵심어가 희석된다(실제로 "농어촌 기본소득
                # 시범사업" 대신 "시범사업"만 뽑혀서 검색어가 너무 뭉툭해진 사례가
                # 있었다). 바로 앞에 "이하"가 붙은 따옴표는 제외한다.
                if re.search(r"이하[,\s]*$", text[:m.start()]):
                    continue
                content = m.group(1)
                # "'고준위 방사성폐기물 관리에 관한 특별법(이하 특별법)'"처럼, "이하 약칭"
                # 정의가 따옴표 "안쪽" 끝부분에 붙는 경우도 있다(위는 따옴표 "앞"에 붙는
                # 경우만 걸러냄). 이것도 검색어로는 불필요한 군더더기라 잘라낸다.
                content = re.sub(r"\(이하[,\s]*[^)]*\)\s*$", "", content).strip()
                if not content:
                    continue
                matches.append((m.start(), m.end(), content))
        matches.sort(key=lambda t: t[0])
        return [content for _, _, content in matches]

    quoted_title = _extract_quoted(title)
    quoted_summary = _extract_quoted(summary or "")

    clean = _clean_query(title)
    words = clean.split()
    tail = " ".join(words[-5:]) if len(words) > 5 else clean
    first_clause = re.split(r"[,:·]", clean)[0].strip()
    # 제목 맨 앞 1~2어절("고준위 방사성폐기물...")은 보통 이 보도자료의 핵심 주제/기관명이고,
    # 기관명이나 다른 수식어 없이 그 자체로 검색하면 네이버에서 훨씬 폭넓게(=느슨하게) 걸린다.
    # 실제로 "고준위 방사성폐기물 관리위원회 출범 1년, 50년 국가 난제 해결의 기틀 다져"는
    # "기관명+전체 구절"로 검색하면 정작 실린 기사(제목이 "후보부지 내년 공모..."처럼
    # 전혀 다르게 뽑힌 기사)가 결과에 안 걸렸는데, "고준위"만 검색하면 바로 나왔다 —
    # 즉 쿼리가 길고 구체적일수록 오히려 recall(재현율)이 떨어지는 경우가 있다.
    # 그래서 기관명 없이 앞쪽 핵심어만 짧게 뽑은 쿼리를 최우선으로 하나 추가한다.
    # (오탐은 _MATCH_TITLE_MIN/_MATCH_THRESHOLD 점수 필터링이 걸러주므로, 쿼리를
    # 넓게 잡는 것 자체는 위험하지 않다.)
    lead_phrase = " ".join(words[:2]) if len(words) >= 2 else clean

    queries = []

    def add(*parts: str):
        seen_words = set()
        out_words = []
        for part in parts:
            for w in part.split():
                if w not in seen_words:
                    seen_words.add(w)
                    out_words.append(w)
        q = " ".join(out_words)[:80]
        if q and q not in queries:
            queries.append(q)

    if quoted_summary:
        add(agency, " ".join(quoted_summary))
    if len(queries) < 3 and lead_phrase:
        add(lead_phrase)
    if len(queries) < 3 and quoted_title:
        add(agency, " ".join(quoted_title), tail)
    if len(queries) < 3 and agency and first_clause:
        add(agency, first_clause)
    if len(queries) < 3 and tail:
        add(agency, tail)
    if len(queries) < 3:
        add(clean)
    return queries[:3] or [clean]


_REL_DATE_PATTERNS = [
    (re.compile(r"(\d+)\s*분\s*전"), lambda m, now: now.date()),
    (re.compile(r"(\d+)\s*시간\s*전"), lambda m, now: now.date()),
    (re.compile(r"(\d+)\s*일\s*전"), lambda m, now: (now - timedelta(days=int(m.group(1)))).date()),
    (re.compile(r"어제"), lambda m, now: (now - timedelta(days=1)).date()),
    (re.compile(r"오늘"), lambda m, now: now.date()),
    (re.compile(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\."),
     lambda m, now: datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()),
]


def _extract_result_date(title_anchor, now_kst):
    """네이버 뉴스 검색결과에서 각 기사의 "N일 전"/"YYYY.MM.DD." 같은 날짜 표시를
    찾아서 실제 날짜로 변환한다. 정확한 class명이 아니라, 제목 링크의 조상 블록을
    몇 단계 거슬러 올라가며 텍스트에서 날짜 패턴을 정규식으로 찾는 방식이라, 네이버가
    class명을 또 해시값으로 바꿔도(지금까지 두 번 그랬다) 잘 안 깨진다. 날짜를 못
    찾으면 None을 돌려주고, 호출하는 쪽에서는 "모르면 걸러내지 않는다"로 처리해서
    이 함수가 오작동해도 매칭 자체가 완전히 막히지는 않게 한다."""
    node = title_anchor
    for _ in range(4):
        node = node.find_parent()
        if node is None:
            break
        text = node.get_text(" ", strip=True)
        for pat, to_date in _REL_DATE_PATTERNS:
            m = pat.search(text)
            if m:
                try:
                    return to_date(m, now_kst)
                except ValueError:
                    continue
    return None


def _naver_news_search(query: str):
    """네이버 뉴스 검색결과 웹페이지(search.naver.com)를 그대로 가져와
    [(언론사명, 기사제목, 링크, 미리보기문구), ...]로 돌려준다.
    차단/오류면 None을 반환해서 '결과 없음'과 구분한다.

    네이버가 검색결과 디자인을 새 컴포넌트 체계(SDS)로 바꾸면서 class 이름이
    전부 해시값처럼 랜덤화되어(news_tit, news_wrap 같은 예전 class는 더 이상
    존재하지 않음) class 기반 선택자로는 결과를 하나도 못 찾는다. 대신 각
    블록의 역할을 나타내는 data-heatmap-target 속성(".tit"=제목 링크,
    ".body"=본문 미리보기 링크)은 의미 기반이라 class보다 안정적이라 이걸로 찾는다.

    제목 링크 목록과 언론사명 목록은 페이지에 같은 순서로, 항목당 정확히 1개씩
    나란히 나온다(실제 응답으로 검증함). 조상을 거슬러 올라가 "같은 블록 안"의
    언론사명을 찾는 방식은 항목들이 얕은 공통 조상을 공유할 때 엉뚱한(이전 항목의)
    언론사명을 집어오는 버그가 있어, 순서 기반(인덱스) 매칭으로 바꿨다. 본문
    미리보기는 항목마다 있는 게 아니라서(이미지만 있는 항목 등) 링크 URL을 키로
    매핑해서 가져온다."""
    try:
        resp = requests.get(
            "https://search.naver.com/search.naver",
            # sort=1(최신순)을 한 번 시도했었는데, 추가한 직후부터 전혀 무관한 여러
            # 검색어에서 동시에 제목링크 0개가 떴다(실제로 존재할 법한 "외교부 제8차
            # 한-아세안 싱크탱크 전략대화" 같은 공식 행사명까지 0건). sort=1이 걸린
            # 결과 페이지는 관련도순(기본값)과 다른 템플릿을 내려줄 가능성이 있어
            # (아래 선택자가 그 템플릿엔 안 맞을 수 있음), 원인이 분명해질 때까지는
            # 안전하게 기본 정렬(관련도순)로 되돌린다.
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

    # 템플릿이 살짝 바뀌어도 깨지지 않도록 선택자를 조금 더 느슨하게 잡는다:
    # - 제목 링크: 새 컴포넌트(data-heatmap-target)뿐 아니라 구버전 class(a.news_tit)도 같이 본다.
    # - 언론사명: 정확한 전체 class명 대신 "profile-info-title-text"를 포함하는 class를
    #   찾는다([class*=...]) — 네이버가 해시 접미사만 살짝 바꿔도 안 깨지게.
    title_anchors = soup.select('a[data-heatmap-target=".tit"], a.news_tit')
    press_spans = soup.select('[class*="profile-info-title-text"]')
    body_anchors = soup.select('a[data-heatmap-target=".body"]')

    if not title_anchors:
        # 선택자가 지금 네이버 페이지 구조와 안 맞거나, 봇 탐지로 다른 페이지를 받은 경우,
        # 아니면 정말로 그 검색어에 뉴스 결과가 하나도 없는 경우(이것도 정상적인 상황)다.
        # 예전엔 본문 앞 200자만 잘라서 남겨서, 그게 메뉴/탭 이름에서 바로 끊겨버리면
        # "진짜 결과 없음"인지 "선택자가 깨짐"인지 구분이 안 됐다. 그래서 네이버가 실제로
        # 쓰는 "검색결과가 없습니다" 안내문구가 있는지부터 명시적으로 확인하고, 본문
        # 스니펫도 800자로 늘려서 더 뒤쪽(진짜 본문이 있다면 그 부분)까지 보이게 한다.
        full_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        no_result_phrases = ["에 대한 검색결과가 없습니다", "검색결과가 없습니다", "다른 검색어로 찾아보세요"]
        genuinely_empty = any(p in full_text for p in no_result_phrases)
        body_snippet = full_text[:800]
        title_tag = soup.title.get_text(strip=True) if soup.title else "(없음)"
        print(
            f"    [검색진단] 네이버: 제목링크 0개 / 진짜결과없음={genuinely_empty} / "
            f"최종URL={resp.url} / 응답길이={len(resp.text)}자 / <title>={title_tag} / "
            f"본문일부=\"{body_snippet}\"",
            file=sys.stderr,
        )

    desc_by_link = {}
    for body_a in body_anchors:
        href = body_a.get("href", "")
        if href and href not in desc_by_link:
            desc_by_link[href] = body_a.get_text(" ", strip=True)

    now_kst = datetime.now(KST)
    out = []
    for i, title_a in enumerate(title_anchors):
        title = title_a.get_text(" ", strip=True)
        link = title_a.get("href", "")
        if not title or not link:
            continue
        press = press_spans[i].get_text(" ", strip=True) if i < len(press_spans) else ""
        desc = desc_by_link.get(link, "")
        result_date = _extract_result_date(title_a, now_kst)
        out.append((press, title, link, desc, result_date))
    return out


def _is_photo_article(link: str) -> bool:
    """연합뉴스 포토(사진) 기사는 캡션 위주라 제목이 짧고 보도자료 제목과 거의
    안 겹쳐서 글자쌍 유사도로는 거의 매칭되지 않지만, 혹시라도 우연히 기준을
    넘겨 잘못 매칭되는 걸 막기 위해 후보에서 아예 제외한다.
    연합뉴스 포토 기사 링크는 /view/PYH... 형태(사진·그래픽 전용 기사 ID 접두어)."""
    return "/view/PYH" in link


def find_media_coverage(title: str, agency: str = "", summary: str = "", pr_date: str = ""):
    """연합뉴스 > 뉴시스 > 뉴스1 순서로 실제 보도 기사를 찾는다.
    검색어를 여러 단계로 시도한다: 요약문/제목 속 따옴표·전각괄호 핵심어 → 제목
    끝부분 → 기관명+제목 첫 구절(사람이 실제로 검색하는 방식과 비슷) → 정제된
    전체 제목(_build_queries 참고). 검색 결과를 합쳐서 그 안에서 매체명으로 걸러
    우선순위대로 확인하고, 제목 유사도가 기준(_MATCH_THRESHOLD)에 못 미치면
    '이 매체엔 없음'으로 보고 다음 매체로 넘어간다.

    pr_date(보도자료 날짜, "YYYY-MM-DD")가 주어지면, 검색결과에서 날짜를 읽어낼 수
    있었던 후보 중 보도자료와 날짜가 다른 건 제외한다(당일 보도자료인데 몇 주 전
    기사가 엉뚱하게 잡히는 걸 막기 위함). 날짜를 못 읽은 후보는 "모른다"로 보고
    걸러내지 않는다 — 날짜 추출 자체가 선택자 문제로 실패할 수 있어서, 모른다고
    무조건 버리면 멀쩡한 매칭까지 다 막힐 수 있기 때문이다."""
    queries = _build_queries(title, agency, summary)
    seen_links = set()
    merged = []
    any_ok = False
    for q in queries:
        results = _naver_news_search(q)
        time.sleep(random.uniform(2.0, 3.5))  # 검색엔진 부담/차단 방지 (간격을 넓히고 매번 랜덤화해 더 자연스럽게)
        if results is None:
            print(f"    - 네이버 검색 실패(차단/오류) [검색어: {q}]")
            continue
        if not results:
            print(f"    - 네이버 검색결과 없음 [검색어: {q}]")
            continue
        any_ok = True
        for r in results:
            if _is_photo_article(r[2]) or r[2] in seen_links:
                continue
            seen_links.add(r[2])
            merged.append(r)

    if not any_ok:
        return None
    if not merged:
        print("    - 검색은 됐지만 쓸만한 후보가 없음 (포토 기사 제외 후 0건)")
        return None

    pr_date_obj = None
    if pr_date:
        try:
            pr_date_obj = datetime.strptime(pr_date, "%Y-%m-%d").date()
        except ValueError:
            pr_date_obj = None

    org_terms = _extract_org_terms(title, agency, summary)

    for press_name in MEDIA_PRIORITY:
        candidates = [r for r in merged if press_name in r[0]]
        if not candidates:
            print(f"    - {press_name}: 검색결과 없음")
            continue
        if pr_date_obj:
            before = len(candidates)
            # 날짜를 읽어낸 후보 중 보도자료와 날짜가 다른 건 제외. 날짜를 못 읽은
            # 후보(result_date=None)는 걸러내지 않고 그대로 둔다(위 함수 설명 참고).
            candidates = [r for r in candidates if r[4] is None or r[4] == pr_date_obj]
            dropped = before - len(candidates)
            if dropped:
                print(f"    - {press_name}: 날짜 불일치로 {dropped}건 제외")
            if not candidates:
                print(f"    - {press_name}: 날짜 일치 후보 없음")
                continue
        best = max(candidates, key=lambda r: _match_score(title, r[1] + " " + r[3]))
        title_only_score = _match_score(title, best[1])
        print(f"    - {press_name}: 후보 {len(candidates)}건, 제목단독유사도 {title_only_score:.2f}")
        # 1차 관문: 제목끼리 아예 무관하면(예: 주한외교단↔중동뉴스, 0.0) 본문까지
        # 가져올 필요도 없이 바로 걸러낸다.
        if title_only_score < _MATCH_TITLE_MIN:
            print(f"    - {press_name}: 제목단독유사도 기준 미달로 제외")
            continue

        article_text, article_date = _fetch_article_text(best[2])
        time.sleep(random.uniform(1.0, 2.0))  # 언론사 사이트에도 예의상 간격을 둔다
        if article_text:
            # 기사 원문 페이지 자체에서 읽은 날짜는 검색결과 스니펫보다 훨씬 믿을
            # 만하므로, 여기서도 한 번 더 날짜를 확인한다. 검색결과 단계
            # (위 pr_date_obj 필터)에서 날짜를 못 읽어 걸러지지 않고 넘어온 후보라도,
            # 원문을 실제로 열어보면 날짜를 읽을 수 있는 경우가 많다.
            if pr_date_obj and article_date and article_date != pr_date_obj:
                print(f"    - {press_name}: 기사원문 날짜({article_date}) 보도자료 날짜({pr_date_obj})와 달라 제외")
                continue
            body_score, body_overlap = _match_score_with_overlap(title + " " + summary, best[1] + " " + article_text)
            print(f"    - {press_name}: 기사원문 확인함, 본문비교유사도 {body_score:.2f} (겹친 글자쌍 {body_overlap}개)")
            if body_score >= _MATCH_BODY_MIN and body_overlap >= _MATCH_BODY_MIN_OVERLAP:
                if org_terms and not any(t in (best[1] + " " + article_text) for t in org_terms):
                    print(f"    - {press_name}: 기관명({', '.join(sorted(org_terms))}) 기사에 안 보여 제외")
                    continue
                return {"press": press_name, "title": best[1][:200], "link": best[2]}
            print(f"    - {press_name}: 본문비교 기준 미달로 보류")
        else:
            # 기사 원문에 못 들어갔으면(차단/오류) 어쩔 수 없이 기존 방식(제목+미리보기)으로 대체 판단.
            combined_score = _match_score(title, best[1] + " " + best[3])
            print(f"    - {press_name}: 기사원문 확인 실패 → 제목+미리보기유사도 {combined_score:.2f}로 대체 판단")
            if combined_score >= _MATCH_THRESHOLD:
                if org_terms and not any(t in (best[1] + " " + best[3]) for t in org_terms):
                    print(f"    - {press_name}: 기관명({', '.join(sorted(org_terms))}) 기사에 안 보여 제외")
                    continue
                return {"press": press_name, "title": best[1][:200], "link": best[2]}
    return None


def enrich_with_media(items):
    """새로 발견된 항목마다 실제 보도 매체(연합뉴스/뉴시스/뉴스1) 매칭을 시도한다.
    매칭에 실패해도 item 자체는 그대로 유지되고, 매체 관련 필드만 비워진다.
    media_checked_at을 남겨서, 이후 recheck_pending_media()가 너무 자주
    같은 항목을 재검색하지 않도록 한다."""
    now_iso = datetime.now(KST).isoformat()
    for it in items:
        media = find_media_coverage(it["title"], it.get("agency", ""), it.get("summary", ""), it.get("date", ""))
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
RECHECK_BATCH_LIMIT = 4         # 한 번 실행(5분)에서 재확인할 최대 건수 (검색엔진 부하 제한, 짧은 시간에 몰아서 쏘지 않도록 축소)


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
        media = find_media_coverage(it["title"], it.get("agency", ""), it.get("summary", ""), it.get("date", ""))
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
