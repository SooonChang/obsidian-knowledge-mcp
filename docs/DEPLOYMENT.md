# Linux 서버 설치

## 준비

64비트 Linux와 Docker Engine/Compose를 사용합니다. ARM64와 AMD64는 같은 소스를 각각 빌드합니다.
모델 변환은 별도 PC에서 수행할 수 있습니다. 최초 기본 검색에는 모델이 필요 없습니다.
의미 검색을 사용할 새 서버는 [모델 설치 가이드](MODEL_INSTALL.md)를 먼저 확인하세요.

서버에 아래 전용 경로를 만들고 실행 UID/GID가 필요한 경로에만 쓸 수 있도록 소유권을 지정합니다.

    /srv/obsidian/AI-Agent-Wiki       서버 전용 Git checkout
    /srv/obsidian/source-snapshots    S3 원문 사본 (위키 밖)
    /srv/obsidian/runtime             SQLite·잠금·복구 기록
    /srv/obsidian/models              선택적 모델
    /srv/obsidian/secrets             토큰·rclone 설정·Git SSH 키

실제 원문 PC나 사용자가 편집하는 checkout을 서버 작업 디렉터리로 직접 사용하지 않습니다.
로컬의 미커밋 변경은 자동 업로드되지 않습니다. 반영할 변경은 사용자가 먼저 선택하여 commit/push합니다.
원격 저장소 주소는 현재 위키의 origin을 사용합니다. 서버는 이미 원격에 있는 내용을 clone합니다.

## 설정

1. 프로젝트의 .env.example → .env, config.example.toml → config.toml로 복사합니다.
2. .env의 경로와 APP_UID/APP_GID를 실제 서버에 맞춥니다. COMPOSE_FILE이 설정되어 있다면 compose.yaml을 가리키도록 합니다.
3. config.toml의 source_sync.remote를 실제 S3 rclone remote와 vault prefix로 지정합니다.
4. secrets/mcp_token에는 무작위 토큰을 저장합니다. 예: python -c 'import secrets; print(secrets.token_urlsafe(48))'
5. secrets/git_key와 검증한 SSH 호스트 키인 secrets/known_hosts를 준비합니다. StrictHostKeyChecking을 끄지 않습니다.
6. 비밀 파일은 서비스 UID가 읽을 수 있게 하되 다른 사용자에게 공개하지 않습니다.

## Remotely Save 원문 최신화

아래 예시는 Remotely Save가 암호화하지 않은 원문을 S3에 저장하는 구성입니다. 사용 중인 저장 방식과 암호화 여부를 먼저 확인합니다.
플러그인의 설정 파일 자체는 서버나 Git에 복사하지 않습니다.
S3 endpoint·region·bucket·prefix를 확인한 뒤 별도 목록 조회/다운로드 전용 자격증명을 발급합니다.
AWS 기준 s3:ListBucket, s3:GetObject만 필요합니다. S3 호환 서비스에서는 같은 범위의 권한을 적용합니다.

secrets/rclone.conf 예시 (실제 비밀은 사용자 입력):

    [notes]
    type = s3
    provider = Other
    endpoint = https://YOUR-S3-ENDPOINT
    region = YOUR-REGION
    access_key_id = YOUR-READ-ONLY-KEY
    secret_access_key = YOUR-READ-ONLY-SECRET

config.toml:

    [source_sync]
    enabled = true
    remote = "notes:BUCKET/PREFIX"

worker는 lsjson과 원격→서버 copyto만 실행합니다. 서버 사본은 런타임 캐시이며 source_root 원본을 수정하는 기능이 아닙니다.
Markdown은 5분마다 목록을 확인하고 변경분만 내려받습니다. HTML/PDF는 연결된 문서 조회 요청이 들어오면 다운로드합니다.
첨부 최초 응답은 pending일 수 있으므로 수 초 뒤 다시 호출합니다.

두 번의 원격 목록 비교로 전송 도중 변경을 감지하지만, Remotely Save의 여러 파일 업로드 전체를 하나의 트랜잭션으로 보장하지는 않습니다.
PC의 아직 업로드하지 않은 내용은 접근할 수 없습니다. 플러그인의 자동 동기화 주기를 사용자가 설정해야 합니다.
암호화 설정을 나중에 바꿀 때는 운영자가 worker를 중단하고 설정을 재검토해야 합니다. 이 서버는 PC의 플러그인 설정 변경을 자동 감지하지 않습니다.
rclone crypt 포맷은 filename_encoding=base64 호환 설정이 필요합니다.
OpenSSL 포맷을 rclone crypt로 오인하거나 자동 변환하지 않습니다.

## Git 서버 브랜치 준비와 시작

UID/GID는 `.env`의 APP_UID/APP_GID와 Compose의 `user`에서만 지정합니다.
컨테이너 시작 시 실행 UID/GID에 필요한 계정 정보를 `/tmp`에 자동 생성하고 nss_wrapper로 SSH에 제공합니다.
이미지 빌드 시 사용자 번호를 지정하지 않으므로 같은 이미지를 다른 UID/GID의 서버에서 사용할 수 있습니다.
선택적으로 `./compose.sh`를 사용하면 현재 호스트 사용자의 UID/GID를 자동으로 전달합니다.

전용 checkout을 clone한 뒤:

    docker compose build
    docker compose run --rm sync --config /config/config.toml git-init
    docker compose up -d
    docker compose logs --tail 80

의미 검색 모델과 설정까지 준비했다면 시작 명령 대신 다음을 사용합니다. MCP·동기화 작업자·의미 검색을 함께 실행합니다.

    docker compose --profile semantic up -d --build

아래 브랜치 관리는 위키 저장소의 전용 checkout에만 적용되며 MCP 코드 저장소의 브랜치와는 무관합니다.

서버 브랜치 이름은 `config.toml`의 `git.branch`에서 설정합니다. 기본값은 `codex/wiki-memory`이며
다른 유효한 Git 브랜치 이름도 사용할 수 있습니다. `main`과 `master`는 서버 브랜치로 허용하지 않습니다.

git-init은 깨끗한 checkout에서만 설정한 서버 브랜치를 선택하거나 origin/main으로부터 생성합니다.
서버는 저장된 문서를 전용 브랜치에 commit/push합니다. 사용자 PC main 반영은 사용자가 별도로 병합합니다.
자동 PR 생성이나 기본 브랜치 push는 하지 않습니다.
원격 merge 충돌 시 자동 처리를 중단하고 수동 해결을 기다립니다.

## 클라이언트 연결

클라이언트 장비마다 SSH 터널을 엽니다. SSH 서버 주소는 배포 시 입력합니다.

    ssh -N -L 8765:127.0.0.1:8765 USER@SERVER

Codex config.toml 예시:

    [mcp_servers.work_knowledge]
    url = "http://127.0.0.1:8765/mcp"
    bearer_token_env_var = "KNOWLEDGE_TOKEN"

클라이언트를 실행하는 환경에 서버와 같은 토큰을 전달합니다. 토큰을 vault·대화·Git에 넣지 않습니다.
Claude Code 등 HTTP MCP 클라이언트도 같은 URL과 Authorization: Bearer 헤더를 사용합니다.
클라이언트별 설정 화면/설정 파일에서 헤더를 등록하고 실제 initialize·tools/list 연결을 확인합니다.
추가로 AGENT_GUIDE.md의 지침을 클라이언트의 사용자 규칙 또는 스킬에 넣습니다.

HTTP 포트는 기본적으로 호스트 loopback에만 공개합니다. 같은 LAN에서 직접 연결하려면
`.env`의 `MCP_BIND_HOST`를 서버 LAN IP로 설정하고 `config.toml`의 `allowed_hosts`에
`"서버LAN-IP:*"`를 추가한 뒤 `docker compose up -d --no-deps --force-recreate mcp`로 반영합니다.
클라이언트는 `http://서버LAN-IP:8765/mcp`와 기존 Bearer 토큰을 사용합니다.
VPN 공개나 공인 인터넷 OAuth 배포는 이 버전 범위가 아닙니다.
Docker 이미지가 동일하더라도 실제 x64/ARM 처리 속도와 메모리는 장비별로 다릅니다.
