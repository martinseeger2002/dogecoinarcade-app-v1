"""Taking a cross-chain swap order from the website (arcadesite/swaptake.py), offline: V1.0's swap commands and two
chains (Pepecoin testnet, Dogecoin) stood in here, real keys and signatures, the browser's part done in Python.

    .venv/bin/python tests/swaptake_test.py
"""
import hashlib, json, os, sys, tempfile, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exchange_test as X  # noqa: E402

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, utils  # noqa: E402

from arcade import htlc  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402
from arcade.script import b58check_encode, hash160  # noqa: E402
from arcadesite.v1 import V1, V1Error  # noqa: E402

COIN = 100_000_000
fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


def key():
    k = ec.generate_private_key(ec.SECP256K1())
    return k, k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)


def sign(k, digest: bytes) -> bytes:
    return k.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256()))) + b"\x01"


def dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def varint(b, at):
    n = b[at]
    if n < 0xfd:
        return n, at + 1
    w = {0xfd: 2, 0xfe: 4, 0xff: 8}[n]
    return int.from_bytes(b[at + 1:at + 1 + w], "little"), at + 1 + w


def decode(raw: bytes) -> dict:
    at = 4
    n, at = varint(raw, at)
    vin = []
    for _ in range(n):
        txid = raw[at:at + 32][::-1].hex(); vout = int.from_bytes(raw[at + 32:at + 36], "little")
        ln, at = varint(raw, at + 36)
        ss = raw[at:at + ln]; at += ln
        seq = int.from_bytes(raw[at:at + 4], "little"); at += 4
        vin.append({"txid": txid, "vout": vout, "scriptSig": {"hex": ss.hex()}, "sequence": seq})
    n, at = varint(raw, at)
    vout = []
    for i in range(n):
        v = int.from_bytes(raw[at:at + 8], "little")
        ln, at = varint(raw, at + 8)
        vout.append({"value": v / COIN, "n": i, "scriptPubKey": {"hex": raw[at:at + ln].hex()}}); at += ln
    return {"vin": vin, "vout": vout, "locktime": int.from_bytes(raw[at:at + 4], "little")}


# ---------------------------------------------------------------- the stand-in chains and V1.0
CH = {"test": {}, "doge": {}}           # network -> txid -> {"vout": [...], "confirmations": n, "spent": set()}
SENT = {"test": [], "doge": []}
MEDIAN = {"test": int(time.time()), "doge": int(time.time())}
ASKED = []
ME_K = {"test": key(), "doge": key()}
MK_K = {"test": key(), "doge": key()}
ADDR = {n: b58check_encode(NETWORKS[n].pubkeyhash_version, hash160(ME_K[n][1])) for n in ("test", "doge")}
ORDER = {"id": "a1b2c3d4", "give": {"chain": "doge", "kind": "coin", "asset": "", "amount": str(5 * COIN)},
         "get": {"chain": "peptest", "kind": "coin", "asset": "", "amount": str(COIN)}, "status": "open"}
MAKER = {"lock": None, "time": 0}


def put_tx(net, txid, outs, conf=1):
    CH[net][txid] = {"vout": [{"value": v / COIN, "n": i, "scriptPubKey": {"hex": s}} for i, (v, s) in enumerate(outs)],
                     "confirmations": conf, "spent": set()}


def fake_core(self, chain, method, *p, timeout=None):
    net = "test" if chain in ("test", "peptest") else chain
    if method == "getrawtransaction":
        t = CH[net].get(p[0])
        if not t:
            raise V1Error("No such mempool or blockchain transaction")
        return {"vout": t["vout"], "confirmations": t["confirmations"]}
    if method == "gettxout":
        t = CH[net].get(p[0])
        if not t or p[1] in t["spent"] or p[1] >= len(t["vout"]):
            return None
        o = t["vout"][p[1]]
        return {"value": o["value"], "scriptPubKey": o["scriptPubKey"], "confirmations": t["confirmations"]}
    if method == "decoderawtransaction":
        return decode(bytes.fromhex(p[0]))
    if method == "sendrawtransaction":
        raw = bytes.fromhex(p[0]); d = decode(raw)
        for v in d["vin"]:
            CH[net][v["txid"]]["spent"].add(v["vout"])
        txid = dsha(raw)[::-1].hex()
        SENT[net].append((txid, d))
        return txid
    if method == "getblockchaininfo":
        return {"mediantime": MEDIAN[net]}
    raise V1Error(f"no {method} here")


def fake_cmd(self, _command, _timeout=None, **p):
    if _command == "chains.list":
        return [{"key": "peptest", "installed": True, "home": True, "indexes": ["ledger", "addresses"]},
                {"key": "doge", "installed": True, "indexes": ["addresses"]}]
    if _command == "swap.quote":
        ASKED.append(("quote", p))
        return {"order": ORDER, "maker_keys": {"doge": MK_K["doge"][1].hex(), "peptest": MK_K["test"][1].hex()},
                "maker_secs": 4 * 3600, "sized": True}
    if _command == "swap.told":
        ASKED.append(("told", p))
        return {"step": "locking"}
    if _command == "swap.status":
        return {"step": "locked" if MAKER["lock"] else "locking",
                "maker_lock": {"step": "locked", "txid": MAKER["lock"], "vout": 0, "time": MAKER["time"]} if MAKER["lock"] else {"step": "start"}}
    return X.fake_cmd(self, _command, _timeout, **p)


def main():
    V1.page, V1.cmd, V1.core = X.fake_page, fake_cmd, fake_core
    home = tempfile.mkdtemp()
    json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
    c, app = X.site(home, X.ME)
    chains = app.state.chains
    chains.account_address = lambda pubkey, chain: ADDR.get(chain.network, "")
    who = "02" + "11" * 32
    for ch in chains.account_chains():
        app.state.site.set_setting(chains.coinkey_key(who, ch), ME_K[ch.network][1].hex())
    src = app.state.frame_src(None, X.PIECE + "i0")
    v = "?v=" + urllib.parse.parse_qs(urllib.parse.urlparse(src).query)["v"][0]

    r = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]})
    check(r.status_code == 400 and "Arcade app" in r.json()["error"], "with taking off, a take across chains is refused")
    app.state.site.set_setting("market_taker", True)
    qid = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]
    q = c.post("/account/swap/quote", json={"id": qid}).json()
    check(q.get("give", {}).get("amount") == "5" and q.get("lock") == "1.01" and q.get("my_key") == ME_K["test"][1].hex(),
          f"quote: 5 DOGE for 1 PEP test, lock 1.01 ({q})")
    check(ASKED[-1][1]["taker"] == qid, "the maker is asked for this visitor (the question's reference)")

    secret = os.urandom(32)
    h = hashlib.sha256(secret).digest()
    bad = htlc.redeem_script(h, MK_K["test"][1], ME_K["test"][1], q["taker_time"] - 60)
    r = c.post("/account/swap/lock", json={"id": qid, "hash": h.hex(), "redeem": bad.hex()})
    check(r.status_code == 400, "a lock with another time is refused")
    redeem = htlc.redeem_script(h, MK_K["test"][1], ME_K["test"][1], q["taker_time"])
    w = c.post("/account/swap/lock", json={"id": qid, "hash": h.hex(), "redeem": redeem.hex()}).json()
    check(w.get("amount") == "1.01" and w.get("network") == "test", f"lock: where and how much ({w})")

    lock_txid = "11" * 32
    put_tx("test", lock_txid, [(5 * COIN, "76a914" + "00" * 20 + "88ac"), (COIN + 1_000_000, htlc.p2sh_script(redeem).hex())], conf=0)
    lk = c.post("/account/swap/locked", json={"id": qid, "txid": lock_txid}).json()
    check(lk.get("vout") == 1, f"the lock's output found ({lk})")

    # The refund, signed first and held.
    sp = c.post("/account/trade/spend", json={"chain": "test", "redeem": redeem.hex(), "txid": lock_txid, "vout": 1, "kind": "refund"}).json()
    check(sp.get("locktime") == q["taker_time"] and len(sp.get("sighashes", [])) == 1, "refund: unsigned, at the lock's time")
    sig = sign(ME_K["test"][0], bytes.fromhex(sp["sighash"]))
    r = c.post("/account/trade/watch", json={"chain": "test", "redeem": redeem.hex(), "raw": sp["raw"], "signatures": [sig.hex()], "kind": "refund"})
    check(r.status_code == 200, f"the refund is held ({r.json()})")
    other = sign(MK_K["test"][0], bytes.fromhex(sp["sighash"]))
    r = c.post("/account/trade/watch", json={"chain": "test", "redeem": redeem.hex(), "raw": sp["raw"], "signatures": [other.hex()], "kind": "refund"})
    check(r.status_code == 400, "a refund signed by another key is not held")
    app.state.swap_watch_once()
    check(not SENT["test"], "the held refund is not sent before the lock's time")

    st = c.post("/account/swap/step", json={"id": qid}).json()
    check(st["step"] == "locked" and not any(a[0] == "told" for a in ASKED), "the maker is not told before the lock is mined")
    CH["test"][lock_txid]["confirmations"] = 1
    st = c.post("/account/swap/step", json={"id": qid}).json()
    told = next((a[1] for a in ASKED if a[0] == "told"), {})
    check(st["step"] == "told" and told.get("taker_lock", {}).get("txid") == lock_txid
          and told.get("taker_keys", {}).get("doge") == ME_K["doge"][1].hex(), f"the maker told, with this visitor's keys ({st})")

    # The maker's lock: sized, claimed by this visitor's Dogecoin key.
    MAKER["time"] = int(time.time()) + 4 * 3600
    mredeem = htlc.redeem_script(h, ME_K["doge"][1], MK_K["doge"][1], MAKER["time"], sized=True)
    MAKER["lock"] = "22" * 32
    put_tx("doge", MAKER["lock"], [(5 * COIN, htlc.p2sh_script(mredeem).hex())], conf=1)
    st = c.post("/account/swap/step", json={"id": qid}).json()
    check(st["step"] == "failed" and "not what was agreed" in st["say"], "a maker lock holding less than agreed is not claimed")
    # (a fresh swap for the good case)
    MAKER["lock"] = None
    s = json.loads(json.dumps(c.post("/account/swap/step", json={"id": qid}).json()))
    import sqlite3
    db = sqlite3.connect(os.path.join(home, "site-swaps.sqlite"))
    data = json.loads(db.execute("SELECT data FROM swap WHERE id=?", (qid,)).fetchone()[0])
    data["step"] = "told"
    db.execute("UPDATE swap SET data=? WHERE id=?", (json.dumps(data), qid)); db.commit()
    MAKER["lock"] = "33" * 32
    put_tx("doge", MAKER["lock"], [(5 * COIN + 1_000_000, htlc.p2sh_script(mredeem).hex())], conf=1)
    st = c.post("/account/swap/step", json={"id": qid}).json()
    check(st["step"] == "claimable" and st["redeem"] == mredeem.hex() and st["network"] == "doge", f"the maker's lock checked: claimable ({st.get('step')})")

    sp = c.post("/account/trade/spend", json={"chain": "doge", "redeem": mredeem.hex(), "txid": MAKER["lock"], "vout": 0, "kind": "claim"}).json()
    sig = sign(ME_K["doge"][0], bytes.fromhex(sp["sighash"]))
    r = c.post("/account/trade/spend/sign", json={"chain": "doge", "redeem": mredeem.hex(), "raw": sp["raw"], "signatures": [sig.hex()], "kind": "claim", "secret": "00" * 32})
    check(r.status_code == 400, "a claim with a wrong secret is refused")
    r = c.post("/account/trade/spend/sign", json={"chain": "doge", "redeem": mredeem.hex(), "raw": sp["raw"], "signatures": [sig.hex()], "kind": "claim", "secret": secret.hex()})
    sent = SENT["doge"][-1][1] if SENT["doge"] else {}
    check(r.status_code == 200 and secret.hex() in (sent.get("vin") or [{}])[0].get("scriptSig", {}).get("hex", ""),
          f"the claim sent, carrying the secret ({r.json()})")
    check(sent.get("vout", [{}])[0].get("value") == 5, "the claim pays this visitor 5 DOGE (the lock less its spend)")

    # The refund's time comes: the lock was never claimed by the maker here, so the held refund is sent.
    MEDIAN["test"] = q["taker_time"] + 1
    app.state.swap_watch_once()
    check(bool(SENT["test"]) and SENT["test"][-1][1]["locktime"] == q["taker_time"], "after its time the held refund is sent")
    app.state.swap_watch_once()
    check(len(SENT["test"]) == 1, "and only once")
    print("\nALL OK" if not fails else f"\n{len(fails)} FAILED")
    return not fails


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
