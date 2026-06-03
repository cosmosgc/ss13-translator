@echo off
setlocal enabledelayedexpansion

REM Usage:
REM   revert_corrupted_dm_files.bat [repo_path] [dryrun|run]
REM Examples:
REM   revert_corrupted_dm_files.bat
REM   revert_corrupted_dm_files.bat "D:\servers emulators\ss13\Bubberstation\code" dryrun
REM   revert_corrupted_dm_files.bat "D:\servers emulators\ss13\Bubberstation\code" run

if "%~1"=="" (
    set "repo_path=%cd%"
) else (
    set "repo_path=%~1"
)

if "%~2"=="" (
    set "mode=prompt"
) else (
    set "mode=%~2"
)

pushd "%repo_path%" >nul 2>&1 || (
    echo Repository path not found: "%repo_path%"
    exit /b 1
)

for /f "usebackq delims=" %%R in (`git rev-parse --show-toplevel 2^>nul`) do set "git_root=%%R"
if not defined git_root (
    echo Not a git repository: "%repo_path%"
    popd
    exit /b 1
)
echo Git root: %git_root%

set "scratch=%TEMP%\revert_corrupted_dm_files.txt"
powershell -NoProfile -Command "Get-ChildItem -Path '%git_root%' -Recurse -Filter '*.dm' -File | ForEach-Object { $text = Get-Content -LiteralPath $_.FullName -Raw; if ($text -match '(?m)^///.*\r?\n\r?\n/.*') { $_.FullName } } | Sort-Object -Unique | Set-Content -LiteralPath '%scratch%' -Encoding UTF8"

if not exist "%scratch%" (
    echo Search failed.
    popd
    exit /b 1
)

set "count=0"
for /f "usebackq delims=" %%F in ("%scratch%") do (
    set /a count+=1
    echo [%%F]
)

if "%count%"=="0" (
    echo No corrupted .dm files found.
    del "%scratch%" 2>nul
    popd
    exit /b 0
)

echo Found %count% candidate file(s).
if /i "%mode%"=="dryrun" (
    echo Dry run complete. Run again with run to perform git checkout.
    goto cleanup
)

if /i "%mode%"=="run" (
    echo Reverting files...
    for /f "usebackq delims=" %%F in ("%scratch%") do (
        echo git checkout -- "%%F"
        git checkout -- "%%F"
    )
    echo Revert complete.
    goto cleanup
)

set /p choice=Revert these files? [y/N] 
if /i "%choice%"=="y" (
    for /f "usebackq delims=" %%F in ("%scratch%") do (
        echo git checkout -- "%%F"
        git checkout -- "%%F"
    )
) else (
    echo No files were reverted.
)

:cleanup
del "%scratch%" 2>nul
popd
exit /b 0
