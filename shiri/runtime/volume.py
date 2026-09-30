"""A bounded Shairport callback; never invokes a shell or network API directly."""

import argparse
import asyncio
import math
import sys

from shiri.rpc import RpcError, call_rpc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--room-id", required=True)
    parser.add_argument("volume", type=float)
    args = parser.parse_args()
    if not math.isfinite(args.volume):
        parser.error("Volume must be finite")
    volume = max(0, min(100, round((args.volume + 30) * 100 / 30)))
    try:
        asyncio.run(
            call_rpc(args.socket, "phone_volume", {"room_id": args.room_id, "volume": volume}, timeout=4)
        )
    except RpcError as exc:
        print(f"Shiri phone volume is pending: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
