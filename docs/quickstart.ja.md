# Quickstart — 最短で動かす

> この手順書は「最低限の作業で動かす」ことだけを目的にしています。設定の理由や背景は [usage-guide.ja.md](./usage-guide.ja.md) に寄せてあります。

Claude Code または codex CLI を、ローカルの Ollama で **$0** で回せる状態までを 10〜15 分で作ります。チェーンは共通で、最後のステップだけエージェント別に分かれます。

**構成**

```
Claude Code / codex  →  CodeRouter (localhost:8088)
                            ├─ ① ollama qwen2.5-coder:7b   (ローカル、主)
                            ├─ ② ollama qwen2.5-coder:1.5b (ローカル、軽量フォールバック)
                            └─ ③ OpenRouter qwen3-coder:free (無料クラウド、最後の砦)
```

有料 API は `ALLOW_PAID` 環境変数を設定しない限り**絶対に呼ばれません**。macOS / Linux どちらでも同じ手順で動きます。

---

## 前提

- Python 3.12 以上 (`python3 --version` で確認)
- `git`
- 空きディスク 6 GB 程度 (7b が約 4.7GB、1.5b が約 1GB)
- Ollama がインストール済み、または以下の手順でインストール

OpenRouter の無料枠を使う場合は [openrouter.ai](https://openrouter.ai/) で無料アカウントを作って API キーを 1 本発行しておく (任意、飛ばしてもローカルだけで動きます)。

---

## 共通セットアップ (Pattern A/B で使う 6 ステップ)

### 1. Ollama をインストール + モデル 2 本を pull

```bash
# macOS / Linux どちらも同じ
curl -fsSL https://ollama.com/install.sh | sh

# モデルを pull (合計 ~6GB、ネット速度によるが 5〜15 分)
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5-coder:1.5b

# Ollama サービスを起動 (macOS は自動起動、Linux は systemd)
ollama serve &   # すでに動いていれば不要
```

### 2. CodeRouter をインストール

用途別に 2 経路あります。**Claude Code / codex の前に立てて使うだけ**なら (a) の `uv tool install` 1 発が最短です。`providers.yaml` を書き換えて遊んだり、ソースを読みたい場合は (b) の clone + venv に進んでください。

> 2026 年の Python は macOS (Homebrew) / Ubuntu 23+ / Debian bookworm+ で PEP 668 が効いており、システム Python への素の `pip install` はエラーになります。(a) / (b) どちらも、その問題を踏まない形になっています。

**(a) とりあえず使いたい場合 — `uv tool install` 1 発**

```bash
# uv をインストール (既に入っていれば飛ばす)
curl -LsSf https://astral.sh/uv/install.sh | sh

# CodeRouter を隔離された tool 環境に入れる (`coderouter` コマンドが PATH に乗る)
uv tool install --from git+https://github.com/zephel01/CodeRouter.git coderouter
```

`pipx` 派なら同等の `pipx install git+https://github.com/zephel01/CodeRouter.git` でも構いません。(a) を選んだ場合は、後の手順 3 で置く `providers.auto.yaml` / `providers.auto-custom.yaml` だけ別途取得しておきます:

```bash
# examples だけ拾う
curl -fsSL -o ~/.coderouter/providers.yaml \
  https://raw.githubusercontent.com/zephel01/CodeRouter/main/examples/providers.yaml
```

**(b) ソースを読む / `auto_router:` ルールを手元でいじる場合 — clone + venv**

```bash
git clone https://github.com/zephel01/CodeRouter.git
cd CodeRouter
python3 -m venv .venv
source .venv/bin/activate        # Windows は .venv\Scripts\activate
pip install -e .
```

`coderouter serve` を実行するターミナルでは毎回 `source .venv/bin/activate` が必要です (direnv や shell 起動フックを使うのも一案)。

### 3. `providers.yaml` を配置

サンプル設定をコピーするだけで OK です (中身は本手順書の構成と一致しています)。

```bash
mkdir -p ~/.coderouter
# 経路 (b) = clone 済みの場合
cp examples/providers.yaml ~/.coderouter/providers.yaml

# 経路 (a) = uv tool install で入れた場合は直接ダウンロード
# curl -fsSL -o ~/.coderouter/providers.yaml \
#   https://raw.githubusercontent.com/zephel01/CodeRouter/main/examples/providers.yaml
```

### 4. (任意) OpenRouter API キーを設定

ローカル 2 モデルで十分なら飛ばして OK。無料クラウドを最終砦として使いたい場合のみ:

```bash
export OPENROUTER_API_KEY="sk-or-v1-xxxxxxxxxxxxxxxx"
```

永続化したい場合は `~/.zshrc` / `~/.bashrc` に同じ行を追記してください。

### 5. CodeRouter を起動

```bash
coderouter serve --port 8088
```

別ターミナルで、起動確認:

```bash
curl http://localhost:8088/healthz
# → {"status":"ok"}
```

必要ならダッシュボードもブラウザで確認: http://localhost:4000/dashboard
(デフォルトは 4000 番、`--port` で変えた場合は合わせる。`/healthz` と `/dashboard` は同一ポート上)

### 6. `coderouter doctor` で設定が効いているか確認 (任意、推奨)

```bash
coderouter doctor --check-model ollama-qwen-coder-7b
```

`OK` が出れば Claude Code / codex の実利用時に躓く典型パターンはクリア済みです。

---

## Pattern A: Claude Code で使う

### A-1. Claude Code をインストール

```bash
npm install -g @anthropic-ai/claude-code
```

### A-2. 環境変数で CodeRouter に向ける

```bash
export ANTHROPIC_BASE_URL="http://localhost:8088"
export ANTHROPIC_AUTH_TOKEN="dummy"   # CodeRouter は認証を見ない、ダミーで OK
```

### A-3. 起動

```bash
claude
```

profile は自動で `claude-code` (ローカル 2 段 + 無料クラウド) が選ばれます。最初のプロンプトで Ollama が答えているかは、別ターミナルで `coderouter stats --once` を叩くか、ダッシュボードの Providers パネルで `ollama-qwen-coder-7b` が緑になっていることで確認できます。

---

## Pattern B: codex CLI で使う

### B-1. codex をインストール

```bash
npm install -g @openai/codex
```

### B-2. 環境変数で CodeRouter に向ける

```bash
export OPENAI_BASE_URL="http://localhost:8088/v1"
export OPENAI_API_KEY="dummy"   # ダミーで OK (同上)
```

### B-3. 実行

```bash
codex "write a python function that reverses a string"
```

同じバックエンドチェーンに対して OpenAI 形式で話しかけているだけです。profile は `default` (ローカル 7b + 無料クラウド) が選ばれます。

---

## うまく動かない時 (よくある 3 つ)

### (1) `coderouter serve` が `address already in use` で落ちる

別プロセスが 8088 を掴んでいます。別ポートで起動するか、該当プロセスを落とす:

```bash
lsof -i :8088        # 占有プロセスを確認
coderouter serve --port 8089   # ポート変更で逃げる
```

ポートを変えたら `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` も合わせて変更。

### (2) Claude Code / codex が応答を返さない

Ollama 自体が起動していない可能性が高いです。

```bash
curl http://localhost:11434/api/version
# → {"version":"0.x.x"}
```

これが繋がらない場合は `ollama serve` を再起動してください。繋がるなら `coderouter doctor --check-model ollama-qwen-coder-7b` で詳細診断。

### (3) 応答に `<think>...</think>` が混入する

`providers.yaml` の `output_filters: [strip_thinking]` が有効になっているか確認してください。サンプル配布版では最初から有効です。

### (4) `pip install` が `externally-managed-environment` で落ちる

macOS (Homebrew Python) / Ubuntu 23+ / Debian bookworm+ で PEP 668 によりシステム Python への素の `pip install` が拒否されているパターンです。手順 2 の経路 (a) (`uv tool install`) または経路 (b) (venv を作ってから `pip install -e .`) のどちらかに乗り換えてください。`--break-system-packages` を付けての強行はシステムの Python 環境を壊す原因になるので非推奨です。

---

## 補足: プロファイル選択を CodeRouter に任せる (v1.6 `auto_router`)

Pattern A/B では「Claude Code は `claude-code` profile、codex は `default` profile」のようにクライアントごとに profile が固定されていました。v1.6 から、リクエスト本文の中身 (画像が付いているか / コード濃度が高いか / それ以外) を見て profile を自動で選ぶ `auto_router` が使えます。**まず動かして、profile を意識せずに使いたい** 人向けのショートカットです。

### C-1. モデルを 3 本 pull する

`auto_router` が既定で想定している 3 profile (`multi` / `coding` / `writing`) 用にモデルを揃えます。コード用・文章用はすでに入っているはずなので、画像対応の VL モデルを追加するだけです。

```bash
ollama pull qwen2.5:7b           # writing  (~4.7 GB)
ollama pull qwen2.5vl:7b         # multi    (~6 GB、画像を送らないなら省略可)
# qwen2.5-coder:7b は共通セットアップ #1 で pull 済み
```

画像を一切送らないなら `qwen2.5vl:7b` は省略して構いません。画像リクエストだけが明示的にエラーで落ちる (fast-fail) だけで、テキストリクエストは動き続けます。

### C-2. `providers.yaml` を `providers.auto.yaml` に差し替える

```bash
cp examples/providers.auto.yaml ~/.coderouter/providers.yaml
```

中身の肝は 2 行だけです:

```yaml
default_profile: auto   # ← この sentinel が auto_router を有効化する
# profiles: には multi / coding / writing の 3 本を用意しておく
```

この状態で `coderouter serve` を再起動すると、以降のリクエストは内蔵ルール (画像添付 → `multi` / コードフェンス比率 ≥ 0.3 → `coding` / それ以外 → `writing`) で自動振り分けされます。`X-CodeRouter-Profile` ヘッダや `body.profile` で都度上書きする道は残っているので、「普段は任せて、特定のリクエストだけ手動指定」という使い方も可能です。

### C-3. ルールをカスタマイズしたい場合

「翻訳依頼は文章モデル」「"Review this PR" が含まれるリクエストはコードモデル」のように、独自ルールを足したくなったら `examples/providers.auto-custom.yaml` をベースに `auto_router:` ブロックを書き足します。このブロックが存在する場合、内蔵ルールは一切マージされず丸ごと上書き (first match wins) になります。ルール間で順序が重要なことと、`match:` には matcher を 1 種類だけ書く (複数書くと起動時に fail) の 2 点に注意してください。

---

## 次に読むもの

- [usage-guide.ja.md](./usage-guide.ja.md) — 各設定項目の意味、複数プロバイダの詳細チューニング、doctor の全診断内容
- [security.md](./security.md) — 有料 API を opt-in する時の注意
- [README.ja.md](../README.ja.md) §「CodeRouter は自分に必要か？」 — そもそも自分の用途に要るかの判断フロー
