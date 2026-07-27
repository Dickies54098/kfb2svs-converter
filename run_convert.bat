@echo off
chcp 65001 >nul
setlocal DisableDelayedExpansion

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

rem ===========================================================================
rem  KFB to QuPath-compatible SVS Converter - One-click Batch Entry
rem ---------------------------------------------------------------------------
rem  USAGE:
rem    1. Copy run_convert.example.ini to run_convert.local.ini and edit it.
rem    2. Double-click this file to start the conversion.
rem
rem  Everything else is resolved automatically relative to this batch file.
rem ===========================================================================

rem ============================ USER CONFIG ===================================
rem  Keeping machine-specific paths outside this batch file prevents editors
rem  from accidentally changing this script's required CRLF line endings.
set "CONFIG_FILE=%ROOT%\run_convert.local.ini"
set "WORKERS=2"

if not exist "%CONFIG_FILE%" (
    echo [ERROR] Configuration file not found: %CONFIG_FILE%
    echo Copy run_convert.example.ini to run_convert.local.ini and edit the paths.
    goto :error
)

for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%CONFIG_FILE%") do (
    if /i "%%A"=="INPUT_DIR" set "INPUT_DIR=%%B"
    if /i "%%A"=="OUTPUT_DIR" set "OUTPUT_DIR=%%B"
    if /i "%%A"=="WORKERS" set "WORKERS=%%B"
)

if not defined INPUT_DIR (
    echo [ERROR] INPUT_DIR is missing from %CONFIG_FILE%
    goto :error
)
if not defined OUTPUT_DIR (
    echo [ERROR] OUTPUT_DIR is missing from %CONFIG_FILE%
    goto :error
)
rem ========================== END USER CONFIG ================================
rem  Do not edit below unless you know what you are doing.
rem ===========================================================================

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
    echo Please edit %CONFIG_FILE%.
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

rem IMPORTANT: This call is written as a single physical line on purpose.
rem Windows cmd's '^' line-continuation only works when the file is saved
rem with CRLF line endings. If the bat is downloaded with LF line endings
rem (which happens for files committed from a Unix/Linux toolchain), '^' is
rem silently ignored and the next line is executed as a brand-new command,
rem producing confusing "'xxx' is not recognized" errors. Writing the
rem whole command on one line avoids the dependency on line-ending encoding.
"%PYTHON_EXE%" "%SCRIPT%" convert --input-dir "%INPUT_DIR%" --output-dir "%OUTPUT_DIR%" --converter "%CONVERTER_EXE%" --stage-dir "%STAGE_DIR%" --workers %WORKERS%

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
