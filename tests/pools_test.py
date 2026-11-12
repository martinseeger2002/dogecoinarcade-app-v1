"""Prize pools and the referee (arcadesite/pools.py).

Offline: a whole site (wallet.py + pools.py) in front of a fake V1.0 whose chain is a dict of coins and whose decoder is
the original's own (arcade/rawchain.py). Real keys, the original's referee (its key file, its seeds, its judge run),
real transactions: a refereed self-paying claim, an unrefereed NFT claim with the claimer's coins, a pool opened, funded,
its legs built and closed, and an attested result -- every signature in the result checked against its sighash.
Live, read-only: against the dev V1.0 (site.json of $ARCADE_SITE_DEV (default ~/.arcade-site-dev), a scratch home so no referee.key is made
there). Nothing is signed or broadcast live.

    .venv/bin/python tests/pools_test.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import types

SRC = os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src"))
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arcade import fees
from arcade import funding as fundinglib
from arcade import inscriptions as inscriptionlib
from arcade import payload as P
from arcade import rawchain
from arcade import referee as refereelib
from arcade import txbuild
from arcade.config import NETWORKS
from arcade.encoding import encode_class_c
from arcade.script import b58check_encode, hash160, iter_pushes

from arcadesite import pools, wallet
from arcadesite.app import create_app
from arcadesite.chains import Chains
from arcadesite.state import SiteState
from arcadesite.v1 import V1Error

fails = []
# Every scratch home and key of this run under one directory, removed at the end.
import atexit
tempfile.tempdir = tempfile.mkdtemp(prefix="pools-test-")
atexit.register(shutil.rmtree, tempfile.tempdir, True)


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


# The judge runs as the original runs it: `python -m arcade.refjudge` in this interpreter, with quickjs==1.19.4. This
# venv may not have it (the site needs it installed); for the test the subprocess borrows a 3.11 copy if there is one.
try:
    import quickjs  # noqa: F401
    JUDGE_OK = True
except ImportError:
    borrowed = os.environ.get("QUICKJS_SITE_PACKAGES", "")
    JUDGE_OK = bool(borrowed) and os.path.isdir(os.path.join(borrowed, "quickjs"))
    if JUDGE_OK:
        os.environ["PYTHONPATH"] = borrowed + os.pathsep + os.environ.get("PYTHONPATH", "")
    print(f"note: quickjs is not in this venv; judge runs {'borrow ' + borrowed if JUDGE_OK else 'are SKIPPED'}")

PARAMS = NETWORKS["test"]
COIN = 100_000_000


def addr_of(pub: bytes) -> str:
    return b58check_encode(PARAMS.pubkeyhash_version, hash160(pub))


def key(name: str) -> refereelib.Referee:
    """A secp256k1 key that signs (the original's Referee is a key holder like any other)."""
    return refereelib.Referee(tempfile.mkdtemp(prefix=f"key-{name}-"))


def verify(pub: bytes, digest: bytes, sig_with_type: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    k = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), pub)
    try:
        k.verify(sig_with_type[:-1], digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return True
    except InvalidSignature:
        return False


class Resp:
    def __init__(self, status, content=b"", ctype=""):
        self.status_code, self.content, self.headers = status, content, {"content-type": ctype}


class FakeV1:
    """V1.0 as the site asks it, over a dict chain."""

    def __init__(self):
        self.insc = {}        # txid -> page row (/r/inscription shape)
        self.content = {}     # txid -> (type, bytes)
        self.coins = {}       # (txid, vout) -> (value, script)
        self.txs = {}         # txid -> raw hex
        self.tokens = {}      # pid -> {name, divisible}
        self.bal = {}         # address -> {pid: units}
        self.sent = []

    # -- the chain
    def add_tx(self, raw: str) -> str:
        t = rawchain.read_tx(bytes.fromhex(raw))
        self.txs[t.txid] = raw
        for n, (v, s) in enumerate(t.vout):
            self.coins[(t.txid, n)] = (v, s)
        return t.txid

    def inscribe(self, txid, number, creator, owner, ctype, js=None, content=b"", collection=None):
        self.insc[txid] = {"id": txid, "number": number, "creator": creator, "owner": owner, "contenttype": ctype,
                           "json": js, "rawjson": json.dumps(js) if js is not None else "", "collection": collection,
                           "edition": None, "block": 100 + number}
        self.content[txid] = (ctype, content)

    def _row(self, key):
        key = str(key).lower()
        if key in self.insc:
            return self.insc[key]
        return next((r for r in self.insc.values() if str(r["number"]) == key), None)

    # -- the four ways
    def page(self, path, params=None):
        parts = path.strip("/").split("/")
        if parts[0] == "inscription":
            r = self._row(parts[1])
            return (200, r) if r else (404, {"error": "no such inscription"})
        if parts[0] == "prizepool":
            r = self._row(parts[1])
            if not r or not str(r["contenttype"]).startswith(pools.POOL_TYPE):
                return 404, {"error": "no such prize pool"}
            gone = any((x.get("json") or {}).get("prizepool_delete", {}).get("pool") == r["id"]
                       for x in self.insc.values() if isinstance(x.get("json"), dict))
            return 200, {"txid": r["id"], "deleted": gone}
        if parts[0] == "claimpools":
            page = self._row(parts[1])
            if not page:
                return 404, {"error": "no such inscription"}
            out = [{"txid": r["id"], "deleted": False} for r in self.insc.values()
                   if isinstance(r.get("json"), dict) and (r["json"].get("prizepool") or {}).get("game") == page["id"]]
            return 200, {"inscription": page["id"], "pools": out}
        if parts[0] == "token":
            t = self.tokens.get(int(parts[1]))
            return (200, {"propertyid": int(parts[1]), **t}) if t else (404, {"error": "no such token"})
        if parts[0] == "balances":
            return 200, [{"propertyid": pid, "units": u, "leaving": 0} for pid, u in self.bal.get(parts[1], {}).items()]
        if parts[0] == "address":
            return 200, {"address": parts[1], "tag": None}
        if parts[0] == "verified":
            return 200, {"results": [], "next": None}
        if parts[0] == "inscriptions":
            return 200, []
        return 404, {"error": "not a route"}

    def cmd(self, name, _timeout=None, **p):
        if name == "chains.list":
            return [{"key": "peptest", "installed": True, "indexes": ["addresses"]}]
        if name == "tx.coins":
            out = {}
            for a in p["addresses"]:
                script = txbuild.p2pkh_script(a)
                out[a] = {"confirmed": [{"txid": t, "vout": n, "value": v, "height": 1}
                                        for (t, n), (v, s) in self.coins.items() if s == script],
                          "unconfirmed": [], "held_back": []}
            return {"addresses": out}
        if name == "tx.broadcast":
            t = rawchain.read_tx(bytes.fromhex(p["hex"]))
            for i in t.vin:
                if (i["txid"], i["vout"]) not in self.coins:
                    raise V1Error("spends a coin this arcade does not know")
            for i in t.vin:
                del self.coins[(i["txid"], i["vout"])]
            self.add_tx(p["hex"])
            self.sent.append(p["hex"])
            return {"txid": t.txid}
        if name == "profile":
            return {}
        raise V1Error(f"fake: no {name}")

    def core(self, chain, method, *params, timeout=None):
        if method in ("decoderawtransaction", "getrawtransaction"):
            raw = params[0] if method == "decoderawtransaction" else self.txs.get(params[0])
            if raw is None:
                raise V1Error("No such mempool or blockchain transaction")
            v = rawchain.read_tx(bytes.fromhex(raw)).verbose(PARAMS)
            for i in v["vin"]:
                i["scriptSig"].pop("asm", None)          # V1.0 gives the hex only
            return v
        if method == "gettxout":
            c = self.coins.get((params[0], int(params[1])))
            return None if c is None else {"value": c[0] / COIN, "scriptPubKey": {"hex": c[1].hex()}}
        if method == "getrawmempool":
            return []
        if method == "getmempoolentry":
            raise V1Error("Transaction not in mempool")
        raise V1Error(f"fake core: no {method}")

    def raw(self, method, path, **kw):
        if path.startswith("/content/"):
            c = self.content.get(path.split("/")[2])
            return Resp(200, c[1], c[0]) if c else Resp(404)
        return Resp(404)

    def tag_owner(self, tag):
        return ""


# ================================================================ offline
home = tempfile.mkdtemp(prefix="pools-")
# The referee key at migration: the original's referee.key copied into the site home, used as it is.
secret = 0x1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF
with open(os.path.join(home, "referee.key"), "w") as f:
    f.write(f"{secret:064x}\n")
fake = FakeV1()
st = SiteState(home, fake)
chains = Chains(st, fake)
app = FastAPI()
app.state.site, app.state.chains = st, chains
who = {"account": None}
app.state.signed_in = lambda request: who["account"]
app.state.signed_in_account = lambda request: who["account"]
wallet.register(app, st, chains)
pools.register(app, st, chains)
client = TestClient(app)
REF = app.state.pools["referee"]

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
want = ec.derive_private_key(secret, ec.SECP256K1()).public_key().public_bytes(Encoding.X962,
                                                                              PublicFormat.CompressedPoint).hex()
r = client.get("/r/referee").json()
check(r["pubkey"] == want and r["engine"] == refereelib.ENGINE and r["state_publisher"] == ""
      and r["judged_per_minute"] == refereelib.JUDGED_PER_MINUTE, "/r/referee: the key file's key, the original's caps")

creator_k, claimer_k, pool_k, other_k = key("creator"), key("claimer"), key("pool"), key("other")
CREATOR, CLAIMER, OTHER = addr_of(creator_k.pubkey), addr_of(claimer_k.pubkey), addr_of(other_k.pubkey)
creator_acct = types.SimpleNamespace(pubkey="02" + "aa" * 32)
claimer_acct = types.SimpleNamespace(pubkey="02" + "bb" * 32)
st.set_setting(f"address:{creator_acct.pubkey}", CREATOR)
st.set_setting(f"address:{claimer_acct.pubkey}", CLAIMER)

# A judge: a win is a replay of three moves.
JUDGE = "11" * 32
fake.inscribe(JUDGE, 1, CREATOR, CREATOR, "application/javascript",
              content=b"function judge(seed, inputs, params){ return {won: inputs.length === 3, score: inputs.length}; }")
fake.tokens[7] = {"name": "Ghost Credits", "divisible": False}

# --- A: a refereed token pool that pays its own claims ----------------------------------------------------------
redeem = refereelib.redeem_script(pool_k.pubkey, REF.pubkey, creator_k.pubkey)
POOL_A = refereelib.address_of(redeem, PARAMS)
FUND_A = "22" * 32
for v in range(1, 5):
    fake.coins[(FUND_A, v)] = (pools.SPLIT_EACH, txbuild.p2pkh_script(POOL_A))
fake.bal[POOL_A] = {7: 1000}
PHRASE = "open sesame"
spec_a = {"v": 1, "creator": CREATOR, "pool": POOL_A, "index": 5, "property_id": 7, "lot": 500, "count": 2,
          "price": 0, "each": pools.SPLIT_EACH, "fund": FUND_A, "game": "", "once": False,
          "claim_hash": hashlib.sha256(PHRASE.encode()).hexdigest(), "pubkey": pool_k.pubkey.hex(), "fee": "pool",
          "creator_pubkey": creator_k.pubkey.hex(),
          "referee": {"node": "https://app.dogecoinarcade.com", "pubkey": REF.pubkey.hex(), "judge": JUDGE,
                      "require": {"won": True}, "params": {}, "seed_hours": 1}}
PA = "33" * 32
fake.inscribe(PA, 2, CREATOR, CREATOR, pools.POOL_TYPE, js={"name": "Vault", "prizepool": spec_a})
chain = chains.account_chain()
pa = app.state.pools["chain_pool"](chain, PA)
check(pa is not None and pa["redeem"] == redeem.hex() and pa["fee"] == "pool" and pa["referee"]["seed_hours"] == 1,
      "a refereed pool read from its inscription, two-key address checked")
bad = dict(spec_a, pool=CREATOR)
fake.inscribe("34" * 32, 3, CREATOR, CREATOR, pools.POOL_TYPE, js={"prizepool": bad})
check(app.state.pools["chain_pool"](chain, "34" * 32) is None, "a pool whose address is not its keys' is no pool")
check(app.state.pools["chain_pool"](chain, "2") is not None, "a pool by its number")


def lot_sigs(pool, n, k):
    leg = app.state.pools["pool_leg"](chain, pool, n)
    return [k.sign(bytes.fromhex(h), fundinglib.SINGLE_ANYONECANPAY).hex() for h in leg.sighashes]


who["account"] = claimer_acct
r = client.post("/account/prize/take", json={"pool": PA, "lot": 0, "secret": "wrong", "signatures": lot_sigs(pa, 0, pool_k)})
check(r.status_code == 400 and "phrase" in r.json()["detail"], f"take: a wrong phrase refused ({r.json()})")
seed = client.post("/account/referee/seed", json={"pool": PA}).json()
check(len(seed.get("seed", "")) == 64 and seed["expires"] > 0, "seed: issued here (this site is the pool's referee)")
if JUDGE_OK:
    r = client.post("/account/prize/take", json={"pool": PA, "lot": 0, "secret": PHRASE, "signatures": lot_sigs(pa, 0, pool_k),
                                                 "replay": {"seed": seed["seed"], "inputs": [1]}})
    check(r.status_code == 400 and r.json()["detail"] == "the replay did not win", f"take: a losing replay refused ({r.json()})")
    # Somebody else's seed: the claim would pay this wallet, the seed's wallet played.
    stolen = client.post("/r/referee/seed", json={"pool": PA, "address": OTHER}).json()
    r = client.post("/account/prize/take", json={"pool": PA, "lot": 0, "secret": PHRASE, "signatures": lot_sigs(pa, 0, pool_k),
                                                 "replay": {"seed": stolen["seed"], "inputs": [1, 2, 3]}})
    check(r.status_code == 400 and "other than the wallet that played" in r.json()["detail"],
          f"take: a stolen winning seed pays nobody ({r.json()})")
    r = client.post("/account/prize/take", json={"pool": PA, "lot": 0, "secret": PHRASE, "signatures": lot_sigs(pa, 0, pool_k),
                                                 "replay": {"seed": seed["seed"], "inputs": [1, 2, 3]}})
    check(r.status_code == 200 and r.json().get("txid"), f"take: a winning replay claims the lot ({r.json()})")
    t = rawchain.read_tx(bytes.fromhex(fake.sent[-1]))
    ins = [{"txid": i["txid"], "vout": i["vout"]} for i in t.vin]
    outs = list(t.vout)
    ok = [(i["txid"], i["vout"]) for i in t.vin] == [(FUND_A, 1), (FUND_A, 2)] \
        and outs[-1] == (fees.DUST_LIMIT, txbuild.p2pkh_script(CLAIMER))
    for n, i in enumerate(t.vin):
        pushes = iter_pushes(i["script"])
        # claim_script_sig: the referee's signature, the pool's, OP_1, the redeem script.
        # The pool's SINGLE|ANYONECANPAY signature was made over the leg (its own input, its own output n) and holds
        # in the finished claim: the same digest, read from the claim's input n and output n alone.
        ok = ok and len(pushes) == 3 and pushes[2] == redeem \
            and verify(pool_k.pubkey, fundinglib.sighash(ins, outs, n, redeem,
                                                         sighash_type=fundinglib.SINGLE_ANYONECANPAY), pushes[1]) \
            and verify(REF.pubkey, fundinglib.sighash(ins, outs, n, redeem), pushes[0])
    check(ok, "take: the claim spends the lot's two coins, pays the claimer last, pool+referee signatures verify")
    r = client.post("/account/prize/take", json={"pool": PA, "lot": 1, "secret": PHRASE, "signatures": lot_sigs(pa, 1, pool_k),
                                                 "replay": {"seed": seed["seed"], "inputs": [1, 2, 3]}})
    check(r.status_code == 400 and "seed was used" in r.json()["detail"], f"take: a seed pays once ({r.json()})")
    r = client.post("/account/prize/take", json={"pool": PA, "lot": 0, "secret": PHRASE, "signatures": lot_sigs(pa, 0, pool_k),
                                                 "replay": {"seed": seed["seed"], "inputs": [1, 2, 3]}})
    check(r.status_code == 400, f"take: a claimed lot is gone ({r.json()['detail'][:70]})")
    # /r/referee/sign from another node, for lot 1 with a fresh seed: signatures over the claim it was handed.
    seed2 = client.post("/r/referee/seed", json={"pool": PA, "address": CLAIMER}).json()
    leg1 = app.state.pools["pool_leg"](chain, pa, 1)
    outs1 = list(leg1.outputs) + [(fees.DUST_LIMIT, txbuild.p2pkh_script(CLAIMER))]
    raw1 = txbuild.build_raw_tx([(c["txid"], c["vout"]) for c in leg1.inputs], outs1)
    r = client.post("/r/referee/sign", json={"pool": PA, "lot": 1, "raw": raw1,
                                             "replay": {"seed": seed2["seed"], "inputs": [1, 2, 3]}}).json()
    check(len(r.get("signatures", [])) == 2 and r["verdict"]["won"] is True
          and verify(REF.pubkey, fundinglib.sighash([{"txid": c["txid"], "vout": c["vout"]} for c in leg1.inputs],
                                                    outs1, 0, redeem), bytes.fromhex(r["signatures"][0])),
          "/r/referee/sign: the referee's ALL signatures over a winning claim")
    seed3 = client.post("/r/referee/seed", json={"pool": PA, "address": CLAIMER}).json()
    r = client.post("/r/referee/sign", json={"pool": PA, "lot": 1, "raw": raw1.replace(txbuild.p2pkh_script(CLAIMER).hex(),
                                                                                         txbuild.p2pkh_script(OTHER).hex()),
                                             "replay": {"seed": seed3["seed"], "inputs": [1, 2, 3]}})
    check(r.status_code == 400 and "other than the wallet that played" in r.json()["detail"],
          "/r/referee/sign: a claim steered elsewhere refused")

# --- B: an unrefereed NFT pool, claimed with the claimer's own coins -------------------------------------------
POOL_B = addr_of(pool_k.pubkey)
FUND_B, PIECE = "44" * 32, "55" * 32
fake.coins[(FUND_B, 0)] = (pools.SPLIT_EACH, txbuild.p2pkh_script(POOL_B))
fake.coins[(FUND_B, 1)] = (pools.SPLIT_EACH, txbuild.p2pkh_script(POOL_B))
fake.inscribe(PIECE, 4, CREATOR, POOL_B, "image/png", js={"name": "Golden Frog"})
fake.coins[("66" * 32, 0)] = (5 * COIN, txbuild.p2pkh_script(CLAIMER))
spec_b = {"v": 1, "kind": "nft", "creator": CREATOR, "pool": POOL_B, "index": 6, "pieces": [PIECE], "first": 0,
          "count": 1, "price": COIN // 10, "each": pools.SPLIT_EACH, "fund": FUND_B, "game": "", "once": True,
          "claim_hash": hashlib.sha256(b"frog").hexdigest(), "pubkey": pool_k.pubkey.hex()}
PB = "77" * 32
fake.inscribe(PB, 5, CREATOR, CREATOR, pools.POOL_TYPE, js={"name": "Frog pool", "prizepool": spec_b})
pb = app.state.pools["chain_pool"](chain, PB)
sigs_b = lot_sigs(pb, 0, pool_k)
r = client.post("/account/prize", json={"pool": PB, "lot": 0, "secret": "frog", "signatures": sigs_b}).json()
shown = r
check(shown.get("piece") == "Golden Frog (#4)" and shown.get("kind") == "nft" and shown.get("price") == COIN // 10
      and [(i["txid"], i["vout"]) for i in shown["inputs"][:2]] == [(FUND_B, 0), (FUND_B, 1)],
      f"prize: the claim shown, the lot's two coins first ({str(shown)[:120]})")
claimer_sigs = [claimer_k.sign(bytes.fromhex(h)).hex() for h in shown["sighashes"]]
r = client.post("/account/prize/sign", json={"pool": PB, "lot": 0, "secret": "frog", "signatures": sigs_b,
                                             "raw": shown["raw"], "pubkey": claimer_k.pubkey.hex(),
                                             "claimer": claimer_sigs})
check(r.status_code == 200 and r.json().get("txid"), f"prize/sign: pasted and broadcast ({r.json()})")
if r.status_code == 200:
    t = rawchain.read_tx(bytes.fromhex(fake.sent[-1]))
    check(t.vout[0][1] == txbuild.op_return_script(pb and app.state.pools["pool_leg"](chain, pb, 0).payload)
          and len(t.vin) >= 3, "prize/sign: the swap payload at output 0, the claimer's coins behind the lot")
r = client.post("/account/prize", json={"pool": PB, "lot": 0, "secret": "frog", "signatures": sigs_b})
check(r.status_code == 400, f"prize: a claimed lot cannot be claimed again ({r.json()['detail'][:60]})")

# --- C: a pool opened, funded, its legs built, closed (the creator's side) -------------------------------------
who["account"] = creator_acct
fake.coins[("88" * 32, 0)] = (10 * COIN, txbuild.p2pkh_script(CREATOR))
fake.bal[CREATOR] = {7: 1000}
pool2 = key("pool2")
r = client.post("/account/pools/open", json={"index": 9, "pubkey": pool2.pubkey.hex(),
                                              "referee": {"judge": JUDGE, "require": {"won": True}},
                                              "creator_pubkey": creator_k.pubkey.hex()}).json()
red2 = refereelib.redeem_script(pool2.pubkey, REF.pubkey, creator_k.pubkey)
check(r.get("referee") is True and r["address"] == refereelib.address_of(red2, PARAMS),
      f"pools/open: a refereed pool at its two-key address ({r})")
P2 = r["address"]
r = client.post("/account/pools/open", json={"index": 9, "pubkey": key("x").pubkey.hex()})
check(r.status_code == 400 and "already has a pool" in r.json()["detail"], "pools/open: one pool per index")
r = client.post("/account/pools/fund", json={"pool": P2, "property_id": 7, "lot": "100", "count": 3, "fee": "pool",
                                              "claim_hash": "ab" * 32}).json()
naming = encode_class_c(P.SimpleSend(property_id=7, amount=300).encode())
outs = rawchain.read_tx(bytes.fromhex(r["raw"])).vout if r.get("raw") else []
check(r.get("offer") and outs[0][1] == txbuild.op_return_script(naming)
      and [o for o in outs[1:7]] == [(pools.SPLIT_EACH, txbuild.p2pkh_script(P2))] * 6,
      "pools/fund: one send, the tokens named, six coins at the pool")
fund_txid = fake.add_tx(r["raw"])
r = client.post("/account/pools/legs", json={"pool": P2, "fund": fund_txid}).json()
pz = r.get("prizepool") or {}
check(len(r.get("legs", [])) == 3 and pz.get("fee") == "pool" and pz.get("referee", {}).get("pubkey") == REF.pubkey.hex()
      and pz.get("creator_pubkey") == creator_k.pubkey.hex() and pz.get("count") == 3
      and r["legs"][0]["redeem"] == red2.hex(), "pools/legs: three lots and the pool's inscription JSON")
fake.bal[P2] = {7: 300}
r = client.post("/account/pools/close", json={"pool": P2}).json()
check(r.get("offer") and r.get("creator_signs") is True and len(r["inputs"]) == 6
      and all(i.get("redeem") == red2.hex() for i in r["inputs"]),
      "pools/close: every coin back, signed by the creator's key (ELSE branch)")
cp = client.get("/account/claimpools").json()
check(cp["pools"] == [] and cp["next_index"] == 10 and cp["setup"] and cp["setup"][0]["closing"] is True,
      f"claimpools: setup rows, next index ({cp})")

# --- D: an attested result (another player: the claimer used its three judged replays this minute) --------------
player_k = key("player")
PLAYER = addr_of(player_k.pubkey)
player_acct = types.SimpleNamespace(pubkey="02" + "dd" * 32)
st.set_setting(f"address:{player_acct.pubkey}", PLAYER)
who["account"] = player_acct
if JUDGE_OK:
    s = client.post("/account/referee/seed", json={"judge": JUDGE}).json()
    content = hashlib.sha256(b"my score").hexdigest()
    r = client.post("/account/referee/attest", json={"judge": JUDGE, "seed": s["seed"], "inputs": [1, 2, 3],
                                                     "content": content}).json()
    att = r.get("attested") or {}
    check(att.get("result", {}).get("won") is True
          and refereelib.verify_attestation(att, content=content, address=PLAYER),
          "attest: a signed verdict any node can check")
    r = client.post("/r/referee/attest", json={"judge": JUDGE, "seed": s["seed"], "inputs": [1, 2, 3],
                                               "address": PLAYER, "content": content})
    check(r.status_code == 400 and "used" in r.json()["detail"], "attest: once per seed")

# ================================================================ live, read-only (dev V1.0)
print("--- live (dev V1.0)")
lhome = tempfile.mkdtemp(prefix="pools-live-")
shutil.copy(os.path.join(os.path.expanduser(os.environ.get("ARCADE_SITE_DEV", "~/.arcade-site-dev")), "site.json"), lhome + "/site.json")
try:
    lapp = create_app(lhome)
except Exception as exc:                                    # noqa: BLE001
    lapp = None
    check(False, f"create_app: {exc}")
if lapp is not None:
    if not any(getattr(r, "path", "") == "/r/referee" for r in lapp.router.routes):
        pools.register(lapp, lapp.state.site, lapp.state.chains)
        # The integrator registers pools before apps.py's /r/{rest} reader; here, put ours first.
        ours = [r for r in lapp.router.routes if getattr(r, "path", "").startswith(("/r/referee", "/r/verified"))]
        lapp.router.routes = ours + [r for r in lapp.router.routes if r not in ours]
    lc = TestClient(lapp)
    rr = lc.get("/r/referee")
    check(rr.status_code == 200 and len(rr.json()["pubkey"]) == 66, f"live /r/referee ({rr.status_code})")
    lchain = lapp.state.chains.account_chain()
    cp_ = lapp.state.pools["chain_pool"]
    d = cp_(lchain, "7071")
    a = cp_(lchain, "7066")
    check(d is not None and d["referee"] and d["redeem"] and d["fee"] == "pool" and d["kind"] == "nft",
          f"live: test prize D read as a refereed NFT pool ({d and d['pool']})")
    check(a is not None and a["referee"] is None and a["price"] == 10_000_000, "live: test prize A, unrefereed")
    r = lc.post("/r/referee/seed", json={"pool": "7071", "address": "ngqtCMKMFHQGqEdQJ1sXYF7uHkouup59cQ"})
    check(r.status_code == 400 and r.json()["detail"] == "this node is not that pool's referee",
          f"live: a pool refereed elsewhere is not seeded here ({r.json()})")
    r = lc.post("/r/referee/seed", json={"judge": "7069", "address": "ngqtCMKMFHQGqEdQJ1sXYF7uHkouup59cQ"})
    check(r.status_code == 400 and "not JavaScript" in r.json()["detail"], f"live: a judge must be JavaScript ({r.json()})")
    r = lc.post("/r/referee/seed", json={"pool": "7066", "address": "ngqtCMKMFHQGqEdQJ1sXYF7uHkouup59cQ"})
    check(r.status_code == 400 and r.json()["detail"] == "this pool has no referee", "live: an unrefereed pool has none")
    acct = types.SimpleNamespace(pubkey="02" + "cc" * 32)
    lapp.state.signed_in = lambda request: acct
    lapp.state.signed_in_account = lambda request: acct
    lapp.state.chains.account_address = lambda pubkey, chain: "ngaWgK7LWYfkSPgSN2zARCyyEQypJxaRWY"
    r = lc.post("/account/prize", json={"pool": "7066", "lot": 0})
    check(r.status_code == 400 and r.json()["detail"] == "this prize opens only with its phrase",
          f"live: /account/prize asks for the phrase ({r.json()})")
    r = lc.get("/account/claimpools")
    check(r.status_code == 200 and r.json()["pools"] == [], "live: /account/claimpools answers")
    r = lc.get("/r/verified/7069")
    check(r.status_code in (200, 404), f"live: /r/verified passes through ({r.status_code} {str(r.json())[:60]})")

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
