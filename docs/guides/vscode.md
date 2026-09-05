# VSCode 連携ガイド — CodeRouter を VSCode 上のエージェント拡張から使う

> English: (未整備)

VSCode（および Cursor / Windsurf / VSCodium）上で動く AI 拡張から CodeRouter に繋ぐためのガイドです。結論を先に: **Claude Code は `coderouter vscode-init` で一発、他拡張は下のスニペットをコピペ**。

`ANTHROPIC_BASE_URL` を毎回シェルに export し忘れる、`.envrc` を書いたはいいがワークスペース外に漏れる、といった罠を CLI で構造的に避けます。

---

## 前提 — CodeRouter の 2 つの入口

`coderouter-t serve --port 8088` を起動すると、以下の 2 つの入口が同じプロセスで待ち受けます。

| 入口 | パス | 使う拡張 |
|---|---|---|
| **Anthropic 互換** | `http://localhost:8088`（`/v1/messages`） | Claude Code |
| **OpenAI 互換** | `http://localhost:8088/v1`（`/v1/chat/completions`） | Cline / Roo Code / Kilo Code / Continue.dev |

`coderouter-t serve` の `--port` を指定していない場合、既定は **4000** です。README・本ガイド・`docs/backends/*.md` はすべて 8088 を前提に書いているので、迷ったら `--port 8088` で揃えるのが楽です。

> 別 PC から繋ぐ場合は [remote-access.md](./remote-access.md) を先に。

---

## Claude Code — `coderouter vscode-init` で自動化

VSCode の統合ターミナルから `claude` を叩くとき、`ANTHROPIC_BASE_URL` と `ANTHROPIC_AUTH_TOKEN` が環境変数として渡っている必要があります。手作業だと **シェル起動時のみ有効・別プロジェクトに漏れる・claude.ai コネクタと競合** といった罠を踏みがちなので、`vscode-init` に任せます。

### 使い方

プロジェクトルートで一発:

```bash
cd /path/to/your/project
coderouter vscode-init
```

これで `.vscode/settings.json` に以下が **マージ書き込み** されます（既存キーは触りません）:

```json
{
  "terminal.integrated.env.osx":     { "ANTHROPIC_BASE_URL": "http://localhost:8088", "ANTHROPIC_AUTH_TOKEN": "dummy" },
  "terminal.integrated.env.linux":   { "ANTHROPIC_BASE_URL": "http://localhost:8088", "ANTHROPIC_AUTH_TOKEN": "dummy" },
  "terminal.integrated.env.windows": { "ANTHROPIC_BASE_URL": "http://localhost:8088", "ANTHROPIC_AUTH_TOKEN": "dummy" }
}
```

以後、そのプロジェクトを VSCode で開き、統合ターミナルで `claude` と打つだけで CodeRouter 経由になります。**そのワークスペースにいる間だけ**環境変数が効くので、他プロジェクトや claude.ai には影響しません。

### 主なオプション

```bash
coderouter vscode-init [--target PATH]
                       [--port PORT]        # デフォルト 8088
                       [--profile NAME]     # CODEROUTER_MODE を追加
                       [--with-envrc]       # direnv 用 .envrc も生成
                       [--dry-run]          # 差分だけ表示、書き込まない
                       [--force]            # 既存値の上書き / 未管理の .envrc の取り込み
```

- `--port 4000`: `coderouter-t serve` を素で起動して 4000 で動かしている場合
- `--profile local-first`: 常に `local-first` プロファイルへルーティング（`CODEROUTER_MODE=local-first` が terminal env に載る）
- `--dry-run`: 書き込む予定の内容を unified diff で表示するだけ（`.envrc` を生成する場合はそれも含む）。ファイルは `.bak` も含めて一切作りません
- `--force`: 既定はコンフリクト報告のみで書かない、を解除するフラグ。効き方はファイルごとに違うので次節を参照

#### `--force` の挙動（v2.14.0 以降）

`--force` の効き方は 2 つのファイルで異なります。

- `.vscode/settings.json`: **マージ**です。CodeRouter が管理する 3 キー（`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `CODEROUTER_MODE`）だけを上書きし、`editor.fontSize` などの無関係なキーはそのまま残ります
- `.envrc`: **管理ブロックだけの書き換え**です。CodeRouter が書く部分はマーカーで囲まれ、その内側だけが差し替わります。フェンスの外側の行（例: 手で足した `source_env_if_exists .envrc.local`）は 1 バイトも変わりません

```bash
# BEGIN coderouter-managed
# Managed by `coderouter vscode-init`. Edits inside this block are
# overwritten on the next run; put your own lines outside it.
export ANTHROPIC_BASE_URL="http://localhost:8088"
export ANTHROPIC_AUTH_TOKEN="dummy"
# END coderouter-managed
```

> v2.11〜v2.13 では `--force` 付きの `.envrc` は**ファイル全体の置き換え**で、手で足した行は消えていました。v2.14.0 からはフェンス方式になり、`--force` の意味も「既存の値を上書きする」から「**自分が書いていない `.envrc` を取り込む**」に変わっています。

`.envrc` に `--force` が必要かどうかは、対象ファイルの状態で決まります。

| `.envrc` の状態 | `--force` なし | `--force` あり |
|---|---|---|
| 存在しない | 新規作成 | 同じ |
| フェンスがある | ブロックだけ書き換え（**`--force` 不要**） | 同じ |
| v2.14.0 以前に CodeRouter が生成したまま（同じ `--port` / `--profile` の出力とバイト単位で一致） | 黙ってフェンスに取り込む | 同じ |
| 自分で書いた `.envrc`（フェンスなし） | `conflict`（書かない） | 末尾に管理ブロックを**追記**。既存行はすべて残る |

- フェンスの**外側**で `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `CODEROUTER_MODE` を export しているフェンス済みファイルは `conflict` になり、`--force` を付けても書きません。direnv は export を上から順に適用するため、どちらが有効か分からない重複を作らない、という判断です。その行を消すか、フェンスの内側へ移してください。`#` で始まるコメント行は競合と見なしません
- フェンスの無い `.envrc` を `--force` で取り込む場合だけは例外で、この競合チェックは走りません。管理ブロックが末尾に追記されるため、同名の export が上にあっても後勝ちで CodeRouter の値が有効になります（元の行は消えずに残ります）

既存ファイルの中身が変わるときは、`--force` の有無に関係なく**必ず退避コピーを作ります**。

| 元のファイル | 退避先 |
|---|---|
| `.envrc` | `.envrc.bak` |
| `.vscode/settings.json` | `.vscode/settings.json.bak` |

- 退避先のパスは実行結果の `→` 行に表示されます
- `unchanged`（変更なし）と `conflict`（書かない）のときは `.bak` も作りません
- `--dry-run` では退避も含めて**一切ファイルを作りません**（差分と「どこへ退避するか」だけ表示）
- `.bak` は 1 世代だけで、毎回上書きされます（mode と mtime は元ファイルのまま保存）。戻したいときは次節の `coderouter rollback`
- `*.bak` はプロジェクトの `.gitignore` に入れておくことを推奨します

#### 間違えたときの戻し方 — `coderouter rollback`（v2.14.0）

`.bak` からの復元は CLI でできます。

```bash
coderouter rollback --workspace . --dry-run   # 何が戻るか確認
coderouter rollback --workspace .             # .vscode/settings.json と .envrc を復元
```

- 復元は**スワップ**です。現在の内容が新しい `.bak` になるので、2 回実行すると元に戻ります
- `--workspace` は復元対象の**追加**です。既定では `providers.yaml` と `~/.coderouter-t/model-capabilities.yaml`（`doctor --apply` の書き込み先）も対象に入ります。1 ファイルだけ戻したいときは `coderouter rollback --path .envrc` のように `--path` で指定してください（指定するとその他の探索は行いません）
- 終了コードは **0**=復元した / **2**=戻すものが無かった / **1**=復元に失敗

### 再実行しても壊れない

`vscode-init` は冪等です。同じ引数で再実行すれば `unchanged` を報告して終了。異なる値と衝突した場合は `conflict` を出して**ファイルに触りません**（`--force` 必要）。オンボーディングスクリプトに含めて安全です。

`--port` を変えての再実行も、`.envrc` がフェンス済みなら単なる更新（`updated`）で、`--force` は要りません。`conflict` になるのは `.vscode/settings.json` の管理キーが違う値のときと、上表の `.envrc` のケースだけです。

### direnv 派の場合

```bash
coderouter vscode-init --with-envrc
```

これで `.envrc` も生成されます。生成後に **`direnv allow`** を 1 回実行してください。シークレットは `.envrc.local` に分けて `source_env_if_exists .envrc.local` の形が安全です（`.envrc` はプロジェクトに commit することが多いので）。

パーミッションについて（v2.11 以降）:

- **新規生成した `.envrc` は `0600`（所有者のみ読み書き）**で作られます。`ANTHROPIC_AUTH_TOKEN` を含むファイルなので、`coderouter --check-env` が `.env` に課しているのと同じ基準に揃えてあります
- **既存ファイルを上書きするときは、そのファイルの現在の mode をそのまま維持**します（以前は `os.replace` で umask 既定＝多くの環境で `0644` に戻っていました）
- Windows では POSIX の mode ビットに意味がないため、mode の設定・維持は行いません
- なお v2.14.0 以降、`--force` を付けてもフェンスの外側の行は消えません。`source_env_if_exists .envrc.local` のような手書きの行はそのまま残ります（v2.13 以前は消えていました）

### 注意点

- `.vscode/` は CodeRouter リポジトリの `.gitignore` に既に入っているため、`.vscode/settings.json` は既定で **git に含まれません**。あなたのプロジェクトの `.gitignore` は個別に確認してください
- `ANTHROPIC_AUTH_TOKEN` はダミー値です（CodeRouter は検証しない）。**本物の API キーは絶対に置かない**でください
- **claude.ai コネクタとの競合**: `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` がグローバルに export されていると、`claude.ai connectors are disabled…` のエラーが出ます。`vscode-init` はワークスペース内のターミナルにだけ export するので構造的に安全ですが、既存の `.zshrc` / `.bashrc` に手書きで残っていたら消してください

---

## Cline / Roo Code / Kilo Code — 手動設定

これらは自前の設定 UI を持つので、`vscode-init` は触りません。拡張の設定画面で以下を入れます。

| 項目 | 値 |
|---|---|
| API Provider | **OpenAI Compatible** |
| Base URL | `http://localhost:8088/v1` |
| API Key | `dummy`（任意の非空文字列） |
| Model ID | 任意（CodeRouter が `default_profile` / `auto_router` で解決） |

プロファイルを明示指定したい場合は、拡張がカスタムヘッダを送れるなら `X-CodeRouter-Profile: <プロファイル名>` を追加してください。送れない拡張なら、`providers.yaml` の `default_profile` を切り替えるか、`auto_router.rules` の `model_pattern` で拡張が送る Model ID にマッチさせます。

---

## Continue.dev — `config.json` にスニペット追記

`~/.continue/config.json` の `models` 配列に以下を追加:

```json
{
  "title": "CodeRouter",
  "provider": "openai",
  "model": "any-model-id",
  "apiBase": "http://localhost:8088/v1",
  "apiKey": "dummy"
}
```

Continue はモデル ID をそのままサーバに渡すので、CodeRouter 側で `auto_router.rules` に `model_pattern: any-model-id` のようなマッチを書けば、Continue から届いたリクエストを狙いのプロファイルへ回せます。

Anthropic 互換入口（`/v1/messages`）を叩きたい場合は `"provider": "anthropic"` + `"apiBase": "http://localhost:8088"` にしてください。

---

## プロファイルの指定順（precedence）

どの入口・どの拡張から届いたリクエストも、CodeRouter が最終的にどのプロファイルを使うかは次の順で決まります:

```
body.profile > X-CodeRouter-Profile ヘッダ > X-CodeRouter-Mode ヘッダ > auto_router > default_profile
```

拡張がカスタムヘッダ / body に何も足せない場合は、`default_profile` か `auto_router` で受けます。

---

## トラブルシューティング

### `claude.ai connectors are disabled...` が出る

`ANTHROPIC_AUTH_TOKEN` または `ANTHROPIC_API_KEY` がグローバル環境に残っています。`.zshrc` / `.bashrc` を確認して手書きの `export` を消し、ワークスペーススコープ（`vscode-init` が書く `terminal.integrated.env.*` か direnv `.envrc`）だけに絞ってください。

### `vscode-init` が `conflict` を出す

既存 `.vscode/settings.json` の `ANTHROPIC_BASE_URL` が違う値のときの安全側動作です。`--dry-run` で差分を確認し、上書きしていいなら `--force` で再実行。

`.envrc` の `conflict` は 2 種類あります。フェンスの無い既存 `.envrc` を見つけた場合は `--force` で管理ブロックを追記できます（既存行は残ります）。フェンスの外側で `ANTHROPIC_BASE_URL` などを export している場合は `--force` でも書かないので、その行を消すかフェンスの内側へ移してから再実行してください。

### 統合ターミナルの `claude` が繋がらない

VSCode を**開き直してください**。`terminal.integrated.env.*` は新規ターミナル起動時にしか反映されません。既存のターミナルは古い env を持ち続けます。

### Cline 等から 403 `Host '...' is not allowed`

CodeRouter 側の Host 検証（DNS リバインディング対策）に引っかかっています。localhost で完結する構成なら発生しないはずですが、`--host 0.0.0.0` にしていたり、別ホスト名で叩いている場合は `CODEROUTER_ALLOWED_HOSTS` を設定してください（詳細は [remote-access.md](./remote-access.md)）。

### 別 PC の VSCode から繋ぎたい

[remote-access.md](./remote-access.md) の SSH トンネル or Tailscale が推奨。トンネル側で `localhost:8088` として現れるようにすれば、上記のスニペット・`vscode-init` はそのまま動きます。

---

## 関連

- [Quickstart](../start/quickstart.md) — CodeRouter の 10 分導入
- [利用ガイド](./usage-guide.md) — `providers.yaml` の書き方
- [リモートアクセス](./remote-access.md) — 別 PC から繋ぐ
- [セキュリティ](./security.md) — 信頼境界と脅威モデル

---

最終更新: 2026-08-10（v2.14.0 時点）
