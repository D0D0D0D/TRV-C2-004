# Test Specification — TRV-C2-004 Accommodation Plan Generation & Branding Agent

## Test Strategy
- Coverage target: 主要ビジネスロジック（kb_service / 全ノード / 3 モード E2E / HITL ループ）100% 実行
- Test types: Unit（ノード・サービス単体）/ Integration（外側グラフ 3 モード E2E + HITL round-trip）/ Proof-of-Boundary
- LLM / Vector / DB はすべて注入シーム経由の fake（`tests/fakes.py`）— 外部インフラ・実クレデンシャル不要

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | Type check pass, no Pydantic/dataclass | ✅ `test_state_safety.py`（AST 検査）+ State は primitives のみ |
| TC-02 | SecurityViolationError fires on invalid input | Error raised | ✅ `test_nodes.py::test_s2_size_cap_raises_security_violation` |
| TC-03 | No JWT/Credential in State | CI `gate-credential-scan`: 0 violations (S-5 enforcement moved to CI by (internal issue reference removed)) | ✅ CI PASS + `test_pb_hitl_checkpoint.py::test_no_credential_patterns_in_checkpoint` |
| TC-04 | InvocationContext via configurable only | Direct access raises error | ✅ State に InvocationContext を格納しない（PB-2/PB-5 検査でオブジェクト混入なし） |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start` / `node_complete` / `node_error` absent from `execute()` body | ✅ 0 duplicates（全 execute はドメインイベントのみ発火） |
| TC-06 | S-2: `_security_gate_input()` not overridden (`FunctionNode` subclass) | `TypeError` raised at class definition if overridden (`@final` enforced by framework) | ✅ 0 overrides（拡張は `_extra_security_gate_input` のみ） |
| TC-07 | S-3: `_security_gate_output()` not overridden (`FunctionNode` subclass) | `TypeError` raised at class definition if overridden (`@final` enforced by framework) | ✅ 0 overrides（拡張は `_extra_security_gate_output` のみ） |
| TC-08 | `required_trust_level` enforced | Insufficient trust → refused | ✅ `test_nodes.py::TestTrustLevels`（全ノード VERIFIED_EXTERNAL 明示宣言）+ PB-6 |
| TC-09 | S-2: `_extra_security_gate_input()` non-trivial when domain checks needed | Domain-specific input checks execute correctly | ✅ PreProcessNode: リクエストサイズ上限（512KB / CSV 5MB） |
| TC-10 | S-3: `_extra_security_gate_output()` non-trivial when domain checks needed | Domain-specific output checks execute correctly | ✅ RefinementNode: NG 表現 content filtering / PostProcessNode: tenant_id 漏洩検査 |
| TC-11 | S-4: at least one domain `emit_trace_event()` inside each `execute()` | Domain event emitted on every invocation path | ✅ ≥1 per node（input_validated / facility_profiled / plans_retrieved / plans_generated / plan_review_* / kb_ingested / kb_feedback_appended 等） |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-1 | BaseNode → EventEmitter | `emit_trace_event()` fires on every invocation path | No silent failures | ✅ PB-6 spy で node_start/complete 発火確認 |
| PB-2 | State serialization | Post-invoke State is primitives only | No Pydantic/dataclass | ✅ `test_pb_hitl_checkpoint.py::test_suspended_checkpoint_is_primitives_only` |
| PB-3 | L1 → External service | KbService（DB/Vector 注入シーム）経由の実データ取得 | Data retrieved | ✅ `test_kb_service.py`（fake 注入で SQL 経路 / vector 経路とも検証。実インフラは STG で確認） |
| PB-4 | Import isolation | No Level 0 imports | AST scan: 0 violations | ✅ `test_import_isolation.py` |
| PB-5 | Checkpoint safety | No JWT/Pydantic in checkpoint | Inspection pass | ✅ `test_pb_hitl_checkpoint.py::TestPB5CheckpointSafety`（suspend 中 checkpoint を実検査） |
| PB-6 | Invoke execution order | `__call__()`: S-1 trust gate → S-4 `node_start` → S-2 `_security_gate_input` → `execute()` → S-3 `_security_gate_output` → S-4 `node_complete` | Order verified | ✅ `test_pb_invoke_order.py`（src/nodes 全ノード自動発見） |
| PB-7 | HITL interrupt propagation | `interrupt()` inside `execute()` → `GraphInterrupt` propagates through `BaseNode.__call__()` + GraphNode boundary, NOT caught by error boundary | status = awaiting_human（error にならない）+ **resume → success 完走** | ✅ `test_pb_hitl_checkpoint.py::TestPB7InterruptPropagation`（suspend 確認だけでなく resume→承認完了・多周回終了まで assert — TourPlanningProcurementRoutingAgent 既知事象対策） |

## Business Logic Tests

| TC-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| BL-01 | ingest: PMS CSV 一括投入（日本語列名エイリアス） | プラン名/季節/価格帯/ターゲット CSV 2 行 | inserted=2、KB スキーマへマッピング | ✅ `test_agent_modes.py::TestIngestMode` |
| BL-02 | ingest: 冪等再投入 | 同一 CSV を 2 回 | 2 回目は updated=2 / inserted=0（重複なし） | ✅ `test_reingest_is_idempotent` |
| BL-03 | generate: HITL suspend → approve | 施設テキスト + 季節 | awaiting_human → resume("approve") → success + plans | ✅ `test_hitl_suspends_then_approves` |
| BL-04 | generate: HITL 修正指示ループ | resume({"instructions": ...}) | 再 suspend → 修正反映プランで approve → revision_rounds=1 | ✅ `test_hitl_revision_loop_reflects_instructions` |
| BL-05 | generate: 価格は過去実績からのみ | 実績なしテナント / 実績ありテナント | 実績なし→price_range=null+「価格未提示」注記 / 実績あり→price_range 提示 | ✅ `test_price_null_without_history_and_present_with` |
| BL-06 | feedback: 採用プラン再蓄積 | adopted_plan + original_draft（差分あり） | adopted_flag=True + operator_edited=True | ✅ `test_adoption_updates_kb` |
| BL-07 | KB: テナント分離 | t1/t2 に別プラン投入後 t1 検索 | 他テナントのプランがヒットしない | ✅ `test_kb_service.py::test_tenant_isolation` |
| BL-08 | KB: 実績メタ更新は再埋め込みしない | 本文同一の feedback / 本文変更の feedback | reindexed=False / True | ✅ `test_reindex_only_when_text_changed` |
| BL-09 | 異常系: tenant_id 欠落 / 未知 mode / 不正 CSV | 各異常入力 | status=error + error_log（例外で落ちない） | ✅ `TestErrorPaths` + unit 各異常系 |
| BL-10 | Refinement は素材を失わない | LLM が非 JSON を返す | 推敲前ドラフトへフォールバック | ✅ `test_refinement_never_loses_drafts` |
| BL-11 | 価格フィルタは厳密（他季節/他ターゲット実績で価格を出さない） | 冬実績のみ→夏リクエスト 等 | sample_size=0 / price_range=null / 「価格未提示」注記 | ✅ strict filter 単体 2 件 + 統合 `test_price_null_when_history_is_other_season_only`（レビュー指摘 F-01 回帰） |
| BL-12 | S-3: tenant_id のプラン文への漏洩遮断 | tenant_id を含むプラン文 | RuntimeError で遮断・クリーン時は内部マーカー除去 | ✅ `test_s3_tenant_leak_blocked` / `test_s3_tenant_marker_stripped_on_clean_output`（レビュー指摘 F-02 回帰） |
| BL-13 | facility_url 取得（S-2 SSRF ガード） | loopback/private/link-local/非標準ポート/ftp/file/userinfo URL・混在解決・**DNS rebinding**・リダイレクト・fetch 失敗 | 全拒否（UrlFetchError）・**接続は検証済み IP に固定**（再解決なし）・ノードは status=error でクリーン化・facility_text 優先 | ✅ `test_url_fetcher.py` 14 ケース + `test_facility_profile_*` 4 件 |
| BL-14 | HITL 承認判定 2 段階 | "承認"（fast-path）/「いいよ、それで行こう」（LLM 分類）/ LLM 不在・故障（安全側） | approve / approve / revise | ✅ `TestPlanReviewIntent` 6 件 + 統合 `test_natural_language_approval` |
| BL-15 | LLM フォールバック廃止（fail-fast） | provider 未設定 / "static" / 不明値 | 起動時 ConfigError | ✅ `TestBuildLlm` |
| BL-16 | generate: provider が LLM 呼び出しを拒否（content filter / rate limit / 5xx） | `FacilityProfileNode` に例外を投げる `BaseLLM` | `status: error` + `error_log` にノード名・例外型（`error_log: None` の不透明エラーにしない） | ✅ `test_nodes.py::TestLlmCallFailure::test_facility_profile_reports_the_provider_failure` |
| BL-17 | 同上（プラン生成段） | `PlanGenerationNode` に例外を投げる `BaseLLM` | `status: error` + `error_log` にノード名 | ✅ `::test_plan_generation_reports_the_provider_failure` |
| BL-18 | 同上（推敲段） | `RefinementNode` に例外を投げる `BaseLLM` | 推敲は付加価値のため縮退: 未推敲 drafts を保って `success`（設計「refinement must never lose the drafts」と同じ扱い） | ✅ `::test_refinement_keeps_the_drafts_on_provider_failure` |
| BL-19 | provider メッセージの非保存 | 要求本文を反映した拒否メッセージ | `error_log` に provider メッセージの断片を一切残さず、allow-list カテゴリ（`content_filtered` 等）＋例外型＋HTTP status のみ | ✅ `::test_error_log_carries_no_part_of_the_provider_message` / `::test_provider_failures_map_to_allow_listed_categories`（4 分類）/ `::test_an_http_status_is_reported_when_the_provider_gives_one` |
| BL-20 | **呼び出し側が理由を受け取る**（#13 の症状そのもの） | end-to-end invoke で FacilityProfile の LLM が例外 | `status: error` かつ **`error_log` が caller に届く**（ノード名 + カテゴリ）・traceback なし・重複なし・provider 本文なし | ✅ `test_agent_modes.py::TestProviderFailureReachesTheCaller`（4 件）|
| BL-21 | 失敗経路の S-4 監査 | 同上 | `facility_profile_llm_failed` が発火し、payload に provider 本文を含まない | ✅ `::test_the_failure_is_audited` |
| BL-22 | 制御フロー例外を飲まない | LLM が `GraphInterrupt` / `SecurityViolationError` を送出 | そのまま再送出（framework が処理する） | ✅ `test_nodes.py::TestLlmCallFailure::test_control_flow_exceptions_are_not_swallowed` |

## push 前レビュー（実ファイル・2026-07-02）

F-01（High: price_stats の全履歴フォールバック=価格ポリシー違反）/ F-02（Medium: S-3 tenant 検査が不発）/
F-03（Medium: 回帰テスト不足）→ 3 件とも修正し、再検証で **全件 RESOLVED**（副作用なし・新規指摘なし）。

追加差分（HITL 2 段階判定 / LLM fail-fast / facility_url）の 2 回目レビューでは
F-01（High: **DNS rebinding** — 検証時と接続時の二重 DNS 解決）を検出 →
接続を検証済み IP に固定（Host ヘッダ/TLS SNI は元ホスト名を維持）する方式へ書き直して解消。

## Test Execution Summary
- Execution date: 2026-07-02
- Total tests: 98
- Pass: 98 / Fail: 0 / Skip: 0
- ruff check src/ tests/: All checks passed
- 実行環境: Python 3.11.13 / agenticstar-agentcore==1.0.0（released wheel）

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | ローカル CLI（Azure OpenAI `gpt-5.5`）を `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 3 回 |
| 出力 | ['reason', 'revision_round'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（ローカル CLI と鍵が要るため）。

