"""Entry point.

  python main.py                      start the server on 127.0.0.1:8765
  python main.py --port 9000          other port
  python main.py worker scrape 12     (internal) run a scrape job in this process
"""
import argparse
import os
import sys

import config


def setup_logging():
    """Send all output to a log file in the data folder (and never crash if stdout doesn't exist)."""
    try:
        path = config.DATA_DIR / "backend.log"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(config.DATA_DIR / "backend.old.log")
        f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stderr = f
    except Exception:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w")


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "worker":
        import scraper
        scraper.worker_main(sys.argv[2], int(sys.argv[3]))
        return

    ap = argparse.ArgumentParser(description="DericBI CRM backend")
    ap.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--parent-pid", type=int, default=None,
                    help="exit automatically when this process disappears")
    args = ap.parse_args()

    if args.data_dir:
        config.set_data_dir(args.data_dir)
    else:
        config.set_data_dir(config.DATA_DIR)
    if not os.environ.get("DERICBI_CONSOLE"):
        setup_logging()

    import uvicorn
    from server import create_app
    app = create_app(port=args.port, parent_pid=args.parent_pid)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
