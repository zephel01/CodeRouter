# 言語税 (Language Tax) ガイド

[`README.md`](../../README.md) の補足。CodeRouter v2.6.0 で追加された **言語税トラッキング** の仕組み・設定・チューニングを説明します。

English version: [`language-tax.en.md`](./language-tax.en.md)

目次:

1. [言語税とは](#1-言語税とは)
2. [実測データ](#2-実測データ)
3. [計測を有効化する (`tokenizer_path`)](#3-計測を有効化する-tokenizer_path)
4. [ルーティングで回避する (`cjk_ratio_min`)](#4-ルーティングで回避する-cjk_ratio_min)
5. [ダッシュボードで見る](#5-ダッシュボードで見る)
6. [仕組みと確信度](#6-仕組みと確信度)
7. [チューニング](#7-チューニング)

---

## 1. 言語税とは

クラウド LLM のトークナイザは、日本語などの CJK テキストを英語より細かく分割します。その結果、**同じ意味でも日本語のほうがトークン数が多くなり、クラウド API の課金が増えます**。これが「言語税 (language tax)」です。

ローカル LLM はトークン単位で課金されないので、言語税が効くのは**クラウドに投げるリクエストだけ**です。CodeRouter はローカル ↔ クラウドのルーターなので、この税を「計測・回避・可視化」する位置にいます。

2つの異なる軸があることに注意してください。

- **経済的な税（同義 JA vs EN）**: 同じタスクを日本語で書くと、英語で書くより何倍トークンを食うか。これがユーザーが体感する「割高さ」です。
- **char/4 比の倍率（CodeRouter の指標）**: CodeRouter のルーターは `char/4` ヒューリスティック（英語基準で約4文字=1トークン）でトークンを概算します。CJK ではこの概算が大きく過小評価になるため、実トークナイザ比の倍率は経済的な税より大きく出ます。

---

## 2. 実測データ

実在の本番トークナイザで計測した結果です（再現スクリプト: `_OUTPUTS/01-機能実装/language-tax/measure/`）。

### 経済的な税 — 同じ意味で JA vs EN（対訳5ペアの平均）

| トークナイザ | 代表モデル | JA/EN トークン比 |
|---|---|---|
| `cl100k_base` | GPT-4 / Claude-3 世代 | **2.04×** |
| `o200k_base` | GPT-4o 世代 | **1.60×** |

短い指示文では o200k で **1.28〜1.31×** まで下がるケースもあります。**新しいトークナイザほど日本語効率が良い**のが実測の重要な示唆です。一般に言われる「1.2〜1.5倍」は、最新トークナイザ・短文寄りの条件と整合します。

### char/4 比の倍率（CodeRouter の `tax_multiplier`）

実トークナイザ = ローカルの **Qwen2.5** `tokenizer.json` で計測。

| サンプル | CJK 比率 | char/4 | 実トークン | 倍率 |
|---|---|---|---|---|
| 英語コード | 0% | 18 | 22 | 1.22× |
| 英語の散文 | 0% | 29 | 19 | 0.66× |
| 混在（日本語コメント+コード） | 51% | 16 | 40 | **2.50×** |
| 日本語の指示文 | 100% | 14 | 36 | **2.57×** |
| 日本語の技術文 | 98% | 29 | 87 | **3.00×** |

> 補足: 英語の散文が 0.66× なのは、char/4 が英語の散文を**過大**評価する（実際は約6文字=1トークン）ためです。char/4 はあくまで近似で、`tax_multiplier` は「CodeRouter 自身の char/4 基準に対する倍率」である点に注意してください（確信度: MODERATE）。

---

## 3. 計測を有効化する (`tokenizer_path`)

プロバイダに、そのモデルの **ローカル `tokenizer.json`** を指定するだけです。**未設定なら言語税計測は無効（倍率 1.0）** で、ホットパスに一切影響しません。

```yaml
providers:
  - name: cloud-sonnet
    kind: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-6
    tokenizer_path: ~/.coderouter-t/tokenizers/sonnet.json
```

### tokenizer.json の入手（一度だけ）

ネットワークアクセスは CodeRouter 本体では行いません（ローカルファイルのみ読み込み）。トークナイザは運用者が一度だけ手元に落としておきます。

```bash
pip install "coderouter-t[accuracy]"   # 正確計測バックエンド (tokenizers)

# 例: Hugging Face から該当モデルの tokenizer.json を取得して配置
python - <<'PY'
from huggingface_hub import hf_hub_download
p = hf_hub_download(repo_id="Qwen/Qwen2.5-0.5B", filename="tokenizer.json")
print(p)   # これを ~/.coderouter-t/tokenizers/ にコピーして tokenizer_path に指定
PY
```

> クラウドモデル（Claude / GPT 等）の厳密なトークナイザが手元に無い場合は、課金挙動が近い公開トークナイザ（例: o200k 相当）を代理に使えます。倍率は近似値になります。

---

## 4. ルーティングで回避する (`cjk_ratio_min`)

最新ユーザーメッセージの **CJK 文字比率がしきい値以上**なら、課金ゼロのローカルプロファイルへ自動ルーティングします。コードや英語のターンはクラウドへ通します。`code_fence_ratio_min` と同じ「ターン単位」の matcher です。

```yaml
auto_router:
  rules:
    - match: { cjk_ratio_min: 0.3 }   # 日本語が3割以上 → ローカル
      profile: local
    - match: { has_tools: true }      # ツール使用 → クラウド
      profile: cloud
  default_rule_profile: cloud
```

`default_profile: auto` のときに発火します。`cjk_ratio_min` は空白を除いた非空白文字に占める CJK 文字の割合（0.0〜1.0）です。

---

## 5. ダッシュボードで見る

`http://localhost:8088/dashboard` の **「Cost & Language Tax」** パネルに、総支出・キャッシュ節約・**言語税（CJK 割増 USD、集計＋プロバイダ別）** が 2 秒ポーリングで表示されます。`tokenizer_path` 未設定なら「no tax measured」と表示されるだけです。

CLI 派は `/metrics.json` の `counters.language_tax_usd_aggregate` / `counters.language_tax_usd`（プロバイダ別）を直接読めます。Prometheus の `/metrics` にも露出します。

---

## 6. 仕組みと確信度

- **計測**: `language_tax.estimate_language_tax(text, tokenizer_path=...)` が char/4（ヒューリスティック）と実トークナイザ（正確）の2本立てでカウントし、`tax_multiplier = 実 / char4`、`extra_tokens = 実 − char4` を返します。
- **コスト**: `compute_cost_for_attempt(..., language_tax=...)` が `extra_tokens × プロバイダの input 単価` で言語税 USD を算出。フリー / ローカルプロバイダは 0。
- **集計**: `cache-observed` ログに `language_tax_usd` / `language_tax_multiplier` を載せ、`MetricsCollector` がプロバイダ別＋集計を保持。

設計上の不変条件:

- **新規コア依存なし**（正確計測は既存の任意 extra `accuracy` の `tokenizers`）。
- **ネットワーク非依存**（`tokenizer.json` のローカル読み込みのみ。HF Hub へアクセスしない）。
- **後方互換**（全フィールド/引数にデフォルト。`tokenizer_path` 未設定なら無効）。

確信度は **MODERATE**: char/4 自体が英語の近似なので、倍率は「CodeRouter 自身の英語基準に対する税」です。とはいえネットワーク不要・推測なしで完全に再現可能な実測値です。

---

## 7. チューニング

- **しきい値**: `cjk_ratio_min` は 0.3 前後が出発点。コード中心で日本語コメントが少し混じる程度ならローカルに送りたくない場合は 0.5 へ上げます。日本語の対話が主ならむしろ 0.2 へ下げます。
- **混在ターンの扱い**: ASCII コードが主体で日本語指示が少量のリクエストは経済的税が 1.2〜1.5× に収まりやすく、`cjk_ratio_min` も低く出ます。ツール使用ターンは `has_tools: true` を上位ルールに置いて確実にクラウドへ。
- **計測だけ先に**: まず `tokenizer_path` を設定して `/dashboard` で実額を見てから、ルーティング回避の効果を `coderouter replay` で A/B 比較するのがおすすめです。
