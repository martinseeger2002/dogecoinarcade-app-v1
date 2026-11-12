"""Use a stock Meshtastic radio as a reference arcade T-Deck.

  python -m arcade_lora.devcli --port /dev/ttyACM0 --gateway '!433c8154' --words FILE login [--join]
  ... bal | coins test | who @robin | send test @robin 1.5
"""
import argparse
import os
import sys
from decimal import Decimal

from .device import DeviceClient
from .device_keys import Account
from .link import MeshLink


def coin(text: str) -> int:
    return int(Decimal(text) * 10**8)


def main():
    ap = argparse.ArgumentParser(prog="devcli")
    ap.add_argument("--port")
    ap.add_argument("--ble")
    ap.add_argument("--gateway", required=True, help="gateway node, e.g. !433c8154")
    ap.add_argument("--words", required=True, help="file holding the 12 words (kept 0600)")
    ap.add_argument("--join", action="store_true")
    ap.add_argument("cmd", nargs="+")
    a = ap.parse_args()
    if os.stat(a.words).st_mode & 0o077:
        sys.exit(f"{a.words} must be readable by you only (chmod 600)")
    account = Account(open(a.words).read())
    link = MeshLink(port=a.port, ble=a.ble)
    gw_node = int(a.gateway.lstrip("!"), 16)
    dev = DeviceClient(account, lambda frame: link.send(gw_node, frame))
    link.on_frame(lambda node, frame: dev.receive(frame) if node == gw_node else None)
    try:
        tag = dev.login(join=a.join)
        print("signed in as", f"@{tag}" if tag else "(no @tag claimed yet)", flush=True)
        print("chains:", ", ".join(f"{c.network} ({c.label})" for c in dev.chains), flush=True)
        cmd, *rest = a.cmd
        if cmd == "bal":
            for net, (conf, pend) in dev.balances().items():
                print(f"  {net}: {Decimal(conf) / 10**8} (+{Decimal(pend) / 10**8} on its way)")
        elif cmd == "coins":
            for c in dev.coins(rest[0]):
                print(f"  {c['txid']}:{c['vout']} {Decimal(c['value']) / 10**8}")
        elif cmd == "register":
            for net in rest or [c.network for c in dev.chains]:
                print(f"  {net}: {dev.register(net)}")
        elif cmd == "who":
            w = dev.who(rest[0])
            print(f"  @{w['tag']} {w['address']} key {w['key'].hex()[:16]}… {w['name']}")
        elif cmd == "send":
            net, to, amount = rest
            def confirm(offer, checked):
                print(f"  pay {to} ({offer['to']}) {Decimal(checked.paid) / 10**8}, fee {Decimal(checked.fee) / 10**8}, "
                      f"change {Decimal(checked.change) / 10**8}")
                return input("  sign? [y/N] ").strip().lower() == "y"
            print("  sent", dev.send(net, to, coin(amount), confirm))
    finally:
        link.close()


if __name__ == "__main__":
    main()
