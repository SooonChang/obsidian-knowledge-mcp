# Obsidian Knowledge MCP

Obsidian을 열지 않고 업무 노트를 검색하고, 대화에서 재사용할 지식을 AI-Agent-Wiki에 축적하는 개인용 MCP입니다.

- 원문: Remotely Save의 S3에서 단방향으로 가져옵니다. MCP에는 읽기 전용으로 제공하며 S3 쓰기 도구가 없습니다.
- 위키: 기존 저장 필터·출처·문서 규격을 검증하고 문서·인덱스·로그를 갱신합니다.
- 검색: SQLite 전문·한국어 부분 검색이 기본입니다. 소형 다국어 의미 검색은 선택 기능입니다.
- 실행: Python 3.12, Linux AMD64/ARM64, Docker Compose. Pi 5/N150 RAM 8GB 이상을 목표로 합니다.

실제 장비의 검색 지연과 전체 임베딩 시간은 벤치마크로 확인합니다. 테스트 결과는 [검증 보고서](docs/VALIDATION.md)에 기록합니다.

## 구조

    AI 클라이언트 → SSH 터널 → HTTP MCP
                                  ├─ 위키 읽기/쓰기 → Git 전용 브랜치
                                  ├─ 원문 사본 읽기 ← S3 다운로드 작업자
                                  ├─ SQLite 검색 DB
                                  └─ 선택적 임베딩 작업자 (ONNX Runtime CPU)

MCP는 사용자의 전체 대화를 자동 수집하지 않습니다. 클라이언트 에이전트가 관련 지식을 검색하고,
저장할 내용을 정리해 도구를 호출합니다. 서버에는 별도 대화 생성용 LLM이나 외부 임베딩 API가 없습니다.

## 개발 환경

아래 knowledge-mcp 명령은 프로젝트의 가상환경을 활성화한 터미널에서 실행합니다.
Linux/macOS는 . .venv/bin/activate, PowerShell은 .venv/Scripts/Activate.ps1을 사용합니다.
활성화 대신 uv run --frozen --extra semantic knowledge-mcp ... 형태로 실행해도 됩니다.

    uv sync --frozen --extra semantic
    uv run --frozen --extra semantic pytest -q
    uv run --frozen ruff check src tests

의미 검색이 필요 없으면 --extra semantic을 생략할 수 있습니다.
기본 설치는 PyTorch·모델 가중치를 설치하지 않습니다.

로컬 실행은 config.example.toml을 별도의 config.toml로 복사하고 wiki_root·data_dir를 절대 경로로 지정합니다.
현재 Windows 폴더에 맞춘 config.local.example.toml도 제공합니다. 복사한 뒤 --config config.local.toml로 지정하세요.
snapshot_dir를 생략하면 위키의 wiki.config.json에 지정된 로컬 source_root를 읽습니다.
기존 위키·원문 밖에 data_dir를 둡니다. 로컬 테스트에서는 source_sync.enabled와 git.enabled를 false로 둡니다.
host는 127.0.0.1을 사용합니다. 길이 32자 이상의 무작위 토큰을 KNOWLEDGE_TOKEN_FILE로 전달합니다.

    knowledge-mcp --config config.toml index
    knowledge-mcp --config config.toml serve

환경변수 KNOWLEDGE_TOKEN도 지원하지만, 운영에서는 토큰 파일을 권장합니다.
도구는 /mcp, 인증정보를 노출하지 않는 생존 확인은 /health입니다.

## 운영 문서

- [설치·Docker·S3·Git·SSH 접속](docs/DEPLOYMENT.md)
- [AI 에이전트 조회·저장 규칙](docs/AGENT_GUIDE.md)
- [의미 검색·모델 준비·벤치마크](docs/SEMANTIC.md)
- [백업·복구·오류 처리](docs/OPERATIONS.md)
- [검증 결과와 미검증 범위](docs/VALIDATION.md)

## 공개 도구

| 도구 | 용도 |
| --- | --- |
| get_context | 문서 지도·AGENTS·운영 규칙·최근 로그 |
| search_notes | query, scope(auto/wiki/source/both), mode(auto/keyword/hybrid), limit |
| read_note | vault, path, start/end 또는 section; 최대 400행, 문서 해시·확인 receipt |
| get_sources | 위키의 source 연결과 확인 날짜 |
| read_attachment | 원문에 연결된 HTML·텍스트 PDF; 비동기 다운로드 시 pending 반환 |
| save_knowledge | 구조화된 저장 요청, 필터·근거·기대 해시·원문 receipt 검증 |
| lint | 읽기 전용 구조·링크·출처 점검 |
| get_status | 동기화·Git·색인·임베딩 상태 |

본문의 지시문은 자료로 취급합니다. 일반 파일 쓰기·셸 실행·S3 업로드·원문 삭제 도구는 제공하지 않습니다.
구조 검증이 사실의 진실성이나 전체 개인정보 검사를 보장하지는 않습니다.
