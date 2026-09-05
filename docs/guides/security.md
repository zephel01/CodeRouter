# セキュリティ方針

CodeRouter は、ローカルファーストのルーターです。コーディングエージェント (Claude Code など) と、1 つ以上の LLM エンドポイントの間に立ちます。エンドポイントの中には**状態を持ち、課金が発生し、長命のシークレットで認証される**リモート有料 API も含まれます。以下のポリシーはすべて、このスレットモデルから派生しています。

このドキュメントは 2 つのことを扱います。

1. **CodeRouter 自体が安全を保つためにしていること** — コードまたはプロセスで強制される設計上の不変項・CI ゲート・方針
2. **運用者が CodeRouter を走らせるときにすべきこと** — CodeRouter 側では決められない選択 (鍵をどこに置くか、どのプロバイダを信頼するか、マシンをどうネットワークに繋ぐか)

v1.0 時点のベースラインは「**多層防御、最小限のアタックサーフェス**」です。ここに書かれていることはどれも絶対ではありません。このプロジェクトは**1 人で端から端まで監査できるサイズに保つ**ことを意図していて、その小ささの維持から安全特性が生まれています。

---

## 1. シークレットと資格情報

**方針**: API キーは設定ファイルに書きません。`providers.yaml` は環境変数名で参照し (`api_key_env: OPENROUTER_API_KEY`)、ローダが起動時に解決します。指定された環境変数が存在しなければ、該当プロバイダは**スタブ化せずにスキップ**されます。呼び出し側には明示的なエラーが返り、別階層に静かにフォールスルーすることはありません。

**根拠**: 設定ファイルは事故でコミットされます。環境変数は `echo` ではシェルに出てきても `git` には乗りません。この分離は慣習ではなく**機構**で担保されています。`ProviderConfig` 型には生の API キーを受ける欄がそもそも無いので、コミット対象のファイルに鍵を書き込む場所が物理的に存在しません。

**CI での強制**: `secret-scan` ジョブが push / PR の都度、全コミット履歴に対して `gitleaks` を走らせます。検出されたらビルド失敗。

### 1.1 `credential.source` — 環境変数以外の資格情報 (v2.14.0)

v2.14.0 以前、プロバイダの資格情報は `api_key_env` (環境変数) だけでした。v2.14.0 で `credential` ブロックが入り、ソースを 2 つから選べます。

- `source: env` — `api_key_env` を明示的に書いた形 (`credential.env: OPENROUTER_API_KEY`)
- `source: cli_session` — ベンダーの CLI (Kimi Code CLI、Grok CLI など) が**既にディスクへ書いた** OAuth トークンの JSON を読み、HTTP 呼び出しは CodeRouter 自身が行う

`cli_session` の狙いは、サブスク認証のプロバイダを `kind: agent_cli` の孤島 (リクエストごとに CLI を one-shot 起動するため、ストリーミングも tool call 修復もフォールバックチェーンも効かない) ではなく、普通の `openai_compat` / `anthropic` エントリにすることです。

```yaml
- name: kimi-sub
  kind: openai_compat
  base_url: https://api.moonshot.cn/v1
  model: kimi-k2
  credential:
    source: cli_session
    path: ~/.kimi-code/credentials/kimi-code.json   # $HOME 配下必須
    field: access_token          # ネストしているなら "tokens.access" のようにドット区切り
    expiry_field: expires_at     # 秒でもミリ秒でも可。無ければ上流 401 が唯一の合図
    refresh:
      command: ["kimi", "auth", "status"]   # argv リスト。shell=False で起動
      min_lead_s: 300            # 期限の 5 分前には必ず更新
      early_ratio: 0.5           # 残り寿命が半分を切ったら更新
      timeout_s: 30
```

セキュリティ上の不変項:

- **`credential` と `api_key_env` は設定ロード時に排他**。両方書くと起動せずエラーになります。「どちらの鍵でリクエストが飛んだのか」を後から答えられない状態を作らないためです
- **`credential.path` は `$HOME` 配下必須** (ロード時に検証)。ベンダー CLI がトークンを書く先は必ずホーム配下であり、`/etc/...` や共有ディレクトリを読ませる正当な用途が無いためです (POSIX 環境でのみ強制。Windows では常に許可)
- **`refresh.command` は argv の*リスト*で `shell=False` 実行**。文字列形式はスキーマに存在しないので、設定ファイルがシェル経由の任意コード実行に化けません (v2.13.0 の `restart_command` と同じ判断)
- **OAuth は自前実装しません**。更新はベンダーの CLI を叩いてファイルを読み直すだけです。エンドポイント・client_id・rotation の作法はベンダーごとに違い、しかも変わるので、自前実装は必ず腐るからです
- 更新は**プロセス内はパス単位のロックで単一フライト化**し、**プロセス間はサイドカー `<セッションファイル名>.coderouter-lock` への advisory `flock`** で直列化します。どちらのロックも取得後に必ずファイルを読み直してから更新要否を判断します (待っている間に別のワーカーが更新済みかもしれないため)
- **更新コマンドの stderr はログに出しません**。失敗した認証 CLI は、まさにデバイスコードやトークンを表示しがちなためです
- セッションファイルが**無い / 壊れている / まだログインしていない**場合は例外を投げず `None` に解決します。認証なしで送られ、上流が 401 を返し、フォールバックチェーンが次のプロバイダへ進みます (起動不能になるより、チェーンが動くほうが実運用では正しい)
- 読み取れたトークンは、ヘッダに載る前に §1.2 のスクラブレジストリへ登録されます

動く設定例一式は `examples/providers.cli-session.yaml` にあります。

### 1.2 ログへの秘密情報スクラブ (v2.14.0)

v2.14.0 以前、このコードベースには「この文字列は秘密である」という概念が**どこにも**ありませんでした。鍵は `resolve_api_key` が環境変数から読み、そのまま `x-api-key` / `Authorization` に入って終わりです。ヘッダ辞書をログに出す呼び出し箇所は当時も見つかりませんでしたが、問題は「将来のログ行の安全性が、書いた人が手元の値を鍵だと覚えているかどうかに依存していた」ことです。

**完全一致レジストリが主、パターンは backstop。**

- 登録される場所は 2 つ: `resolve_api_key` (プラグインが足したプロバイダも含め、全ての鍵が通る関門) と `load_config` (プロバイダ名を含む起動時ログが出る前に arm するため)。`cli_session` のトークンは解決時に登録されます
- `SecretRedactingFilter` は formatter ではなく `logging.Filter` として、stderr ハンドラ・`RequestLogHandler`・`AuditLogHandler` の**すべて**に付きます。レコードを in-place で書き換えるので、あとから走るハンドラが前段のスクラブを取り消すことがありません
- スクラブ対象はメッセージ本体・printf 引数・`exc_text`・`extra={...}` の全フィールド (ネストした辞書・リスト・タプル・集合も再帰的に)
- backstop パターンは少数かつアンカー付き: `sk-…` / `gh[pousr]_…` / `AIza…` / `Bearer <token>` / URL の `?api_key=` `access_token=` `auth_token=` `key=` / `scheme://user:pass@host` の userinfo。「高エントロピー文字列」の検出は**やりません** — コミットハッシュや base64 ペイロードを食い始めるからです
- **8 文字未満の値はレジストリが登録を拒否します。** 3 文字の "鍵" (プレースホルダ、空に近い環境変数、テスト用スタブ) は無関係な単語の内側にマッチし、通常のログテキストを壊してしまうためです
- `/metrics.json` が返す `base_url` も、ブラウザへ渡る前にスクラブを通ります

**非目標**: 保存時の暗号化、既に書かれたログの修復 (検出は §1.3、修復は鍵のローテーション)、プロセスの環境変数を読める相手への防御。塞いでいるのは「資格情報がログシンクへ到達する」1 点だけです。

### 1.3 `coderouter doctor --check-secrets` (v2.14.0)

`--check-env` が「鍵の入ったファイルは保護されているか」を訊くのに対し、`--check-secrets` は「走っているプロセスは自分の秘密を知っているか、そしてそれは既にログへ出ていないか」を訊きます。

```bash
coderouter doctor --check-secrets
coderouter doctor --check-env --check-secrets   # 併用可。終了コードは最悪値
```

4 つのチェックが走ります。

| チェック | 内容 |
|---|---|
| `redaction-filter` | カナリア値を実物の `SecretRedactingFilter` に通し、メッセージ側と `extra` 側の**両方**から消えることを確認 (「コードがある」ではなく実測) |
| `registered-secrets` | 宣言済みの `api_key_env` のうち実際に環境変数が設定されている数。未設定があれば `warn` |
| `config-embedded-credentials` | `base_url` に貼り付けられた資格情報を検出。当たれば `error` |
| `written-log-scan` | `state_dir` 配下の `requests.jsonl` / `audit.jsonl` (ローテーション済みの `.1` を含む) を走査し、登録済みの鍵の**生の値**が残っていないか確認。当たれば `error`。読むだけで、何も書き換えません |

終了コードは `--check-env` と同じ契約で **0 = clean / 2 = 要対応 / 1 = ブロッカー**。設定ファイルが読めない場合は skip ではなく 1 です (読めなかったファイルについて衛生を主張できないため)。

`written-log-scan` が当たったときの正しい対処は**鍵のローテーション**です。ログを消しても、既に出た値が出たという事実は消えません。

なお `registered-secrets` と `written-log-scan` が見るのは `api_key_env` 由来の鍵だけです。`credential.source: cli_session` のトークンは実際にリクエストを処理したプロセスでしか登録されないため、`doctor` の単発起動では走査対象になりません。

**運用者のチェックリスト**:

- 鍵は `~/.zshenv` / `~/.bashrc` / `launchctl setenv`、あるいはシークレットマネージャ (1Password CLI `op run --env-file=...`、macOS Keychain など) に置く。リポジトリ内の `.env` には置かない
- プロバイダ鍵は定期的にローテート。ルーターは鍵と進行中のリクエストを紐付けるような状態を一切持ちません
- Issue や PR のコメントに鍵を貼らない
- ログや `requests.jsonl` を誰かに渡す前に `coderouter doctor --check-secrets` を走らせる
- `written-log-scan` が鍵を見つけたら、ログを削除する前に該当プロバイダの鍵をローテートする

---

## 2. サプライチェーン衛生

2023〜2025 年にかけて、Python と GitHub Actions のエコシステムでサプライチェーン攻撃が日常化しました。パッケージハイジャック (`ctx`)、タイポスクワット、タグの指し替え (`tj-actions/changed-files`)、メンテナアカウントの乗っ取りなど。CodeRouter の方針は**どの上流の単発侵害も黙って通さない**よう、多層化してあります。

### 2.1 最小限のランタイム依存

ランタイム依存は意図的に **5 パッケージ**に絞っています: `fastapi`、`uvicorn[standard]`、`httpx`、`pydantic`、`pyyaml`。

プロバイダ SDK (`anthropic`、`openai`、`litellm`、`langchain`) は**コードレベルで禁止**されています。CI の `test` ジョブがソースに対して `import anthropic|openai|litellm|langchain` を grep し、一致すればビルド失敗。ルーターは `httpx` で各 wire プロトコルを直接喋ります。

これは設計上の不変項 (plan.md §5.4) であると同時に、**アタックサーフェスの選択**でもあります。よく監査されている 5 つの著名パッケージは、便利 SDK 経由で引き込まれる膨大な推移的依存グラフより信頼できるからです。

### 2.2 Lockfile による固定インストール

`uv.lock` はすべての直接・推移依存をバージョンとハッシュで厳密にピン留めしています。CI では `uv sync --frozen --extra dev` を使っていて、`pyproject.toml` と lockfile にドリフトがあれば install 自体を拒否します。新しい推移依存が `main` に現れるには、**誰かがレビューした明示的な lockfile 更新**が必要です。

### 2.3 複数ソースの CVE 監査

アドバイザリ DB の対応範囲が完全には重ならないので、push 時に 2 つのスキャナを走らせています:

| スキャナ | データソース | カバー範囲 |
|---|---|---|
| `pip-audit` | PyPA Advisory Database (Python の一次フィード) | PyPA にミラーされた CVE |
| OSV-Scanner | Google OSV (GHSA + 言語横断) | PyPA 未反映の GHSA、エコシステム横断のアドバイザリ |

検出が 1 件でもあればビルド失敗。両フィードにはアドバイザリ反映のタイムラグが実在していて、**典型的に OSV の方が PyPA より数時間〜1 日早い**ため、CI が 1 分余分に走る価値はあります。

### 2.4 PR 時の依存レビュー

`actions/dependency-review-action` は**プルリクエストのときだけ**走り、その PR が High / Critical のアドバイザリが付いた**新規依存**を導入していたらビルド失敗にします。マージ後に気づくのではなく、PR 時点で止めるのが狙い。

### 2.5 GitHub Actions も依存である

`dependabot.yml` では 2 つのエコシステムを設定しています: `pip` (Python グラフ) と `github-actions` (`.github/workflows/*.yml` で参照しているアクションのバージョン)。Actions はランタイムライブラリと同じく週次で bump されます。

Action のバージョンは現状、メジャータグ (`@v4`、`@v3`、`@v2`) で参照しています。より厳しくピン留めしたい場合は、各タグをコミット SHA に置き換えます (`@3df4ab11eba7bda6032a0b82a6bb43b11571feac # v4` の形式)。Dependabot は SHA ピン留めされたエントリも追従して更新します。

### 2.6 リポジトリに置かれた `providers.yaml` も入力である (v2.13.0)

`providers.yaml` は `restart_command`、`launcher.backends[*].binary`、`launcher.bench.command_template` という**実行ファイルを名指しするフィールド**を持ちます。v2.13.0 以前は、その `providers.yaml` があるディレクトリで `coderouter` (および GUI ランチャ) を起動しただけで、そのファイルが暗黙に読まれていました。clone してきたリポジトリの中で起動する、というだけで任意コード実行の経路になります。

v2.13.0 以降、設定の探索順は次のとおりで、**3 番目はオプトイン**です。

1. `--config` で明示的に渡したパス
2. 環境変数 `CODEROUTER_CONFIG`
3. `./providers.yaml` (カレントディレクトリ) — `CODEROUTER_ALLOW_CWD_CONFIG` が設定されているときだけ
4. `~/.coderouter-t/providers.yaml`

オプトインが有効になるのは `CODEROUTER_ALLOW_CWD_CONFIG` の値が `1` / `true` / `yes` / `on` のときだけです (前後の空白は無視、大文字小文字は区別しません)。それ以外の値 — `0`、`false`、空文字列、`enabled` のような綴り — はすべて「無効」に倒れます。

```bash
# 信頼できるディレクトリでだけ有効化する
CODEROUTER_ALLOW_CWD_CONFIG=1 coderouter-t serve
```

**「今まで動いていたのに設定が読まれなくなった」とき**: `./providers.yaml` が存在するのに読まれなかった場合、プロセスに一度だけ `cwd-config-skipped` 警告が出ます (スキップしたパスと直し方を含む)。設定が 1 つも見つからなければ、`FileNotFoundError` のメッセージにも同じ注記が付きます。直し方は 3 つで、上ほど安全です。

1. `coderouter-t serve --config ./providers.yaml` — そのファイルを明示的に名指しする
2. `export CODEROUTER_CONFIG=$PWD/providers.yaml` — 同じことを環境変数で
3. `export CODEROUTER_ALLOW_CWD_CONFIG=1` — 暗黙探索を戻す。**信頼するディレクトリでのみ**

恒久的な置き場所は `~/.coderouter-t/providers.yaml` です。オプトインを有効にして CWD から読んだ場合は `cwd-config-loaded` 警告が一度だけ出ます。`--config` / `CODEROUTER_CONFIG` で明示的に指したときは (たまたま同じファイルであっても) どちらの警告も出ません — 明示的な選択は、ここで塞いでいる暗黙の挙動ではないからです。

---

## 3. ネットワーク姿勢

CodeRouter は既定で `127.0.0.1` にバインドします (`coderouter-t serve --host`)。運用者が明示的にオプトインしない限り `0.0.0.0` には出ません。信頼境界は「**ループバックのみ**」と定義され、全ルートで Host ヘッダ検証 (DNS rebinding 対策) が働きます — loopback 系以外の Host を持つリクエストは 403 で拒否され、意図的な外部公開時のみ `CODEROUTER_ALLOWED_HOSTS` (カンマ区切り) で許可ホスト名を追加します。チャット入口 (`/v1/messages` / `/v1/chat/completions`) に認証は**ありません**。launcher API の状態変更エンドポイント (start / stop / delete) のみ、`CODEROUTER_LAUNCHER_TOKEN` を設定すると `X-CodeRouter-Token` ヘッダによるトークン認証をオプトインできます (未設定なら従来どおり無認証)。リクエストボディは既定 64 MB が上限で (超過は 413)、`CODEROUTER_MAX_BODY_BYTES` で変更できます。

**読み取り専用エンドポイントも既定では開いています** (v2.14.0)。`/dashboard`、`/metrics.json`、`/metrics` は状態を変えないので launcher のトークン化の対象外でしたが、「読み取り専用」は「無害」ではありません。`/metrics.json` は全プロバイダの名前・種類・有料フラグ・`base_url` と、プロファイルグラフを返します — つまり**どのモデルをどのベンダーへ払って運用しているかのトポロジ全体**です。ラップトップでは問題になりませんが、ポートが他から届くマシンでは無料の偵察エンドポイントになります。

`CODEROUTER_METRICS_TOKEN` を設定すると、この 3 つに `X-CodeRouter-Token` ヘッダによるトークン認証が入ります (`CODEROUTER_LAUNCHER_TOKEN` と同じ仕組み・同じヘッダ名。突合は `secrets.compare_digest`、不一致・欠落は 401)。**トークンをクエリパラメータでは受け付けません** — URL に載せたトークンはアクセスログ・`Referer`・ブラウザ履歴に残るためです。

```bash
export CODEROUTER_METRICS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
coderouter-t serve
curl -H "X-CodeRouter-Token: $CODEROUTER_METRICS_TOKEN" http://127.0.0.1:4000/metrics.json
```

未設定なら従来どおり開いたままで (アップグレードで既存の Prometheus scrape が壊れない)、対象エンドポイントへの最初のアクセス時にプロセスで一度だけ `metrics-auth-disabled` 警告が出ます。認証チェックはスナップショットとプロバイダ一覧を**組み立てる前**に走るので、401 は何も漏らしません。`/dashboard` の HTML に埋め込まれるのは「認証が必要か」の真偽値だけで、トークン自体は決して埋め込まれません (v2.13.0 に `curl /launcher | grep` でトークンが回収できた事故があったための分離)。ブラウザ側はプロンプトで入力した値を `sessionStorage` に保持します (タブを閉じれば消える)。

**運用者のチェックリスト**:

- マルチユーザホストで `0.0.0.0` にバインドするのは、認証を強制するリバースプロキシを別途挟まない限り避ける。loopback 以外のホスト名でアクセスさせる場合は `CODEROUTER_ALLOWED_HOSTS` に当該ホスト名を追加する (無ければ Host 検証で 403)
- launcher が loopback の外から到達しうる構成では `CODEROUTER_LAUNCHER_TOKEN` を設定する
- 同じ構成では `CODEROUTER_METRICS_TOKEN` も設定する。`CODEROUTER_LAUNCHER_TOKEN` は launcher API しか守らず、`/metrics.json` のトポロジは開いたままだから
- ネットワーク越しに公開する必要がある (リモート開発など) 場合は、ポートを開くのではなく SSH や VPN 越しにトンネルする
- 上流プロバイダの URL は config ロード時にチェックされます。`base_url` のタイポは即座に失敗するので、間違ったエンドポイントに静かに到達することはありません

---

## 4. CI が強制すること・しないこと

| ゲート | CI で強制? | 根拠 |
|---|---|---|
| `pytest` (全スイート) | はい | コアの回帰サーフェス |
| `ruff check` | はい | 実バグを低コストで検出 |
| 禁止 SDK の grep | はい | アーキテクチャ不変項 (§2.1) |
| `uv sync --frozen` | はい | Lockfile ドリフト = 失敗 (§2.2) |
| `gitleaks` | はい | シークレット漏洩検出 (§1) |
| `pip-audit` | はい | PyPA の CVE フィード (§2.3) |
| OSV-Scanner | はい | OSV の CVE フィード (§2.3) |
| `dependency-review-action` | PR 時のみ | PR 時点で新規の脆弱依存をブロック (§2.4) |
| `ruff format --check` | **いいえ** | 体裁のみ。ローカルで走らせる |
| `mypy --strict` | **いいえ** | 必要ならローカルで。機能上の基準は `pytest` |

開発中はスタイルと strict 型付けに価値があります。ただし CI 上ではセキュリティゲートと注意のリソースを取り合う関係になるため、**1 人プロジェクトでの明示的な判断**として、これらはローカル関心事に留めています。

### ローカルですべての CI ゲートを走らせる

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run pytest -v
uv export --frozen --no-emit-project --no-hashes --extra dev --format requirements-txt -o requirements-audit.txt
uv run --with pip-audit pip-audit --strict -r requirements-audit.txt
grep -RnE "^\s*(import|from)\s+(anthropic|openai|litellm|langchain)" coderouter/ && echo FAIL || echo OK
```

---

## 5. 脆弱性の報告

ユーザの鍵を侵害しうる、リクエスト内容を漏洩しうる、またはルーターから上流プロバイダアカウントに pivot できるような問題を見つけた場合は、**公開 Issue を立てない**でください。

1. GitHub Security Advisory を開く: `https://github.com/zephel01/CodeRouter/security/advisories/new`
2. 可能であれば再現手順を含める
3. 数日以内の acknowledgment を想定してください。これは個人プロジェクトであり、24×7 のサービスではありません

セキュリティに該当しないバグは通常の Issue トラッカへ。

---

## 6. 方針の更新履歴

- **v1.0 (2026-04)** — 初版の security.md。v1.0.0 アンブレラ後、CI を回帰 + サプライチェーンに再スコープ。`pip` と `github-actions` の両方で Dependabot を有効化。OSV-Scanner と dependency-review-action を追加。`mypy --strict` と `ruff format --check` を CI から外す
- **v2.13.0 (2026-08)** — `./providers.yaml` の暗黙読込を `CODEROUTER_ALLOW_CWD_CONFIG` オプトイン化 (§2.6)。`restart_command` をシェル経由から argv (`shell=False`) 実行へ
- **v2.14.0 (2026-08)** — `credential.source: cli_session` (§1.1)、全ログシンクへの秘密スクラブ (§1.2)、`coderouter doctor --check-secrets` (§1.3)、`CODEROUTER_METRICS_TOKEN` による `/dashboard` `/metrics.json` `/metrics` のオプトイン認証 (§3)

*最終更新: 2026-08-10 (v2.14.0 時点)*
