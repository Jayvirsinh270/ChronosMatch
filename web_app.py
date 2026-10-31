"""
ChronosMatch: Interactive Web Trading Terminal Launcher.
Opens a sleek, dark-mode real-time graphical trading interface in your default browser.

Usage:
    python web_app.py
    python web_app.py --port 8080
"""

import argparse
import asyncio
import os
import signal
import sys
import time
import webbrowser
import uvicorn

# 1. On Windows, suppress spurious ConnectionResetError [WinError 10054] when browser refreshes or closes
if sys.platform == "win32":
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        _orig_call_conn_lost = _ProactorBasePipeTransport._call_connection_lost

        def _silent_call_conn_lost(self, exc=None):
            try:
                _orig_call_conn_lost(self, exc)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                pass

        _ProactorBasePipeTransport._call_connection_lost = _silent_call_conn_lost
    except Exception:
        pass

# 2. Native Windows Console Control Handler to guarantee instant exit on Ctrl+C / Ctrl+Break
if sys.platform == "win32":
    try:
        import ctypes

        def _win_ctrl_handler(ctrl_type):
            # CTRL_C_EVENT = 0, CTRL_BREAK_EVENT = 1, CTRL_CLOSE_EVENT = 2
            if ctrl_type in (0, 1, 2):
                print("\n[+] Web server stopped cleanly.")
                os._exit(0)
            return False

        _handler = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)(_win_ctrl_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler, True)
    except Exception:
        pass


def parse_args():
    parser = argparse.ArgumentParser(description="ChronosMatch Web Trading Terminal")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--symbol", type=str, default="BTC-USD", help="Initial active stock ticker (default: BTC-USD)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open web browser")
    return parser.parse_args()


def print_banner(url: str, symbol: str):
    print("\n" + "=" * 65)
    print("  ⚡ CHRONOS MATCH — REAL-MARKET AUTONOMOUS TRADING TERMINAL")
    print(f"  Lightweight ML Pattern Engine • Real Market Data • Active: {symbol}")
    print("=" * 65)
    print(f"  [+] Web Terminal running at:  {url}")
    print("  [+] Press Ctrl+C in terminal to stop server.\n")


def main():
    args = parse_args()
    url = f"http://{args.host}:{args.port}"
    print_banner(url, args.symbol)

    from chronos_match.web.server import server as trading_server
    trading_server.active_symbol = args.symbol

    def _shutdown(sig=None, frame=None):
        print("\n[+] Web server stopped cleanly.")
        os._exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _shutdown)

    if not args.no_browser:
        def _open():
            time.sleep(0.8)
            webbrowser.open(url)

        import threading
        threading.Thread(target=_open, daemon=True).start()

    try:
        config = uvicorn.Config(
            "chronos_match.web.server:app",
            host=args.host,
            port=args.port,
            log_level="warning",
            loop="asyncio",
            timeout_graceful_shutdown=1,
        )
        server = uvicorn.Server(config)
        server.run()
    except (KeyboardInterrupt, SystemExit):
        _shutdown()
    finally:
        _shutdown()


if __name__ == "__main__":
    main()
