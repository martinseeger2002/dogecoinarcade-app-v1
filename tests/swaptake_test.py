"""Taking a cross-chain swap order from the website (arcadesite/swaptake.py), offline: V1.0's swap commands and two
chains (Pepecoin testnet, Dogecoin) stood in here, real keys and signatures, the browser's part done in Python.

    .venv/bin/python tests/swaptake_test.py
"""
import hashlib, json, os, sqlite3, sys, tempfile, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exchange_test as X  # noqa: E402

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, utils  # noqa: E402

from arcade import htlc  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402
from arcade.script import b58check_encode, hash160  # noqa: E402
from arcade.txbuild import p2pkh_script  # noqa: E402
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
    segwit = raw[4] == 0 and raw[5] == 1
    if segwit:
        at = 6
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
    if segwit:
        for v in vin:
            k, at = varint(raw, at)
            items = []
            for _ in range(k):
                ln, at = varint(raw, at)
                items.append(raw[at:at + ln].hex()); at += ln
            v["txinwitness"] = items
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
INSCRIBED = {}                          # inscription id -> "txid:vout:offset", V1.0's explorer.inscription "at"
LEDGER = {}                             # address -> its arcade-ledger tokens, V1.0's explorer.address
SELLING = {}                            # the visitor's held-back coin carrying the inscription it sells
LOCK_COINS = {}                         # a lock's P2SH address -> its coins, V1.0's tx.coins
OWNERS = {}                             # inscription id -> its owner


def secret_in(tx, secret):
    return secret.hex() in tx["vin"][0]["scriptSig"]["hex"]


def first_push(script: bytes) -> bytes:
    return script[1:1 + script[0]]


def put_tx(net, txid, outs, conf=1):
    CH[net][txid] = {"vout": [{"value": v / COIN, "n": i, "scriptPubKey": {"hex": s}} for i, (v, s) in enumerate(outs)],
                     "confirmations": conf, "spent": set()}


def fake_core(self, chain, method, *p, timeout=None):
    net = "test" if chain in ("test", "peptest") else chain
    if method == "getrawtransaction":
        t = CH[net].get(p[0])
        if not t:
            raise V1Error("No such mempool or blockchain transaction")
        return {"vout": t["vout"], "confirmations": t["confirmations"], **({"version": t["version"]} if t.get("version") else {})}
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
    if _command == "tokens":
        return [{"tick": "frgy", "overall": "2000", "transferable": "0"}]
    if _command == "explorer.token":
        return {"tick": p["tick"], "dec": 0}
    if _command == "explorer.tx":
        return {"txid": p["txid"], "outputs": [{"vout": 0, "inscriptions": [i for i, at in INSCRIBED.items() if at.startswith(p["txid"] + ":0:")]}]}
    if _command == "tx.coins" and any(a in LOCK_COINS for a in p.get("addresses", [])):
        return {"addresses": {a: {"confirmed": LOCK_COINS.get(a, []), "unconfirmed": []} for a in p["addresses"]}}
    if _command == "tx.coins" and SELLING:
        got = X.fake_cmd(self, _command, _timeout, **p)
        for a in p["addresses"]:
            got["addresses"].setdefault(a, {"confirmed": [], "unconfirmed": []})["held_back"] = [dict(SELLING)]
        return got
    if _command == "explorer.address":
        return {"address": p["address"], "ledger_tokens": LEDGER.get(p["address"], [])}
    if _command == "explorer.inscription":
        if p["id"] not in INSCRIBED:
            raise V1Error("no inscription")
        return {"id": p["id"], "at": INSCRIBED[p["id"]], "owner": OWNERS.get(p["id"])}
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
    # ---- with this site's fee: 1% of what the visitor GIVES, paid with their lock;
    # the claim then comes out whole
    OPK = key()
    op_doge = b58check_encode(NETWORKS["doge"].pubkeyhash_version, hash160(OPK[1]))
    op_test = b58check_encode(NETWORKS["test"].pubkeyhash_version, hash160(OPK[1]))
    app.state.site.set_setting("trade_cut", 1)
    app.state.site.set_setting("trade_cut_to", {"doge": op_doge, "test": op_test})
    MAKER["lock"] = None
    qid2 = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]
    q2 = c.post("/account/swap/quote", json={"id": qid2}).json()
    check(q2.get("fee") == "0.01" and q2.get("receive") == "5" and q2.get("fee_with_lock"),
          f"quote: the fee is 1% of the 1 PEP given, paid with the lock; 5 DOGE comes whole ({q2.get('fee')}, {q2.get('receive')})")
    secret2 = os.urandom(32); h2 = hashlib.sha256(secret2).digest()
    r2 = htlc.redeem_script(h2, MK_K["test"][1], ME_K["test"][1], q2["taker_time"])
    w2 = c.post("/account/swap/lock", json={"id": qid2, "hash": h2.hex(), "redeem": r2.hex()}).json()
    from arcade.txbuild import p2pkh_script
    pays2 = [(o["value"], o["scriptPubKey"]["hex"]) for o in decode(bytes.fromhex(w2["offer"]["raw"]))["vout"]] if w2.get("offer") else []
    check((1.01, htlc.p2sh_script(r2).hex()) in pays2 and (0.01, p2pkh_script(op_test).hex()) in pays2,
          f"the lock offer pays the lock 1.01 and the operator 0.01 PEP ({[x[0] for x in pays2]})")
    put_tx("test", "44" * 32, [(COIN + 1_000_000, htlc.p2sh_script(r2).hex()), (1_000_000, p2pkh_script(op_test).hex())], conf=1)
    c.post("/account/swap/locked", json={"id": qid2, "txid": "44" * 32})
    c.post("/account/swap/step", json={"id": qid2})
    MAKER["time"] = int(time.time()) + 4 * 3600
    m2 = htlc.redeem_script(h2, ME_K["doge"][1], MK_K["doge"][1], MAKER["time"], sized=True)
    MAKER["lock"] = "55" * 32
    put_tx("doge", MAKER["lock"], [(5 * COIN + 1_000_000, htlc.p2sh_script(m2).hex())], conf=1)
    st2 = c.post("/account/swap/step", json={"id": qid2}).json()
    check(st2.get("step") == "claimable" and not st2["leg"].get("fee_value") and st2["leg"]["amount"] == str(5 * COIN),
          f"the claim's terms: 5 DOGE whole, no fee at the claim ({st2.get('leg')})")
    sp2 = c.post("/account/trade/spend", json={"chain": "doge", "redeem": m2.hex(), "txid": MAKER["lock"], "vout": 0, "kind": "claim"}).json()
    outs = decode(bytes.fromhex(sp2["raw"]))["vout"]
    check([(o["value"], o["scriptPubKey"]["hex"]) for o in outs] == [(5, p2pkh_script(ADDR["doge"]).hex())],
          f"the claim pays the visitor 5 DOGE ({[(o['value']) for o in outs]})")
    sig2 = sign(ME_K["doge"][0], bytes.fromhex(sp2["sighash"]))
    r = c.post("/account/trade/spend/sign", json={"chain": "doge", "redeem": m2.hex(), "raw": sp2["raw"], "signatures": [sig2.hex()], "kind": "claim", "secret": secret2.hex()})
    check(r.status_code == 200, f"the claim is sent ({r.json()})")
    # ---- an inscription bought (Ord Market): the price locked with this site's fee in the same transaction, the piece
    # claimed first and whole with its reserve as the fee, no fee at the claim
    INS = "66" * 32 + "i0"
    ORDER.update(id="0dd0dd01", give={"chain": "doge", "kind": "ord", "asset": INS, "amount": "1"},
                 get={"chain": "peptest", "kind": "coin", "asset": "", "amount": str(2 * COIN)})
    MAKER["lock"] = None
    qid3 = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]
    q3 = c.post("/account/swap/quote", json={"id": qid3}).json()
    check(q3.get("kind") == "ord" and q3.get("inscription") == INS and q3.get("fee") == "0.02" and q3.get("fee_with_lock"),
          f"an inscription's quote: the fee is 1% of the price, paid with the lock ({q3.get('fee')}, {q3.get('kind')})")
    secret3 = os.urandom(32); h3 = hashlib.sha256(secret3).digest()
    r3 = htlc.redeem_script(h3, MK_K["test"][1], ME_K["test"][1], q3["taker_time"])
    w3 = c.post("/account/swap/lock", json={"id": qid3, "hash": h3.hex(), "redeem": r3.hex()}).json()
    lo = decode(bytes.fromhex(w3.get("offer", {}).get("raw", "")))["vout"] if w3.get("offer") else []
    pays = [(o["value"], o["scriptPubKey"]["hex"]) for o in lo]
    check((2.01, htlc.p2sh_script(r3).hex()) in pays and (0.02, p2pkh_script(op_test).hex()) in pays,
          f"the lock offer pays the lock 2.01 and the operator 0.02 ({[p_[0] for p_ in pays]})")
    put_tx("test", "77" * 32, [(2 * COIN + 1_000_000, htlc.p2sh_script(r3).hex()), (2_000_000, p2pkh_script(op_test).hex())], conf=1)
    c.post("/account/swap/locked", json={"id": qid3, "txid": "77" * 32})
    c.post("/account/swap/step", json={"id": qid3})
    MAKER["time"] = int(time.time()) + 4 * 3600
    m3 = htlc.redeem_script(h3, ME_K["doge"][1], MK_K["doge"][1], MAKER["time"], sized=True)
    MAKER["lock"] = "88" * 32
    INSCRIBED[INS] = f"{MAKER['lock']}:0:0"
    put_tx("doge", MAKER["lock"], [(100_000, htlc.p2sh_script(m3).hex()), (2_000_000, htlc.p2sh_script(m3).hex())], conf=1)
    st3 = c.post("/account/swap/step", json={"id": qid3}).json()
    check(st3.get("step") == "claimable" and st3["leg"].get("kind") == "doginal" and not st3["leg"].get("fee_value"),
          f"the seller's lock holds the inscription at its first coin: claimable, no fee at the claim ({st3.get('leg')})")
    sp3 = c.post("/account/trade/spend", json={"chain": "doge", "redeem": m3.hex(), "txid": MAKER["lock"], "vout": 0, "kind": "claim",
                                               "leg": st3["leg"]}).json()
    d3 = decode(bytes.fromhex(sp3["raw"]))
    check([(x["txid"], x["vout"]) for x in d3["vin"]] == [(MAKER["lock"], 0), (MAKER["lock"], 1)]
          and [(o["value"], o["scriptPubKey"]["hex"]) for o in d3["vout"]] == [(0.001, p2pkh_script(ADDR["doge"]).hex())],
          f"the claim: the inscription's coin first and whole to the visitor, the reserve the fee ({d3['vout']})")
    sigs3 = [sign(ME_K["doge"][0], bytes.fromhex(h_)).hex() for h_ in sp3["sighashes"]]
    r = c.post("/account/trade/spend/sign", json={"chain": "doge", "redeem": m3.hex(), "raw": sp3["raw"], "signatures": sigs3,
                                                  "kind": "claim", "secret": secret3.hex()})
    check(r.status_code == 200, f"the inscription's claim is sent ({r.json()})")
    # a lock with the inscription somewhere else is not claimed
    MAKER["lock"] = "99" * 32
    put_tx("doge", MAKER["lock"], [(100_000, htlc.p2sh_script(m3).hex()), (2_000_000, htlc.p2sh_script(m3).hex())], conf=1)
    qid4 = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]
    q4 = c.post("/account/swap/quote", json={"id": qid4}).json()
    secret4 = os.urandom(32); h4 = hashlib.sha256(secret4).digest()
    r4 = htlc.redeem_script(h4, MK_K["test"][1], ME_K["test"][1], q4["taker_time"])
    c.post("/account/swap/lock", json={"id": qid4, "hash": h4.hex(), "redeem": r4.hex()})
    put_tx("test", "aa" * 32, [(2 * COIN + 1_000_000, htlc.p2sh_script(r4).hex())], conf=1)
    c.post("/account/swap/locked", json={"id": qid4, "txid": "aa" * 32})
    c.post("/account/swap/step", json={"id": qid4})
    m4 = htlc.redeem_script(h4, ME_K["doge"][1], MK_K["doge"][1], MAKER["time"], sized=True)
    put_tx("doge", MAKER["lock"], [(100_000, htlc.p2sh_script(m4).hex()), (2_000_000, htlc.p2sh_script(m4).hex())], conf=1)
    st4 = c.post("/account/swap/step", json={"id": qid4}).json()
    check(st4.get("step") == "failed", f"a lock without the inscription at its first coin is not claimed ({st4})")
    # ---- tokens bought for coins: an arcade-ledger token held by the maker's lock, claimed with the ledger's send
    ORDER.update(id="70ce70ce", give={"chain": "doge", "kind": "token", "asset": "7", "amount": "500"},
                 get={"chain": "peptest", "kind": "coin", "asset": "", "amount": str(COIN)})
    app.state.site.set_setting("trade_cut", 0)
    MAKER["lock"] = None
    qt = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]
    q5 = c.post("/account/swap/quote", json={"id": qt}).json()
    check(q5.get("kind") == "token" and q5.get("give", {}).get("amount") == "500" and "#7" in q5.get("give", {}).get("coin", ""),
          f"a token's quote names 500 of token #7 ({q5.get('give')})")
    s5 = os.urandom(32); h5 = hashlib.sha256(s5).digest()
    r5 = htlc.redeem_script(h5, MK_K["test"][1], ME_K["test"][1], q5["taker_time"])
    c.post("/account/swap/lock", json={"id": qt, "hash": h5.hex(), "redeem": r5.hex()})
    put_tx("test", "b1" * 32, [(COIN + 1_000_000, htlc.p2sh_script(r5).hex())], conf=1)
    c.post("/account/swap/locked", json={"id": qt, "txid": "b1" * 32})
    c.post("/account/swap/step", json={"id": qt})
    MAKER["time"] = int(time.time()) + 4 * 3600
    m5 = htlc.redeem_script(h5, ME_K["doge"][1], MK_K["doge"][1], MAKER["time"], sized=True)
    MAKER["lock"] = "b2" * 32
    put_tx("doge", MAKER["lock"], [(3_000_000, htlc.p2sh_script(m5).hex())], conf=1)
    LEDGER[htlc.address_of(m5, NETWORKS["doge"])] = [{"propertyid": 7, "units": "500"}]
    st5 = c.post("/account/swap/step", json={"id": qt}).json()
    check(st5.get("step") == "claimable" and st5["leg"].get("kind") == "token", f"the token lock holds the 500: claimable ({st5.get('say') or st5.get('leg')})")
    sp5 = c.post("/account/trade/spend", json={"chain": "doge", "redeem": m5.hex(), "txid": MAKER["lock"], "vout": 0, "kind": "claim", "leg": st5["leg"]}).json()
    d5 = decode(bytes.fromhex(sp5["raw"]))["vout"]
    want = b"arcd" + bytes(4) + (7).to_bytes(4, "big") + (500).to_bytes(8, "big")
    check(len(d5) == 2 and d5[0]["value"] == 0 and want.hex() in d5[0]["scriptPubKey"]["hex"] and d5[1]["scriptPubKey"]["hex"] == p2pkh_script(ADDR["doge"]).hex(),
          "the claim: the ledger's send of 500 of #7, the visitor last")
    sig5 = sign(ME_K["doge"][0], bytes.fromhex(sp5["sighash"]))
    r = c.post("/account/trade/spend/sign", json={"chain": "doge", "redeem": m5.hex(), "raw": sp5["raw"], "signatures": [sig5.hex()], "kind": "claim", "secret": s5.hex()})
    check(r.status_code == 200, f"the token's claim is sent ({r.json()})")
    # ---- a DRC-20 note bought: the note revealed onto the lock's first coin, claimed whole with its reserve
    ORDER.update(id="70ce70cf", give={"chain": "doge", "kind": "token20", "asset": "dogi", "amount": "1000"})
    MAKER["lock"] = None
    q6 = c.post("/account/swap/quote", json={"id": c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]}).json()
    check(q6.get("kind") == "token20" and q6["give"]["coin"] == "DOGI", f"a DRC-20's quote ({q6.get('give')})")
    # ---- an arcade token given for coins: the ledger's send to the lock's address, the fee in the token beside it
    app.state.site.set_setting("trade_cut", 1)
    ORDER.update(id="70ce70d0", give={"chain": "doge", "kind": "coin", "asset": "", "amount": str(2 * COIN)},
                 get={"chain": "peptest", "kind": "token", "asset": "7", "amount": "1000"})
    MAKER["lock"] = None
    qg = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]
    q7 = c.post("/account/swap/quote", json={"id": qg}).json()
    s7 = os.urandom(32); h7 = hashlib.sha256(s7).digest()
    r7 = htlc.redeem_script(h7, MK_K["test"][1], ME_K["test"][1], q7["taker_time"])
    w7 = c.post("/account/swap/lock", json={"id": qg, "hash": h7.hex(), "redeem": r7.hex()}).json()
    lock_outs = decode(bytes.fromhex(w7.get("offer", {}).get("raw", "00"))).get("vout", []) if w7.get("offer") else []
    fee_outs = decode(bytes.fromhex(w7.get("fee_offer", {}).get("raw", "00"))).get("vout", []) if w7.get("fee_offer") else []
    check(any(o["scriptPubKey"]["hex"] == htlc.p2sh_script(r7).hex() and o["value"] == 0.03 for o in lock_outs),
          f"the token lock pays the lock's address its 0.03 coin ({[o['value'] for o in lock_outs]})")
    check(any(o["scriptPubKey"]["hex"] == p2pkh_script(op_test).hex() for o in fee_outs), "the fee in the token goes to the operator, its own send")
    put_tx("test", "b3" * 32, [(3_000_000, htlc.p2sh_script(r7).hex())], conf=1)
    lk7 = c.post("/account/swap/locked", json={"id": qg, "txid": "b3" * 32}).json()
    check(lk7.get("leg", {}).get("kind") == "token" and lk7.get("vout") == 0, f"the lock found; its refund is a token's ({lk7.get('leg')})")
    # ---- an inscription sold to an offer: the visitor locks it (its coin first and whole, the reserve beside it); the fee
    # comes out of the coins it gets, at the claim
    INS2 = "c1" * 32 + "i0"
    INSCRIBED[INS2] = "c1" * 32 + ":0:0"
    OWNERS[INS2] = ADDR["test"]
    SELLING.update(txid="c1" * 32, vout=0, value=100_000, height=4)
    ORDER.update(id="5e115e11", give={"chain": "doge", "kind": "coin", "asset": "", "amount": str(4 * COIN)},
                 get={"chain": "peptest", "kind": "ord", "asset": INS2, "amount": "1"})
    MAKER["lock"] = None
    qs = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]
    q8 = c.post("/account/swap/quote", json={"id": qs}).json()
    check(q8.get("sell") and q8.get("inscription") == INS2 and q8.get("fee") == "0.04" and not q8.get("fee_with_lock"),
          f"a sale's quote: the fee 1% of the 4 DOGE it gets, at the claim ({q8.get('fee')}, {q8.get('fee_with_lock')})")
    s8 = os.urandom(32); h8 = hashlib.sha256(s8).digest()
    r8 = htlc.redeem_script(h8, MK_K["test"][1], ME_K["test"][1], q8["taker_time"])
    w8 = c.post("/account/swap/lock", json={"id": qs, "hash": h8.hex(), "redeem": r8.hex()}).json()
    tx8 = decode(bytes.fromhex(w8.get("offer", {}).get("raw", "00"))) if w8.get("offer") else {"vin": [], "vout": []}
    check(tx8["vin"] and tx8["vin"][0]["txid"] == "c1" * 32 and [(o["value"], o["scriptPubKey"]["hex"]) for o in tx8["vout"][:2]]
          == [(0.001, htlc.p2sh_script(r8).hex()), (0.02, htlc.p2sh_script(r8).hex())],
          f"the lock: the inscription's coin first and whole, the 0.02 reserve beside it ({[o['value'] for o in tx8['vout']]})")
    put_tx("test", "c2" * 32, [(100_000, htlc.p2sh_script(r8).hex()), (2_000_000, htlc.p2sh_script(r8).hex())], conf=1)
    lk8 = c.post("/account/swap/locked", json={"id": qs, "txid": "c2" * 32}).json()
    check(lk8.get("vout") == 0 and lk8.get("leg", {}).get("kind") == "doginal", f"found; its refund is an inscription's ({lk8})")
    c.post("/account/swap/step", json={"id": qs})
    MAKER["time"] = int(time.time()) + 4 * 3600
    m8 = htlc.redeem_script(h8, ME_K["doge"][1], MK_K["doge"][1], MAKER["time"], sized=True)
    MAKER["lock"] = "c3" * 32
    put_tx("doge", MAKER["lock"], [(4 * COIN + 1_000_000, htlc.p2sh_script(m8).hex())], conf=1)
    st8 = c.post("/account/swap/step", json={"id": qs}).json()
    check(st8.get("step") == "claimable" and st8["leg"].get("fee_value") == 4_000_000, f"the buyer's coins claimable, the fee at the claim ({st8.get('leg')})")
    sp8 = c.post("/account/trade/spend", json={"chain": "doge", "redeem": m8.hex(), "txid": MAKER["lock"], "vout": 0, "kind": "claim", "leg": st8["leg"]}).json()
    outs8 = [(o["value"], o["scriptPubKey"]["hex"]) for o in decode(bytes.fromhex(sp8["raw"]))["vout"]]
    check((0.04, p2pkh_script(op_doge).hex()) in outs8 and (3.96, p2pkh_script(ADDR["doge"]).hex()) in outs8,
          f"the claim pays the operator 0.04 and the seller 3.96 ({outs8})")
    # ---- a DRC-20 / PRC-20 token given: four offers (note 1, its reveal, the funding to the lock, note 2 onto the lock)
    ORDER.update(id="70ce70d1", give={"chain": "doge", "kind": "coin", "asset": "", "amount": str(2 * COIN)},
                 get={"chain": "peptest", "kind": "token20", "asset": "frgy", "amount": "1000"})
    MAKER["lock"] = None
    qn = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]
    q9 = c.post("/account/swap/quote", json={"id": qn}).json()
    check(q9.get("t20", {}).get("tick") == "frgy" and q9["t20"].get("fee") == 10, f"a token's quote: 1000 FRGY, the fee 10 whole tokens ({q9.get('t20')})")
    s9 = os.urandom(32); h9 = hashlib.sha256(s9).digest()
    r9 = htlc.redeem_script(h9, MK_K["test"][1], ME_K["test"][1], q9["taker_time"])
    w9 = c.post("/account/swap/lock", json={"id": qn, "hash": h9.hex(), "redeem": r9.hex()}).json()
    step = w9.get("lock20") or {}
    check(step.get("step") == "commit", f"the first of four: note 1's commit ({w9.get('detail') or step.get('step')})")
    pk = "02" + "11" * 32
    def sent(offer, txid, outs):
        put_tx("test", txid, outs, conf=0)
        app.state.offers.take(offer["offer"], pk).done(txid)
    if step.get("step") == "commit":
        cv = [o for o in decode(bytes.fromhex(step["raw"]))["vout"]]
        sent(step, "d1" * 32, [(round(o["value"] * COIN), o["scriptPubKey"]["hex"]) for o in cv])
        step = c.post("/account/swap/lock20", json={"id": qn}).json()
        check(step.get("step") == "reveal", f"then note 1's reveal ({step.get('detail') or step.get('step')})")
        sent(step, "d2" * 32, [(100_000, p2pkh_script(ADDR["test"]).hex())])
        step = c.post("/account/swap/lock20", json={"id": qn}).json()
        fv = decode(bytes.fromhex(step.get("raw", "00")))["vout"] if step.get("raw") else []
        check(step.get("step") == "fund" and fv and fv[0]["scriptPubKey"]["hex"] == htlc.p2sh_script(r9).hex() and fv[0]["value"] == 0.001,
              f"then the funding: note 1's coin to the lock first ({step.get('detail') or [o['value'] for o in fv]})")
        sent(step, "d3" * 32, [(round(o["value"] * COIN), o["scriptPubKey"]["hex"]) for o in fv])
        step = c.post("/account/swap/lock20", json={"id": qn}).json()
        lv = decode(bytes.fromhex(step.get("raw", "00")))["vout"] if step.get("raw") else []
        check(step.get("step") == "lock" and [(o["value"], o["scriptPubKey"]["hex"]) for o in lv] ==
              [(0.001, htlc.p2sh_script(r9).hex()), (0.02, htlc.p2sh_script(r9).hex())],
              f"then note 2 onto the lock, the reserve beside it ({step.get('detail') or [o['value'] for o in lv]})")
        sent(step, "d4" * 32, [(100_000, htlc.p2sh_script(r9).hex()), (2_000_000, htlc.p2sh_script(r9).hex())])
        done = c.post("/account/swap/lock20", json={"id": qn}).json()
        check(done.get("step") == "done" and done.get("lock_txid") == "d4" * 32 and done.get("leg", {}).get("kind") == "token20",
              f"locked; its refund is a note's ({done.get('leg')})")
    # ---- DigiDollars bought for coins (the stand-in chain plays DigiByte): the seller's Taproot lock checked, the claim
    # an offer carrying the secret, its dollars to the visitor's own DigiDollar address
    from arcadesite.ddswap import Lock as DDLock, RESERVE as DD_RESERVE, record as dd_record
    from arcade.ddlib import taproot as tr
    app.state.site.set_setting("trade_cut", 0)
    ORDER.update(id="dd00dd01", give={"chain": "doge", "kind": "dd", "asset": "DD", "amount": "2500"},
                 get={"chain": "peptest", "kind": "coin", "asset": "", "amount": str(COIN)})
    MAKER["lock"] = None
    qd = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]
    q10 = c.post("/account/swap/quote", json={"id": qd}).json()
    check((q10.get("dd") or {}).get("get") == 25, f"a DigiDollar buy's quote: $25 ({q10.get('dd')} {q10.get('detail')})")
    s10 = os.urandom(32); h10 = hashlib.sha256(s10).digest()
    r10 = htlc.redeem_script(h10, MK_K["test"][1], ME_K["test"][1], q10["taker_time"])
    c.post("/account/swap/lock", json={"id": qd, "hash": h10.hex(), "redeem": r10.hex()})
    put_tx("test", "e1" * 32, [(COIN + 1_000_000, htlc.p2sh_script(r10).hex())], conf=1)
    c.post("/account/swap/locked", json={"id": qd, "txid": "e1" * 32})
    c.post("/account/swap/step", json={"id": qd})
    MAKER["time"] = int(time.time()) + 4 * 3600
    dl = DDLock(h10, ME_K["doge"][1], MK_K["doge"][1], MAKER["time"], sized=True)
    MAKER["lock"] = "e2" * 32
    put_tx("doge", MAKER["lock"], [(0, dl.script().hex()), (DD_RESERVE, dl.script().hex()), (0, dd_record([2500]).hex())], conf=1)
    from arcade.ddlib import ddtx
    CH["doge"][MAKER["lock"]]["version"] = ddtx.SEND_VERSION
    st10 = c.post("/account/swap/step", json={"id": qd}).json()
    check(st10.get("step") == "claimable" and st10.get("dd"), f"the seller's DigiDollar lock checked: claimable ({st10.get('say') or st10.get('step')})")
    cl = c.post("/account/swap/ddclaim", json={"id": qd, "secret": s10.hex()}).json()
    d10 = decode(bytes.fromhex(cl.get("raw", "00"))) if cl.get("raw") else {"vin": [], "vout": []}
    mine_dd = tr.p2tr(tr.tweak(ME_K["doge"][1][1:])).hex()
    check(len(d10["vin"]) == 2 and d10["vout"] and d10["vout"][0]["scriptPubKey"]["hex"] == mine_dd
          and (cl.get("inputs") or [{}])[0].get("taproot", {}).get("extra") == [s10.hex()],
          f"the claim: both lock coins in, the dollars to the visitor's DigiDollar address, the secret carried ({cl.get('detail')})")
    # ---- DigiDollars bought, handed to this site: the seller locks the dollars to the one-time claim key, and this site
    # claims them by the claim leaf (a BIP-340 signature by that key), whole dollars to the visitor's own DD address
    from arcade import taproot as tap
    ORDER.update(id="dd00dd02", give={"chain": "doge", "kind": "dd", "asset": "DD", "amount": "2500"},
                 get={"chain": "peptest", "kind": "coin", "asset": "", "amount": str(COIN)})
    MAKER["lock"] = None
    qe = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]
    qE = c.post("/account/swap/quote", json={"id": qe}).json()
    sE = os.urandom(32); hE = hashlib.sha256(sE).digest()
    kE, pE = key()
    kE_hex = kE.private_numbers().private_value.to_bytes(32, "big").hex()
    rE = htlc.redeem_script(hE, MK_K["test"][1], ME_K["test"][1], qE["taker_time"])
    w = c.post("/account/swap/lock", json={"id": qe, "hash": hE.hex(), "redeem": rE.hex(), "claim_pub": pE.hex()}).json()
    check(w.get("amount") == "1.01", f"a handed DigiDollar buy: the lock named with a claim key ({w.get('detail') or w.get('amount')})")
    check(c.post("/account/swap/hand", json={"id": qe, "secret": sE.hex(), "claim_key": kE_hex}).status_code == 200, "handed over")
    put_tx("test", "e5" * 32, [(COIN + 1_000_000, htlc.p2sh_script(rE).hex())], conf=1)
    c.post("/account/swap/locked", json={"id": qe, "txid": "e5" * 32})
    app.state.swap_run_once()
    toldE = [a[1] for a in ASKED if a[0] == "told"][-1]
    check(toldE.get("taker_keys", {}).get("doge") == pE.hex(), "the seller told to lock the dollars to the claim key")
    MAKER["time"] = int(time.time()) + 4 * 3600
    dlE = DDLock(hE, pE, MK_K["doge"][1], MAKER["time"], sized=True)
    MAKER["lock"] = "e6" * 32
    put_tx("doge", MAKER["lock"], [(0, dlE.script().hex()), (DD_RESERVE, dlE.script().hex()), (0, dd_record([2500]).hex())], conf=1)
    CH["doge"][MAKER["lock"]]["version"] = ddtx.SEND_VERSION
    nE = len(SENT["doge"])
    app.state.swap_run_once()
    rowE = json.loads(sqlite3.connect(os.path.join(home, "site-swaps.sqlite")).execute("SELECT data FROM swap WHERE id=?", (qe,)).fetchone()[0])
    cE = SENT["doge"][-1][1] if len(SENT["doge"]) > nE else {"vin": [], "vout": []}
    check(rowE["step"] == "claimed", f"this site claimed the dollars ({rowE['step']}, {rowE.get('note')})")
    check(len(cE["vin"]) == 2 and cE["vout"] and cE["vout"][0]["scriptPubKey"]["hex"] == mine_dd,
          "both lock coins in; the dollars to the visitor's own DigiDollar address (not the claim key's)")
    check(cE["vout"] and cE["vout"][-1]["scriptPubKey"]["hex"] == dd_record([2500]).hex(),
          "the record says 2500 cents: $25, whole dollars, nothing split off")
    wit = (cE["vin"] or [{}])[0].get("txinwitness") or []
    check(len(wit) == 4 and wit[1] == sE.hex(), "the claim leaf's witness: signature, secret, leaf, control")
    if len(wit) == 4:
        from arcadesite.ddswap import spend as dd_spend
        u = dd_spend(dlE, {"txid": MAKER["lock"], "vout": 0}, 2500, {"txid": MAKER["lock"], "vout": 1, "value": DD_RESERVE},
                     bytes.fromhex(mine_dd), p2pkh_script(ADDR["doge"]), sE)
        check(tap.verify(pE[1:], bytes.fromhex(u.sighashes[0]), bytes.fromhex(wit[0])), "signed by the one-time claim key (BIP-340)")
    app.state.swap_run_once()
    check(len(SENT["doge"]) == nE + 1, "claimed only once")
    # ---- a take handed to this site: the tab closes once the lock is sent, and this site finishes the swap
    from arcade import escrow as escrowlib
    app.state.site.set_setting("trade_cut", 0)
    ORDER.update(id="cafe0001", give={"chain": "doge", "kind": "coin", "asset": "", "amount": str(5 * COIN)},
                 get={"chain": "peptest", "kind": "coin", "asset": "", "amount": str(COIN)})
    MAKER["lock"] = None
    qh = c.post("/r/market/swap_take" + v, json={"node": "ab" * 8, "order": ORDER["id"]}).json()["id"]
    qq = c.post("/account/swap/quote", json={"id": qh}).json()
    sh = os.urandom(32); hh = hashlib.sha256(sh).digest()
    ck, cpub = key()
    ck_hex = ck.private_numbers().private_value.to_bytes(32, "big").hex()
    rh = htlc.redeem_script(hh, MK_K["test"][1], ME_K["test"][1], qq["taker_time"])
    w = c.post("/account/swap/lock", json={"id": qh, "hash": hh.hex(), "redeem": rh.hex(), "claim_pub": cpub.hex()}).json()
    check(w.get("amount") == "1.01", f"a handed take: the lock named, with a one-time claim key ({w.get('detail') or w.get('amount')})")
    r = c.post("/account/swap/hand", json={"id": qh, "secret": "00" * 32, "claim_key": ck_hex})
    check(r.status_code == 400, "a hand-over with the wrong secret is refused")
    r = c.post("/account/swap/hand", json={"id": qh, "secret": sh.hex(), "claim_key": key()[0].private_numbers().private_value.to_bytes(32, "big").hex()})
    check(r.status_code == 400, "a hand-over with another key than the one named is refused")
    r = c.post("/account/swap/hand", json={"id": qh, "secret": sh.hex(), "claim_key": ck_hex})
    check(r.status_code == 200, f"the swap handed over ({r.json()})")
    # The tab closes before it says which transaction it sent: the runner finds the lock by its own address.
    lock_h = "a7" * 32
    put_tx("test", lock_h, [(COIN + 1_000_000, htlc.p2sh_script(rh).hex())], conf=0)
    LOCK_COINS[htlc.address_of(rh, NETWORKS["test"])] = [{"txid": lock_h, "vout": 0, "value": COIN + 1_000_000}]
    n_told = sum(1 for a in ASKED if a[0] == "told")
    app.state.swap_run_once()
    row = json.loads(sqlite3.connect(os.path.join(home, "site-swaps.sqlite")).execute("SELECT data FROM swap WHERE id=?", (qh,)).fetchone()[0])
    check(row["step"] == "locked" and row["lock_txid"] == lock_h, f"the runner found the lock the tab never reported ({row['step']})")
    check(sum(1 for a in ASKED if a[0] == "told") == n_told, "the maker is not told before that lock is mined")
    CH["test"][lock_h]["confirmations"] = 1
    app.state.swap_run_once()
    told = [a[1] for a in ASKED if a[0] == "told"][-1]
    check(told.get("taker_keys", {}).get("doge") == cpub.hex(), "the maker told, to lock to the one-time claim key")
    MAKER["time"] = int(time.time()) + 4 * 3600
    mh = htlc.redeem_script(hh, cpub, MK_K["doge"][1], MAKER["time"], sized=True)
    MAKER["lock"] = "a8" * 32
    put_tx("doge", MAKER["lock"], [(5 * COIN + 1_000_000, htlc.p2sh_script(mh).hex())], conf=1)
    sent_before = len(SENT["doge"])
    app.state.swap_run_once()
    row = json.loads(sqlite3.connect(os.path.join(home, "site-swaps.sqlite")).execute("SELECT data FROM swap WHERE id=?", (qh,)).fetchone()[0])
    claim = SENT["doge"][-1][1] if len(SENT["doge"]) > sent_before else {}
    check(row["step"] == "claimed" and row.get("claimed_by") == "site", f"the runner claimed the maker's lock ({row['step']}, {row.get('note')})")
    check(claim and secret_in(claim, sh) and [(o["value"], o["scriptPubKey"]["hex"]) for o in claim["vout"]]
          == [(5, p2pkh_script(ADDR["doge"]).hex())], "the claim carries the secret and pays the visitor's own address 5 DOGE")
    if claim:
        ins = [{"txid": x["txid"], "vout": x["vout"], "sequence": x["sequence"]} for x in claim["vin"]]
        outs = [(round(o["value"] * COIN), bytes.fromhex(o["scriptPubKey"]["hex"])) for o in claim["vout"]]
        digest = escrowlib.sighash(ins, outs, 0, mh, claim["locktime"])
        sig = first_push(bytes.fromhex(claim["vin"][0]["scriptSig"]["hex"]))
        check(app.state.signature_good(cpub, digest, sig), "signed by the one-time claim key")
    st = c.post("/account/swap/step", json={"id": qh}).json()
    check(st.get("step") == "claimed" and st.get("txid") == row.get("claim_txid"), "the tab, if it comes back, sees the claim")
    app.state.swap_run_once()
    check(len(SENT["doge"]) == sent_before + 1, "claimed only once")
    print("\nALL OK" if not fails else f"\n{len(fails)} FAILED")
    return not fails


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
