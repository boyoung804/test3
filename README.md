중앙부처 보도자료 자동 수집기
매일 자동으로 korea.kr(정책브리핑)에서 지정한 19개 부처·처 + 대통령실의
보도자료를 수집해서, 웹페이지(`index.html`)에서 날짜별로 조회할 수 있게 합니다.
수집: GitHub Actions가 매일 KST 09:10에 자동 실행 (`scripts/collect.py`)
저장: `data/YYYY-MM-DD.json`, `data/latest.json`, `data/dates.json`
열람: `index.html`을 GitHub Pages로 올려서 접속
---
1. GitHub 저장소 만들기
https://github.com 가입/로그인
우측 상단 `+` → `New repository`
이름 예: `pr-tracker` (아무 이름이나 가능, Public으로 생성)
이 폴더(`pr-tracker/`) 안의 파일 전체를 그대로 업로드
웹 브라우저에서: 저장소 페이지 → `Add file` → `Upload files` → 폴더째 드래그
또는 git 명령어를 쓸 줄 아시면:
```
     cd pr-tracker
     git init
     git add .
     git commit -m "init"
     git branch -M main
     git remote add origin https://github.com/<본인계정>/pr-tracker.git
     git push -u origin main
     ```

## 2\. GitHub Pages 켜기 (웹으로 보이게 하기)

1. 저장소 → `Settings` → 왼쪽 메뉴 `Pages`
2. `Source`를 `Deploy from a branch`로, `Branch`를 `main` / `/(root)`로 설정 후 저장
3. 몇 분 뒤 `https://<본인계정>.github.io/pr-tracker/` 로 접속하면 페이지가 뜹니다.

## 3\. 자동 수집(Actions) 켜기

1. 저장소 → `Actions` 탭 클릭
2. "I understand my workflows, go ahead and enable them" 같은 안내가 뜨면 클릭해서 활성화
3. `.github/workflows/daily-collect.yml`이 이미 들어있으므로, 별도 설정 없이
**매일 KST 09:10에 자동 실행**됩니다.
4. 바로 테스트해보고 싶다면: `Actions` 탭 → 왼쪽에서 "보도자료 일일 수집" 선택 →
우측 `Run workflow` 버튼으로 즉시 1회 실행 가능합니다.
5. 실행이 끝나면 `data/` 폴더에 그날 날짜 파일이 자동으로 커밋됩니다.

## 4\. 접속 방법

* 최신 자료: `https://<본인계정>.github.io/pr-tracker/`
* 특정 날짜 지정: `https://<본인계정>.github.io/pr-tracker/?date=20260917`
(`?date=2026-09-17` 형식도 가능)

## 5\. 감시 대상 기관 수정

`scripts/collect.py` 상단의 `AGENCIES` 리스트와,
`index.html` 상단의 `AGENCIES` 배열을 **둘 다** 같은 내용으로 고치면 됩니다.
(수집 스크립트용, 화면 드롭다운용으로 두 군데 있습니다.)

## 6\. 주의사항 / 한계

* **스크래핑 기반**입니다. korea.kr이 페이지 구조를 바꾸면
`scripts/collect.py`의 `parse\_items()` 함수가 깨질 수 있습니다.
Actions 탭에서 매일 실행 결과(초록 체크 / 빨간 X)를 가끔 확인하시길 권장합니다.
* 목록 페이지 자체 검색(저작권, AI 등 키워드)은 아직 자동화하지 않았습니다.
기본은 "그날 게재된, 감시 대상 기관의 보도자료 전체"를 가져오는 구조입니다.
* `index.html`은 로컬에서 파일로 직접 열면(file://) 작동하지 않습니다.
반드시 GitHub Pages 등 웹 서버 주소(https://...)로 접속해야 합니다.
* 완전 무료로 운영 가능합니다 (GitHub Actions 무료 사용량 내, GitHub Pages 무료).
