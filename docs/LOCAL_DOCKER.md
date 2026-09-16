# 로컬 vault로 Docker 실행

호스트에 있는 원문 vault와 AI-Agent-Wiki를 직접 연결하는 구성입니다.
Windows의 Docker Desktop(Linux 컨테이너) 또는 Linux Docker Engine과 Compose를 사용합니다.
S3/Git 동기화 작업자는 실행하지 않습니다. MCP 저장 도구를 호출하면 연결한 위키 파일이 변경됩니다.

아래 파일과 명령의 기준 위치는 프로젝트 루트입니다.

## 1. 경로와 .env 준비

위키의 `wiki.config.json`과 문서 구조는 [README의 준비 사항](../README.md#1-vault와-실행-환경-준비)을 확인합니다.
기본 Compose는 두 vault를 `/vaults/AI-Agent-Wiki`와 `/vaults/zettelkasten`에 연결하므로
위키의 `source_root`는 `../zettelkasten`이어야 합니다.
다른 소스 경로를 유지하려면 Compose의 컨테이너 마운트 경로도 그 설정에 맞춥니다.
호스트의 폴더 이름과 위치는 `.env`로 지정할 수 있습니다.

다음 예시를 `.env`에 저장하고 vault 경로를 실제 경로로 바꿉니다.
Windows 경로도 `C:/vaults/AI-Agent-Wiki`처럼 슬래시를 사용합니다.

```dotenv
COMPOSE_FILE=compose.local.yaml
COMPOSE_PROJECT_NAME=obsidian-knowledge-local
APP_UID=1000
APP_GID=1000
WIKI_DIR=/absolute/path/AI-Agent-Wiki
SOURCE_DIR=/absolute/path/zettelkasten
DATA_DIR=./data
MODEL_DIR=./models
SECRETS_DIR=./secrets
CONFIG_FILE=./config.toml
```

`data`, `models`, `secrets` 폴더를 프로젝트 루트에 만듭니다. DB·모델·토큰은 vault 밖에 둡니다.
Linux에서는 APP_UID/APP_GID에 맞는 사용자에게 위키·data 쓰기 권한과 원문·모델·설정·토큰 읽기 권한이 필요합니다.
서버 동기화용 `.env.example`을 그대로 사용하는 대신 위 로컬 구성을 사용합니다.

## 2. config.toml과 인증 토큰 준비

`config.toml`에는 컨테이너 내부 경로를 지정합니다. 기본 검색으로 시작하는 예시입니다.

```toml
wiki_root = "/vaults/AI-Agent-Wiki"
data_dir = "/data"
host = "0.0.0.0"
port = 8765
allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
allowed_origins = []

[source_sync]
enabled = false

[git]
enabled = false

[semantic]
enabled = false
```

`snapshot_dir`는 지정하지 않습니다. 원문 경로는 위키의 `source_root`에서 읽습니다.
Python 직접 실행용 `config.local.example.toml`은 이 Docker 설정과 경로가 다릅니다.

`secrets/mcp_token`에 32자 이상의 무작위 토큰을 저장합니다. 기존 토큰이 있으면 그대로 사용합니다.
Python이 설치된 호스트에서는 다음 명령으로 **새 파일**을 만들 수 있습니다.
Linux에서 실행 파일 이름이 `python3`이면 명령의 `python`을 바꿉니다.

```sh
python -c "import secrets; from pathlib import Path; p=Path('secrets/mcp_token'); p.parent.mkdir(exist_ok=True); f=p.open('x', encoding='utf-8'); f.write(secrets.token_urlsafe(48)); f.close()"
```

이미 파일이 있으면 이 명령은 덮어쓰지 않고 실패합니다. 토큰은 화면·대화·Git에 기록하지 않습니다.
`.env`, `config.toml`, `secrets/`, `models/`, `data/`는 Git에서 제외됩니다.

## 3. 의미 검색 선택

기본 검색만 사용하면 이 단계를 생략합니다.
의미 검색을 사용하려면 [모델 설치 가이드](MODEL_INSTALL.md)에 따라 모델을 준비하고
기존 `[semantic]` 절을 수정합니다. 모델은 컨테이너가 자동 다운로드하지 않습니다.

## 시작

Docker 엔진을 실행한 뒤 프로젝트 루트에서 실행합니다.

기본 검색:

```sh
docker compose config --quiet
docker compose up -d --build mcp
```

의미 검색을 설정한 경우:

```sh
docker compose --profile semantic config --quiet
docker compose --profile semantic up -d --build mcp semantic
```

상태와 로그 확인:

```sh
docker compose --profile semantic ps
docker compose logs --tail 100
curl http://127.0.0.1:8765/health
```

PowerShell에서는 마지막 명령 대신 `Invoke-RestMethod http://127.0.0.1:8765/health`도 사용할 수 있습니다.
MCP 주소는 `http://127.0.0.1:8765/mcp`이며 같은 토큰으로 인증합니다.
현재 `compose.local.yaml`의 호스트 포트 바인딩은 `0.0.0.0:8765:8765`입니다.
로컬 접속으로 제한하려면 해당 `ports` 값을 `127.0.0.1:8765:8765`로 설정합니다.
다른 주소로 직접 접속할 경우에는 `allowed_hosts`도 접속 호스트에 맞게 설정해야 합니다.

`/health`는 MCP 생존 확인입니다. 의미 검색 준비 상태는 [검색과 준비 상태](SEMANTIC.md#검색과-준비-상태)를 확인합니다.
같은 data 디렉터리·포트를 사용하는 기존 MCP 서버가 있으면 먼저 중지합니다.

## 중지와 다시 시작

```sh
docker compose --profile semantic stop
docker compose --profile semantic up -d
```

기본 검색만 사용하면 다시 시작할 때 `--profile semantic`을 생략합니다.
사용하던 의미 검색을 끄려면 `semantic.enabled=false`로 바꾸고 `docker compose stop semantic` 후 MCP를 재시작합니다.
원격 접속과 S3/Git 동기화는 [서버 배포 안내](DEPLOYMENT.md)를 따릅니다.
