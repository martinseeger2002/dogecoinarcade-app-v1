"""A Meshtastic radio as a frame pipe: USB serial, TCP or Bluetooth.

Frames travel as PRIVATE_APP direct messages; stock firmware encrypts those
end to end (PKC) and every Meshtastic node relays them.

Frames go with no radio ack (2026-10-26): each send waited for the other
radio's ack, up to 75 s, so one lost ack held a whole answer for a minute; and
the T-Deck's acks collided with the next parts (MAX_RETRANSMIT, 2026-10-23).
Now the parts of an answer go one after another, a short gap apart, and the
protocol recovers what is lost: the T-Deck asks again for an answer that does
not come and sends RESEND for the parts of one that stopped. Received
frames are handled on a worker thread, never the radio library's reader
thread, so a handler that sends can wait for acks the reader delivers.
"""
from __future__ import annotations

import logging
import queue
import threading
import time

from pubsub import pub
from meshtastic.protobuf import portnums_pb2

from . import frames as F

log = logging.getLogger("arcade_lora.link")
# seconds to wait for the radio's verdict on a frame: Meshtastic retries itself 3 times first, and on a weak LongFast
# link that took ~30 s -- with 25 s the gateway sent the frame again while the radio was still trying (and the T-Deck
# had it), doubling the airtime (2026-10-26: 2,301 "no ack" since 10-23, each followed ~5 s later by MAX_RETRANSMIT).
ACK_WAIT = 75.0         # (send_acked only)
AFTER_ACK = 0.3         # a breath between frames so relays and the T-Deck can answer
#: Between frames sent with no ack: about one LongFast frame's airtime (a 200-byte packet is ~1.2-1.5 s on air). At
#: 0.4 s the radio's transmit queue overflowed in a burst and dropped frames (2026-10-26, a sign-in's dozen answers).
FRAME_GAP = 1.6


class MeshLink:
    def __init__(self, *, port: str | None = None, ble: str | None = None, host: str | None = None):
        if ble:
            from meshtastic.ble_interface import BLEInterface
            self.iface = BLEInterface(ble)
        elif host:
            from meshtastic.tcp_interface import TCPInterface
            self.iface = TCPInterface(host)
        else:
            from meshtastic.serial_interface import SerialInterface
            self.iface = SerialInterface(port)
        self.my_node = self.iface.myInfo.my_node_num
        self.handlers = []
        self.heard_handlers = []
        self.send_lock = threading.Lock()
        self.inbox: queue.Queue = queue.Queue()
        self.stats = {"sent": 0, "acked": 0, "failed": 0}
        # hops each node's packets took to reach us (hopStart - hopLimit): a reply needs no more (2026-10-26, over the
        # air: our frames went out with 3 hops and the public mesh's nodes relayed every one -- more airtime, more
        # collisions -- though the T-Deck and the gateway hear each other directly)
        self.hops: dict[int, int] = {}
        self.worker = threading.Thread(target=self._work, name="arcade-frames", daemon=True)
        self.worker.start()
        pub.subscribe(self._on_receive, "meshtastic.receive")

    def on_frame(self, handler):
        """handler(node_num, frame_bytes) for every arcade frame heard for us."""
        self.handlers.append(handler)

    def on_heard(self, handler):
        """handler(node_num) for every packet heard from a node, of any kind."""
        self.heard_handlers.append(handler)

    def _on_receive(self, packet, interface):
        # the radio library's reader thread: only hand over, never block here
        if interface is not self.iface:
            return
        node = packet.get("from")
        d = packet.get("decoded") or {}
        start, left = packet.get("hopStart"), packet.get("hopLimit")
        if isinstance(start, int) and isinstance(left, int) and start >= left >= 0:
            self.hops[node] = start - left
        frame = None
        if d.get("portnum") in ("PRIVATE_APP", F.PORTNUM):
            payload = d.get("payload") or b""
            if payload and payload[0] == F.MAGIC:
                frame = bytes(payload)
                log.debug("rx %d B from %08x snr=%s", len(payload), node, packet.get("rxSnr"))
        self.inbox.put((node, frame))

    def _work(self):
        while True:
            node, frame = self.inbox.get()
            for h in self.heard_handlers:
                try:
                    h(node)
                except Exception:
                    log.exception("heard handler failed")
            if frame is None:
                continue
            for h in self.handlers:
                try:
                    h(node, frame)
                except Exception:
                    log.exception("frame handler failed")

    def send(self, node: int, frame: bytes) -> bool:
        """One frame to one node, with no radio ack: True once the radio took it (the protocol recovers losses)."""
        with self.send_lock:
            hops = self.hops.get(node)
            # one more than it took, so one relay can still carry a frame the other radio does not hear directly
            # (over the air 2026-10-26: the T-Deck heard the gateway only through a relay about half the time)
            kw = {"hopLimit": min(hops + 1, 7)} if hops is not None else {}   # unknown: the radio's default
            self.iface.sendData(frame, destinationId=node, portNum=portnums_pb2.PortNum.PRIVATE_APP, wantAck=False, **kw)
            self.stats["sent"] += 1
            time.sleep(FRAME_GAP)
        return True

    def send_acked(self, node: int, frame: bytes) -> bool:
        """One frame, waiting for the node's radio ack (one more try if not): the old way, kept for tools."""
        return self._send_once(node, frame) or self._send_once(node, frame)

    def _send_once(self, node: int, frame: bytes) -> bool:
        with self.send_lock:
            done = threading.Event()
            result = {"ok": False}

            def answered(packet):
                routing = (packet.get("decoded") or {}).get("routing") or {}
                result["ok"] = routing.get("errorReason", "NONE") == "NONE"
                if not result["ok"]:
                    log.info("frame to %08x: %s", node, routing.get("errorReason"))
                done.set()

            self.iface.sendData(frame, destinationId=node, portNum=portnums_pb2.PortNum.PRIVATE_APP,
                                wantAck=True, onResponse=answered, onResponseAckPermitted=True)
            self.stats["sent"] += 1
            if not done.wait(ACK_WAIT):
                log.info("frame to %08x: no ack in %.0f s", node, ACK_WAIT)
            self.stats["acked" if result["ok"] else "failed"] += 1
            time.sleep(AFTER_ACK)
            return result["ok"]

    def broadcast(self, frame: bytes):
        with self.send_lock:
            self.iface.sendData(frame, portNum=portnums_pb2.PortNum.PRIVATE_APP, wantAck=False)
            time.sleep(2.0)

    def close(self):
        pub.unsubscribe(self._on_receive, "meshtastic.receive")
        self.iface.close()
