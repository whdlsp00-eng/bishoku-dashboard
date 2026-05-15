# 비쇼쿠모노 Instagram 대시보드

매일 오전 8:30 KST에 GitHub Actions가 자동으로 Instagram Graph API를 호출해 최신 데이터를 가져와 정적 HTML 대시보드로 발행합니다.

## 어떻게 동작하나

```
매일 23:30 UTC (08:30 KST)
        ↓
GitHub Actions 트리거
        ↓
scripts/update.py 실행
  - 토큰 자동 갱신 (60일 연장)
  - /me, /me/media, 콘텐츠별 인사이트, 계정 인사이트, 인구통계 fetch
  - history.json에 일별 스냅샷 누적
  - index.html 재생성
        ↓
변경사항 main 브랜치에 커밋
        ↓
gh-pages 브랜치로 배포
        ↓
https://<github-username>.github.io/bishoku-dashboard/ 에 반영
```

## 초기 설정 (한 번만)

### 1. GitHub 레포지토리 생성

1. https://github.com/new 접속
2. Repository name: `bishoku-dashboard` (원하는 이름으로 변경 가능)
3. **Public** 선택 (무료 계정에서 GitHub Pages를 쓰려면 public 필요. `noindex` 메타 태그가 있어 검색엔진 노출은 안 됨)
4. "Create repository" 클릭

### 2. 이 폴더를 레포에 푸시

이 폴더(`bishoku-dashboard`)에서 터미널 열고:

```bash
cd "/Users/imac/Documents/Claude/Projects/SNS 성과관리/bishoku-dashboard"
git init
git add .
git commit -m "Initial dashboard setup"
git branch -M main
git remote add origin https://github.com/<your-username>/bishoku-dashboard.git
git push -u origin main
```

`<your-username>`은 본인 GitHub 계정 이름으로 교체.

### 3. Secret 등록 (Instagram 토큰)

1. GitHub 레포 페이지 → **Settings** → **Secrets and variables** → **Actions**
2. **New repository secret** 클릭
3. Name: `BISHOKU_MONO_API_KEY`
4. Value: 아래 값 붙여넣기

```
IGAAXly73144BBZAGI3VW1HY0F6VUJfckp6UV9vMTM3U1pzVFAtcjNDR1A4MlkyRlQ2c3ZAIX1UzMGJPVE5qZA3haa3pZALWh0S2MyNWZARcm5DUTZAlZAkpLdkJqQThRY3NSNW84c1NoUGtxUnRIV2NsUUR1X1B4RHZAJakplWUlFSHlDawZDZD
```

5. **Add secret** 클릭

### 4. GitHub Pages 활성화

1. 레포 페이지 → **Settings** → **Pages**
2. **Source**: `Deploy from a branch`
3. **Branch**: `gh-pages` / `/ (root)` 선택 (첫 워크플로우 실행 후 이 브랜치가 자동 생성됨)
4. **Save**

### 5. 첫 실행

1. 레포 페이지 → **Actions** 탭
2. **Update dashboard** 워크플로우 선택
3. 오른쪽 **Run workflow** 버튼 클릭 → **Run workflow** 확정
4. 약 1~2분 후 실행 완료
5. `gh-pages` 브랜치가 자동 생성되고, GitHub Pages 설정에서 다시 한 번 브랜치를 `gh-pages`로 지정해야 할 수 있음
6. **Settings → Pages**에서 표시되는 URL이 대시보드 주소

### 6. URL 공유

대시보드 URL 예시:
```
https://<your-username>.github.io/bishoku-dashboard/
```

이 URL을 알고 있는 사람만 접속 가능합니다. `<meta name="robots" content="noindex">`가 들어있어 구글 검색에는 안 잡힙니다.

## 토큰 만료에 대해

Instagram 장기 토큰은 60일 후 만료됩니다. 이 스크립트는 매 실행마다 `refresh_access_token` 엔드포인트를 호출해 토큰 만료를 60일 연장합니다. 따라서 매일 실행되는 한 만료되지 않습니다.

만약 7일 이상 워크플로우가 멈춰 있다가 토큰이 만료된 경우:
1. https://developers.facebook.com 에서 새 장기 토큰 발급
2. GitHub Secrets의 `BISHOKU_MONO_API_KEY` 값을 갱신

> 참고: 갱신된 토큰을 GitHub Secret에 자동으로 다시 저장하려면 별도의 `PAT(Personal Access Token)`로 Secret을 업데이트하는 단계가 필요합니다. 현재는 단순화를 위해 갱신만 하고 동일 토큰을 계속 사용합니다 (Instagram이 같은 토큰을 갱신하므로 외부 저장은 옵션).

## 수동 실행

기다리지 않고 바로 갱신하고 싶으면:
- GitHub 레포 → **Actions** → **Update dashboard** → **Run workflow**

## 파일 구조

```
bishoku-dashboard/
├── .github/workflows/update.yml  # GitHub Actions 워크플로우
├── scripts/update.py              # 데이터 fetch + HTML 생성
├── index.html                     # 대시보드 (자동 생성됨)
├── history.json                   # 일별 스냅샷 누적 (자동 생성됨)
├── data.json                      # 가장 최근 raw 데이터 (자동 생성됨)
├── .gitignore
└── README.md
```

## 문제 해결

**Actions가 실패하는 경우**
- Actions 탭에서 실패한 실행을 클릭해 로그 확인
- 가장 흔한 원인: 토큰 만료, API rate limit, 새로 추가된 메트릭 호환성

**대시보드가 업데이트 안 되는 경우**
- GitHub Pages 설정에서 `gh-pages` 브랜치가 source로 지정됐는지 확인
- 캐시: 브라우저에서 강제 새로고침 (Cmd+Shift+R)

**다른 채널 추가**
- `scripts/update.py`의 메트릭 세트와 API 호출은 그대로 두고, 별도 레포를 만들거나 같은 레포에서 채널별 디렉토리로 분기
