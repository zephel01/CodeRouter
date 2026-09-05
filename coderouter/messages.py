"""Bilingual message catalog (v2.15.0).

Human-facing CLI / doctor / config messages go through this module so
English and Japanese can share the same call site.  JSON logs
(`coderouter.logging`) stay English-only by design — only the
`detail` / `hint` strings rendered to stderr / stdout / HTTP JSON
`detail` are bilingual.

Language resolution (first win):
  1. $CODEROUTER_T_LANG (ja / en / ja-JP etc — case-insensitive prefix match)
  2. $LANG / $LC_MESSAGES (POSIX locale, e.g. ja_JP.UTF-8 -> ja)
  3. fallback: en

Usage:
    from coderouter.messages import tr, resolve_lang
    msg = tr("E1001", lang=None, searched="...")  # None = auto-resolve
"""

from __future__ import annotations

import logging
import os
from typing import Final

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language resolution
# ---------------------------------------------------------------------------

_SUPPORTED: Final[frozenset[str]] = frozenset({"ja", "en"})


def _normalize(raw: str | None) -> str | None:
    if not raw:
        return None
    v = raw.strip().lower()
    if not v:
        return None
    # ja_JP.UTF-8 / ja-JP / ja -> ja ; en_US -> en
    # take first 2 alpha chars
    prefix = v[:2]
    if prefix in _SUPPORTED:
        return prefix
    # also handle "japanese", "english" just in case
    if v.startswith("ja"):
        return "ja"
    if v.startswith("en"):
        return "en"
    return None


def resolve_lang(explicit: str | None = None) -> str:
    """Resolve effective language code ('ja' or 'en')."""
    if explicit is not None:
        n = _normalize(explicit)
        if n is not None:
            return n
        return "en"
    # 1. CODEROUTER_T_LANG
    n = _normalize(os.environ.get("CODEROUTER_T_LANG"))
    if n is not None:
        return n
    # 2. LANG / LC_MESSAGES
    for key in ("LANG", "LC_MESSAGES", "LC_ALL"):
        n = _normalize(os.environ.get(key))
        if n is not None:
            return n
    return "en"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

# Each entry: id -> {lang -> template}.  Templates use str.format kwargs.
_CATALOG: Final[dict[str, dict[str, str]]] = {
    # ---- Config / loader ----
    "E1001": {
        "en": "providers.yaml not found. Searched:\n  {searched}\nHint: copy examples/providers.yaml to ~/.coderouter-t/providers.yaml",
        "ja": "providers.yaml が見つかりません。探した場所:\n  {searched}\nヒント: examples/providers.yaml を ~/.coderouter-t/providers.yaml にコピーしてください",
    },
    "E1001_CWD_NOTE": {
        "en": "Note: {path} exists but was NOT read — implicit CWD discovery is opt-in since v2.13.0. Three ways to use it: pass --config {path}, set CODEROUTER_CONFIG={path}, or set CODEROUTER_ALLOW_CWD_CONFIG=1 (only in directories you trust).",
        "ja": "補足: {path} は存在しますが読み込まれていません — v2.13.0以降、CWDの自動検出はオプトインです。使うには --config {path} を指定するか CODEROUTER_CONFIG={path} を設定するか CODEROUTER_ALLOW_CWD_CONFIG=1 を設定してください（信頼できるディレクトリのみ）。",
    },
    "W1002_CWD_LOADED": {
        "en": "loaded providers.yaml from the current working directory ({path}) because CODEROUTER_ALLOW_CWD_CONFIG opt-in is enabled.",
        "ja": "CODEROUTER_ALLOW_CWD_CONFIG が有効のため、カレントディレクトリの providers.yaml ({path}) を読み込みました。",
    },
    "W1003_CWD_SKIPPED": {
        "en": "found providers.yaml in the current working directory ({path}) but did NOT load it: implicit CWD discovery is opt-in since v2.13.0 (a hostile providers.yaml can steer restart_command / launcher binaries). To use it, set CODEROUTER_ALLOW_CWD_CONFIG=1 (only in directories you trust) or pass --config {path} to name it explicitly.",
        "ja": "カレントディレクトリに providers.yaml ({path}) が見つかりましたが読み込みませんでした。v2.13.0以降、CWDの自動検出はオプトインです（不正なproviders.yamlがrestart_command等を乗っ取る可能性があります）。使うには CODEROUTER_ALLOW_CWD_CONFIG=1 を設定するか --config {path} で明示してください（信頼できるディレクトリのみ）。",
    },
    "E1004_CONFIG_VALIDATION": {
        "en": "failed to load config: {error}",
        "ja": "設定の読み込みに失敗しました: {error}",
    },
    # ---- CLI ----
    "W1101_EXTERNAL_BIND": {
        "en": "serve: binding on {host!r} but CODEROUTER_ALLOWED_HOSTS is not set. Requests whose Host header is not loopback will be rejected with 403 (v2.7.0 DNS-rebinding guard). To allow LAN access, set CODEROUTER_ALLOWED_HOSTS=<THIS machine's address as it appears in the client's URL bar, e.g. 192.168.x.x — NOT the client's own IP> (comma-separated, no port). Note the chat endpoints have no authentication — do not expose CodeRouter directly to the internet.",
        "ja": "serve: {host!r} で待受しますが CODEROUTER_ALLOWED_HOSTS が未設定です。ループバック以外の Host ヘッダーは 403 で拒否されます（v2.7.0 DNSリバインディング対策）。LAN公開する場合は CODEROUTER_ALLOWED_HOSTS=<クライアントのURLバーに表示されるこのマシンのアドレス（例 192.168.x.x — クライアント自身のIPではありません）> を設定してください（カンマ区切り、ポート不要）。チャットエンドポイントに認証はありません — インターネットに直接公開しないでください。",
    },
    "E1102_ENV_FILE_NOT_FOUND": {
        "en": "serve: --env-file: {error}",
        "ja": "serve: --env-file: {error}",
    },
    "E1103_ENV_FILE_ERROR": {
        "en": "serve: --env-file: {error}",
        "ja": "serve: --env-file: {error}",
    },
    "I1104_ENV_FILE_LOADED": {
        "en": "serve: --env-file {path}: loaded {count} variable(s): {keys}",
        "ja": "serve: --env-file {path}: {count} 件の変数を読み込みました: {keys}",
    },
    "I1105_ENV_FILE_EMPTY": {
        "en": "serve: --env-file {path}: 0 variables applied (all keys already in environment, --env-file-override disabled)",
        "ja": "serve: --env-file {path}: 適用された変数は0件です（すべて環境変数に既存のため、--env-file-override は無効）",
    },
    "E1106_UNKNOWN_COMMAND": {
        "en": "unknown command: {command}",
        "ja": "不明なコマンド: {command}",
    },
    "E1107_DOCTOR_USAGE": {
        "en": "doctor: provide --check-model PROVIDER, --check-env [PATH], and/or --check-secrets",
        "ja": "doctor: --check-model PROVIDER、--check-env [PATH]、--check-secrets のいずれかを指定してください",
    },
    "E1108_DOCTOR_CONFIG_NOT_FOUND": {
        "en": "doctor: {error}",
        "ja": "doctor: {error}",
    },
    "E1109_DOCTOR_APPLY_ERROR": {
        "en": "doctor --apply: {error}",
        "ja": "doctor --apply: {error}",
    },
    "E1110_VSCODE_TARGET_MISSING": {
        "en": "vscode-init: target directory does not exist: {path}",
        "ja": "vscode-init: 対象ディレクトリが存在しません: {path}",
    },
    "E1111_AUDIT_NO_LOG": {
        "en": "audit: no audit log found at {path}",
        "ja": "audit: 監査ログが見つかりません: {path}",
    },
    "E1112_AUDIT_HINT": {
        "en": "Ensure state_dir and audit_log are configured in providers.yaml.",
        "ja": "providers.yaml で state_dir と audit_log が設定されているか確認してください。",
    },
    "I1113_AUDIT_NO_ENTRIES": {
        "en": "audit: no matching entries found.",
        "ja": "audit: 条件に一致するエントリがありません。",
    },
    "E1114_REPLAY_NO_LOG": {
        "en": "replay: no request journal found at {path}",
        "ja": "replay: リクエスト履歴が見つかりません: {path}",
    },
    "E1115_REPLAY_HINT": {
        "en": "Ensure state_dir and request_log are configured in providers.yaml.",
        "ja": "providers.yaml で state_dir と request_log が設定されているか確認してください。",
    },
    "I1116_REPLAY_NO_ENTRIES": {
        "en": "replay: no matching entries found.",
        "ja": "replay: 条件に一致するエントリがありません。",
    },
    # ---- Ingress ----
    "E1201_HOST_NOT_ALLOWED": {
        "en": "Host {host!r} is not allowed.",
        "ja": "Host {host!r} は許可されていません。CODEROUTER_ALLOWED_HOSTS を確認してください。",
    },
    "E1202_BODY_TOO_LARGE": {
        "en": "Request body too large: {observed} bytes exceeds the {limit}-byte limit.",
        "ja": "リクエストボディが大きすぎます: {observed} バイト（上限 {limit} バイト）",
    },
    # ---- Env file parsing ----
    "E1301_ENV_MISSING_EQ": {
        "en": "{path}:{lineno}: missing `=` separator: {line!r}",
        "ja": "{path}:{lineno}: `=` が見つかりません: {line!r}",
    },
    "E1302_ENV_INVALID_KEY": {
        "en": "{path}:{lineno}: invalid key {key!r}: {reason}",
        "ja": "{path}:{lineno}: 無効なキー {key!r}: {reason}",
    },
    "E1303_ENV_UNTERMINATED_DQ": {
        "en": "unterminated double-quoted value",
        "ja": "二重引用符で囲まれた値が閉じられていません",
    },
    "E1304_ENV_UNTERMINATED_SQ": {
        "en": "unterminated single-quoted value",
        "ja": "一重引用符で囲まれた値が閉じられていません",
    },
    "E1305_ENV_TRAILING_CONTENT": {
        "en": "unexpected content after closing quote: {tail!r}",
        "ja": "閉じ引用符の後に予期しない内容があります: {tail!r}",
    },
    # ---- Schemas / validation (representative) ----
    "E1401_CREDENTIAL_PATH_HOME": {
        "en": "credential.path must live under your home directory: {path}",
        "ja": "credential.path はホームディレクトリ配下である必要があります: {path}",
    },
    "E1402_CREDENTIAL_ENV_REQUIRED": {
        "en": "credential.source='env' requires credential.env",
        "ja": "credential.source='env' の場合 credential.env が必要です",
    },
    "E1403_CREDENTIAL_PATH_MEANINGLESS": {
        "en": "credential.path is meaningless for source='env'",
        "ja": "source='env' の場合 credential.path は不要です",
    },
    "E1404_CREDENTIAL_PATH_REQUIRED": {
        "en": "credential.source='cli_session' requires credential.path",
        "ja": "credential.source='cli_session' の場合 credential.path が必要です",
    },
    "E1405_CREDENTIAL_ENV_MEANINGLESS": {
        "en": "credential.env is meaningless for source='cli_session'",
        "ja": "source='cli_session' の場合 credential.env は不要です",
    },
    "E1406_API_KEY_EXCLUSIVE": {
        "en": "provider {name!r}: set either api_key_env or credential, not both (credential.source='env' with credential.env=... is the spelled-out form of api_key_env)",
        "ja": "プロバイダー {name!r}: api_key_env と credential はどちらか一方のみ指定してください（credential.source='env' + credential.env=... が api_key_env の明示形です）",
    },
    "E1407_BASE_URL_REQUIRED": {
        "en": "provider {name!r}: base_url is required for kind={kind!r}.",
        "ja": "プロバイダー {name!r}: kind={kind!r} では base_url が必須です。",
    },
    "E1408_AGENT_CLI_REQUIRED": {
        "en": "provider {name!r}: agent_cli sub-config is required for kind='agent_cli'.",
        "ja": "プロバイダー {name!r}: kind='agent_cli' では agent_cli 設定が必須です。",
    },
    "E1409_SANDBOX_CONFLICT": {
        "en": "agent_cli: allow_file_writes=True conflicts with sandbox_mode='read_only'. Set sandbox_mode to 'edit' or 'full_auto' to permit writes, or keep allow_file_writes=False.",
        "ja": "agent_cli: allow_file_writes=True と sandbox_mode='read_only' は矛盾しています。書き込みを許可する場合は sandbox_mode を 'edit' または 'full_auto' にするか、allow_file_writes を False にしてください。",
    },
    # E1410_CREDENTIAL_BOTH removed (unused; E1406 covers the case)
    # ---- Doctor probes (hints) ----
    "E1501_AUTH_FAIL": {
        "en": "upstream returned {status}. Check that env var {env!r} is set and holds a valid key.",
        "ja": "アップストリームが {status} を返しました。環境変数 {env!r} が正しいキーで設定されているか確認してください。",
    },
    "E1502_MODEL_NOT_FOUND": {
        "en": "upstream returned 404 for model {model!r}. For Ollama: run `ollama pull {model}`. For OpenRouter: verify the model slug at https://openrouter.ai/models",
        "ja": "アップストリームがモデル {model!r} で 404 を返しました。Ollama の場合 `ollama pull {model}` を実行してください。OpenRouter の場合は https://openrouter.ai/models でモデル名を確認してください。",
    },
    # NOTE: {budget_note} is appended directly to the preceding sentence.
    # I1507_* templates carry a leading space (" Probe...") by design so
    # W1503/W1504 read as "...prompts. Probe..." without double-space when
    # budget_note is empty. Callers must preserve that leading space (see
    # doctor.py lstrip handling).
    "W1503_NUM_CTX_CANARY_MISSING": {
        "en": "canary {canary!r} missing from reply — upstream truncated the prompt. No `extra_body.options.num_ctx` is declared, so Ollama is running at its 2048-token default, which cannot hold Claude Code's system + tool prompts.{budget_note}",
        "ja": "カナリアトークン {canary!r} が応答に含まれていません — プロンプトが切り捨てられました。`extra_body.options.num_ctx` が未設定のため Ollama はデフォルトの 2048 トークンで動作しており、Claude Code のシステムプロンプトを収容できません。{budget_note}",
    },
    "W1504_STREAM_LENGTH": {
        "en": "stream closed with `finish_reason='length'` after only {chars} chars (expected ≥ {expected}). Upstream is capping output — most likely `options.num_predict`.{budget_note}",
        "ja": "ストリームが {chars} 文字で `finish_reason='length'` により終了しました（期待値 ≥ {expected}）。アップストリームが出力を制限しています — おそらく `options.num_predict` が原因です。{budget_note}",
    },
    # ---- Env security (doctor --check-env) ----
    "I1505_ENV_SECURITY_SUMMARY_ERROR": {
        "en": "Summary: at least one check escalated to ERROR (real leak risk).",
        "ja": "概要: ERROR が1件以上あります（漏洩リスク）。直ちに対処してください。",
    },
    "I1505_ENV_SECURITY_SUMMARY_WARN": {
        "en": "Summary: WARN(s) present — apply the suggested fix(es).",
        "ja": "概要: WARN があります — 提示された fix を適用してください。",
    },
    "I1505_ENV_SECURITY_SUMMARY_OK": {
        "en": "Summary: all checks pass.",
        "ja": "概要: すべてのチェックに合格しました。",
    },
    "I1505_ENV_SECURITY_EXIT": {
        "en": "Exit: {code}",
        "ja": "終了コード: {code}",
    },
    # ---- Secret hygiene (doctor --check-secrets) ----
    "L_FIX_LABEL": {
        "en": "fix",
        "ja": "対処",
    },
    "I1506_SECRET_VERDICT": {
        "en": "verdict: {verdict} (exit {code})",
        "ja": "判定: {verdict} (終了コード {code})",
    },
    "I1506_SECRET_VERDICT_CLEAN": {
        "en": "clean",
        "ja": "クリーン",
    },
    "I1506_SECRET_VERDICT_ATTENTION": {
        "en": "needs attention",
        "ja": "要対応",
    },
    "I1506_SECRET_VERDICT_BLOCKER": {
        "en": "blocker",
        "ja": "ブロッカー",
    },
    # ---- Budget notes (doctor num_ctx / streaming probes) ----
    # NOTE: Leading space is intentional — these are appended to W1503/W1504
    # via {budget_note}. Keep the space so "...prompts. Probe..." is correct
    # and so striplines don't merge words when note is present. W1503/W1504
    # use " {budget_note.lstrip()}" pattern in doctor.py for normalized spacing.
    "I1507_BUDGET_NOTE_NUM_CTX_THINKING": {
        "en": " Probe sent max_tokens={max_tokens} (thinking-aware), so the miss is prompt-side truncation rather than reply truncation.",
        "ja": " プローブは max_tokens={max_tokens}（thinking対応）で送信したため、欠損は応答側ではなくプロンプト側の切り捨てです。",
    },
    "I1507_BUDGET_NOTE_STREAM_THINKING": {
        "en": "Probe sent max_tokens={max_tokens} (thinking-aware), so the cap is server-side `options.num_predict` rather than the probe budget.",
        "ja": "プローブは max_tokens={max_tokens}（thinking対応）で送信したため、上限はプローブ側ではなくサーバー側の `options.num_predict` です。",
    },
    "I1507_BUDGET_NOTE_STREAM_DEFAULT": {
        "en": "Probe sent max_tokens={max_tokens}; the cap is server-side `options.num_predict` rather than the probe budget.",
        "ja": "プローブは max_tokens={max_tokens} で送信したため、上限はプローブ側ではなくサーバー側の `options.num_predict` です。",
    },
}


def tr(
    msg_id: str,
    lang: str | None = None,
    **kwargs: object,
) -> str:
    """Translate *msg_id* to the effective language and format with *kwargs*.

    Falls back to English when the id or language is unknown.  Never raises
    due to a missing key — returns the id itself as a last resort so callers
    never crash because of a catalog typo.
    """
    effective = resolve_lang(lang)
    entry = _CATALOG.get(msg_id)
    if entry is None:
        return msg_id
    template = entry.get(effective) or entry.get("en") or msg_id
    try:
        return template.format(**kwargs)
    except KeyError as e:
        logger.warning(
            "messages: missing kwarg for %s: %s (effective=%s, kwargs=%s)",
            msg_id,
            e,
            effective,
            sorted(kwargs.keys()),
        )
        return template
    except Exception:
        # Other format errors (e.g. bad format spec) — return template as-is
        return template


def has_id(msg_id: str) -> bool:
    return msg_id in _CATALOG
