# Quickstart — 最短で動かす

> この手順書は「最低限の作業で動かす」ことだけを目的にしています。設定の理由や背景は [usage-guide.md](./usage-guide.md) に寄せてあります。

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

> **より良いモデル**を試したい場合（マシンに余裕があるなら）:
>
> ```bash
> # 24 GB+ unified memory / VRAM — note 記事 "日常の王者"、画像入力にも対応
> ollama pull gemma4:26b            # 18 GB / 256K ctx / vision+tools+thinking
>
> # 8-15 GB の laptop で vision を使いたい
> ollama pull gemma4:e4b            # 9.6 GB / 128K ctx / vision+tools+thinking+audio
> ```
>
> **Qwen3.6:35b-a3b を Sonnet 級として狙う場合は Ollama ではなく llama.cpp 直叩き経路を推奨**
> — v1.8.1 〜 v1.8.3 の実機検証 + コミュニティ報告 (X / Reddit) で、Qwen3.6 系
> は **Ollama 経由の chat template / tool 仕様が未成熟**で詰みやすいことが判明。
> Unsloth GGUF + llama.cpp `llama-server` で直叩きすると native `tool_calls` が
> 完璧に動作する。手順は [docs/llamacpp-direct.md](./llamacpp-direct.md) を参照
> (CodeRouter v1.8.3 で実機検証済、`examples/providers.yaml` に provider 例も追加済)。
>
> **ヘッドルーム目安** — OS / ブラウザ / IDE で 8-12 GB 取られるので、
> GGUF サイズに +8-10 GB の余裕を持たせるのが現実的（32 GB Mac で
> 24 GB GGUF を載せると swap で遅くなる）。
>
> RAM を見て自動推奨させたいときは [`./setup.sh`](../setup.sh) を実行
> — RAM tier に応じて安全側のモデルを推奨 + 自動 pull + `~/.coderouter/providers.yaml`
> 生成まで一気にやってくれます。あとで上のような大きいモデルに上げるには
> 手動編集 or `./setup.sh --ram-gb <larger> --force` で再生成。
> 詳しくは v1.8.0 で出した [examples/providers.yaml](../examples/providers.yaml)
> と [docs/hf-ollama-models.md](./hf-ollama-models.md) 参照。

### 2. CodeRouter をインストール

**v1.7.0 から PyPI (`coderouter-cli`) で公開**しています。用途別に 3 経路:

- **(a) `uvx` で都度起動 — 一番軽い**
- **(b) `uv tool install` で PATH に通す — 日常運用**
- **(c) `git clone` + venv — ソースをいじる / 開発**

> 2026 年の Python は macOS (Homebrew) / Ubuntu 23+ / Debian bookworm+ で PEP 668 が効いており、システム Python への素の `pip install` はエラーになります。(a) / (b) / (c) どれも、その問題を踏まない形になっています。

**(a) 一番軽い — `uvx` で都度起動** (PyPI から毎回最新を pull。隔離環境)

```bash
# uv をインストール (既に入っていれば飛ばす)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 起動時に一緒にインストール + 実行される
# 注: PyPI 配布名 (coderouter-cli) と console script 名 (coderouter) が
# 異なるため、uv 0.11+ では --from 形式が必須 (旧 uv でも動く)
uvx --from coderouter-cli coderouter serve --port 8088
```

PyPI のバージョンが常に取れるので、「最新で動かしたい、2 週間ぶりに触る」みたいな人に最適。

**(b) 日常運用 — `uv tool install` で PATH に置く**

```bash
uv tool install coderouter-cli
coderouter --version           # coderouter 1.7.0
coderouter serve --port 8088
```

`pipx` 派なら同等の `pipx install coderouter-cli`。以降 `coderouter` コマンドがどこからでも叩けます。

**(c) ソースを読む / `auto_router:` ルールを手元でいじる場合 — clone + venv**

```bash
git clone https://github.com/zephel01/CodeRouter.git
cd CodeRouter
uv sync                         # venv 自動作成 + 依存インストール
uv run coderouter serve --port 8088
```

毎回 `uv run` プレフィックスを付ければ venv activate は不要 (direnv や shell 起動フックでの自動 activate も一案)。

> **補足**: PyPI 上のパッケージ名は `coderouter-cli` ですが、**コマンド名と Python import 名は `coderouter` のまま**です (`from coderouter import ...` / `coderouter serve ...`)。`pip install` 時の名前だけ若干違う、という形。詳しくは [CHANGELOG `[v1.7.0]`](../CHANGELOG.md#v170--2026-04-25-pypi-公開-uvx-coderouter-cli-一発で動く) 参照。
>
> **v1.8.0 から用途別 4 プロファイル**: `coderouter serve --mode coding|general|multi|reasoning` で起動時に切り替え可能 (デフォルトは `multi`)。詳しくは [CHANGELOG `[v1.8.0]`](../CHANGELOG.md) と [`examples/providers.yaml`](../examples/providers.yaml) のコメントを参照。

### 3. `providers.yaml` を配置

サンプル設定をコピーするだけで OK です (中身は本手順書の構成と一致しています)。

```bash
mkdir -p ~/.coderouter

# 経路 (a) / (b) = uvx / uv tool install で入れた場合は直接ダウンロード
curl -fsSL -o ~/.coderouter/providers.yaml \
  https://raw.githubusercontent.com/zephel01/CodeRouter/main/examples/providers.yaml

# 経路 (c) = clone 済みの場合
# cp examples/providers.yaml ~/.coderouter/providers.yaml
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

必要ならダッシュボードもブラウザで確認: http://localhost:8088/dashboard
(`/healthz` と `/dashboard` は同一ポート上。`--port` を変えた場合はその番号に合わせる)

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

macOS (Homebrew Python) / Ubuntu 23+ / Debian bookworm+ で PEP 668 によりシステム Python への素の `pip install` が拒否されているパターンです。手順 2 の経路 (a) (`uvx --from coderouter-cli coderouter`) または (b) (`uv tool install coderouter-cli`) のどちらかに乗り換えてください。`--break-system-packages` を付けての強行はシステムの Python 環境を壊す原因になるので非推奨です。

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

- [usage-guide.md](./usage-guide.md) — 各設定項目の意味、複数プロバイダの詳細チューニング、doctor の全診断内容
- [security.md](./security.md) — 有料 API を opt-in する時の注意
- [README.md](../README.md) §「CodeRouter は自分に必要か？」 — そもそも自分の用途に要るかの判断フロー
