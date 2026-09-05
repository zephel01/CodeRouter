# Launcher バックエンドバリアント (llama.cpp 特化ビルドの切り替え)

> 対象バージョン: v2.11.0 (予定)
> 関連: `docs/backends/launcher.md` §`backends` / `docs/designs/launcher-model-swap.md` §5 / `coderouter/launcher_devices.py` (デバイス選択, commit `7e1dcfd`)
> ステータス: **実装済み (2026-07-28)** — as-built は §14
> ベース commit: `78a2118` (v2.10.0)
> Owner: zephel01

llama.cpp を複数バックエンド向けにビルドしている環境 (`build/` `build-cuda/` `build-vulkan/` `build-rocm/`) で、起動ごとにどのビルドの `llama-server` を使うかを選べるようにする。実装方式は **バックエンド名にバリアントを足す** 方式 (`llama.cpp-cuda` 等)。当初検討した `builds:` リスト方式との比較と採用理由は §付録A。ファイル・行番号は §付録B に集約した。

## 1. 概要と目的

### 1.1 何を作るか

`providers.yaml` の `launcher.backends` に `llama.cpp-cuda` / `llama.cpp-vulkan` / `llama.cpp-rocm` のような **バリアント名** を書けるようにする。書いた名前はそのまま Launcher (Web版 / GUI版) のバックエンドセレクトに現れ、選ぶとその `llama-server` で起動する。デバイス検出・ベンチスイープ・モデル自動スワップもバリアント単位で動く。

```yaml
launcher:
  backends:
    llama.cpp:
      binary: ~/llm/apps/llama.cpp/build/bin/llama-server
    llama.cpp-cuda:
      binary: ~/llm/apps/llama.cpp/build-cuda/bin/llama-server
    llama.cpp-vulkan:
      binary: ~/llm/apps/llama.cpp/build-vulkan/bin/llama-server
    llama.cpp-rocm:
      binary: ~/llm/apps/llama.cpp/build-rocm/bin/llama-server
```

### 1.2 なぜ作るか

同一ハードウェアでもビルドによって列挙されるデバイスが違う。実機 (NucBox EVO-X2 + RTX 5090 + RTX 3090 + Radeon 8060S) の `--list-devices`:

| ビルド | 列挙されるデバイス |
| --- | --- |
| `build-cuda/` | `CUDA0` RTX 5090 (32149 MiB) / `CUDA1` RTX 3090 (24123 MiB) |
| `build-vulkan/` | `Vulkan0` RTX 3090 (24822 MiB) / `Vulkan1` RTX 5090 (32607 MiB) / `Vulkan2` Radeon 8060S (114164 MiB) |
| `build-rocm/` | `ROCm0` Radeon 8060S (98304 MiB, free 25712 MiB) |

Radeon 8060S に載せたいなら Vulkan か ROCm、NVIDIA 2枚で tensor-split したいなら CUDA、という択がある。現状は `binary` が 1 本しか持てないので YAML 書き換え + CodeRouter 再起動が必要。ここを UI の 1 セレクトにする。

### 1.3 この方式を選ぶ理由 (要旨)

既存の UI・API・設定はすべて **「バックエンド名」を軸に** 組まれている。バックエンドセレクトの選択肢は Web も GUI も `_BACKEND_DEFAULTS.keys()` から生成され、デバイス検出は `GET /api/launcher/devices?backend=...`、`option_profiles` はバックエンド名キー、`swap` のモデルも `backend:` を持つ。バリアントを「新しいバックエンド名」として通せば、**これらがすべて無改造で機能する**。新しいスキーマクラス・新しい API ルート・新しい UI ウィジェットがいずれも不要になる。

代償は 1 点だけで、バックエンド名は挙動の分岐にも使われているため、**バリアント名を取りこぼした分岐が静かに劣化する**。これを正規化ヘルパ 1 本で潰すのが本設計の中核 (§3・§4)。

### 1.4 設計思想との整合

- 依存は増やさない (runtime 5-deps 不変則)。新規ロジックは stdlib のみ。
- `launcher.backends` にバリアントを書かなければ、UI の選択肢も argv も **現状と完全に同一**。
- 実行ファイルパスはオペレータが設定した静的な値のみ。API はバックエンド名しか受け取らず、`launcher.backends` に無い名前は 400 (§8)。

## 2. スコープ / 非スコープ

### 2.1 スコープ

| # | 項目 | 主な対象 |
| --- | --- | --- |
| A | `base_backend()` / `variant_of()` 正規化ヘルパと **全分岐への適用** | `launcher_devices.py` + `launcher_routes.py` + `launcher_gui.py` |
| B | バックエンド一覧の config 由来化 (ハードコード撤去) | `launcher_routes.py` / `launcher_gui.py` |
| C | バリアント名のロード時検証 (fast-fail) | `config/schemas.py` |
| D | デバイス選択のバリアント連動 (再プローブ + ID 検証) | A/B |
| E | `option_profiles` の基底名マージ | A/B |
| F | ベンチスイープのバリアント横断実行 | `launcher_devices.py` / 両ランチャー |
| G | `swap` のモデル別バリアント指定 | `config/schemas.py` / `launcher_swap.py` |
| H | ドキュメント / examples / CHANGELOG | `docs/backends/launcher.{md,en.md}` ほか |

### 2.2 非スコープ

- **`root` 自動探索**。`build*/` の有無でバックエンド名が生えたり消えたりすると、`swap.models[].backend` の設定検証がディスク状態に依存してしまう。バリアントは明示記述のみ。
- **ビルドの自動選択**。検出デバイスの併記まではやるが、CodeRouter がバリアントを勝手に選ぶことはしない。
- **ドライバ / ランタイムの導入と検査**。CUDA Toolkit・Vulkan ランタイム・ROCm はユーザーが自分で入れる前提。`ldd` 相当の事前検査もしない。健全性判定は `--list-devices` が通るかに委ねる (§7.3)。
- **llama.cpp のビルド実行**。`cmake` は呼ばない。
- **vllm / mlx のバリアント**。スキーマ上は `vllm-rocm` 等も同じ規約で書けるが、今回は動作確認もドキュメント化もしない。

## 3. バリアント名の規約と正規化

### 3.1 命名

```
<base>            例: llama.cpp / vllm / mlx        (従来の 3 つ = 基底名)
<base>-<variant>  例: llama.cpp-cuda / llama.cpp-vulkan / llama.cpp-rocm
```

`variant` は `[a-z0-9][a-z0-9._-]*`。基底名は `_BACKEND_DEFAULTS` の 3 キーに限る。

### 3.2 正規化ヘルパ (`coderouter/launcher_devices.py` に新設)

`launcher_devices.py` は「GUI (threading) と Web (asyncio) の双方から使う純ロジック層・pydantic 非依存・stdlib のみ」という既存の位置付けなので、ここに置いて両側から使う。バイナリ解決が Web と GUI で二重実装されている現状の改善も兼ねる。

```python
KNOWN_BASE_BACKENDS: tuple[str, ...] = ("llama.cpp", "vllm", "mlx")

def base_backend(name: str) -> str:
    """'llama.cpp-cuda' -> 'llama.cpp' / 'llama.cpp' -> 'llama.cpp'。

    既知基底名との最長一致で判定する。単純な ``split("-", 1)`` にしない
    のは、将来基底名自体がハイフンを含んだときに静かに壊れないため。
    未知の名前はそのまま返す (呼び出し側が既存どおり Unknown backend で
    弾く)。
    """

def variant_of(name: str) -> str | None:
    """'llama.cpp-cuda' -> 'cuda' / 'llama.cpp' -> None。"""

def is_variant(name: str) -> bool: ...
```

**単純な文字列分割にしない**点が重要。`name.split("-")[0]` だと `llama.cpp` はたまたま通るが、規約の意図が読めず将来壊れる。既知基底名の集合に対する最長一致で実装する。

## 4. 正規化を通す箇所 (本設計の中核)

バックエンド名を挙動の分岐に使っている全箇所。**取りこぼしたときに即エラーになるか、静かに劣化するか** で分類した。fail-open の 5 箇所が本作業の実質的な中身。

### 4.1 fail-open (取りこぼすと静かに壊れる) — 必ず正規化する

| # | 箇所 | 取りこぼした場合に起きること | 深刻度 |
| --- | --- | --- | --- |
| 1 | `_MODEL_FLAGS` `launcher_routes.py:304` (`_assert_no_model_override` 316-328 経由) | `.get(backend, frozenset())` が空集合を返し、**H8 のモデル上書きガードが無効化**。`options` / `extra_args` 経由で `-m` を渡して `model_path` を差し替えられる | **高 (セキュリティ)** |
| 2 | `_backend_ready` `launcher_routes.py:657` / GUI `launcher_gui.py:721` | `if backend in ("llama.cpp","vllm")` を外れ、readiness が素の TCP connect に退行。**モデルのロード完了前に provider が登録される** (readiness ゲーティングが直したバグへの逆戻り) | **高 (正しさ)** |
| 3 | `launcher_speculative.py:51,216` (`_LLAMA_CPP`) | MTP / speculative decoding のフラグが付かなくなる。設定したのに黙って効かない | 中 |
| 4 | device_ids gating `launcher_routes.py:1329` / GUI `launcher_gui.py:1935` | `--device` / `--tensor-split` が argv から黙って落ちる。デバイス選択が無効化 | 中 |
| 5 | インライン JS `isLlama()` `launcher_routes.py:2363` / `2631` | デバイス欄・llama.cpp 固有オプションが UI に出ない | 低 |

### 4.2 fail-closed (取りこぼすと即エラー) — 正規化するが安全側

| # | 箇所 | 取りこぼした場合 |
| --- | --- | --- |
| 6 | `_build_cmd` `launcher_routes.py:556-575` / GUI `launcher_gui.py:408-` | `raise ValueError(f"Unknown backend: {backend!r}")` で起動時に即エラー |
| 7 | `SwapModelSpec.backend` `schemas.py:1547` の `Literal[...]` | config ロード時にバリデーションエラー |
| 8 | `_resolve_binary` `launcher_routes.py:330-333` の `_BACKEND_DEFAULTS.get(backend, backend)` | フォールバックがバックエンド名そのものになり `llama.cpp-cuda` を exec して FileNotFound。**ただし §5.2 でバリアントは `binary` 必須にするのでこの経路には入らない** |

### 4.3 意図せず正しく動く箇所 (確認のみ)

`_suggest_flags` `launcher_routes.py:430-` は `mlx` / `vllm` を先に弾いて **残り全部を llama.cpp 扱いにする** else 構造なので、`llama.cpp-cuda` でも `-ngl` / `--ctx-size` の推奨が正しく出る。正規化を入れても挙動不変。テストで固定する。

## 5. 設定スキーマ

### 5.1 変更しないもの

`LauncherBackendConfig` は `binary` 1 フィールドのまま。`LauncherConfig.backends` は `dict[str, LauncherBackendConfig]` のまま (キーに任意文字列を取れる既存の型がそのままバリアントの受け皿になる)。**新規スキーマクラスはゼロ**。

### 5.2 追加する検証 (`LauncherConfig.model_validator(mode="after")`)

1. `backends` のキーが 基底名 でも `<既知基底>-<variant>` でもない → **エラー**。
   現状はキーが何であっても黙って無視される (`_resolve_backends_sync` が `_BACKEND_DEFAULTS` を回すため)。ここを fast-fail にするので、**`llamacpp:` のような既存の typo が起動エラーになる可能性がある**。これは意図的な改善だが CHANGELOG に破壊的変更として明記する。
2. **バリアントは `binary` 必須** → 未指定はエラー。
   `llama.cpp-cuda: {}` を許すと `_resolve_binary` が基底名の既定 (`llama-server`) を PATH から拾い、**CUDA ビルドを指定したつもりで素のビルドが静かに動く**。これが最も気づきにくい事故なのでロード時に殺す。基底名は従来どおり `binary` 任意 (PATH 解決)。
3. `swap.models[].backend` がバリアントなら `backends` に存在すること → 無ければエラー。

### 5.3 `SwapModelSpec.backend` の緩和

```python
# before
backend: Literal["llama.cpp", "vllm", "mlx"]
# after
backend: str    # base_backend() が既知基底名になることを validator で検証
```

既存の 3 値はそのまま通る (後方互換)。バリアントを書いた場合は §5.2-3 で `launcher.backends` との整合を取る。

### 5.4 `providers.yaml` 記述例

```yaml
launcher:
  model_dirs:
    - ~/models

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

    vllm:
      binary: ~/.coderouter-t/backends/vllm/bin/python

  option_profiles:
    llama.cpp:                    # 基底名 = 全バリアントに継承される
      - name: "標準"
        args: { "-ngl": 99, "--ctx-size": 4096 }
    llama.cpp-cuda:               # このバリアント専用 (基底の後ろに連結)
      - name: "5090単体・速度重視"
        args: { "-ngl": 99, "--ctx-size": 8192 }

  swap:
    enabled: true
    models:
      - name: qwen3-30b
        backend: llama.cpp-cuda   # このモデルは常に CUDA ビルドで起動
        model_path: ~/models/Qwen3-30B-A3B-Q4_K_M.gguf
        port: 18081
```

## 6. バックエンド一覧の config 由来化

現状 `_resolve_backends_sync` (`launcher_routes.py:336-364`) は `_BACKEND_DEFAULTS` の 3 キーを固定で回し、`api_backends` がそれを返し、Web の `<select>` (1928-1936) と GUI の Combobox (1525-1532) はそれぞれ **HTML に直書き / `_BACKEND_DEFAULTS.keys()`** から選択肢を作っている。

変更後の一覧生成:

```
[基底 3 つ (常に出す・従来どおりの順)] + [launcher.backends に書かれたバリアントを記述順で]
```

これにより **`providers.yaml` にバリアントを書かない利用者の画面は 3 択のまま完全に不変**。前回決めた「特化ビルドは上級者向けなので通常利用者の画面を汚さない」は、バッジや注記なしに構造で達成される。Web の `<option>` 直書きは撤去して `fetchBackends()` の結果から動的生成に変える。

`_resolve_binary` は `configured or _BACKEND_DEFAULTS.get(base_backend(backend), backend)` に変える (§4.2-8)。

## 7. UI

### 7.1 見え方

```
バックエンド [llama.cpp ▼]
             ├ llama.cpp                  (素のビルド)
             ├ vllm
             ├ mlx
             ├ llama.cpp-cuda    ⚙        — CUDA0 RTX 5090 / CUDA1 RTX 3090
             ├ llama.cpp-vulkan  ⚙        — Vulkan0 RTX 3090 / Vulkan1 RTX 5090 / Vulkan2 Radeon 8060S
             └ llama.cpp-rocm    ⚙        — ROCm0 Radeon 8060S
             /home/zephel01/llm/apps/llama.cpp/build/bin/llama-server  ● found
```

初期選択は従来どおり `llama.cpp`。バリアントには `⚙` を付け、選択時に「このビルドは対応ランタイム (CUDA / Vulkan / ROCm) が導入済みの環境でのみ動作します」の一行注記を出す。デバイス一覧の併記は `GET /api/launcher/devices` の結果を使い、**セレクトを開いた時点では取らない** (バリアント 3 つ × 5 秒タイムアウトの同期プローブを避ける)。併記は選択後の表示と、後述のスイープ画面で行う。

### 7.2 バリアント切替とデバイス選択の連動 (最重要の罠)

`llama.cpp-cuda` で `CUDA0` を選んだまま `llama.cpp-vulkan` に切り替えて起動すると `--device CUDA0` が Vulkan ビルドに渡り起動失敗する。3 段で防ぐ。

1. **UI**: 既存の `onBackendChange` / `_on_backend_change` がバリアント切替でもそのまま発火するので、そこでデバイス選択と tensor-split をクリアして再プローブする (既存フックの拡張のみ)。
2. **サーバ**: `start` / `sweep/start` 受領時に、そのバリアントのプローブ結果に無い `device_ids` があれば **400**。`probe.ok == False` のときは検証をスキップして通す (best-effort 原則)。
3. **表示**: 選択中バリアントの検出デバイスを常に表示し、取り違えを起きにくくする。

`detect_llama_devices` のキャッシュはバイナリパス単位 (TTL 60s) なので、バリアントごとに独立したキャッシュが自動的に効く。**ここは無改修**。

### 7.3 ランタイム未導入時の扱い

`--list-devices` が失敗したバリアントは「デバイスを列挙できません — ランタイム未導入の可能性」と赤字表示する。起動は禁止しない (検出できないが動く環境を潰さないため)。`launcher.auto_restart` を有効にした環境でランタイム欠如のバリアントを選ぶとクラッシュ→再起動が走るが、`auto_restart_max_attempts` (既定 3) で打ち切られ `status='error'` に落ち着く。無限ループはしない旨をドキュメントに注記する。

## 8. API 変更

**新規ルートなし。リクエスト/レスポンスのフィールド追加もなし。** 既存の `backend` パラメータがバリアント名を受け取れるようになるだけ。

| メソッド / パス | 変更 |
| --- | --- |
| `GET /api/launcher/backends` | 応答に config 由来のバリアントが含まれるようになる (キー構造は不変) |
| `GET /api/launcher/devices?backend=llama.cpp-cuda` | 無改修で動く (`_configured_binary_for` がバックエンド名キーで引くため) |
| `POST /api/launcher/start` | `backend` にバリアント名を許可。**未知の名前は 400** (`launcher.backends` に無い名前はフォールバックせず拒否) |
| `POST /api/launcher/sweep/start` | `backends: list[str] \| None` を追加 (バリアント横断スイープ・§9) |
| `GET /api/launcher/config-debug` | 無改修 (`launcher_cfg.backends` をそのままダンプしている) |

## 9. ベンチスイープのバリアント横断

`SweepStep` に `backend: str | None` を追加し、ステップごとに実行ファイルを解決する。ラベルは `"cuda / CUDA0 単体"` のようにバリアント名を前置するので、`{config}` プレースホルダ経由でベンチ結果 JSON もバリアント別に見分けられる。

```python
def build_cross_variant_sweep_configs(
    probes: Sequence[tuple[str, Sequence[LlamaDevice]]],   # (backend_name, devices)
    *, by: str = "total",
) -> list[tuple[str, str, DeviceSelection]]:               # (label, backend_name, selection)
```

既存の `build_auto_sweep_configs` (`launcher_devices.py:323-368`) は **シグネチャも挙動も変えない**。新関数は各バリアントのプローブに対して既存関数を呼び、ラベルにバリアント名を付けて連結するだけ。`backend=None` のステップは従来どおりプラン単位の backend で動くので、既存スイープの argv は不変。

これで「同一モデルを CUDA ビルドと Vulkan ビルドで順に起動してベンチし、どちらが速いか表を出す」が 1 回のスイープで回る (性能比較という本来の目的に直結)。

## 10. `option_profiles` の基底名マージ

`option_profiles[base_backend(backend)]` (継承) を先に、`option_profiles[backend]` (バリアント固有) を後に連結する。`name` が衝突したらバリアント固有が **同じ位置で置き換える** (並び順を安定させ、後ろに重複を作らない)。バックエンド名が基底名そのもののときは従来と完全に同一の結果になる。

これにより「共通プロファイルを 4 キーに複製する」問題を回避する。

## 11. セキュリティ考慮

- **§4.1-1 の `_MODEL_FLAGS` 正規化が最優先**。ここを落とすと H8 のモデル上書きガードが無効化される。「全バリアントでガードが効く」ことをテストで固定する (§12.3)。
- **API はバックエンド名しか受け取らない**。`binary` パスをリクエストで渡す口は作らない。名前は `launcher.backends` のキーに完全一致でしか解決されず、一致しなければ 400。`model_path` を `model_dirs` に対して再検証している既存方針 (`_resolve_within_model_dirs`) と同じ姿勢。
- **バリアントは `binary` 必須** (§5.2-2)。設定漏れで PATH の別バイナリに落ちる経路を作らない。
- **バリアント名の文字種を制限** (`[a-z0-9][a-z0-9._-]*`)。プロバイダ名・プロファイル名・ログに混ざっても壊れない集合に限り、パス区切りやシェルメタ文字を通さない。
- 追加の `subprocess` 実行はない。`--list-devices` は既存 `detect_llama_devices` の固定 argv 経路のみ。

## 12. テスト計画

CI は `uv run ruff check .` + `uv run pytest -v`。GUI テストは `pytest.importorskip("tkinter")` 必須 (CI の uv Python に tkinter が無い)。

### 12.1 正規化ヘルパ (新規 `tests/test_launcher_backend_variants.py`)

- `base_backend`: 基底 3 名 / `llama.cpp-cuda` / `llama.cpp-vulkan` / `vllm-rocm` / 未知名はそのまま返す / `llama.cpp-` のような不正形
- `variant_of` / `is_variant` の対応
- 最長一致であること (単純 `split("-")` では通らないケースを 1 本入れて実装を固定する)

### 12.2 スキーマ (新規 `tests/test_launcher_config_variants.py`)

- §5.4 の YAML がそのまま `CodeRouterConfig.model_validate` を通る
- 不正キー (`llamacpp` / `llama.cpp-` / `llama.cpp-CUDA`) で fast-fail
- **バリアントの `binary` 未指定で fast-fail** (§5.2-2)
- `swap.models[].backend` がバリアントで `backends` に無い → fast-fail / 既存の 3 値はそのまま通る
- 後方互換: `binary` のみの既存 YAML が無改変で通る

### 12.3 fail-open 分岐の回帰 (新規 `tests/test_launcher_variant_routes.py`)

§4.1 の 5 箇所に 1:1 対応させる。ここが本設計の実質的な保証。

- **`_MODEL_FLAGS`**: `llama.cpp-cuda` で `options={"-m": "/etc/passwd"}` / `extra_args="--model x"` が `ValueError` で弾かれる (全バリアントで parametrize)
- **`_backend_ready`**: `llama.cpp-cuda` が TCP ではなく `GET /health` を叩く (httpx をフェイクして呼ばれた URL を確認)
- **spec / MTP**: `llama.cpp-cuda` で `--model-draft` 等の spec トークンが argv に載る
- **device_args**: `llama.cpp-cuda` + `device_ids` で `--device` が argv に載る / `vllm-x` では載らない
- **`_suggest_flags`**: `llama.cpp-cuda` で `-ngl` が出る (§4.3 の確認)
- `POST /start` で `launcher.backends` に無いバリアント名 → 400 (フォールバックしない)
- そのバリアントに存在しないデバイス ID → 400 / プローブ失敗時はスキップして通る
- **`backends` にバリアントを書かない config で、`api_backends` の応答と argv が現行実装とバイト単位で一致する回帰** (後方互換の核)
- バリアント横断スイープ: 2 バリアント分のステップが生成され、各ステップが正しい実行ファイルで spawn される
- `option_profiles` の基底 → バリアントのマージ順と name 衝突時の置換位置

### 12.4 GUI (新規 `tests/test_launcher_variant_gui.py`)

- `importorskip("tkinter")`
- backend Combobox の選択肢が config 由来のバリアントを含む / 書かなければ 3 択のまま
- バリアント切替でデバイスチェックボックスとプロファイルが再構築され、選択がクリアされる
- GUI 側の `_backend_ready` (721) / `_build_cmd` (408) / device gating (1935) が正規化されている

### 12.5 既存テストへの追加

- `tests/test_launcher_devices_routes.py`: バリアント横断スイープのライフサイクル
- `tests/test_launcher_swap.py`: バリアント指定モデルが該当バイナリで spawn される
- `tests/test_launcher_mtp.py`: `_build_cmd` のバリアント parametrize

想定追加ケース数はおよそ 55〜70。

## 13. 実装見積り

| 区分 | 想定差分 |
| --- | --- |
| `coderouter/launcher_devices.py` (正規化ヘルパ + 横断スイープ + `SweepStep.backend`) | +90 行 |
| `coderouter/config/schemas.py` (validator + `Literal` 緩和) | +70 行 |
| `coderouter/ingress/launcher_routes.py` (正規化適用 + 一覧 config 由来化 + JS) | Python +90 / JS+HTML +70 行 |
| `coderouter/launcher_swap.py` | +15 行 |
| `launcher_gui.py` (正規化適用 + Combobox 動的化) | +110 行 |
| テスト (新規 4 ファイル + 既存 3 ファイル追記) | +520 行 / 55〜70 ケース |
| ドキュメント / examples | +180 行 |

合計およそ **+1,145 行**。当初の `builds` 方式 (Phase 1 のみで +1,150 行、全体 +1,700 行) に対し、機能は全部入りでほぼ Phase 1 相当のコストに収まる。

### 13.1 フェーズ分割

| Phase | 内容 | 差分目安 |
| --- | --- | --- |
| 1 | A (正規化ヘルパ + 全分岐適用) + §12.1/§12.3 のテスト。**バリアント名はまだ受け付けない** | +350 行 |
| 2 | B/C/D/E (一覧 config 由来化・検証・デバイス連動・profiles マージ) + 両 UI | +450 行 |
| 3 | F/G (横断スイープ・swap) + H (ドキュメント) | +345 行 |

Phase 1 を独立させる意図は、**fail-open のリスク (ガード無効化・readiness 退行) を、機能追加より先に潰しておく** こと。Phase 1 は挙動を一切変えない純粋なリファクタなので、既存テストが全部通ることがそのまま検証になる。

## 14. as-built 記録 (2026-07-28 実装)

Phase 1〜3 をまとめて実装した。テストは **2098 passed** (実装前 1916 → +182)、ruff clean、mypy の新規エラー 0 (既存 51 件は据え置き)、インライン SPA の JS は `node --check` 通過。

### 14.1 設計との差分

| 箇所 | 設計 | 実装 | 理由 |
| --- | --- | --- | --- |
| 横断スイープの API | `SweepRequest.backends: list[str]` | **`SweepConfigItem.backend: str \| None`** (構成ごと) | サーバ側で「ビルド × デバイス構成」の直積を組む必要がなくなり、構成ごとに自由なビルドを指せる。フロントは各ビルドの `auto_configs` を連結するだけで済む |
| `build_sweep_steps` | 2 要素タプルのみ | **2 要素/3 要素の両対応** | 既存の呼び出し (`(label, selection)`) を無改修で通すため。3 要素形が横断用 |
| `_assert_no_model_override` | 正規化するだけ | **fail-closed 化も追加** | 未知の基底名で `banned` が空集合になる既存の弱さを、正規化と同時に潰した (全 banned 集合の和を使う) |
| デバイス ID 検証 | 設計 §7.2 のとおり | 共有関数 `foreign_device_ids` を `launcher_devices.py` に置き、**判定を id の完全一致ではなくバックエンド接頭辞単位** (`backend_of`) にした | 捕まえたいのは「ビルド違い=名前空間の不一致」であって同一ビルド内の番号ズレではない。完全一致だと `--list-devices` の出力形式が変わって `parse_list_devices` が一部の行を取りこぼしたときに正しい id を誤って拒否する |
| `option_profiles` マージ | 設計 §10 のとおり | 同じ。`resolve_option_profiles` を PEP 695 ジェネリックにした | pydantic の `LauncherOptionProfile` と GUI の dataclass `OptionProfile` の双方を 1 関数で受けるため |
| GUI の正規化ヘルパ | 共有層から import | **import + standalone フォールバック実装** | `launcher_gui.py` は `coderouter` パッケージ無しでも動く要件がある。`_build_cmd` / `_backend_ready` は `_HAS_DEVICES` に関係なく通る経路なので `None` にできない |

### 14.2 変異テストによる検証

「取りこぼすと静かに壊れる」性質上、テストが本当に退行を捕まえるかを変異させて確認した。

| 変異 | 捕まえた失敗数 |
| --- | --- |
| `_MODEL_FLAGS` の正規化を外す | 22 |
| `_backend_ready` の正規化を外す | 3 |
| `resolve_speculative` の正規化を外す | 3 |
| GUI `_backend_ready` の正規化を外す | 3 |
| GUI `_resolve_binary` の正規化を外す | 1 |
| `_assert_backend_declared` / `_assert_device_ids_known` を外す | 2 |
| バックエンド一覧を config 由来にしない | 4 |

### 14.3 実際の差分規模

見積り +1,145 行に対し、テストが厚くなった分だけ増えた。内訳はコード約 +560 行 / テスト 5 ファイル +1,050 行 (150 ケース) / ドキュメント・examples +260 行。

### 14.4 実機投入で判明した不具合 (2026-07-28 追記)

Linux 実機 (PATH の `llama-server` が Vulkan ビルド) でテストを回したところ、**既存テスト `test_launcher_devices_routes.py::test_start_passes_device_args` が失敗**した。

原因は本機能そのものではなく、**新しいデバイス ID 検証が既存テストを実行ホスト依存にしてしまった**こと。当該テストは `device_ids=["CUDA0","CUDA1"]` を渡すが `detect_llama_devices` をスタブしていないため、実バイナリの `--list-devices` が走る。llama-server が PATH に無い Mac ではプローブ失敗 → 検証スキップ → 200、Vulkan ビルドがある Linux では `Vulkan0/1/2` が返って `CUDA0` が弾かれ 400、と結果が環境で変わっていた。

対処は 2 つ:

1. `tests/test_launcher_devices_routes.py` に autouse フィクスチャを追加し、`detect_llama_devices` を `_SAMPLE_DEVICES` (CUDA0/CUDA1) に固定した。個別に monkeypatch しているテストは後から setattr するのでそちらが優先される。
2. 併せて `foreign_device_ids` の判定を接頭辞単位に緩めた (上表参照)。

検証は偽 `llama-server` を PATH に置いて実機環境を再現して行った。修正前は当該テストが再現性をもって失敗し、修正後は「Vulkan ビルドが PATH にある状態」「PATH に何も無い状態」の双方で 23 passed になることを確認した。

**教訓**: 起動経路に新しく subprocess を伴う検証を足すと、それを想定していない既存テストが静かにホスト依存になる。同種の追加をするときは「device_ids を渡している既存テスト」を先に洗い出してスタブすること。

### 14.5 残作業

- `docs/README.md` の designs 索引への追記 (任意 — 既存も網羅的ではない)
- リリース時に `[Unreleased]` を `v2.11.0` に確定し、破壊的変更 (§5.2-1) をリリースノート冒頭に出す

## 付録A: 当初案 (`builds:` リスト) との比較と採用理由

| 観点 | A案: `builds:` リスト | B案: バックエンド名バリアント (**採用**) |
| --- | --- | --- |
| 新規スキーマクラス | `LauncherBuildSpec` + `builds`/`root`/`default_build` | **なし** (`dict[str, ...]` の既存キーを使う) |
| 新規 API ルート | `GET /api/launcher/builds` | **なし** |
| UI | ビルドセレクトを Web/GUI に新設 | **既存セレクトが自動的に増える** |
| デバイス検出 | `build` クエリを追加 | **無改修** (`?backend=` が既にある) |
| ビルド別 `option_profiles` | マージ機構を新設 | **既存のバックエンド名キーがそのまま使える** (+基底マージ) |
| `swap` のビルド固定 | `SwapModelSpec.build` を追加 | **既存の `backend:` で足りる** |
| `root` 自動探索 | 可能 | 不可 (バックエンド名がディスク状態に依存するのを避けるため) |
| 意味の軸 | backend (どのサーバ) と build (どうコンパイル) を分離 | 2 軸が同一名前空間に混ざる |
| 主なリスク | 新設面が多く実装量が大きい | **バックエンド名の分岐 5 箇所が fail-open** |
| 差分目安 | +1,700 行 (Phase 1 で +1,150) | **+1,145 行 (全機能込み)** |

B案の主リスクは正規化ヘルパ 1 本 + テスト 5 本で構造的に潰せる (§4・§12.3)。一方 A案の「新設面が多い」は減らす方法がない。失う `root` 自動探索はバリアント 3 行を手書きすれば代替できる。よって B案を採用する。

## 付録B: シンボル早見表 (commit `78a2118`)

### B.1 変更対象

| ファイル | シンボル / 行 | 変更内容 |
| --- | --- | --- |
| `coderouter/launcher_devices.py` | `__all__` 32-52 | 新シンボル追加 |
| | (新規) `KNOWN_BASE_BACKENDS` / `base_backend` / `variant_of` / `is_variant` / `build_cross_variant_sweep_configs` / `validate_device_ids` | §3.2 / §9 / §7.2 |
| | `SweepStep` 375-397 | `backend: str \| None` 追加 + `as_dict` |
| | `SweepPlan` 400-409 | ステップ別 backend の受け皿 |
| | `detect_llama_devices` 159-214 / `build_auto_sweep_configs` 323-368 | **無改修** |
| `coderouter/config/schemas.py` | `LauncherConfig` validator 1912- | §5.2 の 3 検証を追加 |
| | `LauncherConfig.backends` 1801-1809 / `LauncherBackendConfig` 1396-1429 | description のみ更新 (バリアント規約を明記) |
| | `SwapModelSpec.backend` 1547 | `Literal` → `str` + validator (§5.3) |
| `coderouter/ingress/launcher_routes.py` | `_BACKEND_DEFAULTS` 284-288 | 基底名の辞書として維持 (参照側を正規化) |
| | `_MODEL_FLAGS` 304-314 / `_assert_no_model_override` 316-328 | **正規化 (§4.1-1・最優先)** |
| | `_resolve_binary` 330-333 | `_BACKEND_DEFAULTS.get(base_backend(backend), backend)` |
| | `_resolve_backends_sync` 336-364 | 一覧を config 由来に (§6) |
| | `_suggest_flags` 430- | 確認のみ (§4.3) |
| | `_build_cmd` 556-575 | 正規化 (§4.2-6) |
| | `_backend_ready` 657 | **正規化 (§4.1-2)** |
| | `StartRequest` 990-1006 | 変更なし |
| | `api_backends` 1096-1112 | config 由来の一覧を返す |
| | `spawn_process` 1137- (config 参照 1176-1180) | 未知バリアントの拒否 + デバイス ID 検証 |
| | `api_start` 1320-1358 (device gating 1329) | **正規化 (§4.1-4)** |
| | `_configured_binary_for` 1438-1444 / `api_devices` 1447-1498 | 無改修で動くことをテストで固定 |
| | `SweepRequest` 1509- | `backends: list[str] \| None` 追加 |
| | `_LAUNCHER_HTML` 1839-2797 (`<option>` 直書き 1928-1936 / `fetchBackends` 2251-2261 / `renderBinaryHint` 2263- / `onBackendChange` 2322-2326 / `isLlama` 2363 / 2631) | 選択肢の動的生成 + **`isLlama` 正規化 (§4.1-5)** |
| `coderouter/launcher_speculative.py` | `_LLAMA_CPP` 51 / 216 | **正規化 (§4.1-3)** |
| `coderouter/launcher_swap.py` | spawn 経路 | バリアント backend の解決 |
| `launcher_gui.py` | `_BACKEND_DEFAULTS` 126-130 / backend Combobox 1525-1532 | 選択肢を config 由来に |
| | `_build_cmd` 408- | 正規化 |
| | `_backend_ready` 721 | **正規化 (§4.1-2)** |
| | `_resolve_binary` 352-355 / `_check_binary` 358-360 | 共有ヘルパへ委譲 |
| | device gating 1935 | **正規化 (§4.1-4)** |
| | `_update_binary_hint` 1811-1834 / device probe 1704-1708 / launch 1940,1958 / sweep 2533,2556,2739,2799 | バリアント反映 |

### B.2 ドキュメント / 付随

| ファイル | 変更 |
| --- | --- |
| `docs/backends/launcher.md` | `### backends — バイナリパス設定` (558-573) を拡張 + 新規 `## 特化ビルドの切り替え (llama.cpp)` セクション。**バリアント命名規約・`binary` 必須・前提ランタイム (CUDA Toolkit / Vulkan ランタイム + ICD / ROCm) を明記**し `install-backends.md` へリンク。`auto_restart` 有効時の注意も併記 |
| `docs/backends/launcher.en.md` | 同内容の英語版 |
| `docs/backends/launcher-quickstart.{md,en.md}` | 2 行程度の追記 |
| `docs/designs/launcher-multi-build.md` | 本ドキュメント (実装後に「as-built」節を追記) |
| `examples/providers.llamacpp-vllm.yaml` | バリアント 3 つのコメント付き例 |
| `examples/README.md` | 1 行の説明追記 |
| `README.md` | Launcher 節の機能表に 1 行 (デバイス選択機能の前例に倣い詳細はリンク先) |
| `CHANGELOG.md` | `[Unreleased]`。**`launcher.backends` の不正キーが fast-fail になる破壊的変更を明記** (§5.2-1) |
