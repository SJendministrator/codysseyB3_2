# ai-gitgen — AI 기반 Git 커밋 / PR 초안 생성기

`git status` · `git diff` 결과를 AI API에 보내 **커밋 메시지**와 **PR 제목·본문 초안**을 만들어 주는 터미널 도구입니다.
단순 호출이 아니라 **프롬프트(형식·규칙·컨텍스트) 설계 → 1회 호출 → 형식 검증·후처리**까지 한 번에 동작합니다.

```
git status / git diff  →  안전 모드(마스킹·전송 제한)  →  프롬프트 구성  →  AI API 1회 호출  →  검증·후처리  →  터미널 출력
```

- **외부 라이브러리 0개**: Python 표준 라이브러리(`urllib`, `subprocess`, `argparse`)만 쓰기 때문에 `pip install`이 필요 없습니다.
- **AI 제공자 3종**: Gemini(기본, 무료 키) / OpenAI / Anthropic 지원, 그 밖의 OpenAI 호환 서버는 `--base-url`로 연결
- **호출은 명령당 정확히 1회**: 자동 재시도가 없고 호출 횟수를 로그로 출력합니다.
- **안전 모드 기본 ON**: API Key·토큰·비밀번호·이메일·전화번호를 마스킹하고, 최대 10파일·200줄까지만 전송합니다.

---

## 1. 설치

필요한 것: **Python 3.10 이상** (3.9에서도 동작), **Git**

```bash
python3 --version          # 3.10 이상인지 확인
git clone https://github.com/SJendministrator/codysseyB3_2.git
cd codysseyB3_2
python3 main.py -h         # 도움말이 나오면 설치 끝 (pip install 불필요)
```

## 2. API Key 설정 (환경변수)

키는 **환경변수로만** 받습니다. 코드나 설정 파일에는 절대 넣지 않습니다. 설정 파일에 키를 넣으면 실행이 거부됩니다.

| 제공자 | 키 발급 | 비고 |
|---|---|---|
| `gemini` (기본) | https://aistudio.google.com/apikey | 카드 없이 무료 키 발급 |
| `openai` | https://platform.openai.com/api-keys | 유료 |
| `anthropic` | https://console.anthropic.com/settings/keys | 유료 |

```bash
# 현재 터미널 세션에만 설정 (공용 PC라면 이 방법을 권장: 창을 닫으면 사라짐)
export AI_API_KEY="발급받은_키"

# (선택) 제공자/모델 기본값도 환경변수로
export AI_PROVIDER=gemini        # gemini | openai | anthropic
export AI_MODEL=gemini-2.5-flash
```

> `AI_API_KEY`가 없으면 제공자별 변수(`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`)도 찾아봅니다.
> 개인 PC에서만 `~/.zshrc`에 넣으세요. 공용 PC에 넣으면 다음 사람이 그 키를 씁니다.

## 3. 사용법

**반드시 대상 프로젝트의 Git 루트에서 실행합니다.**

```bash
# 이 저장소 자체에서
python3 main.py commit
python3 main.py pr

# 다른 프로젝트에서 쓰기: 그 프로젝트 루트로 이동한 뒤 main.py 경로를 지정
cd ~/codysseyB3_1
python3 ~/codysseyB3_2/main.py commit

# 자주 쓰면 alias 등록
alias gitgen="python3 ~/codysseyB3_2/main.py"
gitgen pr --base main
```

### 자주 쓰는 예시

```bash
gitgen commit                                    # 스테이징된 변경(없으면 워킹트리 전체)으로 커밋 메시지 생성
gitgen commit -c "로그인 실패 시 재시도 로직 요청사항 반영"   # 변경 이유를 알려주면 품질이 올라감
gitgen commit -o msg.txt && git commit -F msg.txt   # 파일로 저장 → 검토 후 그대로 커밋
gitgen pr --base main -o pr.md                   # main 대비 현재 브랜치 변경으로 PR 초안
gitgen commit --dry-run                          # API를 호출하지 않고 보낼 프롬프트만 확인 (무료)
gitgen commit -m gemini-2.5-flash -t 0.1 --max-tokens 1024   # 파라미터 변경
gitgen commit --provider anthropic               # 다른 제공자
```

### 옵션

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--provider` | `gemini` | `gemini` / `openai` / `anthropic` |
| `--model`, `-m` | 제공자별 기본 | gemini `gemini-2.5-flash`, openai `gpt-4.1-mini`, anthropic `claude-haiku-5-5` |
| `--temperature`, `-t` | `0.3` | 0~2. 낮을수록 같은 diff에 같은 결과 (커밋 메시지엔 낮게) |
| `--max-tokens` | `2048` | 응답 최대 토큰. 넘으면 응답이 잘림 |
| `--timeout` | `60` | 요청 타임아웃(초) |
| `--base-url` | — | OpenAI 호환 서버 주소 직접 지정 |
| `--context`, `-c` | — | 변경 이유·요구사항을 프롬프트에 함께 전달 |
| `--lang` | `ko` | 출력 언어 `ko` / `en` |
| `--output`, `-o` | — | 결과를 파일로 저장 |
| `--dry-run` | — | 프롬프트만 출력, API 호출 0회 |
| `--show-prompt` | — | 호출 전에 프롬프트도 같이 출력 |
| `--safe-mode` / `--no-safe-mode` | ON | 민감정보 마스킹 + 전송 제한 |
| `--max-files` / `--max-lines` | `10` / `200` | 안전 모드 전송 한도 |
| `--base` (pr) | 자동 | 비교 기준 브랜치 (`main` → `master` → `origin/main` 순서로 탐지) |
| `--config` | `./.ai-gitgen.json` | 설정 파일 (있으면 자동 로드) |

설정 우선순위: **기본값 < 설정 파일 < 환경변수 < CLI 옵션**

## 4. 출력 예시

> 아래는 출력 형식 예시입니다. 실제 키로 실행한 화면은 `screenshots/`에 있습니다.

### 커밋 메시지 (`python3 main.py commit`)

```
[INFO] Git status 수집 완료: 2개 파일 변경 감지
[INFO] Git diff 수집 완료: 19줄
[INFO] 스테이징된 변경이 없어 워킹트리 전체(새 파일 포함)를 사용했습니다.
[INFO] 안전 모드: ON
[INFO]   전송 diff: 19줄 → 19줄, 파일 2개
[INFO]   마스킹: SLACK_TOKEN 1건, EMAIL 1건
[INFO] AI API 요청 중... (gemini / gemini-2.5-flash, temperature=0.3, max_tokens=2048)
[DONE] 커밋 메시지 생성 완료 (1.8s, 토큰 입력 412 / 출력 58)
[INFO] AI API 호출 횟수: 1

--- Commit Message ---
feat(app): hello 함수에 이름 인자 추가

- app.py: hello(name)으로 시그니처 변경
- config.py: 알림 설정 파일 추가
----------------------------------------
[CHECK] 형식 검증
  ✔ 제목 1줄 존재  feat(app): hello 함수에 이름 인자 추가
  ✔ 제목 길이 ≤50자 (권장)  29자
  ✔ 제목 길이 ≤72자 (최대)  29자
  ✔ type: 형식 (Conventional Commits)  예) feat: ..., fix(cli): ...
  ✔ 본문 불릿 요약  불릿 2개

※ AI 초안입니다. 반드시 검토·수정 후 사용하세요.
```

### PR 초안 (`python3 main.py pr`)

```
[INFO] 현재 브랜치: feature/commit-pr-generator (base: main)
[INFO] Git status 수집 완료: 1개 파일 변경 감지
[INFO] Git diff 수집 완료: 9줄
[INFO] `main...HEAD` 커밋 차이를 사용했습니다.
[INFO] 안전 모드: ON
[INFO]   전송 diff: 9줄 → 9줄, 파일 1개
[INFO] AI API 요청 중... (gemini / gemini-2.5-flash, temperature=0.3, max_tokens=2048)
[DONE] PR 초안 생성 완료 (2.3s, 토큰 입력 530 / 출력 121)
[INFO] AI API 호출 횟수: 1

--- PR Title ---
feat: hello 함수에 이름 인자 추가

--- PR Body ---
## Why
- 사용자 이름을 넣은 인사 문구가 필요했습니다.

## What
- `app.py`: `hello()` → `hello(name)` 시그니처 변경
- 반환 문자열을 f-string으로 변경

## How to Test
- `python -c "from app import hello; print(hello('쥭'))"` 실행
- 출력이 `hello 쥭` 인지 확인
----------------------------------------
[CHECK] 형식 검증
  ✔ PR 제목 1줄 존재  feat: hello 함수에 이름 인자 추가
  ✔ PR 제목 ≤80자  24자
  ✔ ## Why 섹션 + 불릿 ≥1  불릿 1개
  ✔ ## What 섹션 + 불릿 ≥1  불릿 2개
  ✔ ## How to Test 섹션 + 불릿 ≥1  불릿 2개
```

### 오류 / 예외 상황

```
$ python3 main.py commit            # 변경 없음
[INFO] 변경 사항이 없습니다. 커밋 메시지를 생성하지 않고 종료합니다.

$ python3 main.py commit            # 키 없음
[ERROR] AI_API_KEY 환경변수가 설정되지 않았습니다.
## 예) export AI_API_KEY="YOUR_KEY"
키 발급: https://aistudio.google.com/apikey

$ python3 main.py commit            # 키가 틀림
[ERROR] API 호출 실패 (HTTP 401 인증 실패): Incorrect API key provided
## API Key가 틀렸거나 만료됐습니다. AI_API_KEY 값과 --provider 가 맞는지 확인하세요.

$ python3 main.py commit            # 인터넷 끊김
[ERROR] 네트워크 오류: [Errno 8] nodename nor servname provided, or not known
## 인터넷 연결, 프록시/방화벽, --base-url 주소를 확인하세요.

$ cd src && python3 ../main.py commit   # 루트가 아닌 곳
[ERROR] 프로젝트 루트에서 실행해야 합니다.
```

| HTTP / 상황 | 메시지 | 안내 |
|---|---|---|
| 400 | 잘못된 요청 | 모델 이름·파라미터 확인 |
| 401 / 403 | 인증 실패 / 권한 없음 | 키·제공자·결제 상태 확인 |
| 404 | 찾을 수 없음 | 모델 이름 / `--base-url` 확인 |
| 429 | 요청 한도 초과 | 무료 할당량 초과, 잠시 후 재시도 |
| 5xx | 서버 오류 | 제공자 일시 장애 |
| 타임아웃 / DNS / SSL | 원인별 메시지 | `--timeout`, 네트워크, macOS 인증서 설치 안내 |
| 빈 응답 | `finish_reason` 표시 | thinking 모델이면 `--max-tokens` 증가 안내 |

---

## 5. 동작 원리

### 5-1. Git 변경 수집 (`gitgen/git_collector.py`)

| 명령 | 수집 대상 |
|---|---|
| `commit` | 스테이징된 변경(`git diff --cached`)이 있으면 **그것만**, 없으면 `git diff` + 새 파일(`git diff --no-index /dev/null <파일>`) |
| `pr` | `git diff <base>...HEAD`(브랜치에서 커밋한 것) + 아직 커밋 안 한 변경 |
| 공통 | `git status --porcelain`로 파일 목록·상태(수정/추가/삭제/새 파일), `git diff --stat`로 통계 |

Git 명령은 미션 범위대로 `status`·`diff`(+ 위치·브랜치 확인용 `rev-parse`)만 씁니다. 커밋·푸시·PR 생성은 하지 않습니다.

### 5-2. REST API 연동 (`gitgen/ai_client.py`)

1. **요청 구성**: 제공자별 URL, 헤더(`Authorization: Bearer` / `x-api-key`), JSON 본문(`model`, `messages`, `temperature`, `max_tokens`)
2. **전송**: `urllib.request.urlopen(..., timeout=)`로 1회만 보냅니다. 재시도가 없어 비용을 예측할 수 있습니다.
3. **응답 처리**: OpenAI 형식(`choices[0].message.content`)과 Anthropic 형식(`content[].text`)을 각각 파싱하고, 토큰 사용량·`finish_reason`을 기록합니다.
4. **예외 대응**: `HTTPError`(상태코드별), `URLError`(네트워크/SSL), 타임아웃, JSON 오류, 빈 응답을 각각 **원인과 해결 힌트가 담긴 메시지**로 바꿔서 보여줍니다.

### 5-3. 파라미터가 품질에 주는 영향

| 파라미터 | 낮으면 | 높으면 | 이 도구의 선택 |
|---|---|---|---|
| `temperature` | 같은 diff → 거의 같은 문장, 형식을 잘 지킴 | 표현이 다양해지지만 형식을 깨거나 내용을 지어낼 위험이 커짐 | **0.3**: 커밋/PR은 정확성이 우선 |
| `max_tokens` | 응답이 중간에 잘림 (`finish_reason=length`) | 비용 상한만 올라감 (실제 사용량만큼 과금) | **2048**: thinking 모델이 추론에 쓰는 토큰까지 감안 |
| `model` | 작은 모델: 빠르고 저렴 | 큰 모델: 맥락 이해가 깊지만 느리고 비쌈 | 요약 작업이라 Flash/mini/Haiku급으로 충분 |

### 5-4. 프롬프트 설계 (`gitgen/prompts.py`)

- **system** (매번 같음): 역할(시니어 개발자) + **출력 형식 고정** + 금지 규칙
  - 커밋: `type(scope): subject` / 제목 50자(최대 72) / 빈 줄 / `- ` 불릿 2~4개, 파일·모듈 1~3개 언급
  - PR: `TITLE:` 1줄 + `## Why` / `## What` / `## How to Test`, 섹션마다 불릿 1개 이상
  - "diff에 없는 내용 지어내지 말 것", "[MASKED] 언급 금지", "코드블록·머리말 금지"
- **user** (매번 바뀜): 브랜치·base, 수집 방식, **파일 목록**, **diff 통계**, **작성자가 준 변경 이유(`-c`)**, diff 본문
- 출력 형식을 **파싱 가능한 고정 구조**로 요구하기 때문에 후처리와 검증이 쉬워집니다.

### 5-5. 형식 검증 · 후처리 (`gitgen/postprocess.py`)

재호출 대신 **후처리 방식**을 택했습니다. 그래서 비용은 항상 1회분입니다.

| 대상 | 규칙 | 처리 |
|---|---|---|
| 커밋 제목 | 50자 권장 / 최대 72자 | 72자 초과 시 단어 경계에서 자름, 50자 초과는 경고 |
| 커밋 제목 | 머리말·따옴표·마침표 | `커밋 메시지:`, `"..."`, 끝 마침표, 코드블록 제거 |
| 커밋 본문 | 불릿 | `*`, `•`, `1.` → `- `로 통일 |
| PR 제목 | 최대 80자 | 초과 시 자름 |
| PR 본문 | Why/What/How to Test 헤더 필수 | 헤더 표기 정규화(`## What (핵심 변경)` → `## What`), 문단은 불릿으로 변환, **빠진 섹션은 자리표시 불릿을 넣고 ⚠ 경고** |

결과는 `[CHECK]` 영역에 ✔/⚠로, 자동으로 고친 내용은 `↺ 후처리:`로 표시됩니다.

---

## 6. 주의사항 · 운영 관점

### 민감정보: 안전 모드 (기본 ON)

무료 API는 보낸 내용을 서비스 개선에 쓸 수 있다고 약관에 명시하는 경우가 있습니다. 그래서 **보내기 전에 거르는 것**을 기본값으로 했습니다.

| 정책 | 기준 |
|---|---|
| **마스킹** | 개인키 블록, `sk-ant-…`, `sk-…`(OpenAI), `AIza…`(Google), `AKIA…`(AWS), `ghp_…`/`github_pat_…`, `xox…`(Slack), JWT, `password/secret/token/api_key = 값`, 이메일, 휴대폰 번호, 주민번호 → `[MASKED:종류]` |
| **파일 제외** | `.env*`, `*.pem`, `*.key`, `id_rsa*`, `*credentials*`, `*secret*` 같은 파일은 **내용 자체를 보내지 않음** (lock 파일·`*.min.js`도 제외해서 토큰 절약) |
| **전송 제한** | 최대 **10개 파일**, 최대 **200줄** (`--max-files`, `--max-lines`로 조정) |
| **사용자 정의 패턴** | `.ai-gitgen.json`의 `mask_patterns`에 정규식 추가 |
| **안전 모드 OFF** | `--no-safe-mode`: 원문 전송, 경고 출력, 그래도 2000줄 하드 리밋 유지 |

보내기 전에 정확히 무엇이 나가는지 보려면 `--dry-run`을 쓰세요. API를 호출하지 않으니 비용도 들지 않습니다.

### 비용 · 요청 횟수

- `commit` / `pr` 명령마다 **AI API 1회 호출**, 로그에 `AI API 호출 횟수: 1`을 출력합니다.
- 비용은 대략 **보낸 토큰 + 받은 토큰**입니다. 안전 모드의 200줄 제한이 입력 토큰 상한 역할을 합니다.
- 무료 키는 분당·일일 호출 한도가 있습니다. `429`가 나오면 잠시 기다리세요. 반복 호출 루프에 넣지 마세요.
- 권장 흐름: 변경을 작게 나눠 **`git add`로 커밋할 것만 스테이징한 뒤** `commit`을 실행합니다. diff가 작을수록 요약이 정확하고 비용도 적습니다.

### 생성 결과는 초안입니다

AI는 diff에 없는 의도를 알 수 없습니다. **왜 바꿨는지는 `-c`로 알려주고**, 결과는 반드시 읽어 보고 고친 뒤에 쓰세요.

---

## 7. 팀 컨벤션 설정 (`.ai-gitgen.json`)

프로젝트 루트에 두면 자동으로 적용됩니다. 예시는 [`.ai-gitgen.example.json`](.ai-gitgen.example.json)에 있습니다. API Key는 이 파일에 넣을 수 없게 막아 두었습니다.

```json
{
  "provider": "gemini",
  "max_lines": 200,
  "mask_patterns": { "INTERNAL_URL": "https?://[\\w.-]*\\.internal\\.[\\w./-]+" },
  "convention": {
    "commit_types": ["feat", "fix", "docs", "refactor", "test", "chore"],
    "scopes": ["git", "ai", "safe", "cli", "docs"],
    "require_scope": false,
    "subject_max": 50,
    "include_body": true,
    "extra_rules": ["관련 이슈가 있으면 제목 끝에 (#번호)를 붙인다"],
    "pr_checklist": ["로컬에서 실행해 봤다", "민감정보가 포함되지 않았다"]
  }
}
```

| 키 | 효과 |
|---|---|
| `commit_types` | 허용할 type prefix 목록 |
| `scopes` / `require_scope` | scope 후보 / 필수 여부 |
| `subject_max` | 권장 제목 길이 (검증 기준에도 반영) |
| `include_body` | 커밋 본문 생성 여부 |
| `extra_rules`, `pr_extra_rules` | 프롬프트에 추가할 팀 규칙 |
| `pr_title_prefix` | PR 제목에 `type:` 접두어를 붙일지 |
| `pr_checklist` | PR 본문 끝에 `## Checklist` 추가 |

---

## 8. 테스트

실제 API Key 없이 **로컬 가짜 API 서버 + 임시 Git 저장소**로 전체 흐름을 검증합니다.

```bash
python3 -m unittest discover -s tests -t .
# Ran 24 tests ... OK
```

검증 항목: 변경 없음 종료 / 루트가 아닌 곳에서 실행 차단 / 키 누락 / 401 인증 실패 / 네트워크 오류 / **정확히 1회 호출** / 파라미터(model·temperature·max_tokens)가 실제 요청에 반영되는지 / 안전 모드 마스킹·파일 제외·줄 제한 / 스테이징 우선 수집 / PR base 비교 / Anthropic 형식 / 후처리 규칙 / 컨벤션 설정 반영

## 9. 구조

```
.
├── main.py                    # 진입점: python3 main.py {commit,pr}
├── gitgen/
│   ├── cli.py                 # 인자 파싱, 설정 병합, 전체 흐름, 출력
│   ├── git_collector.py       # git status / diff 수집
│   ├── safe_mode.py           # 마스킹, 민감 파일 제외, 전송량 제한
│   ├── prompts.py             # system/user 프롬프트, 팀 컨벤션
│   ├── ai_client.py           # REST 호출 (urllib), 예외 → 메시지
│   └── postprocess.py         # 형식 검증 및 후처리
├── tests/                     # unittest (가짜 API 서버 포함)
└── .ai-gitgen.example.json    # 설정 예시
```
