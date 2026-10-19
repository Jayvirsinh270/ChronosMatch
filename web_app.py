"""
ChronosMatch: Interactive Web Trading Terminal Launcher.
Opens a sleek, dark-mode real-time graphical trading interface in your default browser.

Usage:
    python web_app.py
    python web_app.py --port 8080
"""

import argparse
import sys
import time
import webbrowser
import uvicorn


def parse_args():
    parser = argparse.ArgumentParser(description="ChronosMatch Web Trading Terminal")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open web browser")
    return parser.parse_args()


def print_banner(url: str):
    print("\n" + "=" * 65)
    print("  ⚡ CHRONOS MATCH — INTERACTIVE WEB TRADING GUI")
    print("  Zero-Copy Pure-C LOB • Live Level 2 Depth • Interactive Orders")
    print("=" * 65)
    print(f"  [+] Web Terminal running at:  {url}")
    print("  [+] Press Ctrl+C in terminal to stop server.\n")


def main():
    args = parse_args()
    url = f"http://{args.host}:{args.port}"
    print_banner(url)

    # Register instant shutdown handler for Windows Ctrl+C / Ctrl+Break
    def _shutdown(sig=None, frame=None):
        print("\n[+] Web server stopped cleanly.")
        import os
        os._exit(0)

    import signal
    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _shutdown)

    if not args.no_browser:
        # Open default browser after a brief delay so server has started listening
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
        )
        server = uvicorn.Server(config)
        server.run()
    except (KeyboardInterrupt, SystemExit):
        _shutdown()


if __name__ == "__main__":
    main()
