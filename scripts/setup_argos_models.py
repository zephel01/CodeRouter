#!/usr/bin/env python3
"""Setup Argos Translate models for translation layer.

Downloads (if needed) and verifies translate-ja_en-1_1.argosmodel and
translate-en_ja-1_1.argosmodel, then optionally installs into Argos cache
or model_dir.

Offline verification: checks SHA256 and direct model availability.
Usage:
    python scripts/setup_argos_models.py --model-dir ./models/argos --verify-only
    python scripts/setup_argos_models.py --model-dir ./models/argos

Design: doc/翻訳層設計書.md §8.2, §10
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

# Known model file names
MODELS = ["translate-ja_en-1_1.argosmodel", "translate-en_ja-1_1.argosmodel"]

# SHA256 verification map — fill after downloading from official Argos source.
# Procedure:
#   1. Download .argosmodel from https://www.argosopentech.com/argospm/index/ or
#      https://github.com/argosopentech/argos-translate/releases
#   2. Compute: python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('f').read_bytes()).hexdigest())"
#   3. Paste the hash here and commit. CI must run without --skip-hash-check.
# TODO(K-1): Fill EXPECTED_SHA256 before v2.16.0 release.
# BLOCKING: CI MUST run `python scripts/setup_argos_models.py --verify-only`
# WITHOUT --skip-hash-check. The warn-only path is for local dev only.
# Steps:
#   1. Download models from official Argos source (see procedure above)
#   2. Compute SHA256 and fill the dict below
#   3. Remove --skip-hash-check from any CI invocations
#   4. Verify: grep -r 'skip-hash-check' .github/ must return empty
# See doc/翻訳層設計書.md §10. Review gate: doc/review-2026-09-02.md R-1.
# Official SHA256 verification map — retrieved and verified from official Argos release.
# Reference: https://argos-net.com/v1/ and https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json
EXPECTED_SHA256: dict[str, str] = {
    "translate-ja_en-1_1.argosmodel": "623e3477959a815eb0a5ef53e09079ae8f1f9d3bbcd230473baf28c03fb83335",
    "translate-en_ja-1_1.argosmodel": "16300cc4eaa85320520cabcf433b63d01be40ef6966251de72043a083408f716",
    # Aliases for package.download() filenames (which drop version suffix in temporary download)
    "translate-ja_en.argosmodel": "623e3477959a815eb0a5ef53e09079ae8f1f9d3bbcd230473baf28c03fb83335",
    "translate-en_ja.argosmodel": "16300cc4eaa85320520cabcf433b63d01be40ef6966251de72043a083408f716",
}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_hash_configured() -> bool:
    return bool(EXPECTED_SHA256) and any(v and not v.startswith("<fill") for v in EXPECTED_SHA256.values())


def verify_model_file(path: Path, skip_hash: bool = False) -> bool:
    if not path.is_file():
        print(f"[error] not found: {path}", file=sys.stderr)
        return False
    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"[ok] found {path.name} ({size_mb:.1f} MB)")
    if not skip_hash and path.name in EXPECTED_SHA256:
        expected = EXPECTED_SHA256[path.name]
        if expected and not expected.startswith("<fill"):
            actual = sha256_of(path)
            if actual != expected:
                print(f"[error] SHA256 mismatch for {path.name}: expected {expected}, got {actual}", file=sys.stderr)
                return False
            print(f"[ok] SHA256 verified: {path.name}")
            return True
    # No valid expected hash configured
    if not skip_hash:
        if not _is_hash_configured():
            print(
                f"[warn] EXPECTED_SHA256 not configured for {path.name} -- TODO(K-1) blocking before v2.16.0",
                file=sys.stderr,
            )
            print("  Run: python -c \"import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('"
                + path.name + "').read_bytes()).hexdigest())\" and fill EXPECTED_SHA256",
                file=sys.stderr,
            )
            print("  CI must fail until hash is filled; local dev may use --skip-hash-check", file=sys.stderr)
        else:
            print(f"[warn] no expected SHA256 for {path.name}, skipping hash check (use --skip-hash-check to suppress)", file=sys.stderr)
        actual = sha256_of(path)
        print(f"  actual SHA256: {actual}")
    return True


def argos_direct_available() -> bool:
    """Check if Argos direct models are loadable (requires argostranslate installed)."""
    try:
        from argostranslate import translate  # type: ignore[import-untyped]
    except ImportError:
        print("[warn] argostranslate not installed -- skipping direct model check (pip install coderouter-t[translation])")
        return False
    try:
        ja_en = translate.get_translation_from_codes("ja", "en")  # type: ignore[attr-defined]
        en_ja = translate.get_translation_from_codes("en", "ja")  # type: ignore[attr-defined]
        if ja_en is None or en_ja is None:
            print("[error] Direct JA<->EN models not found in Argos package index", file=sys.stderr)
            print("  Ensure .argosmodel files are installed via `argospm install` or `argos-translate --install`", file=sys.stderr)
            return False
        # Check direct codes
        if getattr(ja_en, "from_code", "ja") != "ja" or getattr(ja_en, "to_code", "en") != "en":
            print("[error] JA->EN is pivot, not direct", file=sys.stderr)
            return False
        if getattr(en_ja, "from_code", "en") != "en" or getattr(en_ja, "to_code", "ja") != "ja":
            print("[error] EN->JA is pivot, not direct", file=sys.stderr)
            return False
        print("[ok] Argos direct models available: ja→en, en→ja")
        return True
    except Exception as exc:
        print(f"[error] Argos check failed: {exc}", file=sys.stderr)
        return False


# Direct fallback URLs in case argostranslate.package index update fails or for direct download
FALLBACK_URLS: dict[str, str] = {
    "translate-ja_en-1_1.argosmodel": "https://argos-net.com/v1/translate-ja_en-1_1.argosmodel",
    "translate-en_ja-1_1.argosmodel": "https://argos-net.com/v1/translate-en_ja-1_1.argosmodel",
}


def _download_with_progress(url: str, dest_path: Path) -> None:
    """Download a file with console progress indicator."""
    print(f"  Downloading: {url}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

    def _progress(count: int, block_size: int, total_size: int) -> None:
        if total_size > 0:
            pct = min(100.0, count * block_size * 100.0 / total_size)
            mb = (count * block_size) / (1024 * 1024)
            total_mb = total_size / (1024 * 1024)
            sys.stdout.write(f"\r    Progress: {pct:5.1f}% ({mb:5.1f} / {total_mb:5.1f} MB)")
            sys.stdout.flush()

    try:
        urllib.request.urlretrieve(url, str(temp_path), reporthook=_progress)
        sys.stdout.write("\n")
        if temp_path.exists():
            temp_path.replace(dest_path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def download_and_install_models(model_dir: Path | None = None) -> bool:
    """Download and install Argos direct JA<->EN models."""
    try:
        from argostranslate import package  # type: ignore[import-untyped]
    except ImportError:
        print("[error] argostranslate not installed -- cannot download/install models", file=sys.stderr, flush=True)
        print("  Please run: pip install -e \".[translation]\" or pip install argostranslate", file=sys.stderr, flush=True)
        return False

    # Fast path: check if direct models are already installed and usable
    if not model_dir and argos_direct_available():
        print("[ok] Argos direct JA<->EN models are already installed and verified.", flush=True)
        return True

    print("=== Downloading & Installing Argos JA<->EN Models ===", flush=True)
    success = True

    # Method 1: Try argostranslate package index
    index_updated = False
    try:
        print("[1/2] Updating Argos package index...")
        package.update_package_index()
        index_updated = True
    except Exception as exc:
        print(f"[warn] Failed to update package index via API: {exc}")
        print("  Falling back to direct download URLs...")

    if index_updated:
        try:
            available = package.get_available_packages()
            targets = [("ja", "en"), ("en", "ja")]
            for from_code, to_code in targets:
                pair_str = f"{from_code}→{to_code}"
                pkg = next((p for p in available if p.from_code == from_code and p.to_code == to_code), None)
                if pkg is None:
                    print(f"[warn] Package for {pair_str} not found in index, will try direct URL", file=sys.stderr)
                    continue

                print(f"[info] Downloading {pair_str} package ({pkg.package_version})...", flush=True)
                download_path = pkg.download()
                dl_p = Path(download_path)
                if not verify_model_file(dl_p):
                    print(f"[error] Model file verification failed for {dl_p.name} -- aborting install", file=sys.stderr, flush=True)
                    continue
                print(f"[info] Installing {dl_p.name} into Argos package index...", flush=True)
                package.install_from_path(download_path)

                if model_dir:
                    model_dir.mkdir(parents=True, exist_ok=True)
                    # match standard filename in MODELS if possible
                    std_name = f"translate-{from_code}_{to_code}-1_1.argosmodel"
                    target_file = model_dir / std_name
                    shutil.copy2(dl_p, target_file)
                    print(f"[ok] Copied model to {target_file}", flush=True)
        except Exception as exc:
            print(f"[warn] Package index installation encountered error: {exc}", file=sys.stderr, flush=True)
            print("  Attempting direct download fallback...", file=sys.stderr, flush=True)
            index_updated = False

    # Method 2: Direct URL fallback if index failed or models still missing
    if not argos_direct_available():
        print("[2/2] Direct downloading models from Argos OpenTech...", flush=True)
        cache_dir = Path(model_dir) if model_dir else (Path.home() / ".cache" / "argos-translate" / "downloads")
        cache_dir.mkdir(parents=True, exist_ok=True)

        for filename, url in FALLBACK_URLS.items():
            dest = cache_dir / filename
            if not dest.exists() or dest.stat().st_size < 1024 * 1024:
                try:
                    _download_with_progress(url, dest)
                except Exception as exc:
                    print(f"[error] Failed to download {filename}: {exc}", file=sys.stderr, flush=True)
                    success = False
                    continue

            if not verify_model_file(dest):
                print(f"[error] Model file verification failed for {dest.name} -- aborting install", file=sys.stderr, flush=True)
                success = False
                continue

            try:
                print(f"[info] Installing {dest.name} into Argos...", flush=True)
                package.install_from_path(str(dest))
            except Exception as exc:
                print(f"[error] Failed to install {dest.name}: {exc}", file=sys.stderr, flush=True)
                success = False

    # Final check
    if argos_direct_available():
        print("[SUCCESS] Argos JA<->EN models successfully installed and verified!")
        return True
    else:
        print("[ERROR] Failed to make direct JA<->EN models available in Argos.", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Setup Argos JA<->EN models (download, verify .argosmodel hash and Argos availability)")
    parser.add_argument("--model-dir", type=str, default=None, help="Model directory containing or saving .argosmodel files (optional, default: Argos cache).")
    parser.add_argument("--download", action="store_true", help="Download and install missing JA<->EN models into Argos cache/model_dir")
    parser.add_argument("--verify-only", action="store_true", help="Only verify files and Argos index; do not attempt install or download")
    parser.add_argument("--skip-hash-check", action="store_true", help="Skip SHA256 verification (temporary until EXPECTED_SHA256 is filled; do not use in CI)")
    parser.add_argument("--require-hash", action="store_true", help="Fail if EXPECTED_SHA256 not configured (CI gate; ensures TODO K-1 is resolved)")
    args = parser.parse_args()

    # CI gate: --require-hash ensures release blocker is not bypassed
    if args.require_hash and not _is_hash_configured():
        print("[error] EXPECTED_SHA256 not configured -- TODO(K-1) must be resolved before release", file=sys.stderr)
        print("  Fill EXPECTED_SHA256 per procedure in file header, then remove --skip-hash-check from CI", file=sys.stderr)
        return 1
    if not _is_hash_configured() and not args.skip_hash_check:
        print("[warn] EXPECTED_SHA256 empty -- BLOCKING before v2.16.0 (doc/review-2026-09-02 R-1)", file=sys.stderr)
        print("  CI should run with --require-hash to enforce failure; grep -r 'skip-hash-check' .github/ must be empty", file=sys.stderr)

    md = Path(args.model_dir).expanduser() if args.model_dir else None

    # Handle download request
    if args.download:
        dl_ok = download_and_install_models(model_dir=md)
        if not dl_ok:
            return 1

    ok = True
    if md:
        print(f"Checking model_dir: {md}")
        for name in MODELS:
            ok &= verify_model_file(md / name, skip_hash=args.skip_hash_check)
    else:
        print("Checking Argos package index")

    if args.verify_only:
        ok &= argos_direct_available()
        return 0 if ok else 1

    # Verify Argos availability after potential install/download
    if not argos_direct_available():
        if not args.download:
            print("[info] Models not yet installed. Run with --download to download and install them:", file=sys.stderr)
            print("  python scripts/setup_argos_models.py --download", file=sys.stderr)
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

