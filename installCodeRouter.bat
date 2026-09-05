@echo off
chcp 65001 > nul
setlocal

echo ============================================================
echo  Installing / Updating CodeRouter in Editable Mode (-e)
echo ============================================================
echo.

cd /d "%~dp0"

if not exist "pyproject.toml" (
    echo [ERROR] pyproject.toml not found in "%~dp0"!
    echo Please make sure this script is located in the CodeRouter root directory.
    pause
    exit /b 1
)

echo [1/2] Installing CodeRouter (with translation support)...
python -m pip install -e ".[translation]"

if %ERRORLEVEL% neq 0 (
    echo [WARN] Failed to install with [translation] extra. Trying base install...
    python -m pip install -e .
    if %ERRORLEVEL% neq 0 (
        echo.
        echo ============================================================
        echo  [ERROR] Installation failed. Error code: %ERRORLEVEL%
        echo ============================================================
        pause
        exit /b %ERRORLEVEL%
    )
)

echo.
echo ============================================================
echo  [SUCCESS] CodeRouter package installed successfully!
echo ============================================================
echo.

echo ------------------------------------------------------------
echo  [OPTIONAL] Translation Layer Setup (Argos Translate JA - EN)
echo ------------------------------------------------------------
echo  CodeRouter の日本語・英語 双方向翻訳層（Argos Translate）用の
echo  モデル（約230MB）をダウンロード＆セットアップしますか？
echo.

set DO_TRANSLATE=
if /i "%~1"=="-y" set DO_TRANSLATE=Y
if /i "%~1"=="/y" set DO_TRANSLATE=Y

if "%DO_TRANSLATE%"=="" (
    set /p DO_TRANSLATE="セットアップを実行しますか？ [Y/n]: "
)
if "%DO_TRANSLATE%"=="" set DO_TRANSLATE=Y

if /i "%DO_TRANSLATE%"=="Y" (
    echo.
    echo [2/2] Checking and setting up translation models...
    python -u scripts\setup_argos_models.py --download
    if %ERRORLEVEL% equ 0 (
        echo.
        echo [INFO] 翻訳モデルのセットアップが完了しました。
        echo.
        echo 【ヒント】翻訳層を有効化するには providers.yaml に以下を設定してください:
        echo ------------------------------------------------------------
        echo translation:
        echo   enabled: true
        echo   device: cpu
        echo   log_translations: true
        echo ------------------------------------------------------------
    ) else (
        echo.
        echo [WARN] 翻訳モデルのセットアップでエラーが発生しました。
        echo 手動で再試行する場合は以下を実行してください:
        echo   python -u scripts\setup_argos_models.py --download
    )
) else (
    echo.
    echo 翻訳モデルのダウンロードをスキップしました。
    echo 後からセットアップする場合は以下を実行してください:
    echo   python -u scripts\setup_argos_models.py --download
)

echo.
echo ============================================================
echo  Setup Completed!
echo ============================================================
echo.
pause
