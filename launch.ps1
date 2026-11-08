<#
.SYNOPSIS
    ChronosMatch Master Interactive PowerShell Launcher
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

# Ensure we are in project root
Set-Location -Path $PSScriptRoot

# Activate virtual environment if available
if (Test-Path ".venv\Scripts\Activate.ps1") {
    & ".venv\Scripts\Activate.ps1"
} elseif (Test-Path "venv\Scripts\Activate.ps1") {
    & "venv\Scripts\Activate.ps1"
}

function Show-Menu {
    Clear-Host
    Write-Host "===============================================================================" -ForegroundColor Cyan
    Write-Host "    CHRONOS MATCH  //  HIGH-FREQUENCY TRADING MASTER CONTROL CONSOLE" -ForegroundColor White
    Write-Host "    Zero-Copy SPSC IPC  *  Pure-C nogil LOB  *  Asynchronous SQLite WAL" -ForegroundColor Green
    Write-Host "===============================================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  [1] Interactive Terminal TUI Cockpit (Live Hotkeys [B], [S], [C], [Space])" -ForegroundColor Yellow
    Write-Host "  [2] 1-Click System Verification & Audit Certificate (verify_system.py)" -ForegroundColor Green
    Write-Host "  [3] Executive Showcase & 4 Market Stress Scenarios (demo.py)" -ForegroundColor Magenta
    Write-Host "  [4] Modern Web Browser Trading Cockpit (web_app.py)" -ForegroundColor Cyan
    Write-Host "  [5] Multi-Process 50k Order Soak Benchmark (main.py)" -ForegroundColor White
    Write-Host "  [6] Open Cython Zero-GIL Compiler Annotation Reports in Browser" -ForegroundColor Blue
    Write-Host "  [7] Run Full Automated Test Suite (pytest)" -ForegroundColor DarkCyan
    Write-Host "  [8] Quantitative Strategy Backtester & Bloomberg Tear Sheet (backtest.py)" -ForegroundColor Magenta
    Write-Host "  [9] Exit" -ForegroundColor Red
    Write-Host ""
    Write-Host "===============================================================================" -ForegroundColor Cyan
}

while ($true) {
    Show-Menu
    $choice = Read-Host "Select an option [1-9]"

    switch ($choice) {
        "1" {
            Clear-Host
            Write-Host "[*] Launching Interactive Terminal TUI Cockpit..." -ForegroundColor Cyan
            Write-Host "[*] Use Hotkeys: [B]=Whale Buy, [S]=Whale Sell, [C]=Cancel Storm, [Space]=Pause" -ForegroundColor Yellow
            python main.py --demo
            Write-Host ""
            Pause
        }
        "2" {
            Clear-Host
            Write-Host "[*] Executing Master System Verification & Benchmark Audit..." -ForegroundColor Cyan
            python verify_system.py --fast
            Write-Host ""
            Pause
        }
        "3" {
            Clear-Host
            Write-Host "[*] Running Executive Showcase & Market Stress Scenarios..." -ForegroundColor Cyan
            python demo.py
            Write-Host ""
            Pause
        }
        "4" {
            Clear-Host
            Write-Host "[*] Launching Interactive Web Trading Terminal on http://127.0.0.1:8000 ..." -ForegroundColor Cyan
            python web_app.py
            Write-Host ""
            Pause
        }
        "5" {
            Clear-Host
            Write-Host "[*] Running 50,000 Order Multi-Process Soak Benchmark..." -ForegroundColor Cyan
            python main.py --orders 50000 --rate 50000
            Write-Host ""
            Pause
        }
        "6" {
            Clear-Host
            Write-Host "[*] Opening Cython Zero-Yellow Annotation Reports in your default browser..." -ForegroundColor Cyan
            if (Test-Path "chronos_match\engine\order_book_cy.html") {
                Start-Process "chronos_match\engine\order_book_cy.html"
            } else {
                cython -3 -a chronos_match\engine\order_book_cy.pyx
                Start-Process "chronos_match\engine\order_book_cy.html"
            }
            Write-Host "[+] Opened Cython C-code report." -ForegroundColor Green
            Pause
        }
        "7" {
            Clear-Host
            Write-Host "[*] Running Automated Test Suite across all core modules..." -ForegroundColor Cyan
            pytest -v tests/test_dashboard.py tests/test_metrics.py tests/test_whale_detector.py tests/test_ring_buffer.py tests/test_cancellation.py
            Write-Host ""
            Pause
        }
        "8" {
            Clear-Host
            Write-Host "[*] Executing Quantitative Backtest Engine & Bloomberg PORT Tear Sheet..." -ForegroundColor Magenta
            python backtest.py --symbol NVDA --strategy dual_ema --monte-carlo
            Write-Host ""
            Pause
        }
        "9" {
            Write-Host "`n[+] ChronosMatch launcher closed." -ForegroundColor Green
            exit 0
        }
        default {
            Write-Host "[!] Invalid selection, please choose 1-9." -ForegroundColor Red
            Start-Sleep -Seconds 1
        }
    }
}
