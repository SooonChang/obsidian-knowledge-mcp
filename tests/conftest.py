import json

import pytest

from knowledge_mcp.config import Settings
from knowledge_mcp.storage import Store


@pytest.fixture
def store(tmp_path):
    wiki, source = tmp_path / "AI-Agent-Wiki", tmp_path / "zettelkasten"
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
    for name in ("index.md", "log.md"):
        file = wiki / "AI-Sessions" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# 지도\n" if name == "index.md" else "# 로그\n", encoding="utf-8")
    for name in ("AGENTS.md", "CLAUDE.md", "AI-Sessions/wiki/concepts/wiki-operations.md"):
        file = wiki / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# 운영 규칙\n원문은 읽기 전용입니다.\n", encoding="utf-8")
    (source / "실험.md").write_text("# 프리퀀시 실험\n\n노출 빈도와 앱 전환율, US 분석.\n", encoding="utf-8")
    settings = Settings(wiki_root=wiki, data_dir=tmp_path / "data")
    return Store(settings)


@pytest.fixture
def request_data():
    return {
        "request_id": "request_001",
        "changes": [
            {
                "path": "AI-Sessions/wiki/concepts/frequency.md",
                "metadata": {"type": "concept", "status": "active"},
                "body": "# 프리퀀시\n\n노출 빈도에 따른 성과를 비교한다.",
            }
        ],
        "save_filter": [1, 2],
        "reason": "향후 실험 설계에서 재사용한다.",
        "evidence": [
            {
                "kind": "conversation",
                "reference": "2026-09-15 사용자 대화",
                "summary": "사용자가 설명한 실험 분석 기준이며 운영 승인은 미확인.",
            }
        ],
    }
