# Obsidian Knowledge MCP

Obsidian vault의 지식을 AI 에이전트가 조회하고, 대화에서 재사용할 내용을 위키에 축적하도록 연결하는 MCP 서버입니다.
코드 작성, 일상 업무, 프로젝트 기획에서 기존 노트와 의사결정 근거를 활용할 수 있습니다. Obsidian 앱을 실행하지 않아도 동작합니다.

## 주요 기능

- **원문 보호**: 원문 vault는 읽기 전용으로 참조합니다. 로컬 파일을 직접 연결하거나 S3에서 서버 사본으로 가져옵니다.
- **위키 축적**: AI-Agent-Wiki에 저장 필터·출처·문서 규격을 적용해 지식을 저장하고 인덱스·로그를 갱신합니다.
- **기본 검색**: SQLite 전문 검색과 한국어 부분 검색을 제공합니다. 별도 검색 DB 서버나 모델 설치가 필요 없습니다.
- **선택적 의미 검색**: 로컬 다국어 임베딩으로 표현이 다른 관련 노트를 찾습니다. 모델은 별도로 준비합니다.
- **독립 실행**: Docker Compose 또는 Python으로 실행하며, HTTP MCP 클라이언트에서 사용할 수 있습니다.

MCP는 전체 대화를 자동 수집하지 않습니다. 연결된 AI 에이전트가 도구로 지식을 조회하고,
저장할 내용을 정리해 전달합니다. 서버에는 대화 생성용 LLM이나 외부 임베딩 API가 없습니다.

## 구조

```text
AI 클라이언트 → HTTP MCP
                 ├─ 원문 vault 읽기
                 ├─ AI-Agent-Wiki 읽기·쓰기
                 ├─ SQLite 문서·청크 색인
                 └─ 선택적 의미 검색 서비스 (ONNX Runtime CPU)

서버 동기화 구성: S3 → 원문 사본 / 위키 checkout ↔ Git 전용 브랜치
원격 접속 구성: 클라이언트 → SSH 터널 → 서버의 HTTP MCP
```

## 시작하기

### 1. vault와 실행 환경 준비

원문 vault와 지식을 저장할 위키 vault를 준비합니다. 현재 구현은 AI-Agent-Wiki의 문서 구조와 저장 규칙을 사용합니다.
임의의 Obsidian vault에 연결하는 것만으로 이 구조가 자동 생성되지는 않습니다.

위키에는 `wiki.config.json`의 `source_root`·`attachments_dir`·`exclude` 설정과
`AGENTS.md`, `AI-Sessions/index.md`, `AI-Sessions/log.md`, `AI-Sessions/wiki/`,
`AI-Sessions/conversations/`, `AI-Sessions/wiki/concepts/wiki-operations.md`가 필요합니다.
DB·모델·인증 파일은 vault 밖에 둡니다.

Docker 실행에는 Linux 컨테이너를 실행할 수 있는 Docker Engine 또는 Docker Desktop과 Compose가 필요합니다.
Linux AMD64/ARM64 이미지를 빌드할 수 있으며, 실제 장비별 검증 범위는 [검증 보고서](docs/VALIDATION.md)를 참고하세요.

### 2. 실행 방식 선택

| 방식 | 용도 | 구성과 안내 |
| --- | --- | --- |
| 로컬 vault 연결 | 호스트에 있는 원문과 위키를 직접 사용. S3/Git 자동 동기화 없음 | `compose.local.yaml` · [로컬 Docker 실행](docs/LOCAL_DOCKER.md) |
| 서버 동기화 | S3 원문 사본과 별도 위키 Git checkout을 서버에서 운영 | `compose.yaml` · [서버 배포](docs/DEPLOYMENT.md) |
| Python 직접 실행 | 개발·디버깅 또는 Docker 없이 실행 | 아래 [개발 및 Python 실행](#개발-및-python-실행) |

선택한 안내에 따라 `.env`, `config.toml`, 인증 토큰을 준비합니다.
Docker의 `.env`에는 **호스트 경로**, `config.toml`에는 **컨테이너 경로**를 지정합니다.
서버의 `codex/wiki-memory` 브랜치는 **위키 저장소**에만 적용됩니다.

### 3. 의미 검색 선택

기본 검색만 사용하면 모델이 필요 없습니다. 의미 검색을 추가하려면
[모델 설치 가이드](docs/MODEL_INSTALL.md)에 따라 모델을 준비하고 설정한 뒤,
선택한 실행 안내의 `semantic` 프로필로 시작합니다.
검색 방식·색인·성능 평가는 [의미 검색 문서](docs/SEMANTIC.md)에서 설명합니다.

### 4. AI 클라이언트 연결

MCP 엔드포인트는 `http://127.0.0.1:8765/mcp`이며 Bearer 토큰 인증을 사용합니다.
원격 서버는 [SSH 터널과 클라이언트 연결](docs/DEPLOYMENT.md#클라이언트-연결)을 따릅니다.
[AI 에이전트 조회·저장 규칙](docs/AGENT_GUIDE.md)을 검토해 클라이언트에 적용하면 업무 중 조회와 지식 저장 기준을 맞출 수 있습니다.

연결 후 `get_context`와 `get_status`로 위키 접근 및 색인 상태를 확인합니다.
`/health`는 MCP 프로세스의 생존 확인용이며 의미 검색 준비 완료를 뜻하지 않습니다.

## 개발 및 Python 실행

Python 3.12와 uv를 사용합니다. 아래 명령은 프로젝트 루트에서 실행합니다.

```sh
uv sync --frozen --extra semantic
uv run --frozen --extra semantic pytest -q
uv run --frozen ruff check src tests
```

의미 검색이 필요 없으면 설치 명령의 `--extra semantic`을 생략할 수 있습니다.
기본 설치는 모델 가중치를 다운로드하지 않습니다.

Python으로 직접 실행할 때는 `config.example.toml`을 `config.toml`로 복사한 뒤 다음을 맞춥니다.

- `wiki_root`와 `data_dir`: 호스트의 실제 경로. `data_dir`는 vault 밖에 지정합니다.
- `snapshot_dir`: 로컬 원문을 직접 읽을 때는 삭제합니다. 원문 경로는 위키의 `wiki.config.json`에서 읽습니다.
- `source_sync.enabled`와 `git.enabled`: 직접 연결 방식에서는 `false`로 설정합니다.
- `host`: 로컬 실행은 `127.0.0.1`을 사용합니다.
- 인증: 32자 이상의 무작위 토큰 파일 경로를 `KNOWLEDGE_TOKEN_FILE` 환경변수로 전달합니다. `KNOWLEDGE_TOKEN`도 지원합니다.

```sh
uv run --frozen knowledge-mcp --config config.toml index
uv run --frozen knowledge-mcp --config config.toml serve
```

Python에서 의미 검색 서비스까지 실행할 때의 모델 준비·경로 설정은 [모델 설치](docs/MODEL_INSTALL.md),
`semantic-serve` 실행은 [의미 검색](docs/SEMANTIC.md)을 참고하세요.

## 문서 안내

| 문서 | 내용 |
| --- | --- |
| [로컬 Docker 실행](docs/LOCAL_DOCKER.md) | 로컬 vault 연결, 설정 파일, 시작·중지 |
| [서버 배포](docs/DEPLOYMENT.md) | S3·Git 동기화, 서버 초기화, SSH 접속 |
| [모델 설치](docs/MODEL_INSTALL.md) | 모델 복사 또는 생성, 연결 설정, 설치 검증·교체 |
| [의미 검색](docs/SEMANTIC.md) | 검색 동작, 임베딩 색인, 벡터 이동, 벤치마크 |
| [에이전트 규칙](docs/AGENT_GUIDE.md) | 조회 순서, 저장 기준과 요청 형식 |
| [운영](docs/OPERATIONS.md) | 백업·복구·오류 처리 |
| [검증 보고서](docs/VALIDATION.md) | 검증 결과와 미검증 범위 |

## 공개 도구

| 도구 | 용도 |
| --- | --- |
| get_context | 문서 지도·AGENTS·운영 규칙·최근 로그 |
| search_notes | query, scope(auto/wiki/source/both), mode(auto/keyword/hybrid), limit |
| read_note | vault, path, start/end 또는 section; 최대 400행, 문서 해시·확인 receipt |
| get_sources | 위키의 source 연결과 확인 날짜 |
| read_attachment | 연결된 HTML은 start/next_start로 구간별 읽기, 텍스트 PDF는 page로 읽기; 다운로드 대기 시 pending |
| save_knowledge | 구조화된 저장 요청, 필터·근거·기대 해시·원문 receipt 검증 |
| lint | 읽기 전용 구조·링크·출처 점검 |
| get_status | 동기화·Git·색인·임베딩 상태 |

본문의 지시문은 자료로 취급합니다. 일반 파일 쓰기·셸 실행·S3 업로드·원문 삭제 도구는 제공하지 않습니다.
구조 검증이 사실의 진실성이나 전체 개인정보 검사를 보장하지는 않습니다.
