# 상태·백업·복구

## 상태 확인

    knowledge-mcp --config config.toml status
    knowledge-mcp --config config.toml lint

get_status도 같은 운영 상태를 제공합니다.
`image_sync`는 이미지 동기화 성공 여부·시각·파일 수를 별도로 제공합니다.
이미지 동기화 중에는 `status=running`, `stage`와 `processed/total`, `downloaded`, `reused`가 표시됩니다.
완료 시 `status=complete`, 실패 시 `status=failed`와 경로를 포함하지 않는 `code`·`message`를 기록합니다.
확장자가 이미지 확장자이고 실제 내용도 PNG/JPEG/WebP/GIF 중 하나이면 형식이 달라도 허용합니다.
원본 파일명과 내용은 유지하고 AI 조회 응답만 PNG 미리보기로 반환합니다.
`invalid_image`는 손상·판독 불가 또는 지원하지 않는 실제 형식,
`file_too_large`는 파일 크기 초과, `image_pixel_limit`는 픽셀 수 초과,
`download_size_mismatch`는 전송 크기 불일치, `remote_changed`는 전송 도중 원격 변경입니다.
실패한 파일의 `file_id`는 원문 상대 경로(UTF-8)의 SHA-256이며 실제 경로는 MCP나 로그에 출력하지 않습니다.
오류가 발생하면 게시·삭제를 보류합니다. 원본 파일을 확인하거나 `exclude`로 제외한 뒤 다음 동기화를 기다립니다.
원문 동기화 실패와 원문 부재, Git push 대기, 의미 검색 미준비를 구분합니다.
worker 로그는 비밀 노출을 피하기 위해 오류 종류만 출력합니다.

### 검색 사용량

`search_notes` 호출마다 결과 방식만 `searches` 테이블에 기록하고 질의 원문은 저장하지 않습니다.
기록은 90일 뒤 삭제되며 CLI `benchmark` 검색은 집계하지 않습니다.
get_status의 `search_usage`는 최근 7일, CLI `status`는 1·7·30일 집계를 보여줍니다.
`semantic`은 응답 상태별 건수입니다. `ready`만 hybrid 순위가 적용되었고 `indexing`·`unavailable`·`disabled`는 keyword로 대체되었습니다.
`unavailable`의 원인은 `semantic_errors`에 오류 종류로 집계됩니다.
`results.semantic_only`는 keyword 검색만으로는 반환되지 않았을 결과 수로, 의미 검색의 실제 기여를 나타냅니다.
운영자가 진단할 때는 read-only rclone lsjson과 git status/fetch 등을 별도 실행하되 출력 공유에 주의합니다.

## 저장 장애와 동시 작업

문서·인덱스·로그를 변경하기 전에 runtime/transactions에 복구 정보를 기록합니다.
서버 시작 및 다음 저장 전에 미완료 트랜잭션을 복구합니다.
복구 대상에 외부 편집이 있으면 덮어쓰지 않고 Recovery conflict를 반환합니다.
파일의 최신 내용, journal의 before/after 해시와 저장하려던 내용을 비교해 수동 병합하세요.
병합 후 기존 journal은 백업하고, 일치하는 상태로 복구하거나 새로운 요청으로 저장합니다.
journal에는 위키 내용이 포함되므로 공개하지 않습니다.

동일 요청 ID의 같은 입력은 중복 저장하지 않습니다. 다른 입력으로 같은 ID를 재사용하면 오류입니다.
위키 저장·Git commit/merge·원문 snapshot 전환·색인 갱신은 공통 쓰기 잠금을 사용합니다.
S3 목록 조회/다운로드와 Git fetch/push는 각각 별도 동기화 잠금을 사용하므로 네트워크 대기 중
MCP·의미 검색의 시작과 위키 저장을 막지 않습니다. DB 초기화는 별도 schema 잠금으로 보호합니다.
외부 프로그램이 잠금을 따르지 않는 경우 OS 수준의 전역 편집 잠금이 되는 것은 아닙니다.
서버 checkout을 Obsidian이나 다른 Git 클라이언트로 동시에 직접 편집하지 마세요.

## Git 충돌

원격 merge 충돌은 abort하여 마지막 정상 서버 브랜치를 보존합니다.
동기화 작업자를 일시 중지하고 서버 브랜치에서 수동 merge·충돌 해결·commit한 뒤 재시작합니다.
push 실패 시 로컬 commit은 보존됩니다. 기본 브랜치에 강제 push하지 않습니다.
예상치 못한 로컬 변경은 Unowned checkout change로 중단되므로 어떤 작업자가 만든 것인지 확인하세요.

## 백업

1. MCP·sync·semantic 서비스를 중지하여 진행 중 작업을 마친다.
2. 서버 위키 checkout과 runtime 디렉터리 전체(DB의 WAL/SHM 포함), 모델 manifest를 함께 백업한다.
3. 비밀은 접근 제어된 별도 백업에 보관한다.
4. 원문은 기존 Remotely Save 저장 공간을 유지한다. 서버 원문 사본은 재다운로드 가능하다.
5. 서비스를 다시 시작한다.

복구 시 위키와 journal·요청 기록을 같은 백업 시점으로 복구합니다.
검색 DB만 재구축하려면 index 명령을 사용합니다. 요청·receipt 기록까지 담긴 DB를 임의로 삭제하지 않습니다.
소스 세대는 최신 세대 및 이전 세대를 일정 기간 유지하고, 성공한 동기화 후 오래된 서버 사본만 정리합니다.
`/sources/images/`와 `images.json`은 최신 이미지 사본과 목록이며 세대별 백업이 아닙니다.
원격 이미지 삭제는 정상 이미지 동기화 후 서버에도 반영됩니다. 이미지 과거 버전은 보존하지 않습니다.
PC 원문이나 S3 원본에 삭제 명령을 실행하지 않습니다.

## 제한

- 서버는 첨부 OCR·이미지 해석·HTML JavaScript를 실행하지 않습니다. `read_image`가 전달한 이미지의 해석은 클라이언트 AI가 수행합니다.
- get_context는 운영 규칙을 제공하지만 임의 클라이언트가 이를 따르는지 강제하지 못합니다.
- lint는 구조 검사이며 의미 충돌·사실 정확성·승인 여부는 사람이 또는 에이전트가 근거를 확인해야 합니다.
- S3 요청 실패 시 마지막 정상 사본을 유지합니다. 경로·암호화 설정 변경은 자동 감지하지 못하므로 운영자가 worker를 중단하고 확인해야 합니다.
