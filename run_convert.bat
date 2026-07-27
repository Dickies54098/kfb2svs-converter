@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

rem ===========================================================================
rem  KFB to QuPath-compatible SVS Converter - One-click Batch Entry
rem ---------------------------------------------------------------------------
rem  USAGE:
rem    1. Edit the two variables below (INPUT_DIR and OUTPUT_DIR).
rem    2. Double-click this file to start the conversion.
rem
rem  You ONLY need to change the two absolute paths in the "USER CONFIG" block.
rem  Everything else (stage folder, converter path, python runtime) is resolved
rem  automatically relative to this batch file.
rem ===========================================================================

rem ============================ USER CONFIG ===================================
rem  Put your own absolute paths between the quotes. Use backslashes.
rem  Examples:
rem    set "INPUT_DIR=C:\path\to\your\kfb_input"
rem    set "OUTPUT_DIR=C:\path\to\your\svs_output"

set "INPUT_DIR=C:\path\to\your\kfb_input"
set "OUTPUT_DIR=C:\path\to\your\svs_output"

rem  Optional: number of files to convert in parallel.
rem  External HDD / USB drive -> 2    (default)
rem  Fast NVMe SSD            -> 3 or 4
set "WORKERS=2"

rem ========================== END USER CONFIG ================================
rem  Do not edit below unless you know what you are doing.
rem ===========================================================================

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

rem Resolve the bundled Python runtime and KFbioConverter automatically.
set "PYTHON_EXE=%ROOT%\runtime\python.exe"
set "CONVERTER_EXE=%ROOT%\converter\x86\KFbioConverter.exe"
set "SCRIPT=%ROOT%\kfb_to_qupath_svs.py"

rem Auto-derive a temporary ASCII-only staging folder on the same drive as the
rem output folder. KFbioConverter can fail when the output path contains long
rem non-ASCII components, so we stage on the output drive's root and then move
rem the cleaned SVS into the final output folder.
set "OUTPUT_DRIVE=%OUTPUT_DIR:~0,2%"
set "STAGE_DIR=%OUTPUT_DRIVE%\kfb2svs_stage"

rem --- sanity checks ---------------------------------------------------------
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python runtime not found: %PYTHON_EXE%
    echo Please make sure the 'runtime' folder sits next to this batch file.
    goto :error
)
if not exist "%CONVERTER_EXE%" (
    echo [ERROR] KFbioConverter.exe not found: %CONVERTER_EXE%
    echo Please make sure the 'converter\x86' folder sits next to this batch file.
    goto :error
)
if not exist "%SCRIPT%" (
    echo [ERROR] Python script not found: %SCRIPT%
    goto :error
)
if not exist "%INPUT_DIR%" (
    echo [ERROR] INPUT_DIR does not exist: %INPUT_DIR%
    echo Please edit the USER CONFIG block at the top of this batch file.
    goto :error
)

echo KFB to QuPath-compatible SVS conversion
echo ----------------------------------------
echo Input  : %INPUT_DIR%
echo Output : %OUTPUT_DIR%
echo Stage  : %STAGE_DIR%
echo Workers: %WORKERS%
echo ----------------------------------------
echo.

"%PYTHON_EXE%" "%SCRIPT%" convert ^
    --input-dir "%INPUT_DIR%" ^
    --output-dir "%OUTPUT_DIR%" ^
    --converter "%CONVERTER_EXE%" ^
    --stage-dir "%STAGE_DIR%" ^
    --workers %WORKERS%

set "EXITCODE=%ERRORLEVEL%"
echo.
if "%EXITCODE%"=="0" (
    echo Conversion process finished successfully.
) else (
    echo Conversion process finished with errors ^(exit code %EXITCODE%^).
    echo Check the messages above for details.
)
pause
exit /b %EXITCODE%

:error
echo.
pause
exit /b 1
