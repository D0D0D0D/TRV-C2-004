# Template Design Specification — TRV-C2-004 Accommodation Plan Generation & Branding Agent

## Position in AgentCore Architecture

- **Agent Class**: AccommodationPlanBrandingAgent
- **L1 Base**: AgentBaseGraph（Cat 2 — main スロットに `GraphNode` を置き、内側 domain workflow を包む）
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible)
  - Node: L1 inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` for node substitution)

## Architecture Overview

AGENTIC STAR Marketplace は repo 直下の `cli.py` を専用 entry adapter として使用する。adapter は入力を
`generate` とデプロイ単位の `ASTAR_TENANT_ID` に固定し、書込 payload を除去したうえで read-only KB を注入する。

### 動作モード（3 モード・蓄積型）

過去プラン Vector KB は本 AG の moat の核であり、KB 運用ワークフロー（取込 / 検索 / 注入 / 再蓄積）を
AG が内包する。リクエストの `mode` で内側ワークフローを分岐する。

| mode | 用途 | 主要処理 |
|------|------|---------|
| `ingest` | 初期データ投入 | PMS／予約システムから CSV エクスポートしたプラン一覧を KB スキーマへマッピングし一括投入（初日から自社プラン資産で生成可能に） |
| `generate`（既定） | プラン企画生成 | 施設プロファイル化 → コンテキスト付与 → 過去プラン RAG → 複数プラン案生成 → 推敲 |
| `feedback` | 採用プラン再蓄積 | 人間レビューで採用（必要なら編集）されたプランを KB へ再蓄積（`adopted_flag` 付与）。フィードバックループで生成精度が育つ |

> **HITL（採用・2026-07-02 人間判断）**: `generate` モードは**グラフ内 HITL（D6 `interrupt()`）による
> 対話的改善ループ**を持つ。プラン案生成・推敲後に PlanReviewNode が `interrupt()` でドラフトを提示し、
> 人間の修正指示（「ああして・こうして」）を受けて PlanGeneration へ戻り再生成、承認で完了する。
> `config/config.yaml` に `hitl.enabled: true` + `memory_enabled: true` + `max_hitl: 8` を設定
> （checkpointer は pause/resume に必須 — ADR-016 D5）。
> `feedback` モードは HITL とは別に残す — セッション内承認とは非同期に、実掲載後の採用実績を
> KB へ再蓄積する導線（数週間後の採用確定などセッション外のイベント用）。

### Node Configuration（外側 backbone — 固定 5 ノード）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema_version / session_id / trust_level 設定 | — | 初期化済み State | InitializeNode (default) |
| pre_process | 入力検証（mode 判定・施設情報/CSV ペイロードの構造検証・入力サイズ上限）・`request_params` 抽出 | `user_input` | `request_params`, `mode`, `status` | FunctionNode 継承 `PreProcessNode` |
| main | 内側 domain workflow の実行（mode 別 3 系統） | `request_params`, `mode` | `plan_drafts` / `ingest_result` / `feedback_result`, `status` | **GraphNode** 継承 `PlanBrandingWorkflowNode`（内側 `src/graph/domain_workflow_graph.py`） |
| post_process | 出力整形（構造化プラン企画ドラフト JSON/Markdown）・S-3 domain チェック | main の出力 | `result`, `status` | FunctionNode 継承 `PostProcessNode` |
| finalize | 出力射影（`result` → `output`） | `result` | `output` | FinalizeNode (default) |

### 内側 domain workflow（`generate` モード・5 ノード）

| Inner Node | Responsibility | 出力 |
|------------|---------------|------|
| ① FacilityProfileNode | 施設情報を解析し施設プロファイル（特徴/強み/コンセプト/客室タイプ）を構造化（LLM）。`facility_text` 優先・無ければ `facility_url` を **SSRF ガード付き**で取得（`src/services/url_fetcher.py`: http/https・標準ポートのみ・プライベート IP/リダイレクト拒否・512KB 上限） | `facility_profile` |
| ② ContextEnrichmentNode | 季節・流行・ターゲット層のコンテキスト付与（入力 or 暦から導出。外部トレンド API はスコープ外・任意拡張） | `planning_context` |
| ③ PastPlanRetrievalNode | 過去プラン Vector index を意味検索＋メタフィルタ（season / target_segment / price_band）のハイブリッド検索（RAG）。あわせて **DB から同季節×同ターゲットの price_band 実績集計**を取得（価格案の母集団） | `reference_plans`（類似成功プラン上位 N 件）＋ `price_stats`（実績価格分布） |
| ④ PlanGenerationNode | 施設プロファイル＋コンテキスト＋参照プランを踏まえ、プラン案を複数生成（LLM）（タイトル/訴求文/含まれる体験/想定価格レンジ）。**価格レンジは過去プラン実績メタ（`price_band`・同季節/同ターゲットの参照プラン集計）から導出できる場合のみ提示**し、実績が無ければ「価格未提示」とする（外部市況データによる価格提案はスコープ外・2026-07-02 人間判断）。HITL 2 周目以降は `revision_requests`（人間の修正指示）を反映して再生成 | `plan_candidates` |
| ⑤ RefinementNode | 誤字脱字・言葉遣い・トンマナ・ブランド整合の推敲（LLM）＋ S-3 domain チェック。**LLM が応答しない／落ちた場合は未推敲の `plan_candidates` をそのまま `plan_drafts` とする**（推敲は付加価値・DDR #13） | `plan_drafts` |
| ⑥ PlanReviewNode | **HITL（D6）**: ドラフト（推敲済み、または縮退時は未推敲）を `interrupt({"drafts": ..., "reason": "human review"})` で提示。人間の応答が修正指示なら `revision_requests` に格納して④へループ、承認なら完了へ。`hitl_draft` に提示済みドラフトを保存（resume 時の LLM 再実行防止） | `revision_requests` / 承認済み `plan_drafts` |

### 内側 domain workflow（`ingest` モード・3 ノード）

| Inner Node | Responsibility | Input（内側 State） | Output（内側 State） |
|------------|---------------|----------------------|----------------------|
| ① CsvParseNode | CSV ペイロードのパース・行単位バリデーション（必須列: プランタイトル/訴求文。不正行はスキップし理由を記録） | `request_params.csv_payload` | `parsed_rows`, `skipped_rows` |
| ② SchemaMapNode | パース済み行を KB スキーマ（埋め込み自由文＋メタデータ）へマッピング。`season`/`target_segment` 等は列マッピング設定＋LLM 補完（列欠損時） | `parsed_rows` | `kb_records` |
| ③ KbBulkUpsertNode | `tenant_id` を付与し kb_service 経由で **DB へ冪等 upsert（`plan_key`）→ Vector index を同期**（2 層書込みは kb_service が担う） | `kb_records`, `tenant_id` | `ingest_result`（投入/スキップ件数・エラー一覧） |

### 内側 domain workflow（`feedback` モード・2 ノード）

| Inner Node | Responsibility | Input（内側 State） | Output（内側 State） |
|------------|---------------|----------------------|----------------------|
| ① PlanNormalizeNode | 採用プラン（人間が編集した最終版）を KB スキーマへ正規化。元ドラフトとの差分から `operator_edited` を判定 | `request_params.adopted_plan`（＋任意 `request_params.original_draft`） | `kb_records`（1 件・`adopted_flag=true`） |
| ② KbAppendNode | `tenant_id` を付与し kb_service 経由で **DB へ upsert（既存 `plan_key` なら `adopted_flag`/`operator_edited` を UPDATE）→ 本文が変わった場合のみ Vector 再埋め込み** | `kb_records`, `tenant_id` | `feedback_result`（蓄積したプラン ID・メタ） |

3 系統は 1 つの内側グラフ（`PlanBrandingWorkflowGraph`）の entry 直後の conditional edge で分岐する。
KB への書込（KbBulkUpsert / KbAppend）と検索（PastPlanRetrieval）はいずれも
`src/services/kb_service.py` の共通接続シームを経由し、スキーマ定義を 1 箇所に保つ。

> **実装注意（既知事象）**: 内側グラフの分岐関数・`get_output` の state 注釈は
> **自 State schema**（内側 State クラス）を使うこと。`AgentState` 注釈だと LangGraph が
> `AgentState` に射影し `mode` 等のカスタム欄が消えて常に既定分岐へ落ちる
> （Business Travel Hotel & Plan Recommendation Agent で実機確認済みの事象）。

### Data Flow

```
（外側 backbone — AgentBaseGraph 固定パイプライン）
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                     ↓ (retry, max 3)
                                  pre_process

（内側 domain workflow — main の GraphNode 内・mode 分岐）
                    ┌─ generate ─→ ①FacilityProfile → ②ContextEnrichment
                    │                → ③PastPlanRetrieval(RAG) → ④PlanGeneration
                    │                → ⑤Refinement → ⑥PlanReview(HITL interrupt)
entry ─ {mode 分岐} ─┤                     ↑（修正指示: revision_requests）│
                    │                     └──── ④へループ ←── 修正指示 ──┤（承認）
                    ├─ ingest ───→ CsvParse → SchemaMap → KbBulkUpsert ──┤→ END
                    └─ feedback ─→ PlanNormalize → KbAppend ─────────────┘
```

**HITL 実装注意（実機確認済みの既知事象・MR-B で必ず踏襲）:**
- inner / outer の**両方に明示 `compile(checkpointer=...)`** が必要（1.0.0 wheel は config から
  checkpointer を自動生成しない。無いと `interrupt()` で RuntimeError）
- `GraphNode.get_subgraph()` は **inner graph をキャッシュ**する（毎回新 inner + 新 checkpointer を
  返すと resume が suspend チェックポイントを失い最初から再実行 → 無限 interrupt ループ）
- `hitl_count` はテンプレから**書き込まない**（backbone 専用。Cat 2 に max_hitl breaker は無い —
  `AgentBaseGraph` は max_hitl の値検証のみ）
- `GraphNode` の merge_output に `hitl_draft` / `hitl_feedback` / `hitl_status` を含める
- PB-7（`interrupt()` → `GraphInterrupt` が error boundary に握り潰されず伝播）をテストに追加
- resume → 承認完了（`status: success` 到達）までを通しで assert するテストを置く
  （suspend 確認だけでは resume ループ破綻を捕捉できない）

### State Definition（外側 State — flat TypedDict / AgentState 継承）

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `ingest` / `generate` / `feedback`（既定 `generate`） | ✅ |
| `request_params` | `dict` | 検証済み入力（施設情報テキスト・企画意図・CSV ペイロード等） | ✅ |
| `tenant_id` | `str` | 施設テナント ID（KB 分離キー。分離機構は FW 側） | ✅ |
| `facility_profile` | `dict` | 施設プロファイル（内側 → 外側へ引き上げ） | generate 時 |
| `plan_drafts` | `list[dict]` | プラン企画ドラフト（推敲済み。LLM 縮退時は未推敲の生成結果）（タイトル/訴求文/体験/価格レンジ） | generate 時 |
| `ingest_result` | `dict` | 投入件数・スキップ件数・バリデーションエラー一覧 | ingest 時 |
| `feedback_result` | `dict` | 再蓄積したプラン ID・メタ | feedback 時 |
| `revision_requests` | `list[str]` | HITL レビューでの人間の修正指示履歴（再生成プロンプトに反映） | generate 時 |
| `result` | `dict` | post_process の最終整形出力 | ✅ |

HITL 関連の `hitl_draft` / `hitl_feedback` / `hitl_status` 等はスキーマ 1.4 の framework 定義欄を
そのまま使う（テンプレで再宣言しない。`hitl_count` / `hitl_allowed` へは書き込まない）。

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- InvocationContext via `config["configurable"]` only (not in State)
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)

### 過去プラン KB — 2 層ストア構成（ハイブリッド・2026-07-02 人間判断で確定）

**DB（`shared.services.database`）= system of record、Vector（`shared.services.vector_store`）= 派生インデックス**
の 2 層構成とする。書込みは必ず DB → Vector の一方向同期（書込み経路は ingest / feedback の 2 つのみ）。

**DB レコードスキーマ（system of record）**

| 区分 | フィールド | 用途 |
|---|---|---|
| キー | `plan_key`（PMS プラン ID があれば優先 / 無ければ タイトル＋season の自然キー） | 冪等 upsert・Vector 側との対応付け |
| 本文 | `title`, `pitch_text`（訴求文）, `experience_text`（含まれる体験） | 埋め込み原文・出力素材 |
| メタデータ | `season`, `target_segment`, `room_type`, `price_band`, `included`, `theme` | フィルタ・**価格実績の集計軸**（同季節×同ターゲットの price_band 分布 → 価格案導出） |
| 実績メタ（任意） | `sales_volume` / `occupancy` / `adopted_flag` / `operator_edited` | 成功プラン優先参照・改善シグナル。**採用確定時に後付け UPDATE**（feedback モード） |
| テナント | `tenant_id` | テナント分離（FW 機構を利用） |

**Vector インデックス**: 埋め込み対象 = `title`＋`pitch_text`＋`experience_text` の連結自由文。
payload に `plan_key` / `tenant_id` / 主要メタ（season, target_segment, price_band）を持たせ、
意味検索＋メタフィルタのハイブリッド検索に使う。実績メタの更新は DB のみ（Vector は再埋め込み不要）。

**各層の役割分担**

| 操作 | DB | Vector |
|---|---|---|
| ingest（冪等 upsert） | ✅ 正として upsert | DB 確定後に index 追加/更新 |
| generate の類似プラン参照 | — | ✅ 意味検索＋メタフィルタ |
| generate の価格案導出 | ✅ 同季節×同ターゲットの price_band 集計（全実績が母集団） | — |
| feedback（adopted_flag / operator_edited 更新） | ✅ レコード UPDATE | 本文が編集された場合のみ再埋め込み |

- KB **運用ワークフロー**（取込/検索/注入/再蓄積/テナント分離キー付与/2 層同期）は本 AG が内包する。
- サービス層 `src/services/kb_service.py` に **DB・Vector 両方の接続シームと一方向同期ロジック**を置き、
  ノードは kb_service 経由でのみストアに触れる（スキーマ定義・同期規約を 1 箇所に保つ）。
  テストでは fake 注入で代替する（外部インフラ非依存でユニットテスト可能に）。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, permissions, credential handle)
- [ ] ConnectionPolicy (retry/timeout strategy) — 既定値で足りるため独自設定なし
- [x] SecurityViolationError
- [x] S-2: `_extra_security_gate_input()` — pre_process にて domain チェックを実装
      （入力サイズ上限・CSV ペイロード構造検証・URL 形式検証。既定の PII スキャンは
      framework `@final` ゲートが自動実行）
- [x] S-3: `_extra_security_gate_output()` — post_process にて domain チェックを実装
      （生成プラン文への機密情報・credential 混入検査／不適切表現チェック。
      既定の credential スキャンは framework `@final` ゲートが自動実行。
      **`_security_gate_output()` の直接 override は禁止** — `TypeError` at class definition）
- [x] S-4: `emit_trace_event()` — 全ノードの `execute()` 内で最低 1 回のドメインイベントを発火
      （`node_start` / `node_complete` / `node_error` は `__call__()` が自動発火するため重複させない）

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` subclass（PreProcessNode / PostProcessNode / 内側 5 ノード）→ framework `@final`
>   gate が自動実行。拡張は `_extra_security_gate_input()` / `_extra_security_gate_output()` のみ
> - `GraphNode`（main の PlanBrandingWorkflowNode）→ deliberate no-op（内側ノード各所の gate が適用済み）

### Composition Pattern

- **Pattern**: GraphNode (subgraph)
- **Composition target**: 内側 `PlanBrandingWorkflowGraph`（`src/graph/domain_workflow_graph.py`）
- **Error propagation strategy**: **propagate** — 内側ノードで `status="error"` になった場合、
  内側後続ノードは早期パススルー、`GraphNode.execute()` が外側 State に `status="error"` と
  `error_log` を伝播し、外側 route() が finalize へ回す。main / post_process は上流
  `status="error"` を必ずパススルーする（pre→main は無条件辺のため）。

### セキュリティ設計（S-1〜S-5）

| Layer | 設計 |
|---|---|
| S-1 | 全 FunctionNode に `required_trust_level = TrustLevel.VERIFIED_EXTERNAL` を明示宣言（施設固有の非公開データを扱う標準業務操作）。外側 graph レベルも同宣言 |
| S-2 | framework 既定 PII スキャン＋`_extra_security_gate_input()`（入力サイズ・CSV 構造・URL 検証） |
| S-3 | framework 既定 credential スキャン＋`_extra_security_gate_output()` を**意図的に 2 箇所へ多層配置**: ① RefinementNode（生成直後の NG 表現 content filtering）② PostProcessNode（最終境界での tenant_id 漏洩検査）。S-3 の本体は最終境界（PostProcess）にあり、Refinement 側は多層防御の追加層。推敲の品質処理自体は execute() 内で行い、S-3 ゲートは安全検査に限定 |
| S-4 | 全ノード `execute()` 内で `emit_trace_event()`（例: `facility_profiled` / `plans_retrieved` / `plans_generated` / `kb_ingested`） |
| S-5 | secrets は `ctx.secrets.require()` のみ（`os.environ` 禁止）。State/ソースに credential を置かない。依存はすべて `==` 完全ピン |

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定多段パイプライン（Cat 2）。自律ループ不要 |
| Composition pattern | Standalone（外側 3 スロットに直接実装） | GraphNode subgraph | **GraphNode subgraph** | 5 ステップ＋3 モード分岐の domain 複雑性を内側グラフに封じ、外側は固定 backbone を保つ（Cat 2 標準構成） |
| KB 運用の持ち方 | ユーザー任せ（接続口のみ） | AG が内包（3 モード） | **AG が内包** | KB こそ moat の核であり、ユーザー任せでは Tool 寄りに退化するため。 |
| プランレビュー | グラフ内 HITL（interrupt D6） | 外部レビュー運用のみ | **HITL 採用（＋feedback モード併用）**（2026-07-02 人間判断） | プラン提示後に人間が「ああして・こうして」と対話的に改善するのが実利用の中心動線。セッション外の採用実績再蓄積は feedback モードが担う |
| 外部トレンド取得 | 外部 API 連携 | 入力/暦からの導出のみ | **入力/暦導出のみ** | 外部依存を増やさない。トレンド API は任意拡張としてスコープ外 |
| 価格レンジ提示 | 外部市況データで提案 | 過去プラン実績からのみ導出 | **過去実績からのみ**（2026-07-02 人間判断） | KB の `price_band` 実績（同季節/同ターゲット集計）から出せる場合のみ提示。実績が無ければ価格未提示。外部市況・レベニューマネジメントはスコープ外 |
| ingest 冪等キー | CSV にプラン ID 列を必須化 | 自然キー（タイトル＋season）・プラン ID は任意列 | **自然キー＋任意プラン ID**（2026-07-02 人間判断） | CSV 前処理の負担を増やさない。プラン ID 列があれば優先利用 |
| 蓄積ストア構成 | Vector 単独（RAG のみ） | DB 単独 / ハイブリッド | **ハイブリッド：DB=正・Vector=派生インデックス**（2026-07-02 人間判断） | 意味検索は Vector 必須。一方で価格実績集計（全実績が母集団）・adopted_flag の後付け UPDATE・冪等 upsert は DB が確実。書込み 2 経路のみの一方向同期で複雑度を限定 |
| HITL 承認判定 | 固定語彙の完全一致のみ | 2 段階（固定語彙 fast-path → LLM 意図分類） | **2 段階**（2026-07-02 人間判断「固定語彙は決定論すぎる」） | 「いいよ、それで行こう」等の自然文に対応。判定不能は安全側（revise）に倒し、曖昧応答での自動採用を防ぐ |
| LLM 未設定時の挙動 | static 劣化フォールバック | 起動時 ConfigError | **起動時エラー**（2026-07-02 人間判断） | 資格情報なしで劣化品質のプランが静かに生成される事故を防ぐ。フォールバックは仕様として持たない |
| facility_url 対応 | テキスト入力のみ | SSRF ガード付き URL 取得 | **URL 取得対応**（2026-07-02 人間判断「もったいない」） | Proposal の「HP URL or テキスト」を完全充足。SSRF はスキーム/ポート制限・プライベート IP 拒否・リダイレクト非追従・サイズ上限で遮断 |
| Vector 検索方式 | 意味検索のみ | 意味検索＋メタフィルタ | **ハイブリッド** | 「同季節・同ターゲット」の絞り込みが参照品質に直結（RAG 定石） |

## 実装ステージ（MR-B）への引き継ぎ事項

現時点の `src/` は scaffold 雛形のままであり、本設計とは意図的に乖離している（設計フェーズの baseline）。
MR-B（実装）で以下を本設計へ揃える:

- `src/graph/graph.py` の `Graph` を `AccommodationPlanBrandingAgent` にリネーム（`config/agent.yaml` の `class` と一致させる）
- `src/nodes/main_node.py` の `MainNode`（FunctionNode）を `PlanBrandingWorkflowNode`（GraphNode）へ置換し、内側 `src/graph/domain_workflow_graph.py` を新設
- 全 FunctionNode に `required_trust_level = TrustLevel.VERIFIED_EXTERNAL` を明示宣言（雛形の ANONYMOUS を置換）
- `src/schemas/state.py` に本設計の State フィールドを定義
- `src/services/kb_service.py`（KB 接続シーム）を新設 — **DB（`shared.services.database`）＋
  Vector（`shared.services.vector_store`）の 2 層と一方向同期ロジックを内包**。冒頭で両サービスの
  実 API を introspection し確定（ハイブリッド構成は 2026-07-02 人間判断で承認済み）
- HITL 配線: `config/config.yaml` に `hitl.enabled: true` / `memory_enabled: true` / `max_hitl: 8`、
  inner / outer 両方の明示 `compile(checkpointer=...)`、`get_subgraph()` キャッシュ、
  merge_output への hitl 欄追加、`src/api/server.py` の resume 対応（§Data Flow の実装注意参照）
- PB-7 + resume→承認完了までの通しテストを追加

## Open Items（Design 事後確認・開発ブロッカーではない）

| # | 項目 | オーナー | 状況 |
|---|---|---|---|
| 1 | SDK 共通サービス（`shared.services.vector_store` / `shared.services.database`）の API 実機確認 | 自分（実装時 introspection） | ハイブリッド構成は承認済み（2026-07-02 人間判断）。実 API シグネチャの確認のみ MR-B 冒頭で実施 — 想定と異なる場合も `kb_service.py` の接続シーム内に閉じる（設計不変） |

## Design Decision Record — LLM 呼び出し失敗の扱い（2026-08-13）

実 LLM（Azure OpenAI）で end-to-end 検証したところ、`facility_text` にジェイルブレイク文言を
入れた入力で provider の content filter が発火し、`FacilityProfileNode` が `self._llm.complete()` を
裸で呼んでいたため例外がそのまま伝播した。呼び出し側には `status: error` / **`error_log: None`** だけが
返り、「provider が拒否した」のか「内部で落ちた」のかが区別できなかった。

> content filter が発火すること自体は正しい挙動（provider が仕事をしている）。欠陥は
> **そのための経路をノードが持っていなかった**こと。

| ノード | 判断 | 根拠 |
|---|---|---|
| `FacilityProfileNode` | `status: error` + `error_log`（ノード名・例外型・メッセージ先頭） | 施設プロファイルが無いと後続の生成が成り立たない。呼び出し側が原因を判別できることが要件 |
| `PlanGenerationNode` | 同上 | プラン生成は当 AG の中核。黙って空を返さない |
| `RefinementNode` | **縮退**: 未推敲の `plan_candidates` を `plan_drafts` として `success` | 推敲は付加価値であり、既存の「パース不能なら candidates を使う」= *refinement must never lose the drafts* と同じ扱いにする |
| `error_log` の内容 | 例外型 + `str(exc)` を 200 字に切る | `error_log` は checkpoint に載る。provider のメッセージは要求内容を含みうるため生では残さない |
| 監査 | 3 ノードとも失敗経路で `*_llm_failed` ドメインイベントを発火 | S-4 を成功経路だけに置かない |

`PlanReviewNode._classify_intent()` は以前から `try/except` + 安全側（`revise`）を持っており、
本 MR は残る 3 ノードを同じ規律に揃えたもの。

### 同 MR の push 前敵対レビュー R1 で追加是正した 3 点

初版は「ノードが `error_log` を返す」ところで止まっており、**呼び出し側から見た症状（`error_log: None`）は
解消していなかった**。実機 invoke で再現して確認した。

| # | 指摘 | 是正 |
|---|---|---|
| High-1 | `AgentBaseGraph.get_output()` は output / status / trace_id / correlation_id / node_history しか返さないため、**ノードが理由を書いても caller には届かない**（内側 workflow は `error_log` を GraphNode 境界まで運んでいた） | 外側 `AccommodationPlanBrandingAgent.get_output()` を override し `error_log` を載せる。`error_log` は加算チャネルなので**重複排除**し、framework が付ける **traceback は 1 行目で切って落とす**（ファイルパス・フレームを caller に出さない）。end-to-end テストで固定 |
| High-2 | `str(exc)[:200]` は漏洩を**bound するだけで防いでいない** — provider の拒否メッセージは要求本文を反映しうる。また `type(exc).__name__` は actionable でない（Azure は content filter でも malformed request でも `BadRequestError`） | provider メッセージは**分類にのみ使い保存しない**。`content_filtered` / `rate_limited` / `provider_unavailable` / `provider_rejected` / `provider_error` の allow-list カテゴリ＋例外型＋HTTP status（整数のみ）に正規化。canary 文字列が結果にも `error_log` にも出ないことをテストで固定 |
| Medium-3 | `except Exception` は `GraphInterrupt`（`GraphBubbleUp → Exception`）と `SecurityViolationError` も飲む。当 AG は `hitl.enabled: true` | 4 つの LLM 呼び出し箇所すべてで `except (GraphBubbleUp, SecurityViolationError): raise` を前置（`PlanReviewNode` の既存 catch も同じ境界に揃えた） |

是正はいずれも**変異検査**で有効性を確認（override 削除 / 正規化解除 / traceback 除去解除 / 監査イベント削除 ─ すべてで新テストが落ちる）。
