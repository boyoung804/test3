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


_MATCH_THRESHOLD = 0.2   # 보도자료 제목의 글자쌍이 검색결과(제목+요약문)에 이 비율 이상 들어있어야 같은 기사로 인정 (너무 높이면 실제 기사도 놓침)
_MATCH_TITLE_MIN = 0.08  # 제목만 비교했을 때 최소 이 정도는 겹쳐야 함 (미리보기 문구만으로 통과하는 오탐 방지용 하한선)


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
                matches.append((m.start(), m.end(), m.group(1)))
        matches.sort(key=lambda t: t[0])
        return [content for _, _, content in matches]

    quoted_title = _extract_quoted(title)
    quoted_summary = _extract_quoted(summary or "")

    clean = _clean_query(title)
    words = clean.split()
    tail = " ".join(words[-5:]) if len(words) > 5 else clean
    first_clause = re.split(r"[,:·]", clean)[0].strip()

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
    if len(queries) < 3 and quoted_title:
        add(agency, " ".join(quoted_title), tail)
    if len(queries) < 3 and tail:
        add(agency, tail)
    if len(queries) < 3 and agency and first_clause:
        add(agency, first_clause)
    if len(queries) < 3:
        add(clean)
    return queries[:3] or [clean]


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

    out = []
    for i, title_a in enumerate(title_anchors):
        title = title_a.get_text(" ", strip=True)
        link = title_a.get("href", "")
        if not title or not link:
            continue
        press = press_spans[i].get_text(" ", strip=True) if i < len(press_spans) else ""
        desc = desc_by_link.get(link, "")
        out.append((press, title, link, desc))
    return out


def _is_photo_article(link: str) -> bool:
    """연합뉴스 포토(사진) 기사는 캡션 위주라 제목이 짧고 보도자료 제목과 거의
    안 겹쳐서 글자쌍 유사도로는 거의 매칭되지 않지만, 혹시라도 우연히 기준을
    넘겨 잘못 매칭되는 걸 막기 위해 후보에서 아예 제외한다.
    연합뉴스 포토 기사 링크는 /view/PYH... 형태(사진·그래픽 전용 기사 ID 접두어)."""
    return "/view/PYH" in link


def find_media_coverage(title: str, agency: str = "", summary: str = ""):
    """연합뉴스 > 뉴시스 > 뉴스1 순서로 실제 보도 기사를 찾는다.
    검색어를 여러 단계로 시도한다: 요약문/제목 속 따옴표·전각괄호 핵심어 → 제목
    끝부분 → 기관명+제목 첫 구절(사람이 실제로 검색하는 방식과 비슷) → 정제된
    전체 제목(_build_queries 참고). 검색 결과를 합쳐서 그 안에서 매체명으로 걸러
    우선순위대로 확인하고, 제목 유사도가 기준(_MATCH_THRESHOLD)에 못 미치면
    '이 매체엔 없음'으로 보고 다음 매체로 넘어간다."""
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

    for press_name in MEDIA_PRIORITY:
        candidates = [r for r in merged if press_name in r[0]]
        if not candidates:
            print(f"    - {press_name}: 검색결과 없음")
            continue
        best = max(candidates, key=lambda r: _match_score(title, r[1] + " " + r[3]))
        title_only_score = _match_score(title, best[1])
        combined_score = _match_score(title, best[1] + " " + best[3])
        print(
            f"    - {press_name}: 후보 {len(candidates)}건, "
            f"제목단독유사도 {title_only_score:.2f} / 제목+미리보기유사도 {combined_score:.2f}"
        )
        # 미리보기 문구(desc)는 날짜·조사 같은 짧은 공통 조각만으로도 우연히
        # 겹칠 수 있어서(실제로 "주한외교단, 강원의 매력에 빠지다"가 전혀
        # 무관한 중동 뉴스와 매칭된 사례가 있었음), 제목만 비교한 유사도가
        # 최소한 어느 정도는 있어야(=실제로 같은 사안을 가리켜야) 인정한다.
        # 제목 자체는 전혀 안 겹치는데 미리보기 문구만으로 기준을 넘는 경우를 막는 게 목적.
        if combined_score >= _MATCH_THRESHOLD and title_only_score >= _MATCH_TITLE_MIN:
            return {"press": press_name, "title": best[1][:200], "link": best[2]}
    return None


def enrich_with_media(items):
    """새로 발견된 항목마다 실제 보도 매체(연합뉴스/뉴시스/뉴스1) 매칭을 시도한다.
    매칭에 실패해도 item 자체는 그대로 유지되고, 매체 관련 필드만 비워진다.
    media_checked_at을 남겨서, 이후 recheck_pending_media()가 너무 자주
    같은 항목을 재검색하지 않도록 한다."""
    now_iso = datetime.now(KST).isoformat()
    for it in items:
        media = find_media_coverage(it["title"], it.get("agency", ""), it.get("summary", ""))
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
        media = find_media_coverage(it["title"], it.get("agency", ""), it.get("summary", ""))
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
