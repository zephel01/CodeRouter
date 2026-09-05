"""TranslatorManager — Argos Translate wrapper, CPU-only, resident, thread-safe.

Design: doc/翻訳層設計書.md §3.2
- Singleton-like via app.state
- Lazy import of argostranslate
- CPU fixed, no auto download, no pivot
- threading.Lock for CT2 thread-safety
- Sync blocking API; caller must asyncio.to_thread
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from coderouter.logging import get_logger

logger = get_logger(__name__)


class TranslatorManager:
    """Argos Translate wrapper. CPU専用, 常駐ロード, スレッドセーフ."""

    def __init__(self, model_dir: str | None = None) -> None:
        self._model_dir = model_dir
        self._lock = threading.Lock()
        self._available = False
        self._ja_en = None  # type: ignore[no-untyped-def]
        self._en_ja = None  # type: ignore[no-untyped-def]
        self._translate_module = None  # type: ignore[no-untyped-def]

    def load(self) -> None:
        """Load JA→EN / EN→JA direct models. Raises on failure.

        Auto-install is NOT performed; operator must have installed the
        .argosmodel files beforehand (scripts/setup_argos_models.py).
        When model_dir is set, the directory is verified and, if it
        contains ``*.argosmodel`` files not yet installed in the Argos
        package index, they are installed via ``argostranslate.package``.
        If installation fails, the standard Argos cache is used as
        fallback (fail-open is handled by the caller).
        """
        # Ensure CPU
        os.environ["ARGOS_DEVICE_TYPE"] = "cpu"
        _t0 = time.monotonic()

        try:
            from argostranslate import translate as _translate  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "argostranslate is not installed. Install via `pip install coderouter-t[translation]`"
            ) from exc

        self._translate_module = _translate

        # Optional model_dir handling: Argos uses package metadata, not direct file load.
        # If model_dir is set, verify existence and attempt to install local
        # ``*.argosmodel`` files into the Argos package index if not already
        # available. This makes ``model_dir`` actually effective instead of
        # being a no-op existence check (J-1 fix).
        if self._model_dir:
            md = Path(self._model_dir).expanduser()
            if not md.exists():
                raise FileNotFoundError(f"translation model_dir not found: {md}")
            # Attempt to install local .argosmodel files if present.
            # Failures are non-fatal — we still try the standard cache.
            try:
                argosmodels = list(md.glob("*.argosmodel"))
                if argosmodels:
                    try:
                        from argostranslate import package as _pkg  # type: ignore[import-untyped]
                    except ImportError:
                        _pkg = None  # type: ignore[assignment]
                    if _pkg is not None:
                        for am in argosmodels:
                            try:
                                # package.install_from_path is the official API
                                if hasattr(_pkg, "install_from_path"):
                                    _pkg.install_from_path(str(am))  # type: ignore[attr-defined]
                                elif hasattr(_pkg, "install_from_path_if_needed"):
                                    _pkg.install_from_path_if_needed(str(am))  # type: ignore[attr-defined]
                                logger.info("translation-model-installed", extra={"model": am.name})
                            except Exception as exc:
                                logger.warning(
                                    "translation-model-install-failed",
                                    extra={"model": am.name, "error": str(exc)},
                                )
                        # Refresh package index after installs
                        try:
                            if hasattr(_pkg, "update_package_index"):
                                _pkg.update_package_index()  # type: ignore[attr-defined]
                        except Exception:
                            pass
            except Exception as exc:
                logger.warning("translation-model-dir-scan-failed", extra={"error": str(exc)})

        # Get direct translations
        # Argos: translate.get_translation_from_codes("ja", "en") etc.
        try:
            # Prefer device="cpu" when the installed Argos version supports it
            try:
                ja_en = _translate.get_translation_from_codes("ja", "en", device="cpu")  # type: ignore[attr-defined,call-arg]
                en_ja = _translate.get_translation_from_codes("en", "ja", device="cpu")  # type: ignore[attr-defined,call-arg]
            except TypeError:
                # Older Argos (<1.11) has no device kwarg
                ja_en = _translate.get_translation_from_codes("ja", "en")  # type: ignore[attr-defined]
                en_ja = _translate.get_translation_from_codes("en", "ja")  # type: ignore[attr-defined]

            # Validate direct models exist and are not pivot
            if ja_en is None or en_ja is None:
                raise RuntimeError(
                    "Direct JA↔EN Argos models not found. "
                    "Install translate-ja_en-1_1.argosmodel and translate-en_ja-1_1.argosmodel. "
                    "Pivot translation (JA→ZH→EN) is disabled by design."
                )

            # Additional check: ensure from_code/to_code matches direct (not via pivot chain)
            # Argos ITranslation has from_code/to_code attributes
            if getattr(ja_en, "from_code", "ja") != "ja" or getattr(ja_en, "to_code", "en") != "en":
                raise RuntimeError("JA→EN translation is not direct (pivot detected). Aborting.")
            if getattr(en_ja, "from_code", "en") != "en" or getattr(en_ja, "to_code", "ja") != "ja":
                raise RuntimeError("EN→JA translation is not direct (pivot detected). Aborting.")

            self._ja_en = ja_en
            self._en_ja = en_ja
            self._available = True
            elapsed_ms = (time.monotonic() - _t0) * 1000
            logger.info(
                "translation-manager-loaded",
                extra={"model_dir": self._model_dir or "argos-cache", "elapsed_ms": round(elapsed_ms, 1)},
            )
            # Startup-blocking guard: warn if model load exceeds health-check window
            if elapsed_ms > 5000:
                logger.warning(
                    "translation-manager-slow-startup",
                    extra={"elapsed_ms": round(elapsed_ms, 1), "hint": "model load blocked startup; consider warm cache or pre-install"},
                )
        except Exception:
            self._available = False
            raise

    def is_available(self) -> bool:
        return self._available and self._ja_en is not None and self._en_ja is not None

    def translate_ja_to_en(self, text: str) -> str:
        """日本語→英語翻訳。内部で Lock 取得、失敗時は原文を返す。同期ブロッキングAPI。"""
        if not self.is_available():
            return text
        if not text or not text.strip():
            return text
        with self._lock:
            try:
                # Argos translate API: .translate(text) or argostranslate.translate.translate
                if hasattr(self._ja_en, "translate"):
                    return self._ja_en.translate(text)  # type: ignore[no-any-return]
                # Fallback: module-level translate
                if self._translate_module and hasattr(self._translate_module, "translate"):
                    return self._translate_module.translate(text, "ja", "en")  # type: ignore[no-any-return]
                return text
            except Exception as exc:
                logger.warning("translation-ja-en-failed", extra={"error": str(exc)})
                return text

    def translate_en_to_ja(self, text: str) -> str:
        """英語→日本語翻訳。内部で Lock 取得、失敗時は原文を返す。同期ブロッキングAPI。"""
        if not self.is_available():
            return text
        if not text or not text.strip():
            return text
        with self._lock:
            try:
                if hasattr(self._en_ja, "translate"):
                    return self._en_ja.translate(text)  # type: ignore[no-any-return]
                if self._translate_module and hasattr(self._translate_module, "translate"):
                    return self._translate_module.translate(text, "en", "ja")  # type: ignore[no-any-return]
                return text
            except Exception as exc:
                logger.warning("translation-en-ja-failed", extra={"error": str(exc)})
                return text

    def close(self) -> None:
        """リソース解放(必要に応じて)。"""
        self._available = False
        self._ja_en = None
        self._en_ja = None
        self._translate_module = None
