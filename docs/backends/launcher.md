# Launcher ガイド — llama.cpp / vllm / mlx を GUI で起動する

> English: [`launcher.en.md`](./launcher.en.md)

CodeRouter Launcher は、ローカル推論バックエンド(llama.cpp / vllm / mlx)を**画面の操作で起動・管理**するツールです。長い起動コマンドを毎回打つ代わりに、モデルを選んでボタンを押すだけで起動できます。

Launcher には 2 つの形態があります。

- **デスクトップGUI版**(`launcher_gui.py`)— tkinter 製のデスクトップアプリ。ブラウザ不要。CodeRouter 自体もここから起動できる。
- **Web版**(`/launcher`)— CodeRouter が配信するブラウザページ。

設定(`providers.yaml` の `launcher:` ブロック)・画面構成・トラブルシューティングは両版で共通です。本ガイドは共通部を 1 回ずつ記載しています。

> バックエンドの導入手順は [バックエンド インストール手順書](./install-backends.md)、導入から起動までの通し手順は [Launcher クイックスタート](./launcher-quickstart.md) を参照してください。

---

## 概要 — Launcher でできること

- `model_dirs` 配下の `.gguf` / `.safetensors` 等を再帰スキャンしてモデル一覧を表示
- オプションプロファイル(プリセット)をドロップダウンで選んで起動
- 複数プロセスの同時管理(llama.cpp + vllm 並走など)
- 各プロセスのログをリアルタイム確認
- 搭載メモリと照らしたモデルの[メモリ推奨](#メモリ推奨)表示

---

## 2 つのランチャー — どちらを使うか

| | デスクトップGUI版(`launcher_gui.py`) | Web版(`/launcher`) |
|---|---|---|
| 形態 | tkinter デスクトップアプリ | ブラウザページ |
| CodeRouter の起動 | **このアプリから起動できる** | できない(CodeRouter の中で動くため) |
| 主な用途 | 最初の一発 — backend と CodeRouter をまとめて立ち上げる | CodeRouter 稼働中に backend を管理する運用 UI |
| 設定 | `providers.yaml` の `launcher:` ブロック(共通) | 同左 |

両者は競合ではなく補完関係です。**最初の一発(ブートストラップ)はデスクトップ版、CodeRouter が回り始めた後の日常運用は Web版**、という住み分けになります。

---

## デスクトップGUI版 — 起動方法

`launcher_gui.py` は backend と CodeRouter を**ブラウザなし**で起動・管理する tkinter 製アプリです。CodeRouter 自体もこの GUI から直接起動でき、ローカル LLM を Claude Code に繋ぐまでを 1 ウィンドウで完結できます。

### 必要なもの

- Python 3.10 以上
- tkinter — Python 標準ライブラリ(追加インストール不要。一部の Linux では `python3-tk` パッケージが別途必要)
- PyYAML — CodeRouter の既存依存。CodeRouter の venv から実行すれば自動的に揃う

### 起動

```bash
# 通常起動
python3 launcher_gui.py

# CodeRouter の venv 経由(PyYAML を確実に使う)
uv run python launcher_gui.py

# 設定ファイルを明示指定
python3 launcher_gui.py --config ~/.coderouter-t/providers.yaml
```

設定ファイルの探索順: ① `--config` 指定 → ② カレントの `providers.yaml` → ③ `~/.coderouter-t/providers.yaml`。どれも無ければ空の設定で起動します(UI から手動入力すれば起動自体は可能)。

> **v2.13.0 以降、② のカレントディレクトリ `providers.yaml` の暗黙読込は既定で無効です。** 悪意のある `providers.yaml` を作業ディレクトリに置かれるだけで `launcher.backends[*].binary` 等の実行ファイル指定を乗っ取られ得るためのセキュリティ対策で、`CODEROUTER_ALLOW_CWD_CONFIG=1`(`true`/`yes`/`on` も可)を設定したときだけオプトインで有効になります。未設定のままカレントに `providers.yaml` があると読み込まずスキップします。

### CodeRouter バー(デスクトップ版のみ)

デスクトップ版の最上部には、Web版に無い **CodeRouter バー**があります。

- ステータスドット — `停止中` / `起動中…` / `稼働中` / `エラー` を色付き表示
- ポート — CodeRouter のリッスンポート(既定 `8088`)。停止中・エラー時のみ編集可
- ▶ CodeRouter 起動 / ■ 停止
- Claude Code 接続文字列 — `ANTHROPIC_BASE_URL=http://localhost:<ポート> ANTHROPIC_AUTH_TOKEN=dummy claude`。クリックまたは「コピー」でクリップボードへ

CodeRouter 起動時、`~/.coderouter-t/providers.yaml` が無ければ最小構成を自動生成します(この自動生成ファイルには `launcher:` ブロックは含まれません — 後述)。ウィンドウを閉じると、起動した CodeRouter と全 backend プロセスは自動的に停止します。

---

## Web版 — 起動方法

CodeRouter が稼働しているとき、ブラウザで使う運用 UI です。

1. `providers.yaml` に `launcher:` セクションを追加([設定リファレンス](#設定リファレンス)参照)
2. CodeRouter を起動 — `coderouter-t serve --port 8088`
3. ブラウザで `http://localhost:8088/launcher` を開く

---

## 画面の使い方

Launcher の画面は「MODELS パネル」「LAUNCH フォーム」「PROCESSES テーブル」「ログ」で構成されます。見た目はデスクトップ版(tkinter)と Web版(ブラウザ)で異なりますが、**構成と操作は共通**です。

### MODELS パネル

- スキャンボタンで `model_dirs` を再スキャンしてモデル一覧を更新
- モデル名をクリックすると「モデルパス」欄に自動入力(デスクトップ版は「名前」も自動入力。手入力した名前は保持される)
- ファイルサイズ (GB) を併記。VRAM / メモリと相談しやすい
- 各モデルに**メモリ推奨バッジ**(`✓ 推奨` / `⚠ メモリ厳しい`)を表示 → [メモリ推奨](#メモリ推奨)
- ヘッダに検出ハード(例: `Metal · RAM 64GB`)を表示
- 対象拡張子: `.gguf` `.safetensors` `.bin` `.pt` `.pth` `.ggml`(サブフォルダも再帰検索)

### LAUNCH フォーム

| 項目 | 説明 |
|---|---|
| **名前** | 管理用の任意の識別子(例: `qwen-coder-8080`) |
| **ポート** | 起動するサーバーのポート(既定 `8080`) |
| **バックエンド** | `llama.cpp` / `vllm` / `mlx` から選択。解決されたバイナリパスと利用可否が下に表示される |
| **モデルパス** | MODELS パネルから選択するか直接入力 |
| **オプションプロファイル** | `providers.yaml` で定義したプリセットを選択 |
| **MTP/draft gguf** | 明示的な companion draft/MTP gguf のパス(llama.cpp のみ)。空欄なら自動検出 → [MTP / speculative decoding](#mtp--speculative-decoding-llamacpp) |
| **MTP** | `auto`(既定、自動検出)/ `off`(speculative decoding を無効化) |
| **追加オプション** | プロファイルにないフラグをその場で入力。`shlex` でパースされコマンド末尾に追加される |

`▶ 起動` でプロセスが起動し、PROCESSES テーブルに表示されます。バイナリが見つからない場合は**起動ボタンが自動的に無効化**され、理由が表示されます。「追加オプション」欄の横の **⚙ 推奨値** ボタンについては [メモリ推奨](#メモリ推奨) を参照してください。

### provider 自動同期(v2.7.4、Web版のみ)

Web 版で backend を起動すると、その backend が **provider として自動登録**されます(providers.yaml の編集不要)。

- provider 名は `launcher-<backend>-<port>`(例: `launcher-llamacpp-8085`)。同名で再起動するとエントリは**置き換え**られ、重複しません
- 登録先は `launcher` プロファイル(無ければ自動作成)。**最後に起動した backend が先頭**に来ます
- ルーティングは明示オプトイン: `X-CodeRouter-Profile: launcher` ヘッダ、または body の `"profile": "launcher"`。**`default_profile` は変更されません**
- 登録は**メモリ内のみ**です(providers.yaml には書き込みません — 手書きコメントを壊さないため)。serve を再起動すると消えますが、Launcher のプロセス自体も同寿命なので整合します。恒久化したい場合は providers.yaml に手で転記してください
- provider は `model: ""` で登録されるため、`/v1/models` は**上流がロード中の実モデル ID(gguf 名)**を返します(モデル名パススルー、同 v2.7.4)。gguf を差し替えても config 編集は不要で、外部ベンチマークからモデルを識別できます(30 秒 TTL キャッシュあり)

動作確認:

```bash
# 起動後
curl http://localhost:8088/v1/models
#   → "id": "<ロード中のgguf名>", "owned_by": "coderouter/launcher-llamacpp-8085"

curl http://localhost:8088/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-CodeRouter-Profile: launcher' \
  -d '{"model":"x","messages":[{"role":"user","content":"say hi"}]}'
#   → coderouter_provider が launcher-llamacpp-<port> なら疎通OK
```

デスクトップ GUI 版(launcher_gui.py)は別プロセスで動くため自動同期の対象外です。従来どおり providers.yaml のエントリ(初回自動生成される `llama-cpp-local` など)の `base_url` を、起動ポートに合わせてください。

### providers.yaml とのポート整合(食い違いを構造的に防ぐ)

Launcher が起動する backend のポートと、`providers.yaml` の `base_url` に書かれたポートが**ズレると黙って死にます**。CodeRouter は「そのポートに繋がる backend が居ない」ことを起動時には知らないので、実リクエストが飛んだ瞬間に `transport error: All connections failed` を返し、フォールバックがあれば次のプロバイダが救う(=**ダッシュボードで llama-cpp-local が半々失敗、vllm-local が 100%** という絵になる)、無ければ 502 で作業が止まります。3 通りの回避策があります。

**方式 (A) — ハードコード派(従来どおり、Launcher デスクトップ GUI 版と併用)**

`providers.yaml` に手書きしたエントリを真実の源にします。プロバイダ名を意図的に固定したい(Cline / Continue から `model` ID で狙いたい、`auto_router.rules` で名前を指定したい)場合はこれ。

```yaml
providers:
  - name: llama-cpp-local
    kind: openai_compat
    base_url: http://localhost:8085/v1   # ← 真実の源
    model: qwen2.5-coder:14b
```

Launcher 側は**同じ 8085 で起動**する必要があります。LAUNCH フォームのポート欄に 8086 と入れて `providers.yaml` は 8085 のまま、というのがハマりの定型パターン。作業開始前に `coderouter doctor --check-model llama-cpp-local` を 1 回叩けば疎通が確認できます(下の「予防ワンライナー」)。

**方式 (B) — Launcher 自動同期派(v2.7.4 以降、Web 版のみ)**

Launcher が起動した瞬間に provider 名にポートが**埋め込まれた**エントリ(`launcher-llamacpp-8085` など)が自動登録されるので、`providers.yaml` にはそもそも書きません。ポートが変わっても provider 名が変わるだけで、`X-CodeRouter-Profile: launcher` で流している限りルーティングは追従します。デスクトップ GUI 版は対象外なので注意。

**方式 (C) — 折衷(推奨、名前は固定・ポートは 1 箇所で定義)**

`providers.yaml` にハードコードした provider を残しつつ、Launcher の `option_profiles` に**同じポート**を書いておくと、プリセットを選ぶだけで整合が担保されます。ズレ得ないので運用が最も楽です。

```yaml
providers:
  - name: llama-cpp-local
    kind: openai_compat
    base_url: http://localhost:8085/v1

launcher:
  option_profiles:
    llama.cpp:
      - name: "GPU フル活用 (8085)"
        args:
          "-ngl": 99
          "--ctx-size": 4096
          "--port": 8085     # ← providers.yaml と同じ値を書く
```

**予防ワンライナー**(方式 A / C 向け):

```bash
# Launcher で backend を起動した直後・作業開始前に 1 回叩く
coderouter doctor --check-model llama-cpp-local
#   → auth+basic-chat が [OK] なら疎通確認完了
#   → transport error が出たらポート不一致を疑う
```

シェル関数化しておくと楽です:

```bash
# ~/.zshrc / ~/.bashrc
cr-check() {
  coderouter doctor --check-model llama-cpp-local || return 1
  coderouter doctor --check-model vllm-local || return 1
  echo "✅ CodeRouter chain is healthy"
}
```

事象そのものの見え方(ダッシュボードの `unhealthy` バッジや半々失敗の Recent Events)は
[トラブルシューティング §1-7](../guides/troubleshooting.md#1-7-transport-error-all-connections-failed--ポート不一致の疑い) にまとまっています。

### PROCESSES テーブル

起動した backend プロセスの一覧です。NAME / BACKEND(llama.cpp / vllm / mlx)/ MODEL / PORT / PID / STATUS(`starting` / `running` / `stopped` / `error` を色分け)を表示し、プロセスを選んで **停止**(SIGTERM)・**削除**(レジストリから除去)・**ログ表示**ができます。

### ログ

選択中プロセスの標準出力 / 標準エラーをリアルタイム表示します。Web版はログパネルが running 中に 3 秒ごと自動更新されます。長時間稼働でもメモリを圧迫しないよう、保持行数・表示行数に上限が設けられています。

### 典型的な使い方(デスクトップ版)

1. **モデルを選ぶ** — MODELS から使うモデルをクリック
2. **backend を起動** — オプションプロファイルを選び起動ボタンを押す。PROCESSES に `running` で表示される
3. **CodeRouter を起動** — 上部バーの「▶ CodeRouter 起動」
4. **Claude Code を繋ぐ** — 接続文字列をコピーしてターミナルで実行

---

## MTP / speculative decoding (llama.cpp)

llama.cpp の `llama-server` は Multi-Token Prediction (MTP) / speculative decoding を `--spec-type` 系フラグでサポートします。Launcher は LAUNCH フォームの **MTP/draft gguf** 欄と **MTP** 欄(`auto` / `off`)から、これらのフラグを自動的に組み立てます。**llama.cpp バックエンドのみ対応** — vllm / mlx で `draft_model_path` や `mtp_mode` を指定すると起動リクエストは 400 で拒否されます。

### 自動検出の順序(`mtp_mode: auto`、既定)

1. **内蔵 nextn** — 選択したメインの gguf のメタデータに `{arch}.nextn_predict_layers > 0` があれば、追加の draft モデルなしで `--spec-type draft-mtp` を付与します
2. **同フォルダの companion gguf** — 内蔵 nextn が無ければ、メインの gguf と**同じフォルダ**を走査し、以下をすべて満たす gguf を companion として採用します
   - ファイル名に `mtp` または `draft` を含む、またはメインファイルの名前プレフィックス(shard / 量子化サフィックスを除いた部分)を共有する
   - ファイルサイズがメインの gguf の 50% 未満
   - gguf の architecture が読み取れる場合、メインと一致する(不一致は却下 — トークナイザ/語彙の不一致を避けるため)

   採用されると、ファイル名に `mtp` を含む候補は `--spec-type draft-mtp`、それ以外は `--spec-type draft-simple` として `--model-draft <path>` と共に付与されます。
3. **見つからない場合** — speculative decoding なしで通常起動します。プロセスログに `[launcher] MTP/draft gguf not found next to <main>.gguf; starting without speculative decoding` と記録されます。

### 明示的な draft/MTP gguf を指定する

**MTP/draft gguf** 欄に companion の gguf を直接指定できます。指定したパスが存在しない場合は起動リクエストが 400 で拒否されます。ファイル名に `mtp` を含む場合は `--spec-type draft-mtp`、それ以外は `--spec-type draft-simple` になります。

### `mtp_mode: off`

**MTP** 欄で `off` を選ぶと speculative decoding のフラグを一切付与しません(従来どおりの起動コマンド)。`off` と **MTP/draft gguf** の同時指定は矛盾するため 400 で拒否されます。

### 追加オプションで `--spec-type` を指定済みの場合

「追加オプション」またはオプションプロファイルに既に `--spec-type` が含まれている場合、Launcher の自動検出は完全にスキップされます(フラグは追加されません)。ユーザー指定が常に優先されます。

### `-md` / `--model-draft` は追加オプションで使えない

`-m` / `--model` と同様、draft モデルのパスは **MTP/draft gguf** 欄でのみ指定できます。`-md` / `--model-draft` / `--spec-draft-model` を「追加オプション」やオプションプロファイルに書くと起動リクエストは 400 で拒否されます。残りの speculative 系フラグ(`--spec-type` / `--spec-draft-n-max` / `--spec-draft-n-min` / `--spec-draft-p-min` / `-ngld` / `-devd`)は引き続き自由入力できます。

### 既知の問題: `--split-mode tensor` との組み合わせ(llama.cpp issue #24309)

nextn 埋め込みモデル / speculative decoding 有効時に `--split-mode tensor` を組み合わせると llama.cpp がクラッシュする既知の問題があります([issue #24309](https://github.com/ggml-org/llama.cpp/issues/24309))。Launcher はこの組み合わせを検出しても起動はブロックせず、プロセスログに `--split-mode layer` を推奨する警告を記録します。

### 自動検出 MTP が起動時にクラッシュした場合の自動フォールバック

**自動検出**(`mtp_mode: auto`)で付与した speculative フラグが原因で backend が**起動直後(約 3 分以内)にクラッシュ**した場合、Launcher は speculative フラグを外して**自動的に 1 回だけ再起動**します。一部のアーキテクチャの `draft-mtp` サポートは llama.cpp 側でまだ成熟しておらず、検出は正しくても MTP コンテキストの初期化に失敗してプロセスが落ちることがあるためです(例: `failed to measure MTP context memory` / `requires ctx_other to be set`)。プロセスログには `[launcher] MTP startup failure detected (exit code ...); retrying without speculative decoding` と、フラグを外した再起動コマンドが記録されます。再起動は**必ず 1 回まで**で、それでも落ちる場合は `error` になります。**明示的な MTP/draft gguf**(`draft_model_path`)を指定した場合や、`--spec-type` を自分で渡した場合は自動再起動の対象外です(ユーザー指定を尊重します)。

### API

Web版の `POST /api/launcher/start` は以下のフィールドを追加で受け付けます(llama.cpp バックエンドのみ有効。他バックエンドで指定すると 400):

| フィールド | 型 | 既定値 | 説明 |
|---|---|---|---|
| `draft_model_path` | `string \| null` | `null` | 明示的な companion draft/MTP gguf のパス |
| `mtp_mode` | `"auto" \| "off"` | `"auto"` | `auto` = 自動検出、`off` = speculative decoding を無効化 |

起動成功時のレスポンス JSON には、解決された speculative フラグが `"speculative"` キー(トークン配列、例: `["--spec-type", "draft-mtp"]`。何も付与されなければ空配列)として含まれます。

---

## メモリ推奨

MODELS 一覧の各モデルには、CodeRouter を動かしているマシンの搭載メモリ(Apple Silicon は統合メモリ、NVIDIA GPU は VRAM、それ以外は RAM)と照らした判定が表示されます。

- **✓ 推奨** — 余裕を持って動く目安(`モデルサイズ × 1.2 + 2GB` が利用可能メモリ以内)
- **⚠ メモリ厳しい** — 収まらない／余裕が乏しい。スワップして大幅に遅くなる可能性

「追加オプション」欄の横の **⚙ 推奨値** ボタンは、選択中モデル・ハード・**バックエンド**に応じた起動フラグの目安を同欄に入れます。出力はバックエンドで異なります。

- **llama.cpp** — `-ngl`(GPU に載るなら `99`・CPU のみ `0`)/ `--ctx-size`(空きメモリに応じ `4096`〜`32768`)/ `--threads`(CPU コア数 − 2)
- **vllm** — 空。`--max-model-len` 等はモデルの実コンテキスト長に依存するため、エンジンの自動導出に任せます
- **mlx** — 空。統合メモリ前提で、起動時の調整フラグは不要です

いずれも**目安**で、他プロセスのメモリ使用や量子化方式までは考慮しません。実機で調整してください。

---

## 特化ビルドの切り替え (llama.cpp)

**v2.11.0+**。llama.cpp を GPU ランタイム別にビルドしてある環境で、起動ごとにどのビルドの `llama-server` を使うか選べます。GUI/Web 両対応。

### なぜ必要か

同じマシンでもビルドによって見えるデバイスが違います。実機 (Ryzen AI Max 環境 + RTX 5090 + RTX 3090 + Radeon 8060S) の `--list-devices`:

| ビルド | 列挙されるデバイス |
| --- | --- |
| `build/` | (CPU のみ) |
| `build-cuda/` | `CUDA0` RTX 5090 (32149 MiB) / `CUDA1` RTX 3090 (24123 MiB) |
| `build-vulkan/` | `Vulkan0` RTX 3090 / `Vulkan1` RTX 5090 / `Vulkan2` Radeon 8060S (114164 MiB) |
| `build-rocm/` | `ROCm0` Radeon 8060S (98304 MiB) |

Radeon 8060S に載せたいなら Vulkan か ROCm、NVIDIA 2 枚で tensor-split したいなら CUDA、という択があります。モデルによって最適なビルドが変わるので、`providers.yaml` を書き換えて CodeRouter を再起動せずに切り替えられるようにするのが本機能です。

### 設定

`launcher.backends` のキーに `<バックエンド名>-<バリアント>` を追加します。**バリアントは `binary` 必須**です。

```yaml
launcher:
  backends:
    # 基底名。binary 省略なら PATH の llama-server (従来どおり)
    llama.cpp:
      binary: ~/llm/apps/llama.cpp/build/bin/llama-server

    # 特化ビルド = バリアント。binary は必須
    llama.cpp-cuda:
      binary: ~/llm/apps/llama.cpp/build-cuda/bin/llama-server
    llama.cpp-vulkan:
      binary: ~/llm/apps/llama.cpp/build-vulkan/bin/llama-server
    llama.cpp-rocm:
      binary: ~/llm/apps/llama.cpp/build-rocm/bin/llama-server
```

バリアント名は小文字英数と `.` `_` `-` のみ(`[a-z0-9][a-z0-9._-]*`)。基底名は `llama.cpp` / `vllm` / `mlx` のいずれかです。

`binary` が必須なのは事故防止のためです。省略を許すと PATH の `llama-server` にフォールバックし、**CUDA ビルドを指定したつもりで素のビルドが静かに動く**という最も気づきにくい状態になります。設定ロード時にエラーにしています。

### 使い方

書いたバリアントは「バックエンド」セレクトに `llama.cpp-cuda ⚙` のように増えます。`⚙` は「対応ランタイムが必要な上級者向け」の印です。選ぶと解決されたパスの下に前提ランタイムの注記が出ます。

**バリアントを書かなければセレクトは従来どおり 3 択のまま**で、生成される起動コマンドも 1 バイトも変わりません。特化ビルドは書いた人にだけ見えるオプションです。

### 前提ランタイム(ユーザー側で導入)

CodeRouter はドライバやランタイムを導入しません。ビルド済みバイナリを選ぶだけです。

| バリアント | 必要なもの |
| --- | --- |
| `-cuda` | NVIDIA ドライバ + CUDA ランタイム |
| `-vulkan` | Vulkan ランタイム (`libvulkan`) + ICD |
| `-rocm` | ROCm (`hip`) |

導入状況の事前検査もしません。代わりに **そのビルドで `--list-devices` が成功するか** を実質的な健全性チェックとして使います。失敗した場合は「デバイスを列挙できません」と表示しますが、起動自体は止めません(検出できないが動く環境を潰さないため)。

> `launcher.auto_restart` を有効にしている環境でランタイム未導入のビルドを選ぶと、クラッシュ→再起動が走ります。`auto_restart_max_attempts`(既定 3)で打ち切られて `status='error'` に落ち着くので無限ループはしませんが、ログを確認してください。

### デバイス選択との連動(重要)

**デバイス ID の名前空間はビルドごとに違います。** `CUDA0` と `Vulkan0` は同じ GPU を指しません。上の実機例では `CUDA0` が RTX 5090 なのに `Vulkan0` は RTX 3090 です。

そのため、バックエンドを切り替えると**デバイス選択は自動的にクリアされ**、再検出を求められます。仮に持ち越したまま起動しようとしても、サーバ側が「そのビルドに存在しない ID」を検出して 400 で拒否します(`--device CUDA0` が Vulkan ビルドに渡ると `llama-server` が起動失敗するため)。

### ビルド別の option_profiles

`option_profiles` にもバリアント名のキーを書けます。基底名のプロファイルが**継承**され、バリアント固有のものが後ろに追加されます。同名は基底側と同じ位置で差し替えられます。

```yaml
launcher:
  option_profiles:
    llama.cpp:                   # 全ビルドに継承される
      - name: 標準
        args: { "-ngl": 99, "--ctx-size": 4096 }
    llama.cpp-cuda:              # CUDA ビルド専用(上の「標準」の後ろに並ぶ)
      - name: 5090単体・速度重視
        args: { "-ngl": 99, "--ctx-size": 8192 }
```

共通プロファイルをビルドごとに複製する必要はありません。

### ビルド横断ベンチスイープ

llama.cpp のビルドを 2 つ以上宣言すると、ベンチスイープ欄に **⚙ ビルド横断** ボタンが出ます。押すと宣言済みの全ビルドをプローブし、`cuda / CUDA0 単体` `vulkan / Vulkan2 単体` のようにビルド名を前置した構成候補を一括生成します。そのまま実行すれば、同一モデルを各ビルドで順に起動してベンチし、どのビルドが速いかを 1 回で比較できます。

ラベルはベンチコマンドの `{config}` に展開されるので、結果 JSON もビルド別に見分けられます。なお**ビルド間の混成構成は作りません** — 1 プロセスは 1 つの実行ファイルでしか動かないので原理的に不可能です。

### モデル別にビルドを固定する (launcher.swap)

`launcher.swap` のカタログでもバリアントを指定できます。モデルごとに最適なビルドで自動起動させたい場合に使います。

```yaml
launcher:
  swap:
    enabled: true
    models:
      - name: qwen3-30b
        backend: llama.cpp-cuda      # このモデルは常に CUDA ビルドで起動
        model_path: ~/models/Qwen3-30B-A3B-Q4_K_M.gguf
        port: 18081
```

バリアントを指定する場合、そのバリアントが `launcher.backends` に宣言されていることが設定ロード時に検証されます(実行ファイルパスが `backends` にしか無いため)。

---

## デバイス選択 (llama.cpp)

複数 GPU 環境で `llama-server` に `--device` / `--tensor-split` を渡し、オフロード先を明示的に選べます。GUI/Web 両対応。**llama.cpp のみ**(vllm / mlx にはデバイス選択欄自体がありません)。バリアント (`llama.cpp-cuda` 等) でも同様に使えます。

### 検出

LAUNCH フォームの **🔍 検出** ボタンで `{binary} --list-devices` を実行し、検出できたデバイスをチェックボックス(GUI)/カード(Web)で一覧表示します。各デバイスに VRAM(空き/合計 GB)が併記されます。実機出力例:

```
# CUDA マルチGPU(Linux)
CUDA0: NVIDIA GeForce RTX 5090 (32149 MiB, 31626 MiB free)
CUDA1: NVIDIA GeForce RTX 3090 (24123 MiB, 23800 MiB free)

# macOS / Apple Silicon(Metal + BLAS フォールバック)
MTL0: Apple M3 Max (53084 MiB, 53083 MiB free)
BLAS: Accelerate (0 MiB, 0 MiB free)
```

macOS の Metal デバイス id は `Metal` ではなく **`MTL0`** です(`llama-server --list-devices` の実出力に合わせています)。`BLAS: Accelerate` のような `0 MiB` のデバイスは一覧には表示されますが、VRAM を持たず選択・tensor-split 提案の対象にはなりません(除外されるのは「選択候補」からのみで、検出結果自体からは消えません)。

検出に失敗した場合(バイナリが無い / タイムアウト / 出力をパースできない等)は `ok: false` になり、UI はカンマ区切りのデバイス id を直接入力する手入力欄にフォールバックします。

### 選択と tensor-split

- デバイスを 1 個選ぶと `--device <id>` のみが付与されます(`--tensor-split` は付きません)
- 2 個以上選ぶと **VRAM 比(合計 VRAM ベース)で `--tensor-split` を自動提案**します(例: RTX 5090 + RTX 3090 →`0.57,0.43`)。手動で上書きできます
- 検出デバイスが 1 個以下(Mac Metal 単体・単基 CUDA 等)の場合、tensor-split の入力欄自体が無効化/非表示になります(単一デバイスでは意味を持たないため)
- **未選択なら `--device` は一切付与されません** — 起動コマンドは本機能導入前と完全に同じままです(既存デプロイへの影響なし)

### マルチバックエンドの注意点

CUDA と Vulkan の両方をサポートしてビルドされた llama.cpp では、**同一の物理 GPU が `CUDA0` と `Vulkan1` のように複数バックエンドで重複列挙される**ことがあります。実機出力例:

```
CUDA0: NVIDIA GeForce RTX 5090 (32149 MiB, 31610 MiB free)
CUDA1: NVIDIA GeForce RTX 3090 (24123 MiB, 23858 MiB free)
Vulkan0: NVIDIA GeForce RTX 3090 (24822 MiB, 24096 MiB free)
Vulkan1: NVIDIA GeForce RTX 5090 (32607 MiB, 31610 MiB free)
```

tensor-split の自動提案・スイープ構成の自動生成は、この重複を安全に扱うため**バックエンド接頭辞ごと**(`CUDA` / `Vulkan` / `MTL` / `SYCL` など、id 末尾の連番を除いた部分)に行われます。**バックエンドを跨いだ混成構成(例: `CUDA0` + `Vulkan1`)は自動生成されません** — 同じ GPU を二重に数えてしまうためです。バックエンドを跨いだ組み合わせを試したい場合は、デバイス id を手動でチェック(または手入力)して選択してください。

### API(Web版)

| Method | Path | 認証 | 説明 |
|---|---|---|---|
| GET | `/api/launcher/devices` | なし | デバイス検出。`?backend=llama.cpp`(既定)、`?refresh=1` で検出キャッシュを無視して再取得 |

`GET /api/launcher/devices` のレスポンス:

```jsonc
{
  "ok": true,
  "error": null,
  "devices": [
    {"id": "CUDA0", "name": "NVIDIA GeForce RTX 5090",
     "total_mib": 32149, "free_mib": 31626, "total_gb": 31.4, "free_gb": 30.9},
    {"id": "CUDA1", "name": "NVIDIA GeForce RTX 3090",
     "total_mib": 24123, "free_mib": 23800, "total_gb": 23.6, "free_gb": 23.2}
  ],
  // バックエンド接頭辞ごとの VRAM 比提案(0 MiB デバイスは除外、2 枚以上の
  // バックエンドのみキーを持つ)
  "suggested_tensor_split": {"CUDA": [0.57, 0.43]},
  // スイープ構成の自動生成候補(単体構成 + バックエンド内複数枚構成)
  "auto_configs": [
    {"label": "CUDA0 単体", "device_ids": ["CUDA0"], "tensor_split": []},
    {"label": "CUDA1 単体", "device_ids": ["CUDA1"], "tensor_split": []},
    {"label": "CUDA x2", "device_ids": ["CUDA0", "CUDA1"], "tensor_split": [0.57, 0.43]}
  ]
}
```

`POST /api/launcher/start` は以下のフィールドを追加で受け付けます(**llama.cpp のみ有効**。既定は空配列 = 未選択 = 従来どおりの起動コマンド):

| フィールド | 型 | 既定値 | 説明 |
|---|---|---|---|
| `device_ids` | `list[string]` | `[]` | 選択したデバイス id(例 `["CUDA0", "CUDA1"]`) |
| `tensor_split` | `list[float]` | `[]` | `device_ids` が 2 個以上のときのみ有効な分割比(例 `[0.57, 0.43]`) |

---

## ベンチスイープ (llama.cpp)

複数のデバイス構成(例: `CUDA0` 単体 / `CUDA1` 単体 / `CUDA0,CUDA1` 分割)を**自動で順番に**「起動 → readiness 待ち → 外部ベンチ実行 → 停止 → 次の構成へ」と回し、構成ごとの性能を比較する機能です。GUI/Web 両対応。**llama.cpp のみ**。

ベンチ本体は外部ツール [llmbench](https://github.com/zephel01/swe-bench) を想定しています(別途インストールが必要— Launcher 自体には同梱されません)。

### 使い方(GUI)

LAUNCH フォームの **📊 ベンチスイープ** ボタンで別ウィンドウ(`SweepWindow`)が開きます。

1. モデルパス・ポート・runs・results_dir・ベンチコマンドを入力(モデルパス/ポートは親フォームの値を引き継ぎ)
2. **🔍 デバイス検出 → 構成生成** で `--list-devices` を実行し、[デバイス選択](#デバイス選択-llamacpp)の自動生成規則(単体構成 + バックエンド内複数枚構成)で構成候補のチェックボックスを生成
3. 実行したい構成にチェックを入れて **▶ 開始**
4. 進行テーブル(構成 / 状態 / exit / tok/s / ttft(ms))とログが更新される。**■ 中断**で以降の構成をキャンセル(実行中の構成は完了まで進む)

### 使い方(Web版)

`/launcher` ページのベンチスイープカードから、GUI 版と同じ構成マトリクス・ベンチコマンド欄・runs・results_dir を入力して **開始**。**開始/中断は書き込み系のため launcher token 認証が必要**(`launcher.readiness_timeout_s` 等と同様、`X-CodeRouter-Token` ヘッダ)。進行状況は 3 秒ごとの状態ポーリングで表示されます。

### 状態遷移

各構成(`SweepStep`)は `pending → starting →(readiness 通過)→ benching →(ベンチ終了)→ done` と遷移します。

- 起動失敗 / readiness タイムアウト → `failed`(次の構成へ継続)
- ベンチが非ゼロ終了しても `done`(exit code を記録して比較を継続 — ベンチ自体の起動に失敗した場合のみ `failed`)
- 中断要求 → 実行中の構成は完了まで進み、以降の未実行構成は `aborted`

### `launcher.bench` 設定(既定値)

`providers.yaml` の `launcher.bench:` ブロックで、スイープの既定値を設定できます(省略時はハードコードされた既定値を使用 — 完全後方互換)。

```yaml
launcher:
  bench:
    command_template: "llmbench run --model local-openai --runs {runs}"
    runs: 5
    results_dir: ~/llmbench-results
    readiness_timeout_s: 300
```

| フィールド | 型 | 既定値 | 説明 |
|---|---|---|---|
| `command_template` | str | `"llmbench run --model local-openai --runs {runs}"` | 外部ベンチコマンドのテンプレート。`{port}` `{config}` `{base_url}` `{results_dir}` `{runs}` を単純文字列置換(`str.format` ではないので JSON の波括弧を誤爆しない)で展開し `shlex` で argv 化する。Windows では `shlex.split(..., posix=False)` でバックスラッシュ入りパスを保護する |
| `runs` | int | `5`(1〜1000) | 1 構成あたりのベンチ実行回数。`{runs}` に展開される |
| `results_dir` | str \| null | `null` | `llmbench` の results 出力先ディレクトリ。相対パスはサーバの CWD 基準。指定時のみ、各構成の完了後にこのディレクトリ内の最新 JSON を読み比較サマリを付与する |
| `readiness_timeout_s` | float | `300.0`(5〜3600) | スイープの各構成でサーバが ready になるのを待つ最大秒数。大きな GGUF のロード時間を見込んだ既定 5 分 |

`{port}` にはスイープ用に固定したポート、`{config}` には構成ラベル(例 `CUDA0 単体`)、`{base_url}` には `http://localhost:{port}/v1` が入ります。ベンチ子プロセスには環境変数 `OPENAI_BASE_URL=http://localhost:{port}/v1` も設定されるため、`llmbench` 側がテンプレ引数と環境変数のどちらで接続先を受け取っても動作します。

### results 比較

`results_dir` を指定すると、各構成のベンチ終了後にそのディレクトリ内で最も新しい `*.json`(構成の開始時刻以降に更新されたもの)を読み込み、`tokens_per_sec` / `ttft_ms` / `latency_ms` / `runs` を別名キー(`tok_s` / `throughput` / `tps` 等)も含めて best-effort で抽出し、進行テーブルに反映します。`llmbench` の JSON スキーマが変わっても壊れないよう防御的に解析され、抽出できなかった項目は空欄のままです。

### API(Web版)

| Method | Path | 認証 | 説明 |
|---|---|---|---|
| POST | `/api/launcher/sweep/start` | **token** | スイープを開始。既に実行中なら 409、`configs` が空なら 400、ポートが使用中/確保できなければ 400 |
| GET | `/api/launcher/sweep/status` | なし | 現在(または直近)のスイープ状態。`{sweep_id, running, current_index, steps: [...]}` |
| POST | `/api/launcher/sweep/abort` | **token** | 進行中スイープに中断要求。実行中のスイープが無ければ 404 |
| GET | `/api/launcher/sweep/logs?n=200` | なし | 進行ログ末尾 N 行。`{logs: [str], total: int}` |

`POST /api/launcher/sweep/start` のリクエストボディ(主なフィールド):

| フィールド | 型 | 既定値 | 説明 |
|---|---|---|---|
| `backend` | str | `"llama.cpp"` | 対象バックエンド |
| `model_path` | str(必須) | — | 全構成で共通のモデルパス |
| `port` | int(必須) | — | 全構成で使い回すポート(1024〜65535)。各構成の停止はポート解放を待ってから次へ進む |
| `configs` | `list[{label, device_ids, tensor_split}]`(必須・1件以上) | — | 実行する構成の一覧。`/api/launcher/devices` の `auto_configs` をそのまま使うか、手動で組み立てる |
| `bench_command` | str \| null | `null` | `null` なら `launcher.bench.command_template`(未設定ならハードコード既定)を使う |
| `runs` / `results_dir` | int \| null / str \| null | `null` | 同様に `launcher.bench` の既定値にフォールバック |

---

## モデル自動スワップ (launcher.swap) — v2.9.1+

[llama-swap](https://github.com/mostlygeek/llama-swap) 相当の機能を、追加の依存なしで CodeRouter 単体に組み込んだものです。既定は無効(`enabled: false`)の opt-in 機能です。

- リクエストの `model` 名を見て、まだ起動していない backend を**オンデマンドで起動**します
- モデルのロードが完了するまでリクエストは**保留**され、readiness を通過して初めて応答が返ります(ロード未完了への connection-refused / 503 は起きません)
- 進行中リクエストが無い状態(アイドル)が `ttl_seconds` 続くと**自動アンロード**してメモリを解放します

### 最小構成

```yaml
# ~/.coderouter-t/providers.yaml
default_profile: auto

auto_router:
  default_rule_profile: launcher-swap-ornith-9b
  rules: []

launcher:
  model_dirs:
    - ~/models

  swap:
    enabled: true
    ttl_seconds: 1800
    readiness_timeout_s: 180

    models:
      - name: ornith-9b
        backend: llama.cpp
        model_path: ~/models/Ornith-1.0-9B-Q4_K_M.gguf
        port: 18081
        num_ctx: 32768
        extra_args: "-ngl 99"

      - name: qwen3-coder-30b
        backend: llama.cpp
        model_path: ~/models/Qwen3-Coder-30B-Q4_K_M.gguf
        port: 18082
        num_ctx: 32768
```

上記は `providers:` / `profiles:` を一切書いていません。**v2.9.2 以降**、`launcher.swap.enabled: true` かつ `launcher.swap.models` が1件以上あれば、トップレベルの `providers` / `profiles` は省略(または空リスト `[]`)で構いません — カタログの各モデルに対応する `launcher-swap-<name>` プロファイルが設定ロード時に自動注入され、対応する provider は初回のオンデマンド起動時にランタイム登録されます。**v2.9.1 まで**はこの緩和が無く、`providers` / `profiles` に到達不能なダミーエントリ(例: `base_url: http://127.0.0.1:9`)を書く必要がありました。

Ollama など常用の backend と併用する、より実運用寄りの設定例は [`examples/providers.swap.yaml`](../../examples/providers.swap.yaml) を参照してください。

### `launcher.swap` フィールド

| フィールド | 型 | 既定値 | 説明 |
|---|---|---|---|
| `enabled` | bool | `false` | オンデマンドスワップを有効化。`false` ならこれまで通り手動起動のみ(既存デプロイに影響なし) |
| `ttl_seconds` | float \| null | `1800.0` | 進行中リクエストが無い状態がこの秒数続くとプロセスを自動停止。`null` = 無効(明示停止まで起動し続ける)、`0` = 最後のリクエスト完了で即アンロード。全モデル共通のグローバル既定値 — `models[].ttl_seconds` で個別上書き可能([Unreleased]) |
| `readiness_timeout_s` | float | `120.0`(1〜1800) | オンデマンド起動したモデルの準備完了を**リクエスト側が待つ**上限秒数。超えると dispatch フックが retryable な `AdapterError` を送出する。**下記の `launcher.readiness_timeout_s`(既定 300s)とは別物** — こちらは「1リクエストが待つ上限」、あちらは「プロセスの readiness 監視全般の上限」 |
| `sweep_interval_s` | float | `15.0`(1〜600) | TTL 監視(sweeper)がアイドルプロセスを走査する間隔 |
| `port_retry_attempts` | int | `2`(0〜5) | [Unreleased] `models[].port` が未指定のカタログエントリに対する、起動失敗時の追加リトライ回数(毎回新しいエフェメラルポートを取り直す)。`0` ならリトライなし。固定ポート指定時は無関係(常に1回のみ)。TOCTOU窓自体は解消しない — 詳細は `models[].port` の説明を参照 |
| `inject_auto_router_rules` | bool | `true` | カタログの各モデル名ごとに `auto_router` ルール(`id: swap:<name>`、`model_pattern` は名前の完全一致)を自動生成するか。`false` にした場合は `X-CodeRouter-Profile` ヘッダ等で自分でルーティングを配線する |
| `models[].name` | str(必須) | — | リクエストの `model` フィールドと照合される論理名。provider 名・専用プロファイル名は自動的に `launcher-swap-<name>` になる |
| `models[].backend` | `"llama.cpp" \| "vllm" \| "mlx"`(必須) | — | 手動 Launcher UI と同じバックエンド種別 |
| `models[].model_path` | str(必須) | — | モデルファイルの絶対または `~` 相対パス。**必ず `launcher.model_dirs` の配下**である必要があり、設定ロード時と起動時の2回検証される(パストラバーサル対策) |
| `models[].port` | int \| null | `null` | 固定ポート推奨。省略時は OS 割当のエフェメラルポートを使い、起動失敗時に `launcher.swap.port_retry_attempts` 回まで(既定2回)別ポートでリトライする。best-effort — pick してから子プロセスが実際に bind するまでの間隙(TOCTOU)は解消されない。強い保証が要る場合は固定ポートを使うこと |
| `models[].ttl_seconds` | float \| null | `null` | [Unreleased] このモデルだけの TTL 上書き。`null`(既定) = グローバルの `launcher.swap.ttl_seconds` に従う。`0` を指定するとグローバルの `0` と同じ意味(最後のリース解放で即アンロード)がこのモデルにだけ適用される |
| `models[].option_profile` | str \| null | `null` | `launcher.option_profiles[backend]` の既存プリセット名。存在しない名前を指定すると設定ロード時にエラーになる |
| `models[].num_ctx` | int | `8192`(≥256) | KV 見積り・起動パラメータの基準値 |
| `models[].extra_args` | str | `""` | 追加 CLI フラグ(1本の文字列。`shlex` でパース。モデル/draft モデルの再指定は不可) |
| `models[].draft_model_path` | str \| null | `null` | MTP/draft companion gguf の明示指定 |
| `models[].mtp_mode` | `"auto" \| "off"` | `"auto"` | 手動 Launcher UI の **MTP** 欄と同じ意味 |
| `models[].model_pattern` | str \| null | `null` | `name` の完全一致に加えて許容する正規表現(`re.fullmatch`)。カタログ内マッチング(`SwapManager.match`)のみに影響し、自動注入される auto_router ルールは常に `name` の完全一致(`re.escape`)を使う |

> **Phase 2 フィールド(スキーマ宣言のみ・未実装)**: `models[].group`(`"swap" | "persistent" | "exclusive"`)、`models[].est_weights_gb`、`launcher.swap.memory_budget_gb`、`launcher.swap.max_loaded` はスキーマ上は存在しますが、Phase 1(v2.9.1時点)ではロジックから一切参照されません。

### 挙動に関する注記

- **swap プロセスは `launcher.auto_restart` の対象外**です。クラッシュ時の一次的な回復は次のリクエストによる SwapManager 自身の再 spawn に委ねられ、汎用の auto-restart 監督とは重複しません(同じ固定ポートを2つの監督者が取り合う事態を避けるため)
- **ストリーミング応答中はTTLアンロードされません**。応答の最終チャンクに到達するまで「リース」を保持するため、生成途中でプロセスが停止されることはありません
- **カタログに一致しない `model` 名**のリクエストは、`auto_router.default_rule_profile`(または通常のプロファイル解決)へフォールスルーします。swap 側の専用プロファイルには到達しません
- **同時ロード数に上限はありません**(Phase 1)。メモリに載る範囲で複数モデルを同時に起動できます。予算管理・退避を伴う排他 swap は Phase 2 で予定
- **`providers` / `profiles` の省略は v2.9.2 から**です(上記「最小構成」参照)。それ以前のバージョンでは到達不能なダミーの provider/profile を用意する必要がありました

実機検証(macOS / M3 Max / Metal、350MB級 GGUF): `_run/swap-test/` の自動テストキットで cold spawn(約2秒)→ warm reuse(約0秒、再 spawn なし)→ catalog-miss フォールスルー → TTL unload → respawn の一連が ALL PASS。詳細は設計書の実装記録(§10.5)を参照してください。

並行性モデル・セキュリティ考慮・レビュー決定事項などの設計の詳細は [`docs/designs/launcher-model-swap.md`](../designs/launcher-model-swap.md) を参照してください。

---

## 設定リファレンス

MODELS 一覧・オプションプロファイル・バイナリパスは `~/.coderouter-t/providers.yaml` の `launcher:` ブロックから読み込まれます。**デスクトップ版・Web版で共通**です。

### `launcher:` ブロック全体

```yaml
# ~/.coderouter-t/providers.yaml
launcher:
  model_dirs:           # list[str]  必須
    - ~/llm/models
  backends:             # dict  省略可
    llama.cpp:
      binary: null      # null = PATH の llama-server
    vllm:
      binary: null      # null = PATH の python
    mlx:
      binary: null      # null = PATH の python
  option_profiles:      # dict  省略可
    llama.cpp: [...]
    vllm: [...]
```

> CodeRouter 起動ボタンが自動生成する `providers.yaml` には `launcher:` ブロックは含まれません。モデル一覧やプロファイルを使うには `launcher:` ブロックを自分で用意してください。テンプレートは `launcher_profiles.yaml.example` をコピーして始められます。

### `backends` — バイナリパス設定

バイナリが PATH に無い場合(ソースビルド、venv 環境など)にフルパスを指定します。

```yaml
launcher:
  backends:
    llama.cpp:
      binary: ~/llama.cpp/build/bin/llama-server         # ソースビルド例
    vllm:
      binary: ~/.coderouter-t/backends/vllm/bin/python     # venv 例
    mlx:
      binary: ~/.coderouter-t/backends/mlx/bin/python      # venv 例
```

`binary` を省略または `null` にすると、PATH からデフォルト名(`llama-server` / `python`)を探します。チルダ (`~`) 展開に対応。vLLM / MLX 用の venv は `~/.coderouter-t/backends/<バックエンド名>/` 配下にバックエンドごとに分けて作るのが推奨です(詳細は [インストール手順書](./install-backends.md))。UI の「バックエンド」セレクト下に解決されたパスが表示されます。

キーには `llama.cpp-cuda` のような**バリアント名**も書けます(v2.11.0+)。同じ llama.cpp を GPU ランタイム別にビルドしてある環境で、起動ごとにどのビルドを使うか選べるようになります。詳細は [特化ビルドの切り替え](#特化ビルドの切り替え-llamacpp) を参照。

> **v2.11.0 の変更(破壊的)**: `launcher.backends` のキーは `llama.cpp` / `vllm` / `mlx` か、その `-<バリアント>` 形式でなければ**設定ロード時にエラー**になります。以前は `llamacpp:` のような打ち間違いが黙って無視されていました(バックエンド一覧が固定 3 キーだったため)。起動できなくなった場合はキーの綴りを確認してください。

### `model_dirs`

- チルダ (`~`) 展開あり
- 存在しないパスはスキャン時に無視(起動エラーなし)
- 検索対象拡張子: `.gguf` `.safetensors` `.bin` `.pt` `.pth` `.ggml`
- サブフォルダを再帰検索

### `option_profiles`

```yaml
option_profiles:
  llama.cpp:            # バックエンド名(キー)
    - name: "わかりやすい名前"   # UI ドロップダウンに表示
      args:
        "-ngl": 99              # int → "-ngl 99"
        "--ctx-size": 4096
        "--dtype": "float16"    # str → "--dtype float16"
        "--mlock": true         # bool true → "--mlock"(値なし)
        "--no-mmap": false      # bool false → 省略
```

**`args` の型ルール:**

| YAML 型 | CLI 変換 |
|---|---|
| `int` / `float` / `str` | `--flag value` の 2 引数 |
| `bool: true` | `--flag` のみ(値なし) |
| `bool: false` | このフラグを省略 |

### readiness ゲーティングと自動再起動(v2.9.1+)

`launcher:` ブロック直下(`model_dirs` / `backends` と同じ階層)に以下のフィールドを追加できます。手動起動・swap によるオンデマンド起動の両方が対象です。

| フィールド | 型 | 既定値 | 説明 |
|---|---|---|---|
| `readiness_timeout_s` | float | `300.0`(5〜3600) | 起動した backend が「準備完了」になるまで待つ最大秒数。llama.cpp / vllm は `GET /health` が 200 を返すこと、それ以外の backend は TCP 接続成功をもって判定する。期限を超えるとプロセスは動かしたまま provider 登録はされず、status が `error` になる。既定 5 分は大きな GGUF のロード時間を見込んだ値 |
| `readiness_poll_interval_s` | float | `2.0`(0.2〜60) | status が `loading` の間、readiness を再確認する間隔(秒) |
| `auto_restart` | bool | `false` | `true` にすると、クラッシュ(0 以外の終了コード。MTP起動クラッシュの1回限りの自動フォールバックは別途優先して実行される)した backend を、`auto_restart_max_attempts` 回まで指数バックオフで自動再起動する。Stop ボタンによる意図的停止やサーバーシャットダウンはクラッシュ扱いにならない。既定 `false`(`ProviderConfig.restart_command` と同じ opt-in 方針)。**swap が管理するプロセスは対象外**(SwapManager が唯一の監督者) |
| `auto_restart_max_attempts` | int | `3`(0〜20) | 自動再起動の最大連続試行回数。再起動したプロセスが readiness を通過すると 0 にリセットされる。`auto_restart: false` のときは無視される |
| `auto_restart_backoff_s` | float | `2.0`(0.1〜300) | 最初の自動再起動までの初期バックオフ秒数。試行のたびに倍増し `auto_restart_backoff_max_s` で頭打ちになる |
| `auto_restart_backoff_max_s` | float | `30.0`(1〜600) | 自動再起動バックオフの上限秒数 |

**新しいプロセス status `loading`** — 起動直後で readiness 待ち中の状態です。`starting` / `running` / `stopped` / `error` に加えて PROCESSES テーブルに表示されます。readiness を通過すると `running` に遷移し、そこで初めて provider として登録されます(以前は spawn 直後に登録していたため、ロード未完了へのリクエストが connection-refused / 503 になる不具合がありました)。

> **動作変更(v2.9.1)**: `POST /api/launcher/start` は provider 同期を同期的に行わなくなりました。レスポンスの `provider_sync` は常に `null` です。同期結果は `/api/launcher/processes` またはログの `provider sync:` 行で確認してください。

### `launcher.bench` — ベンチスイープ既定値

`launcher:` ブロック直下に `bench:` サブブロックを追加すると、[ベンチスイープ](#ベンチスイープ-llamacpp)の既定値(ベンチコマンド・runs・results_dir・readiness タイムアウト)を差し替えられます。省略時はハードコードされた既定値を使うため、既存の `providers.yaml` はそのまま動作します。フィールド一覧・サンプル YAML は [ベンチスイープ](#ベンチスイープ-llamacpp) セクションの `launcher.bench` 設定表を参照してください。

### 追加オプション(自由入力)

UI の「追加オプション」欄の文字列は `shlex.split()` でパースされ、コマンド末尾に追加されます。プロファイルに無い実験的なフラグを試すときに使います。

```
-ngl 40 --rope-scale 2.0 --rope-freq-base 10000
```

> **注意**: `-m` / `--model` (および `--model=...` 形式) によるモデルの再指定は、追加オプション・オプションプロファイルのどちらでも受け付けません — 指定すると起動リクエストは 400 で拒否されます。モデルは「モデルパス」欄でのみ指定してください。同様に `-md` / `--model-draft` / `--spec-draft-model` による draft モデルの再指定も追加オプション・オプションプロファイルでは受け付けません(llama.cpp のみのフラグ)。draft モデルは「MTP/draft gguf」欄でのみ指定してください — 詳細は [MTP / speculative decoding](#mtp--speculative-decoding-llamacpp)。

---

## オプション早見表

### llama.cpp

よく使うフラグのみ抜粋。完全リストは `llama-server --help`。

| フラグ | 説明 | 推奨値例 |
|---|---|---|
| `-ngl` | GPU にオフロードするレイヤー数 | `99`(全部)/ `0`(CPU のみ) |
| `--ctx-size` | コンテキスト長(トークン) | `4096` / `8192` / `131072` |
| `--threads` | CPU スレッド数 | CPU コア数 − 2 |
| `--batch-size` | バッチサイズ | `512` |
| `--mlock` | メモリにロック(スワップ防止) | `true` |
| `--embedding` | Embedding モードで起動 | `true` |

### vllm

完全リストは `python -m vllm.entrypoints.openai.api_server --help`。

| フラグ | 説明 | 推奨値例 |
|---|---|---|
| `--dtype` | テンソルデータ型 | `"auto"` / `"float16"` / `"bfloat16"` |
| `--max-model-len` | 最大コンテキスト長 | `4096` / `32768` |
| `--gpu-memory-utilization` | GPU メモリ使用率(0–1) | `0.85` |
| `--quantization` | 量子化方式 | `"awq"` / `"gptq"` |
| `--tensor-parallel-size` | テンソル並列数(GPU 台数) | `2` |

### mlx

MLX(`mlx_lm.server`)は統合メモリ前提で、`-ngl` のようなレイヤーオフロードの概念がありません。Launcher が `--model` と `--port` を設定すれば動き、起動時の性能チューニングフラグは基本的に不要です。

---

## 起動後の使い方 — CodeRouter への接続

Launcher で起動した backend は OpenAI 互換 API を提供します。これを CodeRouter のプロバイダとして `providers.yaml` に登録すれば、ルーティング・ガード・フォールバックが使えます。

```yaml
providers:
  - name: local-qwen-launcher
    kind: openai_compat
    base_url: http://localhost:8080/v1   # Launcher で指定したポート
    model: Qwen2.5-Coder-7B-Instruct

profiles:
  - name: default
    providers: [local-qwen-launcher]
```

Claude Code は接続先を CodeRouter に向けて起動します:

```bash
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

---

## プロファイルの追加・共有

`option_profiles` に追記するだけで新しいプリセットを足せます。コード変更は不要です。

```yaml
launcher:
  option_profiles:
    llama.cpp:
      - name: "私のカスタム設定"
        args:
          "-ngl": 40
          "--ctx-size": 8192
```

CodeRouter を再起動すると UI に反映されます。`launcher_profiles.yaml.example` をリポジトリに含めてあるので、新プロファイルを追記して PR を送れば共有できます。

---

## トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| 起動ボタンが押せない(グレーアウト) | バックエンドのバイナリが見つからない | バックエンド欄下の表示を確認し、`launcher.backends.<name>.binary` にフルパスを設定 |
| モデル一覧が空 | `launcher.model_dirs` 未設定、または設定ファイル未検出 | `providers.yaml` に `model_dirs` を設定(デスクトップ版は `--config` で明示指定も可) |
| オプションプロファイルが選べない | `launcher.option_profiles` が無い | `providers.yaml` に `option_profiles` を追加 |
| 起動後すぐ `error` になる | モデルパスの誤り / VRAM 不足 | ログでエラー内容を確認 |
| ポートが衝突する | 同じポートで別プロセスが動いている | ポート番号を変える |
| `PyYAML が見つかりません`(デスクトップ版) | 素の Python から実行した | `uv run python launcher_gui.py` で CodeRouter の venv から実行 |
| 再起動後にプロセスが消える | 仕様 — レジストリは in-memory | 常駐させたい場合は OS の launchd / systemd で管理 |

---

## 関連ドキュメント

- [バックエンド インストール手順書](./install-backends.md) — llama.cpp / vLLM / MLX の導入
- [Launcher クイックスタート](./launcher-quickstart.md) — 導入から起動までの通し手順
- [アーキテクチャ詳細 — Launcher セクション](../concepts/architecture.md#launcher--llamacpp--vllm-プロセス管理-v250)
- [利用ガイド](../guides/usage-guide.md)
- [llama.cpp 直接接続ガイド](./llamacpp-direct.md)
- [モデル自動スワップ 設計ドキュメント](../designs/launcher-model-swap.md) — `launcher.swap` の並行性モデル・セキュリティ考慮・レビュー決定事項
