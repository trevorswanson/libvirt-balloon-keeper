#!/usr/bin/env python3
"""Run the loopback-only Unraid status/configuration API."""
from __future__ import annotations

import argparse
from pathlib import Path

from libvirt_balloon_keeper.web import create_server
from libvirt_balloon_keeper.adapter import VirshAdapter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.config, args.host, args.port, adapter=VirshAdapter())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
