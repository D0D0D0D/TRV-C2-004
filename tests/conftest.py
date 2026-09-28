"""実 LLM 1回転チェック用の repo 固有 fixture。"""

import pytest

from src.services.kb_service import KbService
from tests.fakes import FakeVectorStore


@pytest.fixture
def agent_config() -> dict:
    # tests/integration/test_agent_modes.py:62-68 の generate happy path と同じ KB 構築。
    return {"kb_service": KbService(vector_store=FakeVectorStore())}


@pytest.fixture
def case_payload() -> dict:
    # tests/integration/test_agent_modes.py:62-68 の generate happy path と同じ入力。
    return {
        "mode": "generate",
        "tenant_id": "t1",
        "request_params": {"facility_text": "海辺の温泉旅館", "season": "夏"},
    }


@pytest.fixture
def live_input_context() -> dict:
    # tests/integration/test_agent_modes.py:62-68 は input_context を渡さないため空 dict に合わせる。
    return {}
