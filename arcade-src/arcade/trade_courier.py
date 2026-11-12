"""A node's half of trade messaging: send a step, read the steps that came back.

The carriage is the Messenger's node-to-node API message (messaging/api.py), exactly as
an inscribed page's `/node` talk uses it -- one testnet transaction, sealed to the
recipient's messaging key, authenticated as this node (2026-10-13: "Can't we use
the Pepe coin test net node to node communication protocol?"). A browser tab trading
for itself seals its own steps in the tab (/account/talk) and never needs this; this is
for a FACILITATING node's trader (arcade/node_trader.py, phase 5) and for tests.

Nothing read here is believed: `inbox` hands back parsed steps with who sent them, and
the trade state machine checks every one against the chains (arcade/trade.py).
"""

from __future__ import annotations

import time
from typing import Any

from . import trade_messages as M
from .messaging import api as apilib
from .messaging.keys import fingerprint_of
from .messaging.sender import MessageSender, SendError, funded_address


#: Steps the mesh could not deliver yet, per node: tried again on every inbox read (each tick),
#: for OUTBOX_FOR seconds. Swap talk never goes on the chain (2026-10-17: "I don't think
#: we should be using the chain at all").
OUTBOX_FOR = 30 * 60


def _relay(state: Any):
    try:
        return state.talk_relay()
    except Exception:                                      # noqa: BLE001 -- no mesh here
        return None


def _mesh_send(state: Any, to_pub: bytes, payload: bytes) -> bool:
    from .messaging.envelope import Header
    relay = _relay(state)
    if relay is None:
        return False
    sealed = payload[Header.decode(payload).length:]       # the relay carries it without the header
    try:
        return bool(relay.send(to_pub.hex(), sealed))
    except Exception:                                      # noqa: BLE001 -- kept in the outbox
        return False


def send(state: Any, chain: Any, to_pub: bytes, body: bytes) -> str:
    """One trade step to another trader's messaging key, as this node, over the mesh. Returns
    "mesh" when it was delivered, "queued" when the other side is not on the mesh yet (tried
    again on every `inbox` read). `body` is a `trade_messages.build(...)`: parsed again here,
    so nothing malformed ever leaves. A proposal is not queued: it raises, nothing locked."""
    said = M.parse(body)
    identity = state.ensure_identity()
    payload = apilib.seal(identity, to_pub, body)
    if _mesh_send(state, to_pub, payload):
        return "mesh"
    if said.get("step") == "propose":
        raise SendError("the other side is not on the mesh right now, so nobody can answer")
    box = getattr(state, "_trade_outbox", None)
    if box is None:
        box = state._trade_outbox = []
    box.append((to_pub, payload, time.time()))
    return "queued"


def _flush(state: Any) -> None:
    box = getattr(state, "_trade_outbox", None) or []
    left = []
    for to_pub, payload, at in box:
        if time.time() - at > OUTBOX_FOR:
            continue
        if not _mesh_send(state, to_pub, payload):
            left.append((to_pub, payload, at))
    state._trade_outbox = left


def mesh_inbox(state: Any, after: int = 0) -> tuple[list[dict], int]:
    """Trade steps the mesh brought this node since `after`: ([{sender, step}], cursor). Keeps
    this node's talk rooms (talk_relay.hold) while it is asked, and sends what waits."""
    from .messaging.envelope import Header, TYPE_API
    relay = _relay(state)
    if relay is None:
        return [], after
    identity = state.ensure_identity()
    key = identity.public_bytes.hex()
    try:
        relay.hold(key)
        _flush(state)
        got, cursor = relay.read(key, int(after))
    except Exception:                                      # noqa: BLE001 -- the next tick asks again
        return [], after
    out = []
    for _seq, hexed in got:
        sealed = bytes.fromhex(hexed)
        try:
            sender, body = apilib.open_payload(identity, Header(type=TYPE_API, clen=len(sealed)).encode() + sealed)
            step = M.parse(body)
        except Exception:                                  # noqa: BLE001 -- not a trade step for us
            continue
        out.append({"sender": bytes(sender).hex(), "step": step})
    return out, cursor


def _message_address(rpc: Any) -> str:
    """The wallet address holding the most coins a message can be paid from."""
    totals: dict[str, float] = {}
    for coin in rpc.call("listunspent", 1, 9_999_999) or []:
        script = str(coin.get("scriptPubKey", ""))
        if coin.get("address") and script.startswith("76a914") and script.endswith("88ac"):
            totals[coin["address"]] = totals.get(coin["address"], 0.0) + float(coin["amount"])
    return max(totals, key=totals.get) if totals else ""


def inbox(state: Any, chain: Any, after_id: int = 0, limit: int = 200) -> list[dict]:
    """Trade steps addressed to this node since `after_id`, oldest first:
    [{id, sender (hex messaging key), txid, height, step (the parsed dict)}].
    Anything that is not a well-formed trade step is skipped, not raised: the API
    channel carries other programs' traffic too."""
    identity = state.ensure_identity()
    out: list[dict] = []
    with state.store() as store:
        rows = store.api_messages(fingerprint_of(identity.public_bytes), chain.network,
                                  after_id=after_id, limit=limit)
    for row in rows:
        try:
            step = M.parse(bytes(row["body"]))
        except M.TradeMessageError:
            continue
        out.append({"id": row["id"], "sender": bytes(row["sender_pubkey"]).hex(),
                    "txid": row["txid"], "height": row["height"], "step": step})
    return out
