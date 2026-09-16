# 의미 검색 모델 설치 가이드

새 PC·미니 PC·Raspberry Pi 서버에서 의미 검색 모델을 준비하고 Docker에 연결하는 절차입니다.
현재 서버는 모델 파일을 자동 다운로드하지 않습니다. Docker 이미지 빌드가 설치하는 것은 추론 라이브러리입니다.
모델은 운영자가 아래 A 또는 B 방식으로 한 번 준비합니다.

## 1. 설치 방식 선택

| 상황 | 진행 방법 |
| --- | --- |
| 대상 장비의 models/e5-small에 파일이 이미 있음 | 다시 만들지 않고 4절에서 설치 확인 |
| 다른 PC에 정상 모델이 있음 | A: 모델 폴더 복사 |
| 준비된 모델이 전혀 없음 | B: 공식 모델 다운로드·ONNX 변환 |
| Pi/N150에서 모델 변환 부담을 줄이고 싶음 | 다른 PC에서 B를 수행한 뒤 A로 이동 |

모델은 Git에서 제외되어 있어 저장소 clone만으로는 설치되지 않습니다.
준비된 ONNX 모델 묶음은 Windows x64와 Linux AMD64/ARM64 런타임에서 사용할 수 있습니다.
플랫폼별 검증 결과와 실기기 측정 여부는 [검증 보고서](VALIDATION.md)를 참고하세요.

## 2. 필요한 파일

모델 디렉터리 구조:

    models/
      e5-small/
        model.int8.onnx
        tokenizer.json
        manifest.json
        MODEL_NOTICE.md       출처 고지, 있는 경우 함께 보관
        model.onnx            FP32 비교용, 운영에 필수 아님

| 파일 | 용도 | 기존 검증본 크기 |
| --- | --- | --- |
| model.int8.onnx | 실행할 ONNX 모델 | 약 407 MB |
| tokenizer.json | 텍스트를 모델 입력으로 변환 | 약 17 MB |
| manifest.json | 모델 리비전·파일 체크섬·처리 규칙 | 작은 JSON 파일 |

필수 파일 3개는 같은 준비 작업에서 나온 묶음이어야 합니다.
일반 Hugging Face 원본 가중치나 다른 ONNX 파일의 이름만 바꿔 넣어서는 안 됩니다.
출처는 [모델 고지](MODEL_NOTICE.md)를 참조하고 원본 모델 카드·라이선스 고지를 함께 보관합니다.

### A. 기존 모델 폴더를 복사하는 방법

1. 정상 모델이 있는 PC의 models/e5-small 폴더를 USB·SFTP·파일 공유 등으로 대상 장비에 복사합니다.
2. 대상 폴더 안에 필수 파일 3개가 바로 있는지 확인합니다. e5-small/e5-small처럼 폴더가 중첩되지 않게 합니다.
3. 대상 장비의 .env에서 MODEL_DIR를 e5-small의 **상위 폴더**로 지정합니다.
4. 3절의 연결 설정과 4절의 모델 확인을 수행합니다.

예를 들어 파일이 /srv/obsidian/models/e5-small/model.int8.onnx라면
MODEL_DIR는 /srv/obsidian/models입니다.

복사 방식에는 대상 호스트의 Python·uv·PyTorch 설치가 필요하지 않습니다.
Docker 이미지와 준비된 모델을 사용합니다. 최초 이미지 빌드에는 패키지 다운로드가 필요하므로
모델을 복사했다는 사실만으로 전체 오프라인 설치가 준비되는 것은 아닙니다.

### B. 공식 모델을 다운로드하고 변환하는 방법

이 단계는 프로젝트 소스와 uv가 있는 PC에서 실행합니다. Docker 컨테이너 내부 작업이 아닙니다.
Hugging Face·Python 패키지 저장소에 접근할 인터넷 연결이 필요합니다.
프로젝트의 .python-version과 uv.lock을 사용합니다.

운영 모델 외에 원본 가중치·FP32 ONNX·PyTorch 및 다운로드 캐시가 생기므로 수 GB 이상의 여유 공간을 확보합니다.
정확한 설치 크기는 플랫폼과 패키지 구성에 따라 다릅니다.

#### B-1. uv 설치

이미 uv --version이 동작하면 생략합니다.
Windows에서는 PowerShell에서 다음과 같이 설치할 수 있습니다.

    winget install --id=astral-sh.uv -e

Linux에서는 공식 설치 스크립트를 사용할 수 있습니다.

    curl -LsSf https://astral.sh/uv/install.sh | sh

설치 후 새 터미널을 열고 uv --version으로 확인합니다.
다른 설치 방식은 [uv 공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)를 참조합니다.

#### B-2. 프로젝트 루트로 이동

프로젝트를 내려받은 경로로 이동합니다.
이후 명령은 pyproject.toml과 uv.lock이 있는 디렉터리에서 실행합니다.

이미 정상 모델이 있는 models/e5-small에 다시 변환하지 않습니다.
기존 모델을 교체하려는 경우 7절을 먼저 읽습니다.

#### B-3. Python과 변환 의존성 설치

Windows PowerShell과 Linux 셸에서 공통으로 실행합니다.

    uv python install 3.12
    uv sync --frozen --extra model-build --extra semantic

Python 설치 명령은 [uv 공식 Python 안내](https://docs.astral.sh/uv/guides/install-python/)를 따릅니다.
이 프로젝트는 변환에 필요한 onnx가 현재 dev 그룹에 포함되어 있으므로 위 준비 명령에 --no-dev를 추가하지 않습니다.

#### B-4. 모델 생성

새 설치에서 models/e5-small이 비어 있거나 없는 것을 확인한 뒤 실행합니다.

    uv run --frozen --extra model-build --extra semantic knowledge-mcp prepare-model models/e5-small

이 명령은 아래 작업을 순서대로 수행합니다.

1. intfloat/multilingual-e5-small의 현재 리비전을 조회.
2. 해당 리비전의 토크나이저와 원본 모델 다운로드.
3. FP32 ONNX 변환.
4. MatMul 가중치의 동적 INT8 양자화.
5. tokenizer.json과 체크섬을 포함한 manifest.json 저장.

완료되면 터미널에 모델 리비전과 파일 체크섬이 포함된 JSON이 출력됩니다.
출력 전 오류가 발생했으면 파일 일부가 있어도 설치 완료로 취급하지 않습니다.
정상 완료 후에도 4절의 로드·추론 확인을 수행합니다.

이 명령은 config.toml이나 vault·MCP 인증 토큰 없이 실행할 수 있으며 업무 노트를 읽거나 업로드하지 않습니다.
현재 CLI에는 과거 모델 리비전을 지정하는 옵션이 없습니다.
다른 날짜에 다시 준비하면 원본 리비전이 달라질 수 있으므로 같은 결과를 재사용하려면 검증한 파일 묶음을 복사합니다.

## 3. Docker 설정 연결

먼저 로컬 vault 직접 연결은 [로컬 Docker 안내](LOCAL_DOCKER.md),
S3/Git 동기화 구성은 [서버 배포 안내](DEPLOYMENT.md)에 따라 .env·config.toml과 인증 토큰을 준비합니다.

.env의 MODEL_DIR는 **호스트 경로**입니다. 아래 두 값 중 자신의 환경에 맞는 하나만 사용합니다.

Windows 예:

    MODEL_DIR=C:/projects/obsidian-knowledge-mcp/models

Linux 서버 예:

    MODEL_DIR=/srv/obsidian/models

config.toml의 semantic 설정은 **컨테이너 내부 경로**입니다.
기존 [semantic] 절을 수정하며 같은 이름의 절을 중복 추가하지 않습니다.

    [semantic]
    enabled = true
    model_dir = "/models/e5-small"
    url = "http://semantic:8766"
    host = "0.0.0.0"
    port = 8766
    threads = 2
    timeout_seconds = 5

Compose는 MODEL_DIR를 /models에 읽기 전용으로 연결합니다.
호스트 디렉터리·파일은 서비스 UID가 읽을 수 있어야 합니다.
Linux에서는 기본 APP_UID=1000인 점을 확인하고 소유권·읽기 권한을 맞춥니다.
런타임 DB의 data 디렉터리는 별도로 쓰기 권한이 필요합니다.

## 4. 설치 확인

### Docker에서 확인 — 복사 방식에서도 사용 가능

Docker Desktop의 Linux 엔진 또는 Linux Docker Engine이 실행 중이어야 합니다.
Compose의 .env·config.toml·vault 경로·토큰을 준비한 상태에서 프로젝트 루트에서 실행합니다.

    docker compose --profile semantic config --quiet
    docker compose --profile semantic build semantic
    docker compose --profile semantic run --rm --no-deps --entrypoint python semantic -c "from pathlib import Path; from knowledge_mcp.semantic import Encoder; e=Encoder(Path('/models/e5-small')); v=e.encode('model installation check', query=True); assert v.shape == (384,); print('Model OK: 384 dimensions')"

마지막 명령은 모델 체크섬 확인·ONNX 로드·예시 문장 추론만 수행합니다.
문서 색인 작업이나 위키 저장은 하지 않습니다.
성공하면 Model OK: 384 dimensions가 출력되고 임시 컨테이너는 종료됩니다.

### 호스트에서 확인 — uv를 설치한 경우

    uv run --frozen --extra semantic python -c "from pathlib import Path; from knowledge_mcp.semantic import Encoder; e=Encoder(Path('models/e5-small')); v=e.encode('model installation check', query=True); assert v.shape == (384,); print('Model OK: 384 dimensions')"

이 명령도 현재 폴더의 모델을 사용하며 새 원본 모델을 다운로드하지 않습니다.
필요한 Python 실행 의존성은 uv가 설치할 수 있습니다.

## 5. MCP와 의미 검색 시작

설치 확인을 마치면 [로컬 Docker 실행](LOCAL_DOCKER.md#시작) 또는
[서버 배포](DEPLOYMENT.md#git-서버-브랜치-준비와-시작)의 의미 검색 실행 명령을 따릅니다.
Python 직접 실행은 [의미 검색 서비스 실행](SEMANTIC.md#python에서-의미-검색-서비스-실행)을 참고하세요.

모델 로드 확인 후에는 `get_status`와 실제 질문의 검색 응답으로 임베딩 진행 및 검색 방식을 확인합니다.
초기 색인과 키워드 검색 전환 조건은 [검색과 준비 상태](SEMANTIC.md#검색과-준비-상태)에서 설명합니다.

## 6. 자주 발생하는 문제

| 증상 | 확인·조치 |
| --- | --- |
| manifest.json 없음 / FileNotFoundError | 필수 파일 3개와 MODEL_DIR의 상위 폴더 지정, 중첩 폴더 여부 확인 |
| Model artifact checksum mismatch | 서로 다른 준비 작업의 파일이 섞였거나 복사가 불완전한지 확인하고 같은 묶음으로 다시 복사 |
| uv를 찾지 못함 | uv 설치 후 새 터미널에서 PATH 확인 |
| 모델 다운로드 실패 | 인터넷·프록시·디스크 공간 확인. 업무 토큰을 Hugging Face 토큰으로 넣지 않음 |
| onnx 모듈 누락 | B-3을 다시 실행. 모델 준비 시 --no-dev를 사용하지 않음 |
| semantic 서비스가 실행되지 않음 | --profile semantic과 실제 사용하는 COMPOSE_FILE 확인 |
| Permission denied | 모델 읽기 권한·부모 디렉터리 접근 권한·data 쓰기 권한 확인 |
| 컨테이너 재시작 반복 | docker compose logs --tail 100 semantic으로 최초 오류 확인 |
| ARM64에서 ONNX import 실패 | 잠금 파일의 Linux ARM64 버전 1.22.1을 유지하고 최신 프로젝트로 이미지 재빌드. 임의 패키지 업그레이드 피함 |
| 계속 keyword로 응답 | semantic.enabled, URL, 컨테이너 상태, 임베딩 진행, 요청 제한시간 확인 |

CPU 2개·메모리 3GB는 현재 Compose의 semantic 제한입니다.
실제 Pi/N150에서 충분한지 확인한 뒤 운영 조건을 조정합니다.
모든 파일이나 모델 버전의 호환성을 보장하는 설정은 아닙니다.

## 7. 모델 교체·백업

실행 중인 모델 폴더를 직접 덮어쓰지 않습니다.
새 모델은 예를 들어 models/e5-small-next에 준비한 뒤 그 경로로 4절의 로드 검증을 수행합니다.
교체 시 semantic을 중지하고 config.toml의 model_dir를 새 폴더에 맞춘 뒤 MCP와 semantic을 재시작합니다.
마운트 경로를 바꿨다면 컨테이너를 다시 생성합니다.

이전 모델 묶음은 동작 확인 전까지 보관합니다.
모델·토크나이저·manifest를 함께 백업하며 runtime DB는 [백업 안내](OPERATIONS.md)를 따릅니다.
모델 식별자가 바뀌면 새 임베딩을 생성합니다. 이전 모델의 벡터를 새 모델용으로 그대로 사용할 수 없습니다.

Git에는 코드·설치 안내를 보관하고 models/·data/·인증 토큰은 올리지 않습니다.
