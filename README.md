# spcx-scout — SPCX 출처 탐색 수집기

AI 없이 도는 파이썬 코드입니다. 6시간마다 승인된 피드를 읽고, SpaceX 글이 인용한 바깥 사이트를 세어 새 출처 후보를 `data/candidates.json`에 씁니다.

## 규칙
- `config.json`의 `blocked` 사이트에는 접속하지 않습니다. 인용 횟수만 셉니다.
- 사이트의 robots.txt가 자동 접속을 거부하면 접속하지 않습니다.
- 사이트마다 1초 간격을 둡니다.
- 차단 사이트에 한 번이라도 접속하면 실행을 실패로 끝냅니다.

## 설치 (한 번, 약 10분)
1. github.com 에 로그인합니다.
2. 오른쪽 위 `+` → `New repository` 를 누릅니다.
3. 이름에 `spcx-scout` 를 넣고 `Public` 을 고른 뒤 `Create repository` 를 누릅니다.
4. `uploading an existing file` 을 눌러 이 폴더의 파일을 모두 올립니다. `.github` 폴더도 함께 올립니다.
   - 휴대폰에서 `.github` 폴더가 안 올라가면: `Add file` → `Create new file` → 이름 칸에 `.github/workflows/scout.yml` 을 넣고 파일 내용을 붙여 넣습니다.
5. `Settings` → `Actions` → `General` → `Workflow permissions` 에서 `Read and write permissions` 를 고르고 저장합니다.
6. `Actions` 탭 → `spcx-scout` → `Run workflow` 를 눌러 첫 실행을 합니다.
7. 1~2분 뒤 `data/candidates.json` 파일이 생기면 성공입니다.

## 결과 읽는 법
- `data/candidates.json`
  - `signals.candidates_passed`: 기계 시험을 통과한 후보 수
  - `signals.suggested_workers`: 봇이 부를 일꾼 수(0, 1, 2)
  - `signals.feeds_failing_3x`: 3회 연속 실패한 피드
  - `top`: 점수 상위 5개 후보(인용 횟수, 피드 주소, 최근 7일 글 수, 판정)
  - `seeds`: 씨앗 출처별 결과
  - `stats.blocked_contacted`: 차단 사이트 접속 수. 0이어야 합니다.
- `data/log.csv`: 실행마다 1줄

## 고치기
- 씨앗 출처 추가·삭제: `config.json`의 `seeds`
- 이미 아는 출처: `known` · 차단: `blocked` · 무시: `ignore`
- 주기: `.github/workflows/scout.yml`의 `cron`

## 알아둘 점
- 공개 저장소의 예약 실행은 저장소 활동이 60일 동안 없으면 자동으로 꺼집니다(GitHub 문서). 이 수집기는 결과가 바뀔 때마다 저장하므로 활동이 남습니다. 꺼지면 `Actions` 탭에서 다시 켭니다.
- 예약 실행은 GitHub이 바쁠 때 늦어질 수 있습니다.
- 기계 시험 통과는 접근·형식만 본 것입니다. 본문 품질과 신뢰도는 SPCX 통합봇이 따로 확인합니다.
