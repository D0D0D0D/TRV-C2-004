# TRV-C2-004 — test fakes (BaseLLM / BaseVectorStore / BaseDatabase seams).
# Clearly-labelled test doubles — no real credentials, no network.

from __future__ import annotations

import json
from typing import Any, Iterator

from shared.services.llm.base_llm import BaseLLM
from shared.services.vector_store.base_vector_store import BaseVectorStore


class FakeLLM(BaseLLM):
    """Deterministic LLM double. Varies generated titles when revision
    instructions are present so HITL loop tests can assert the reflection."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def bind_tools(self, tools: list) -> "BaseLLM":
        return self

    def stream(self, messages: list) -> Iterator[str]:
        yield self.complete(messages)["content"]

    def complete(self, messages: list) -> dict:
        prompt = "\n".join(m.get("content", "") for m in messages)
        self.calls.append(prompt)
        if "[TASK:profile]" in prompt:
            return {"content": json.dumps({
                "summary": "海辺の温泉旅館", "strengths": ["オーシャンビュー"],
                "concept": "静かな大人の宿", "room_types": ["和室", "露天風呂付き"],
            }, ensure_ascii=False)}
        if "[TASK:generate]" in prompt:
            revised = "revision_requests\": []" not in prompt and "revision_requests" in prompt \
                and '"revision_requests": []' not in prompt
            suffix = "（修正反映版）" if revised else ""
            price = "20000-30000" if '"price_stats": null' not in prompt else None
            plans = [{
                "title": f"テストプラン{i}{suffix}",
                "pitch_text": f"訴求文{i}",
                "experience_text": "露天風呂と海の幸",
                "included": ["夕食", "温泉"],
                "target_segment": "カップル",
                "season": "夏",
                "theme": "記念日",
                "price_range": price,
            } for i in (1, 2)]
            return {"content": json.dumps(plans, ensure_ascii=False)}
        if "[TASK:refine]" in prompt:
            start = prompt.find("[DRAFTS]")
            return {"content": prompt[start + len("[DRAFTS]"):].strip()}
        if "[TASK:review_intent]" in prompt:
            feedback = prompt.split("[FEEDBACK]")[-1]
            approve_cues = ("いいよ", "いいね", "OK", "オッケー", "それで行", "問題ない", "良い")
            revise_cues = ("直して", "変えて", "修正", "もっと", "やり直")
            if any(c in feedback for c in revise_cues):
                return {"content": '{"intent": "revise"}'}
            if any(c in feedback for c in approve_cues):
                return {"content": '{"intent": "approve"}'}
            return {"content": '{"intent": "revise"}'}
        return {"content": "{}"}


class FakeVectorStore(BaseVectorStore):
    """In-memory vector store double: term-overlap scoring, records upserts."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.upsert_calls: list[list] = []

    def upsert(self, documents: list) -> None:
        self.upsert_calls.append(documents)
        for d in documents:
            self.docs[d["id"]] = d

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        terms = set(str(query).split())
        scored = []
        for d in self.docs.values():
            score = sum(1 for t in terms if t and t in d.get("text", ""))
            scored.append({"id": d["id"], "score": score,
                           "text": d.get("text"), "metadata": d.get("metadata", {})})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def delete(self, ids: list) -> None:
        for i in ids:
            self.docs.pop(i, None)


class RecordingDatabase:
    """BaseDatabase-shaped double for the SQL-path unit test.

    Records every query; fetch_one/fetch_many return preset responses.
    (Duck-typed — KbService only needs execute/fetch_one/fetch_many.)
    """

    def __init__(self) -> None:
        self.queries: list[tuple[str, Any]] = []
        self.fetch_one_returns: list[Any] = []

    def execute(self, query: str, params: dict | None = None) -> Any:
        self.queries.append((query.strip().split()[0].upper(), params))
        return None

    def fetch_one(self, query: str, params: dict | None = None) -> Any:
        self.queries.append(("SELECT_ONE", params))
        return self.fetch_one_returns.pop(0) if self.fetch_one_returns else None

    def fetch_many(self, query: str, params: dict | None = None) -> list[dict]:
        self.queries.append(("SELECT_MANY", params))
        return []
