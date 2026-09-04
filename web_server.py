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
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()
    server = create_server(args.config, adapter=VirshAdapter(), socket_path=args.socket)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
