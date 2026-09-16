# 선택적 의미 검색

기본 검색은 모델 없이 작동합니다. 이 기능은 소형 다국어 임베딩으로 키워드 표현 차이를 보완합니다.
대화 생성 LLM이나 외부 임베딩 API는 사용하지 않습니다.

Linux ARM64는 검증된 ONNX Runtime 1.22.1을 고정합니다. 배경과 플랫폼별 결과는 VALIDATION.md를 참조합니다.
로컬 전달본의 models/e5-small에는 검증한 운영 모델을 별도로 배치하며 Git에는 포함하지 않습니다.

## 모델 준비 (PC 또는 빌드 장비)

    uv sync --frozen --extra model-build --extra semantic
    uv run --frozen --extra model-build --extra semantic knowledge-mcp prepare-model models/e5-small

공식 intfloat/multilingual-e5-small의 모델 리비전을 조회해 고정하고 ONNX 변환과 INT8 양자화를 수행합니다.
PyTorch와 원본 가중치 다운로드는 이 준비 단계에서만 필요합니다. Pi 운영 이미지에는 모델 변환 의존성이 없습니다.
manifest.json은 모델 리비전·처리 규칙·실행 파일 체크섬을 기록합니다. 운영에 사용할 모델 묶음을 별도 보관하세요.
모델은 오픈소스 MIT 라이선스이며 배포 시 원본 모델 카드와 라이선스를 함께 보관합니다.

운영 디렉터리의 e5-small 폴더에 model.int8.onnx, tokenizer.json, manifest.json을 배치합니다.
원본 model.onnx는 비교 검증용이므로 운영 볼륨에 필수는 아닙니다.

config.toml의 semantic.enabled를 true로 설정한 뒤:

    docker compose --profile semantic up -d --build

추론기는 모델을 한 번 로드합니다. 초기값은 2스레드·1개 청크 순차 처리입니다.
조회 요청은 색인 청크 사이에서 우선하며 진행 중인 추론 하나는 끝날 때까지 기다릴 수 있습니다.
질문 벡터 생성이 5초 내 끝나지 않거나 서비스가 없으면 MCP는 keyword 결과를 반환합니다.
대기 시간 제한은 config의 timeout_seconds로 조절할 수 있습니다.

## 색인과 이동

문서 해시가 동일한 청크만 임베딩을 재사용합니다. 삭제·변경된 청크의 벡터는 검색에서 제외합니다.
문서별 긴 구간은 256토큰 제한과 32토큰 겹침으로 임베딩하고 검색 결과는 부모 문맥을 제공합니다.
표·코드는 검색용으로만 분할하며 원문을 잘라 저장하지 않습니다.

    knowledge-mcp --config config.toml export-vectors embeddings.json
    knowledge-mcp --config config.toml import-vectors embeddings.json

PC와 서버가 같은 모델 묶음·문서 바이트·청킹 규칙을 사용할 때 가져온 벡터를 재사용합니다.
줄바꿈 변환 등으로 파일 해시가 달라지면 다시 생성합니다.
벡터 내보내기에는 경로와 벡터가 포함되므로 원문과 같은 접근 권한으로 관리하며 공개 저장소에 올리지 않습니다.

## 벤치마크

benchmarks/questions.example.json은 합성 데이터용 예시 20개입니다.
실제 평가에서는 자신의 업무 질문과 정답 근거의 vault:path를 작성합니다.

    uv run python benchmarks/create_fixture.py .test-artifacts/fixture

위 명령은 이미 존재하는 대상 경로를 거부합니다. 합성 문서 60개를 만들며 실제 vault는 사용하지 않습니다.
fixture/config.toml의 model_dir를 준비한 모델의 절대 경로로 지정합니다.
합성 환경에 별도 테스트 토큰을 설정하고 index 및 semantic-serve를 실행한 다음 questions.json으로 평가합니다.

    knowledge-mcp --config config.toml benchmark my-questions.json

결과에 검색 방식별 recall@10, 중앙값·p95 지연, 실제 사용한 mode를 기록합니다.
hybrid 요청이 keyword로 전환되었는지 반드시 확인합니다. 외부 임베딩 작업자 RSS는 docker stats로 별도 측정합니다.
Linux CLI의 peak_rss는 CLI 프로세스 자체만의 값입니다.
최초 모델 로드 지연은 별도 측정하고, 준비된 뒤의 단일 사용자 검색 3초 이내는 검증 목표입니다.
Pi 5/N150 실측 전에는 성능 달성으로 표시하지 않습니다.

모델 준비 후 FP32와 INT8의 대표 질문 벡터·상위 근거를 비교합니다.
    uv run --extra semantic python benchmarks/check_model.py models/e5-small .test-artifacts/fixture/questions.json

측정 결과는 [검증 보고서](VALIDATION.md)를 참조합니다. 합성 질문의 결과를 실제 업무 검색 품질로 일반화하지 않습니다.
