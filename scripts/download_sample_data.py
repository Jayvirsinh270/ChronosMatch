"""
ChronosMatch: Sample High-Frequency Intraday Data Downloader.
Downloads free public 1-minute historical kline datasets from Binance Vision
directly into data/intraday/ without requiring any API key or external dependencies.
"""

import io
import os
import sys
import urllib.request
import zipfile

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "intraday")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
SAMPLE_DATES = ["2024-01-01", "2024-01-02", "2024-01-03"]


def download_sample_klines(symbol: str = "BTCUSDT", date_str: str = "2024-01-01") -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{symbol}-1m-{date_str}.zip"
    url = f"https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{filename}"
    csv_name = f"{symbol}-1m-{date_str}.csv"
    out_csv = os.path.join(OUTPUT_DIR, csv_name)

    if os.path.exists(out_csv):
        print(f"[OK] {csv_name} already exists in {OUTPUT_DIR}")
        return

    print(f"[*] Downloading {symbol} 1m klines for {date_str} from Binance Vision...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            zip_bytes = resp.read()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            z.extractall(OUTPUT_DIR)
        print(f"[SUCCESS] Extracted: {out_csv}")
    except Exception as e:
        print(f"[ERROR] Could not download {url}: {e}")


def main():
    print("=" * 60)
    print("ChronosMatch: 1-Minute Intraday Data Ingestion Downloader")
    print("=" * 60)
    for sym in SYMBOLS:
        for dt in SAMPLE_DATES:
            download_sample_klines(sym, dt)
    print("\nDownload process completed. Files stored in data/intraday/")


if __name__ == "__main__":
    main()
