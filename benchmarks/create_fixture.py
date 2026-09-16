"""Create synthetic vaults for reproducible tests. Refuse to overwrite any existing directory."""

import json
import sys
from pathlib import Path

TOPICS = [
    (
        "retry",
        "재시도 정책",
        "서버 오류가 발생하면 지수 백오프와 무작위 지연으로 요청을 재전송한다.",
        "실패한 API를 다시 호출할 때 간격을 어떻게 정하지?",
    ),
    (
        "database",
        "데이터베이스 마이그레이션",
        "스키마 변경은 확장, 데이터 이행, 이전 필드 제거 순서로 적용한다.",
        "서비스를 멈추지 않고 테이블 구조를 바꾸는 방법",
    ),
    (
        "cache",
        "캐시 무효화",
        "원본 데이터가 갱신되면 관련 캐시 키를 삭제하고 TTL을 짧게 설정한다.",
        "저장한 값이 바뀌었는데 화면에는 예전 데이터가 나온다",
    ),
    (
        "incident",
        "장애 대응",
        "장애 발생 시 영향 범위를 확인하고 최근 배포를 롤백한 뒤 원인을 분석한다.",
        "운영 서비스가 중단되면 가장 먼저 해야 할 일",
    ),
    (
        "review",
        "코드 리뷰 기준",
        "리뷰어는 동작의 정확성, 오류 처리, 가독성, 필요한 테스트를 확인한다.",
        "동료가 작성한 소스 변경을 검토하는 기준",
    ),
    (
        "scope",
        "프로젝트 범위",
        "MVP는 핵심 사용자 문제를 해결하는 최소 기능으로 정하고 추가 요구는 백로그로 관리한다.",
        "첫 출시에서 기능이 계속 늘어나는 것을 막으려면",
    ),
    (
        "decision",
        "의사결정 기록",
        "결정 문서에는 대안, 선택 이유, 사람 결정권자, 확인 근거를 남긴다.",
        "왜 이 방식을 선택했는지 나중에 추적하고 싶다",
    ),
    (
        "handoff",
        "업무 인수인계",
        "담당자 교체 시 현재 상태, 미완료 작업, 위험, 다음 액션과 근거 문서를 연결한다.",
        "다른 사람이 내 프로젝트를 이어서 진행하게 하려면",
    ),
    (
        "meeting",
        "회의 운영",
        "회의 전에 목적과 안건을 공유하고 종료 시 결정과 담당자별 액션을 정리한다.",
        "팀 회의를 짧고 효과적으로 진행하는 방법",
    ),
    (
        "priority",
        "업무 우선순위",
        "사용자 영향, 긴급성, 투입 비용과 의존성을 비교해 작업 순서를 정한다.",
        "해야 할 일이 많을 때 무엇부터 처리할까",
    ),
    (
        "experiment",
        "실험 설계",
        "대조군과 실험군을 무작위로 나누고 사전 정의한 지표로 성과를 비교한다.",
        "새 기능의 효과를 객관적으로 확인하려면",
    ),
    (
        "frequency",
        "광고 프리퀀시",
        "광고 노출 빈도가 높아질 때 앱 전환율과 피로도를 함께 측정한다.",
        "같은 광고를 반복해서 보여주면 성과가 어떻게 달라질까",
    ),
    (
        "accessibility",
        "접근성 디자인",
        "키보드 탐색, 명확한 포커스, 대체 텍스트와 충분한 명암 대비를 제공한다.",
        "화면을 보기 어렵거나 마우스를 쓰지 못하는 사용자를 위한 UI",
    ),
    (
        "backup",
        "백업 복구",
        "백업 주기와 보존 기간을 정하고 실제 복원 훈련으로 데이터를 검증한다.",
        "파일을 잃어버려도 업무를 다시 시작할 수 있게 준비하기",
    ),
    (
        "secret",
        "인증정보 관리",
        "비밀번호와 API 키는 비밀 저장소에 두고 로그와 Git에 기록하지 않는다.",
        "접속 자격증명이 코드 저장소에 올라가는 것을 막는 방법",
    ),
    (
        "search",
        "검색 품질 평가",
        "실제 업무 질문과 정답 문서를 모아 recall@10 및 지연시간을 측정한다.",
        "지식 검색이 필요한 문서를 잘 찾는지 평가하려면",
    ),
    (
        "source",
        "원문 보존",
        "출처 노트는 읽기 전용으로 유지하고 가공한 요약은 별도 위키에 기록한다.",
        "내가 작성한 원본 메모를 AI가 덮어쓰지 않게 하려면",
    ),
    (
        "conflict",
        "동시 수정 충돌",
        "저장 직전 문서 해시를 비교하고 변경된 경우 최신 내용을 다시 읽어 병합한다.",
        "두 에이전트가 같은 문서를 동시에 고칠 때 보호하는 방법",
    ),
    (
        "budget",
        "예산 추정",
        "필수 비용과 변동 비용을 구분하고 불확실한 항목에는 예비비와 가정을 기록한다.",
        "신규 프로젝트에 필요한 돈을 어떻게 계산할까",
    ),
    (
        "retrospective",
        "회고",
        "완료한 작업에서 효과적인 방식과 실패 원인을 정리하고 다음 개선 실험을 정한다.",
        "일이 끝난 뒤 같은 실수를 반복하지 않기 위한 활동",
    ),
]


def create(root):
    root.mkdir(parents=True, exist_ok=False)
    wiki, source = root / "AI-Agent-Wiki", root / "zettelkasten"
    wiki.mkdir()
    source.mkdir()
    (wiki / "wiki.config.json").write_text(
        json.dumps(
            {
                "source_root": "../zettelkasten",
                "attachments_dir": "_Attachments",
                "exclude": ["**/.*", "_Template", "_CustomJS"],
            }
        ),
        encoding="utf-8",
    )
    for name in (
        "AGENTS.md",
        "AI-Sessions/index.md",
        "AI-Sessions/log.md",
        "AI-Sessions/wiki/concepts/wiki-operations.md",
    ):
        file = wiki / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# 합성 검증 문서\n원문 읽기 전용.\n", encoding="utf-8")
    questions = []
    for slug, title, body, query in TOPICS:
        path = f"{slug}.md"
        (source / path).write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        questions.append({"query": query, "expected": ["source:" + path]})
    for i in range(40):
        (source / f"distractor-{i:02}.md").write_text(
            f"# 합성 방해 문서 {i}\n\n분류번호 {i}. 이 문서는 해양 생물과 계절별 기온 관측 자료이다.\n",
            encoding="utf-8",
        )
    (root / "questions.json").write_text(
        json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "config.toml").write_text(
        'wiki_root = "AI-Agent-Wiki"\ndata_dir = "runtime"\n'
        '[semantic]\nenabled = true\nurl = "http://127.0.0.1:18766"\n'
        'port = 18766\nmodel_dir = "../models/e5-small"\nthreads = 2\n',
        encoding="utf-8",
    )
    print(root.resolve())


if __name__ == "__main__":
    create(Path(sys.argv[1]))
