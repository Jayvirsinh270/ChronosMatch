@echo off
setlocal enabledelayedexpansion
title ChronosMatch - Master High-Frequency Trading Launcher
color 0B

:: Ensure current working directory is the repository root
cd /d "%~dp0"

:: Activate virtual environment if available
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)

:MENU
cls
echo ===============================================================================
echo     CHRONOS MATCH  //  HIGH-FREQUENCY TRADING MASTER CONTROL CONSOLE
echo     Zero-Copy SPSC IPC  *  Pure-C nogil LOB  *  Asynchronous SQLite WAL
echo ===============================================================================
echo.
echo   [1] Interactive Terminal TUI Cockpit (Live Hotkeys [B], [S], [C], [Space])
echo   [2] 1-Click System Verification & Audit Certificate (verify_system.py)
echo   [3] Executive Showcase & 4 Market Stress Scenarios (demo.py)
echo   [4] Modern Web Browser Trading Cockpit (web_app.py)
echo   [5] Multi-Process 50k Order Soak Benchmark (main.py)
echo   [6] Open Cython Zero-GIL Compiler Annotation Reports in Browser
echo   [7] Run Full Automated Test Suite (pytest)
echo   [8] Quantitative Strategy Backtester & Bloomberg Tear Sheet (backtest.py)
echo   [9] Exit
echo.
echo ===============================================================================
set /p opt="Select an option [1-9]: "

if "%opt%"=="1" goto RUN_TUI
if "%opt%"=="2" goto RUN_VERIFY
if "%opt%"=="3" goto RUN_DEMO
if "%opt%"=="4" goto RUN_WEB
if "%opt%"=="5" goto RUN_SOAK
if "%opt%"=="6" goto RUN_REPORTS
if "%opt%"=="7" goto RUN_TESTS
if "%opt%"=="8" goto RUN_BACKTEST
if "%opt%"=="9" goto EXIT

echo [!] Invalid selection, please choose 1-9.
timeout /t 2 >nul
goto MENU

:RUN_TUI
cls
echo [*] Launching Interactive Terminal TUI Cockpit...
echo [*] Use Hotkeys: [B]=Whale Buy, [S]=Whale Sell, [C]=Cancel Storm, [Space]=Pause
echo.
python main.py --demo
echo.
pause
goto MENU

:RUN_VERIFY
cls
echo [*] Executing Master System Verification & Benchmark Audit...
echo.
python verify_system.py --fast
echo.
pause
goto MENU

:RUN_DEMO
cls
echo [*] Running Executive Showcase & Market Stress Scenarios...
echo.
python demo.py
echo.
pause
goto MENU

:RUN_WEB
cls
echo [*] Launching Interactive Web Trading Terminal on http://127.0.0.1:8000 ...
echo [*] Press Ctrl+C in this window to stop the server when finished.
echo.
python web_app.py
echo.
pause
goto MENU

:RUN_SOAK
cls
echo [*] Running 50,000 Order Multi-Process Soak Benchmark...
echo.
python main.py --orders 50000 --rate 50000
echo.
pause
goto MENU

:RUN_REPORTS
cls
echo [*] Opening Cython Zero-Yellow Annotation Reports in your default browser...
if exist "chronos_match\engine\order_book_cy.html" (
    start "" "chronos_match\engine\order_book_cy.html"
) else (
    echo [!] order_book_cy.html not found. Generating now...
    cython -3 -a chronos_match\engine\order_book_cy.pyx
    start "" "chronos_match\engine\order_book_cy.html"
)
echo [+] Opened Cython C-code report. Notice zero yellow Python calls in the hot loop.
echo.
pause
goto MENU

:RUN_TESTS
cls
echo [*] Running Automated Test Suite across all core modules...
echo.
pytest -v tests/test_dashboard.py tests/test_metrics.py tests/test_whale_detector.py tests/test_ring_buffer.py tests/test_cancellation.py
echo.
pause
goto MENU

:RUN_BACKTEST
cls
echo [*] Executing Quantitative Backtest Engine & Bloomberg PORT Tear Sheet...
echo.
python backtest.py --symbol NVDA --strategy dual_ema --monte-carlo
echo.
pause
goto MENU

:EXIT
echo.
echo [+] ChronosMatch launcher closed.
exit /b 0
