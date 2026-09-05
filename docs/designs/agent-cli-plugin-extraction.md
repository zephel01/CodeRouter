# agent_cli の plugin 切り出し設計書（Phase 2: `coderouter-plugin-agents`）

> 対象バージョン: CodeRouter v2.8+ 系 / Phase 2（Adapter hook 配線 + 別リポ移設）
> 関連: [`external-agents-adapter.md`](./external-agents-adapter.md) §9.3・§11.3〜§11.5、[`docs/inside/future.md`](../inside/future.md) §1.2・§2.5、[`plan.md`](../../plan.md)
> ステータス: Phase 2a/2b/2c すべて実装済み（設計完了）

本設計書は、現在 CodeRouter 本体に in-core 実装されている外部コーディングエージェント CLI アダプタ（`kind="agent_cli"`、`coderouter/adapters/agent_cli.py`、1168 行）を、別配布の plugin パッケージ `coderouter-plugin-agents` へ前方互換で切り出す方式を定義するものである。切り出しの前提として、現状デッドエンド（`name: str` のみ）である Adapter Protocol（`coderouter/plugins/base.py` L156-168）を engine に配線する設計を併せて確定する。CLI 固有仕様はすべて `external-agents-adapter.md` §11.3〜§11.5 の実 CLI 検証結果を、拡張点のコード上の根拠はすべて本体実装の該当行を典拠とする。

---

## 1. 目的と動機

### 1.1 何を達成するか

`AgentCliAdapter`（`coderouter/adapters/agent_cli.py` L204）を Core から分離し、`pip install coderouter-plugin-agents` + `plugins.enabled: [agents]` の二段ゲートで有効化される in-process plugin として再配布する。Core 側には Adapter Protocol の engine 配線（`build_adapter` の plugin 対応）だけを残し、churn の激しいアダプタ本体（argv 組み立て・出力パーサ・sandbox フラグ表）を Core のリリース周期から切り離す。

### 1.2 なぜ切り出すか（churn 分離）

`external-agents-adapter.md` §11.3〜§11.5 は、Phase 1 実装中に判明した実 CLI 仕様の破壊的変更を記録している。要点のみ再掲する。

| CLI | 記録された churn（本文想定 → 実機） | 典拠 |
|---|---|---|
| grok v0.2.93 | `--sandbox` が値なしフラグ → プロファイル値必須、`XAI_API_KEY` → `GROK_CODE_XAI_API_KEY`、`-p` が stdin 非対応 → `--prompt-file` 採用、`grok-code-fast-1` は 2026-05-15 廃止 | §11.3 L612-624 |
| codex-cli 0.144.1 | `-a/--ask-for-approval` が `exec` から消失 → `edit`/`full_auto` マッピング obsolete、`--skip-git-repo-check` が実機必須、`reasoning_output_tokens` 新フィールド追加 | §11.4 L631-643 |
| gemini → agy 1.1.1 | 個人アカウント向け gemini CLI の OAuth が 2026-06-18 提供終了（`IneligibleTierError`）→ 対象 CLI そのものを Antigravity CLI に差し替え、`--output-format` なし・stdin パイプでハング | §11.5 L650-660 |

これらの変更はいずれも**アダプタ本体（argv builder / parser / sandbox 表）に閉じており**、Core の他機能（ルーティング・信頼性層 L1-L6）とは無関係である。にもかかわらず、in-core にある限り 1 つの CLI のパッチ追従のたびに Core のリリースを回す必要が生じる。§9.3 L555 が明記するとおり、切り出しの動機は「**CLI churn（バージョン間の破壊的変更）に Core を追従させ続けるコストを Core のリリース周期から切り離す**」ことにある。

### 1.3 三層モデルとの整合

`future.md` §1.2（L187-253）の三層モデルは、機能を Core / Plugin / Ecosystem に振り分ける。判定基準（L225-234）に照らすと、agent_cli は次に該当する。

- **Core ではない**: 信頼性層（L1-L6 guards・self-healing・fallback）ではなく、特定外部 CLI への依存を持つ optional 機能である。Core の「5 deps 固定・`uvx` 1-line 配布」（L208・L221・L248）の薄さを守るには、CLI ごとに増える churn を Core に抱えるべきでない。
- **Ecosystem でもない**: プロセス内（in-process）で `BaseAdapter` を実装し、wire 層のリクエストフローに tight に組み込まれる。別プロセス・別言語（HTTP loose 結合）ではない。
- **Plugin が正位置**: `future.md` L222 の Plugin 層定義「tight（in-process）/ `pip install coderouter-plugin-X` / Python only」に完全一致する。`coderouter-plugin-memory` が既に踏んだ道（L289-322）を、adapter hook で辿る。

したがって本切り出しは、`future.md` タスク #15（L112）「agent_cli Plugin 切り出し（Phase 2）」の実体化であり、三層モデルの当初設計どおりの帰着である。

### 1.4 動機の正確な限定（過剰主張の回避）

切り出しの便益は **churn 分離とコード量削減**であり、**Core の依存削減ではない**。`AgentCliAdapter` は `asyncio` / `subprocess` / `os` / `signal` / `json` / `re` / `shutil` など stdlib のみに依存し（agent_cli.py L103-114）、新規サードパーティ依存を一切持たない。すなわち in-core のままでも Core の 5 deps（`fastapi` / `uvicorn` / `httpx` / `pydantic` / `pyyaml`、`pyproject.toml` L39-45）は不変であり、切り出しによって deps が減るわけではない。§8 で後述するとおり、5 deps 不変条件への影響はゼロである。本設計はこの点を honest に扱い、deps 削減を便益として主張しない。

---

## 2. Adapter hook の実体化設計

### 2.1 現状: 空 Protocol とデッドエンド

`coderouter/plugins/base.py` の `Adapter` Protocol（L156-168）は `name: str` のみを宣言し、生成インターフェースを持たない。docstring（L164-166）が明記するとおり「engine 統合は未実装（Protocol contract only）」であり、loader は `adapter` group を `PLUGIN_GROUPS_FUTURE`（loader.py L50-55）に置いて `plugin-group-not-yet-active` 警告（L116-128）を出しつつロードするのみで、engine のリクエストフローには一切配線されていない。

一方、既存の active hook（`InputFilter` / `Observer`、base.py L40-93）は次のパターンで配線済みである。

1. Protocol に `name: str` + 非同期メソッド（`transform` / `on_event`）を宣言（base.py L65-67, L91-93）。
2. loader が entry-point group `coderouter.<group>` を走査し（loader.py L94-95）、`plugins.enabled` にある名前のみ `cls(**cfg)` で構築して registry に `add`（loader.py L96-134）。
3. `PluginRegistry` が group ごとの typed property（`input_filters` / `observers`、registry.py L53-66）を公開。
4. engine が hot path でその property を反復（fallback.py L2510-2512, L2545 付近）。

### 2.2 adapter が既存 hook と構造的に異なる点

`InputFilter` / `Observer` は「**engine が hot path で反復して呼ぶ instance**」である。対して adapter は「**`kind` 文字列を BaseAdapter へ解決する factory**」であり、リクエストごとに反復されるものではない。アダプタ生成は engine 起動時（fallback.py L1108-1110）と runtime 登録時（register_provider、L1220）の 2 か所で `build_adapter(provider)` を通じて **一度だけ**行われ、生成物は `self._adapters` に provider 名でキャッシュされる（L1108, L1220）。

したがって adapter hook は「instance を反復する」モデルではなく、「**plugin が担当する `kind` 文字列を申告し、その `kind` の ProviderConfig を受けて BaseAdapter を返す factory を提供する**」モデルが正しい。これは `build_adapter` の既存の分岐（registry.py L13-22、`kind` 文字列 → アダプタクラス）を plugin へ拡張する形になる。

### 2.3 実体化する Adapter Protocol

`coderouter/plugins/base.py` L156-168 を次の契約で実体化する。

```python
@runtime_checkable
class Adapter(Protocol):
    """New ``kind`` value in providers.yaml, backed by a plugin.

    The plugin declares the ``kind`` string it serves and a factory that
    turns a matching ``ProviderConfig`` into a ``BaseAdapter`` instance —
    the same surface ``coderouter.adapters.registry.build_adapter`` uses
    for in-core kinds, so the engine treats plugin adapters
    indistinguishably from built-ins once registered.
    """

    name: str
    # providers.yaml の `kind` 値。build_adapter がこの値で dispatch する。
    kind: str

    def build(self, config: ProviderConfig) -> BaseAdapter: ...
```

設計判断:

- **`kind: str` を申告させる**（`name` とは別）。`name` は plugin 識別子（ログ・エラー用、既存 hook と揃える）、`kind` は providers.yaml で dispatch する値。両者を分けるのは、1 plugin が複数 `kind` を出す将来余地を残しつつ Phase 2 では 1:1 で足りるため、まずは単一 `kind` とする（複数申告が必要になったら `kinds: tuple[str, ...]` へ拡張。plugins/__init__.py L16-18 の「real plugin が要求を駆動したら拡張する」方針に従い、今は最小契約）。
- **`build(config) -> BaseAdapter` は同期**とする。`build_adapter`（registry.py L11）が同期であり、生成に I/O を要さない（BaseAdapter.__init__ は httpx client を lazy 構築、base.py L176-181）ため、async 化する理由がない。既存 hook が async なのは hot path で呼ぶからであり、adapter factory は起動時 1 回なので同期で整合する。
- **plugin instance 自身は「adapter provider（factory）」であって adapter ではない**。loader は既存パターン `cls(**cfg)`（loader.py L131-133）でこの factory を構築し、`build_adapter` がリクエスト経路の外で `factory.build(provider)` を呼ぶ。

### 2.4 loader / registry 側の配線

1. **loader.py**: `"adapter"` を `PLUGIN_GROUPS_FUTURE`（L50-55）から active 群へ移す。既存の `PLUGIN_GROUPS_V2_3`（L44）は履歴的な命名なので、`PLUGIN_GROUPS_ACTIVE = ("input_filter", "observer", "adapter")` へ改称（または新タプル追加）する。この 1 変更で `plugin-group-not-yet-active` 警告（L116-128）が adapter に出なくなる。loader の他ロジック（enable ゲート L96-108、`cls(**cfg)` 構築 L130-134、typo 検出 L157-174）は adapter でもそのまま機能する。
2. **registry.py**: `input_filters` / `observers`（L53-66）に倣い `adapters` property を追加する。

```python
@property
def adapters(self) -> list[Any]:
    """Plugins registered as ``coderouter.adapter`` (kind factories)."""
    return list(self._by_group.get("adapter", ()))
```

### 2.5 entry-point group 名の確定

§9.3 L553 は例として `group="coderouter.adapters"`（複数形）を挙げるが、これは loader の実装と**不整合**である。loader.py L94 は `ep_group = f"coderouter.{group}"` を組み立て、`group` は `PLUGIN_GROUPS_*` の**単数形**（`"input_filter"` / `"observer"` / `"adapter"`、L44・L50-55）である。registry.py docstring L10-12 も「group key は `[project.entry-points."coderouter.<group>"]` と一致」と明記する。

**確定: entry-point group は `coderouter.adapter`（単数形）とする。** これにより loader は一切改修せず（L94 の `f"coderouter.{group}"` がそのまま解決）、plugin 側の `pyproject.toml` は次を宣言する。

```toml
[project.entry-points."coderouter.adapter"]
agents = "coderouter_plugin_agents:AgentCliProvider"
```

§9.3 L553 の「`coderouter.adapters`」表記は本設計で単数形へ訂正する（歴史的記録として §9.3 本文は改変しない）。

---

## 3. build_adapter の拡張

### 3.1 現状（23 行）

`coderouter/adapters/registry.py` L11-23 の `build_adapter(provider)` は、`kind` を if 連鎖で in-core アダプタへ解決し、未知 `kind` で `ValueError`（L23）を投げる。agent_cli は lazy import（L17-22）で subprocess/os 系を遅延ロードする。

### 3.2 拡張後のシグネチャと解決順序

`build_adapter` に plugin registry を渡す引数を追加する。

```python
def build_adapter(
    provider: ProviderConfig,
    plugin_registry: PluginRegistry | None = None,
) -> BaseAdapter:
    # 1. in-core kind を先に解決（openai_compat / anthropic /
    #    移行期は agent_cli も）
    if provider.kind == "openai_compat":
        return OpenAICompatAdapter(provider)
    if provider.kind == "anthropic":
        return AnthropicAdapter(provider)
    if provider.kind == "agent_cli":
        # 移行期のみ: in-core fallback（§5 の deprecation 対象）
        from coderouter.adapters.agent_cli import AgentCliAdapter
        # plugin も同 kind を提供している場合は deprecation を 1 度だけログ
        return AgentCliAdapter(provider)
    # 2. plugin 提供 kind を解決
    if plugin_registry is not None:
        for factory in plugin_registry.adapters:
            if factory.kind == provider.kind:
                return factory.build(provider)
    # 3. 未知 kind
    raise ValueError(
        f"Unknown adapter kind {provider.kind!r}. "
        f"in-core kinds: openai_compat, anthropic"
        f"{', agent_cli' if _AGENT_CLI_IN_CORE else ''}; "
        f"plugin-provided kinds: {_plugin_kinds(plugin_registry)}. "
        f"If a plugin should provide {provider.kind!r}, ensure it is "
        f"installed AND listed in plugins.enabled."
    )
```

**解決順序: in-core → plugin → error。** in-core を先に見るのは、Core が保証する kind（openai_compat / anthropic）を plugin が上書き・shadow できないようにする安全側の既定である。移行期（Phase 2b）は agent_cli も in-core が先に勝ち、plugin は同 kind を提供しても後回しになる（§5 で deprecation 経路として扱う）。Phase 2c で in-core agent_cli 分岐を除去すると、agent_cli は plugin 経路（ステップ 2）でのみ解決される。

### 3.3 未知 kind のエラーメッセージ

未知 kind のエラー（L23 相当）を拡張し、**in-core kind 一覧・plugin 提供 kind 一覧・二段ゲートの案内**を含める。特に「plugin が該当 kind を出すはずなのに解決できない」ケース（install 忘れ or `plugins.enabled` 未記載）を切り分けられる文言にする。これは schemas.py の fail-fast 哲学（`_check_output_filters_known` L466-479 / `_check_kind_requirements` L482-505）と揃え、設定ミスを起動時に明示する。

### 3.4 plugins.enabled 二段ゲートの通し方

adapter plugin は既存 hook と同じ二段ゲートを通る（`PluginsConfig` L1477-1519、loader.py L9-16）。

1. `pip install coderouter-plugin-agents` で entry-point `coderouter.adapter` → `agents` が discoverable になる。
2. `plugins.enabled: [agents]` に列挙されて初めて loader が factory を構築（loader.py L96-108 の enable ゲート）、`PluginRegistry.adapters` に載る。

未列挙なら `plugin-skipped`（loader.py L100-107）でログされ、`PluginRegistry.adapters` は空 → `build_adapter` は agent_cli を解決できず §3.3 のエラーになる。これは供給網防御（loader.py L9-16）の設計意図どおりであり、adapter でも同一の防御が働く。

**UX 上の注意（互換への影響）**: 移行前は provider に `kind: agent_cli` を書くだけで有効だったが、移行後（2c 以降）は加えて `plugins.enabled: [agents]` が必須になる。この差分は §5 の後方互換・移行手順で扱う。

### 3.5 registry を build_adapter へ流す配線

`build_adapter` の呼び出し 2 か所に registry を渡す。registry は engine が既に保持している（`self._plugin_registry`、fallback.py L1100、`discover_and_load` の産物を受け取る）。

| 呼び出し箇所 | 現状 | 改修 |
|---|---|---|
| engine 起動時 | `build_adapter(p) for p in config.providers`（fallback.py L1108-1110） | `build_adapter(p, self._plugin_registry)` |
| runtime 登録時 | `self._adapters[provider.name] = build_adapter(provider)`（fallback.py L1220、register_provider） | `build_adapter(provider, self._plugin_registry)` |

`self._plugin_registry` は L1100 で L1108 より前に設定済みなので順序上の問題はない。register_provider（L1178）は launcher 経由の runtime 登録で、launcher backend は openai_compat 前提だが、将来 plugin kind を runtime 登録しても解決できるよう registry を渡しておく。

---

## 4. `coderouter-plugin-agents` パッケージ設計

### 4.1 別リポ構成

```
coderouter-plugin-agents/            # 別 GitHub repo・別 PyPI パッケージ
├── pyproject.toml
├── README.md / README.en.md
├── src/coderouter_plugin_agents/
│   ├── __init__.py                  # AgentCliProvider を export
│   ├── provider.py                  # Adapter Protocol 実装（factory）
│   └── adapter.py                   # AgentCliAdapter 本体（現 agent_cli.py 移設）
└── tests/
    └── test_agent_cli.py            # 移設テスト（§4.5）
```

### 4.2 pyproject.toml と entry-point 宣言

```toml
[project]
name = "coderouter-plugin-agents"
version = "0.1.0"
requires-python = ">=3.11"
# Core への依存は Protocol/基底クラスの契約バージョン範囲で pin（§5.4）
dependencies = ["coderouter-t>=2.8,<3.0"]

[project.entry-points."coderouter.adapter"]
agents = "coderouter_plugin_agents:AgentCliProvider"
```

plugin 本体（adapter.py）は Core と同様 stdlib のみで完結し、新規サードパーティ依存を持たない（現 agent_cli.py L103-114 の import は全 stdlib）。`coderouter-t` への依存は `BaseAdapter` / `ProviderConfig` / `AgentCliConfig` / `AdapterError` などの型契約を得るためであり、実行時 import である。

### 4.3 AgentCliProvider（Adapter Protocol 実装）

`provider.py` に factory を薄く実装する。churn するアダプタ本体（adapter.py）とは分離し、Protocol 適合だけを担わせる。

```python
from coderouter.adapters.base import BaseAdapter
from coderouter.config.schemas import ProviderConfig
from coderouter_plugin_agents.adapter import AgentCliAdapter

class AgentCliProvider:
    """coderouter.adapter entry point: serves kind="agent_cli"."""
    name = "agents"
    kind = "agent_cli"

    def __init__(self, **config: object) -> None:
        # plugins.config["agents"] が渡る（loader.py L132）。
        # Phase 2 では plugin レベルの追加設定は不要なので無視で足りる。
        pass

    def build(self, config: ProviderConfig) -> BaseAdapter:
        return AgentCliAdapter(config)
```

loader は `cls(**cfg)`（loader.py L131-133）で `AgentCliProvider()` を構築し、`build_adapter` が `provider.build(config)` を呼ぶ。

### 4.4 AgentCliConfig の置き場所（3 案比較と推奨）

**問題**: `ProviderConfig` は `model_config = ConfigDict(extra="forbid")`（schemas.py L342）であり、`agent_cli:` キーは `agent_cli: AgentCliConfig | None` フィールド（L421-424）が Core に存在するからこそ受理される。さらに `kind` は `Literal["openai_compat", "anthropic", "agent_cli"]`（L345）、`_check_kind_requirements`（L482-505）が `kind=="agent_cli"` に対し agent_cli サブ設定を要求（L500-504）する。アダプタ本体を plugin へ出すと、この Core 側スキーマ知識をどうするかが焦点になる。

| 案 | 内容 | 長所 | 短所 |
|---|---|---|---|
| **(a) plugin 側 pydantic + Core dict 透過** | `AgentCliConfig` を plugin へ移し、Core は provider 内に汎用の `adapter_config: dict[str, Any]` を持つ。plugin factory が dict を自前 pydantic で検証 | Core がアダプタ固有スキーマを知らない（純粋な分離） | `extra="forbid"` の**起動時 fail-fast を喪失**。typo（例 `sandbox_moe`）は plugin 構築時にしか出ず、loader は degraded-continue（loader.py L18-26）なので `plugin-load-failed` ログのみで engine は起動してしまう。運用体験の後退 |
| **(b) AgentCliConfig を Core に残置** | `AgentCliConfig`（L166-329）・`agent_cli` フィールド・`kind` Literal・`_check_kind_requirements` を Core に残し、**アダプタ本体（adapter.py）だけ**を plugin へ移す | churn は本体（argv/parser/sandbox 表）に閉じており、スキーマは §11.3〜§11.5 で**一度も変わっていない**安定契約。`extra="forbid"` fail-fast 維持。§4.5 のスキーマ検証テストが Core に無改変で残る。Core 改修が最小 | Core が約 164 行の安定スキーマを持ち続ける。「Core は agent_cli を全く知らない」という純度は得られない |
| **(c) 汎用 extra config 機構** | Core に `adapter_config: dict` を新設し、全サード adapter が共通で使う汎用機構にする | 将来の任意 kind に再利用可能 | (a) と同じ fail-fast 喪失に加え、利用者が 1 つも居ない汎用機構の先行実装（plugins/__init__.py L16-18 の「real plugin が要求を駆動するまで待つ」方針に反する YAGNI） |

**推奨: (b) AgentCliConfig を Core に残置。** 根拠は次の 3 点。

1. **churn の所在**: §11.3〜§11.5 に記録された破壊的変更はすべて argv builder / parser / sandbox フラグ表（adapter.py 側）であり、`AgentCliConfig` のフィールド（`agent` / `command` / `workdir` / `exec_timeout_s` / `allow_file_writes` / `sandbox_mode` / `model` / `max_turns` / `passthrough_env` / `agent_depth_limit`、L214-305）は一度も変わっていない。切り出しの目的（churn 分離）は本体を出せば達成され、スキーマを出す必要はない。
2. **fail-fast は Core の価値**: `extra="forbid"`（L342）と `_check_kind_requirements`（L482-505）による起動時検証は Core 全体の設計哲学（L466-479 と同一パターン）である。(a)/(c) はこれを degraded-continue な plugin 構築時検証へ格下げし、設定ミスの発見を遅らせる。
3. **最小改修**: (b) は `build_adapter` の agent_cli 分岐（registry.py L17-22）除去と adapter.py 移設のみで済み、Core スキーマ（L166-329, L345, L421-424, L482-505）は無改変で残る。§4.5 のスキーマ検証テスト群も Core に据え置ける。

補足: (b) を採ると「Core は agent_cli の存在を kind Literal レベルで知り続ける」が、これは欠陥ではなく**安定した公開契約**の残置である。将来 agent_cli 以外の第三者 adapter が現れ、その時に汎用機構が本当に要るなら、その時点で (c) を driver 付きで導入すればよい（現在は不要）。

### 4.5 テスト 91 件の移設マップ

`tests/test_agent_cli.py`（1507 行・91 件）を、推奨案 (b) に基づき分割する。判定基準は「**アダプタ本体の振る舞いか、Core スキーマの振る舞いか**」である。

| 区分 | 対象テスト（代表） | 件数（概算） | 行き先 |
|---|---|---|---|
| argv 構築（T1） | `test_argv_*` / `test_grok_argv_*` / `test_codex_argv_*` / `test_antigravity_argv_*`（L286-560） | 約 30 | plugin |
| 出力パーサ（T2） | `test_parse_*` / `test_grok_parse_*` / `test_codex_parse_*` / `test_antigravity_parse_*`（L659-930 付近） | 約 25 | plugin |
| 子 env 分離（T7） | `test_env_*`（L566-657） | 約 8 | plugin |
| subprocess スタブ / timeout / 再帰 / healthcheck（T3・T6・T8） | `_FakeProc` 系・`test_error_detail_*`・timeout kill・depth limit・healthcheck | 約 14 | plugin |
| E2E（T4） | `test_e2e_chat_completions` / `_grok_` / `_codex_` / `_antigravity_`（L1315-1492） | 4 | plugin（下記注記） |
| gemini 拒否 | `test_gemini_rejected_with_antigravity_migration_message`（L1194、`AgentCliAdapter.__init__` の拒否） | 1 | plugin |
| **Core スキーマ検証** | `test_schema_agent_cli_required_for_kind`（L1218）・`test_schema_base_url_optional_for_agent_cli`（L1223）・`test_schema_base_url_required_for_openai_compat`（L1233）・`test_schema_command_defaults_to_*`（L1238-1255、3 件）・`test_schema_antigravity_accepted_by_literal`（L1256）・`test_schema_antigravity_explicit_command_not_overridden`（L1261）・`test_schema_write_conflict_rejected`（L1266） | 9 | **Core 残置** |

概算: **約 82 件が plugin へ移設、9 件（`ProviderConfig` / `AgentCliConfig` の pydantic 検証）が Core に残置（合計 91 件と一致）。** 案 (a)/(c) を採った場合はこの 9 件も plugin へ移り、代わりに Core は「未知 kind エラー」「dict 透過」の新規テストを持つことになる — (b) はテスト移動量も最小である点で有利。

E2E（T4）についての注記: `test_e2e_*` は `TestClient(create_app())` によるフルスタックで、移設後は plugin をインストール + `plugins.enabled` へ列挙した config を要する。4 件は plugin repo が保有し、Core 側には **fake adapter plugin を使った wiring E2E を 1 件**新設して「plugin 提供 kind が `build_adapter` 経由で解決され `/v1/chat/completions` が通る」ことだけを検証する（Core は agent_cli 具体ではなく「plugin adapter が配線される」ことを守る）。

---

## 5. 移行と互換性

### 5.1 移行期の in-core 残置（deprecation 方針）

Phase 2b では in-core `AgentCliAdapter` を**即時削除しない**。`build_adapter` の agent_cli 分岐（registry.py L17-22）を残したまま、plugin も同 kind を提供できる状態にする（§3.2 の解決順序で in-core が先勝ち）。plugin が enable されている（`PluginRegistry.adapters` に `kind=="agent_cli"` の factory がある）にもかかわらず in-core 分岐が使われた場合、`agent-cli-in-core-deprecated` を **1 プロセス 1 回**ログし、plugin 経路へ移るよう促す。Phase 2c で in-core 分岐と adapter.py 本体を Core から削除する。

### 5.2 providers.yaml の後方互換

- **kind と agent_cli サブ設定**: 推奨案 (b) では `kind: agent_cli` と `agent_cli:` ブロックの記法は Core に残るため、**providers.yaml の provider エントリ自体は無改変で受理される**（スキーマ互換完全維持）。
- **plugins.enabled の追加要件**: 2c 以降は `plugins.enabled: [agents]` の追記が必須になる（未記載だと §3.3 のエラー）。移行手順として、2b リリースの CHANGELOG / doctor 診断で「agent_cli を使う config には `plugins.enabled: [agents]` を追加し `pip install coderouter-plugin-agents` せよ」と案内する。2b の期間（in-core 残置）に猶予を設け、2c で切り替える。
- **doctor 支援**: `kind: agent_cli` を含むが `plugins.enabled` に `agents` が無い config を検出したら、doctor が明示警告 + 修正スニペットを出す（fail-fast 哲学の延長）。

### 5.3 CI / テストの分離

- Core repo: agent_cli 具体テストが消え、Core CI から実 CLI 依存の smoke（T9 相当、opt-in）が外れる。Core は「plugin adapter wiring」1 件 + 残置スキーマ 9 件のみを持つ。
- plugin repo: 移設した約 82 件を独立 CI で回す。実 CLI smoke（§11.3〜§11.5 のような実機検証）は plugin repo の opt-in ジョブに閉じ、Core CI を汚さない。これにより CLI churn 追従は **plugin repo のリリースサイクル**で完結する（§1.2 の目的達成）。

### 5.4 バージョン整合（互換マトリクス方針）

plugin と Core を独立配布するため、契約は **Adapter Protocol + BaseAdapter + ProviderConfig/AgentCliConfig** の 3 面である。方針:

- plugin は `dependencies = ["coderouter-t>=2.8,<3.0"]` のように **Core の互換範囲を pin**する。
- Core は Protocol / BaseAdapter / AgentCliConfig の破壊的変更を **minor 以上**で行い、その際に plugin 側の下限を上げる。Protocol は `runtime_checkable`（base.py L40 の既存方針）なので、loader が `isinstance` で不適合を早期検出でき（plugins/base.py L13-15 の意図）、契約破れは `plugin-load-failed`（loader.py L143-155）として degraded-continue で顕在化する。
- 互換マトリクスは plugin repo の README に「plugin x.y ↔ Core a.b」の表として維持する（`coderouter-plugin-memory` の前例に倣う）。

---

## 6. セキュリティ不変条件の維持

`external-agents-adapter.md` §6（L481-494）が非交渉と定める要件が、plugin 境界を越えても損なわれないことを保証する。要件はすべて**アダプタ本体（adapter.py）内に自己完結**しており、Core の配線に依存しない — したがって本体をそのまま移設すれば保証も移る。

| §6 要件 | 実装箇所（移設後は adapter.py の同一コード） | 境界越えでの保証 |
|---|---|---|
| **allowlist argv のみ**（`shell=True` 禁止） | `create_subprocess_exec(*argv, ...)`（agent_cli.py L355-366）、executable を絶対パス解決（L339-341） | argv 構築は本体内で完結。Core は argv に一切関与しない（build_adapter は生成のみ）。移設で不変 |
| **env allowlist**（親環境非継承・`ANTHROPIC_API_KEY` 非転送） | `_build_child_env`（L1063-1089）、`passthrough_env` 限定（L1085-1088） | env 構築は本体内。Core の環境は子に流れない。移設で不変 |
| **PGID kill** | `_kill_process_group`（L1144-1152）、`start_new_session=True`（L365）、`asyncio.wait_for` + 超過 SIGKILL（L380-391） | timeout/kill は本体内の `generate` に閉じる。移設で不変 |
| **read-only クランプ** | `_claude_permission_mode`（L481）・`_codex_sandbox_args`（L631）・`_grok_sandbox_args`（L806）・`_antigravity_mode_args`（L933）が `allow_file_writes` False で read_only 強制 | クランプは本体内。`AgentCliConfig`（Core 残置）の値を本体が解釈。移設で不変 |
| **再帰上限** | `_current_depth`（L1055）・上限拒否（L303-309）・`CODEROUTER_AGENT_DEPTH` +1 伝播（L1079） | 深度は**環境変数ベースのプロセス横断プロトコル**であり import 依存でない。plugin 化しても env 名（`_DEPTH_ENV`、L136）が同一である限り、ネストした CodeRouter が同じ env を読む。移設で不変 |
| **workdir 境界**（`..` 拒否・隔離ディレクトリ） | `_resolve_workdir`（L1114-1142）、`..` 拒否（L1124-1129） | 本体内。移設で不変 |

加えて §6 L494 の**wire 層 guards**（`tool_loop.py` / `context_budget.py` / `memory_budget.py`）は engine 層で全 `BaseAdapter` に自動適用される。これらは adapter の**上流**（engine のリクエストフロー）で働くため、adapter が in-core か plugin かに無関係に効く。plugin 化はこの保証を弱めない。

**plugin 境界に固有の新リスク**: 悪意ある / バグのある adapter plugin が上記要件を省いた実装を出す可能性。緩和は 2 段。(1) 二段ゲート（§3.4）で未 enable の plugin は factory すら構築されない（供給網防御、loader.py L9-16）。(2) `coderouter-plugin-agents` を**一次配布（first-party）**とし、Core と同じレビュー基準で維持する（§8 のサプライチェーン項参照）。

---

## 7. 実装フェーズ分割

| Phase | 対象 | 主な変更 | 規模見積り |
|---|---|---|---|
| **2a** | Adapter hook を Core に配線（本体移設なし） | base.py（Adapter Protocol 実体化 §2.3）/ loader.py（`adapter` を active 群へ §2.4）/ registry.py（`adapters` property §2.4）/ adapters/registry.py（`build_adapter` に registry 引数・plugin lookup・エラー拡張 §3）/ fallback.py（registry を 2 か所へ流す §3.5） | Core 約 30〜40 行 + wiring テスト（fake adapter plugin）約 80 行 |
| **2b** | plugin パッケージ作成・移設 | 別リポ `coderouter-plugin-agents` 新設（§4.1-4.3）/ adapter.py = 現 agent_cli.py 移設 / テスト約 82 件移設（§4.5）/ Core は in-core agent_cli 分岐残置 + deprecation ログ（§5.1） | plugin 新規 約 1200 行（本体 1168 + provider 約 30）+ テスト約 1300 行 / Core 追加約 5 行 |
| **2c** | in-core 削除 | registry.py の agent_cli 分岐（L17-22）除去 / Core から adapter.py・移設済み約 82 テスト削除 / `AgentCliConfig`・kind Literal・`_check_kind_requirements`・残置 9 テストは維持（§4.4 案 b） / CHANGELOG + doctor 移行案内（§5.2） | Core 削減 約 1168 行 + テスト約 1300 行 / 追加は doctor 診断 約 20 行 |

### 7.1 各フェーズのテスト計画

- **2a**: Core 単体で完結。fake adapter plugin（`kind="fake"`、`build` が echo adapter を返す）を entry-point 登録するテスト用パッケージ or monkeypatch で `PluginRegistry.adapters` を注入し、(i) `build_adapter` が plugin kind を解決 (ii) 未知 kind が §3.3 のメッセージで `ValueError` (iii) in-core kind を plugin が shadow しない（順序 §3.2） (iv) 未 enable なら解決しない、を検証。既存 91 件は無改変で緑のまま（in-core agent_cli 温存のため）。
- **2b**: plugin repo で移設 82 件が緑。Core は残置 9 件 + wiring 1 件が緑。deprecation ログの発火を 1 件で検証。plugin↔Core を同一 venv に入れた統合 smoke を plugin repo に置く。
- **2c**: Core から agent_cli 具体が消えても残置スキーマ 9 件が緑（`AgentCliConfig` 検証は Core 責務のまま）。plugin repo が全 agent_cli 責務を負う。回帰防止として、Core に「`kind: agent_cli` だが plugin 無効」→ doctor 警告のテストを追加。

---

## 8. リスクと未解決事項

| リスク / 事項 | 内容 | 対応方針 |
|---|---|---|
| **plugin 供給網リスク** | adapter は subprocess 実行面（§6）を持つため、悪意 plugin の影響が大きい | 二段ゲート（§3.4、loader.py L9-16）で未 enable を無効化 + `coderouter-plugin-agents` を first-party 維持。third-party adapter を将来受け入れる際の審査基準は別途 open |
| **5 deps 不変条件** | 切り出しが Core deps に影響しないか | **影響ゼロ**を確認。agent_cli は元来 stdlib のみ（agent_cli.py L103-114）で 5 deps（pyproject.toml L39-45）に含まれない。plugin 側も stdlib のみ。切り出しは deps を減らしも増やしもしない（§1.4）。便益は churn 分離であって deps 削減ではない |
| **ダウンロード導線 / 発見性** | in-core なら設定だけで動いたものが、pip install + enable を要する | 2b の in-core 猶予期間 + doctor 診断（§5.2）で導線を明示。README / CHANGELOG に手順明記。`uvx` 配布の Core とは別に plugin を PyPI 配布（`coderouter-plugin-memory` 前例） |
| **entry-point group 名の不整合** | §9.3 L553 は `coderouter.adapters`（複数形）だが loader は単数形（loader.py L94・L50-55） | §2.5 で `coderouter.adapter`（単数形）に確定。§9.3 の表記は本設計で訂正（§9.3 本文は歴史記録として不改変） |
| **移行期の kind 二重提供** | 2b で in-core と plugin が同時に agent_cli を出す | §3.2 の解決順序で in-core 先勝ち + deprecation ログ（§5.1）。2c で in-core 除去し一本化 |
| **Adapter Protocol の将来拡張** | Phase 2 は単一 `kind`。複数 kind / plugin レベル設定検証の需要 | 現状は単一 `kind` 最小契約（§2.3）。`kinds` 複数化・plugin 設定 pydantic 化は real driver 出現時に拡張（plugins/__init__.py L16-18 方針）。案 (b) のため AgentCliConfig 検証は当面 Core が担い、plugin 側検証は未導入 |
| **register_provider 経路の plugin kind** | launcher runtime 登録（fallback.py L1178）は現状 openai_compat 前提 | §3.5 で registry を渡すのみに留め、launcher が plugin kind を登録する UX は本設計の非スコープ（発火条件待ち） |

### 8.1 `ProviderConfig.kind` の Literal → str 拡張（2026-07-11 実装追記）

Phase 2a 実装時に、`ProviderConfig.kind`（schemas.py L353）を
`Literal["openai_compat", "anthropic", "agent_cli"]` から `str` へ拡張した。
これは §4.4 案 (b)（「kind Literal を Core に残置」）に対する**意図的な逸脱**で
あり、記録に残す。

**逸脱の理由（設計の欠落補完）**である。§4.4 案 (b) は AgentCliConfig の置き場所を
論じたものだが、そこで前提とした「kind を Literal のまま残す」構えは、plugin が
新 `kind` を申告する Phase 2a の目的そのものと衝突する。plugin 探索
（`discover_and_load`）は `load_config` の**後**に走る（ingress/app.py L176-182）
ため、pydantic が config-load 時点で有効な `kind` 集合を知り得ない。Literal を
維持すると、`kind: fake_agent` のような plugin kind は plugin が有効化されていても
pydantic 段階で機械的に拒否され、adapter hook は永久に到達不能になる。したがって
`kind` の str 化は plugin kind を受理するための**最小かつ必須**の変更である。

**fail-fast の移設**である。Literal 除去により未知 `kind` の検出時点が
「config-load（pydantic）」から「engine 構築時（`build_adapter`）」へ 1 段後退する。
ただし `FallbackEngine.__init__` は全 provider のアダプタを**起動時に一括生成**する
（fallback.py L1112-1114 の dict 内包表記）ため、typo した `kind`（例
`openai_compt`）は `serve` 起動時＝`create_app` 内で `ValueError("Unknown adapter
kind ...")` として即座に露見する。リクエスト時まで遅延することはなく、fail-fast の
タイミングは実質維持される（起動失敗）。runtime 登録経路（register_provider,
fallback.py L1224）も同様に登録時点で `build_adapter` を通すため即失敗する。
エラーメッセージは in-core kind 一覧・plugin 提供 kind 一覧・二段ゲート案内を含み
（registry.py L54-60）、旧 pydantic Literal エラーより診断性は高い。

**残る副作用（許容）**である。(1) `_check_kind_requirements`（schemas.py L505）は
`kind in ("openai_compat","anthropic")` の時のみ base_url を必須とするため、typo した
`kind` では base_url 必須検証が発火しない。だが未知 kind エラーがそれを上回って
起動を止めるので実害はない。(2) `doctor` は `build_adapter` を呼ばず HTTP プローブを
直接行うため、typo `kind` を `build_adapter` 経路では捕捉しない（doctor の
`kind != "anthropic"` 分岐は未知 kind を openai_compat 相当として扱う）。これは
diagnostics の限定であって fail-fast の権威経路（serve 起動）は別途守られている。
将来 doctor に未知 kind 明示警告を足す余地はある（§5.2 の doctor 支援の延長）。

代替案として「Literal を in-core 用に温存し、plugin kind は `plugin:` 接頭辞で
逃がす」構えも検討し得たが、providers.yaml の `kind` 表記に接頭辞規約を持ち込む
非互換・非直感を嫌い、単純な str 化を採った。in-core kind の shadow 防止は
`build_adapter` の解決順序（in-core → plugin、registry.py L40-53）で担保され、
Literal による静的保証は不要と判断した。

---

## 9. 参照

- [`external-agents-adapter.md`](./external-agents-adapter.md) — agent_cli の Phase 1 設計。§6（セキュリティ L481-494）・§9.3（移行パス L551-555）・§11.3〜§11.5（実 CLI churn 記録 L608-660）
- [`docs/inside/future.md`](../inside/future.md) — §1.2 三層モデル・Core 5 deps 不変条件（L187-253）、§2.5 方向性（L368+）、タスク #15（L112）
- [`plan.md`](../../plan.md) — Core 5 deps 方針 / プラグイン制による外部委譲（L689）
- 実装典拠: `coderouter/plugins/base.py` L156-168 / `coderouter/plugins/loader.py` L44-134 / `coderouter/plugins/registry.py` L43-66 / `coderouter/adapters/registry.py` L11-23 / `coderouter/adapters/agent_cli.py` L204-1168 / `coderouter/adapters/base.py` L162-263 / `coderouter/config/schemas.py` L166-329・L342-505・L1477-1519・L1674-1684 / `coderouter/routing/fallback.py` L1100-1110・L1178-1220 / `tests/test_agent_cli.py`（91 件）

### 9.1 完了記録（2026-07-11）

本設計の Phase 2a・2b・2c はすべて 2026-07-11 に実装完了した（v2.8.0 = Phase 2a、v2.8.1 = Phase 2b、v2.9.0 = Phase 2c、`coderouter-plugin-agents` 0.1.0）。§5.1・§7 が想定していた「2b の in-core 猶予期間」は、owner 判断により意図的にスキップされ、2b→2c を同日中に連続実施する形になった（当初想定していたような複数リリースにまたがる猶予期間は設けられていない）。この短縮に対する緩和策は次の3点である: (1) plugin 未導入のまま `kind: agent_cli` を使うと `serve` 起動時に移行手順込みのエラーで即座に停止する（§3.3 のエラーメッセージを 2c でさらに具体化)、(2) `coderouter doctor` が同じ設定ミスを config-level warning として検出し修正スニペットを提示する、(3) v2.9.0 の CHANGELOG エントリで本変更を **BREAKING** として先頭に明記し、影響範囲・対処コマンド・不変点・上記2つの安全網を明示している。
