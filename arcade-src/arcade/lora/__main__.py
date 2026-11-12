"""Run an arcade LoRa gateway.

  python -m arcade_lora --port /dev/ttyUSB0 --arcade http://127.0.0.1:8420
  python -m arcade_lora --ble <T-Deck BLE address> --arcade https://app.dogecoinarcade.com
"""
import argparse
import logging
import time

import os

from .gateway import Gateway
from .link import MeshLink
from .store import Store

BEACON_EVERY = 900.0
POLL_EVERY = 30.0


def main():
    ap = argparse.ArgumentParser(prog="arcade_lora")
    ap.add_argument("--port", help="serial port of the gateway radio")
    ap.add_argument("--ble", help="Bluetooth address of a radio (a PC acting as gateway)")
    ap.add_argument("--host", help="TCP host of a meshtasticd")
    ap.add_argument("--arcade", required=True, help="the arcade's base URL")
    ap.add_argument("--name", default="arcade gateway")
    ap.add_argument("--chains", help="comma list of networks T-Decks may see (default: all)")
    ap.add_argument("--mail-max", type=int, default=645)
    ap.add_argument("--no-beacon", action="store_true")
    ap.add_argument("--state", default=os.path.expanduser("~/.arcade-lora-gateway.sqlite"),
                    help="where devices, sessions and undelivered mail are kept")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    link = MeshLink(port=a.port, ble=a.ble, host=a.host)
    gw = Gateway(a.arcade, link.send, name=a.name, mail_max=a.mail_max,
                 networks=a.chains.split(",") if a.chains else None, store=Store(a.state))
    link.on_heard(gw.heard)
    link.on_frame(gw.receive)
    logging.info("gateway up on node %08x for %s", link.my_node, a.arcade)
    next_beacon, next_poll = 0.0, time.monotonic() + POLL_EVERY
    try:
        while True:
            now = time.monotonic()
            if not a.no_beacon and now >= next_beacon:
                link.broadcast(gw.beacon())
                next_beacon = now + BEACON_EVERY
            if now >= next_poll:
                gw.poll()
                next_poll = now + POLL_EVERY
            gw.tick()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        link.close()


if __name__ == "__main__":
    main()
