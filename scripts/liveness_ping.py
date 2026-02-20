#!/usr/bin/env python3
"""Simple liveness ping script.

Usage:
  python scripts/liveness_ping.py --url https://your-app.example.com/ --interval 300 --loop

This script uses the stdlib so no extra deps are required. It prints timestamps
and HTTP status codes; return code is 0 on success (last request OK), 1 on error.
"""
import argparse
import time
import urllib.request
import urllib.error
import sys


def ping(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.getcode()
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser(description="Liveness ping script")
    p.add_argument("--url", required=True, help="Full URL to ping (include scheme)")
    p.add_argument("--interval", type=int, default=300, help="Sleep interval in seconds when --loop is used")
    p.add_argument("--timeout", type=int, default=10, help="Request timeout seconds")
    p.add_argument("--loop", action="store_true", help="Keep pinging periodically")
    args = p.parse_args()

    last_ok = False
    try:
        while True:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            status = ping(args.url, timeout=args.timeout)
            if status is None:
                print(f"{now} - {args.url} - FAILED")
                last_ok = False
            else:
                print(f"{now} - {args.url} - {status}")
                last_ok = 200 <= status < 400

            if not args.loop:
                sys.exit(0 if last_ok else 1)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
