"""The Creator (arcadesite/creator.py).

Offline (no V1.0: its answers faked): every builder's offer decodes back through arcade's own readers -- a one-transaction
inscription, a split and all its pieces reassembled, a collection run's piece, a token issuance and grant, an ord's
commit and reveals read as the chain's indexer reads them, the operator's fee on mainnet -- and every page renders.

Live (--live): against the dev home's V1.0 ($ARCADE_SITE_DEV (default ~/.arcade-site-dev)/site.json), in a temporary home: pages render, offers
are built from a funded testnet address's real coins (never signed). --broadcast adds a throwaway account (sign-up and
welcome coins, as live_step1.py) that inscribes a tiny file on Pepecoin testnet and an ord (commit + reveal), each
signed in this test with a key it made.

    .venv/bin/python tests/creator_test.py [--live] [--broadcast]
"""
import base64, hashlib, json, os, secrets, sys, tempfile, time, types
sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from arcade import inscriptions as I, payload as P, tokens as tokenlib, ord_rules, ord_inscribe as oi
from arcade.config import NETWORKS
from arcade.encoding import decode_class_b, decode_class_c
from arcade.script import b58check_encode, hash160
from arcade.txbuild import p2pkh_script
from arcadesite.app import create_app
from arcadesite import creator
from arcadesite.v1 import V1Error

ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
fails = []
def check(ok, m): print(("ok   " if ok else "FAIL ") + m); ok or fails.append(m)
def coin_sign(priv, digest):
    r, s = utils.decode_dss_signature(priv.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256()))))
    return utils.encode_dss_signature(r, min(s, ORDER - s))
def new_key():
    priv = ec.derive_private_key(int.from_bytes(secrets.token_bytes(32), "big") % (ORDER - 1) + 1, ec.SECP256K1())
    return priv, priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)

def pushes(script: bytes) -> list:
    out, i = [], 0
    while i < len(script):
        op = script[i]; i += 1
        if 1 <= op <= 75:
            out.append(script[i:i + op]); i += op
        elif op == 0x4c:
            n = script[i]; out.append(script[i + 1:i + 1 + n]); i += 1 + n
        elif op == 0x4d:
            n = int.from_bytes(script[i:i + 2], "little"); out.append(script[i + 2:i + 2 + n]); i += 2 + n
    return out

def payload_of(off: dict, sender: str) -> bytes:
    """The arcade payload an offer carries, read back the way the chain's reader reads it."""
    outs = outputs_of(bytes.fromhex(off["raw"]))
    for s in outs:
        if s[:1] == b"\x6a":
            return decode_class_c(pushes(s[1:])[0])
    keys = [pushes(s) for s in outs if s[-1:] == b"\xae"]
    return decode_class_b(sender, keys)

def outputs_of(raw: bytes) -> list:
    """The output scripts of a legacy transaction."""
    def varint(b, i):
        n = b[i]
        if n < 0xfd: return n, i + 1
        if n == 0xfd: return int.from_bytes(b[i + 1:i + 3], "little"), i + 3
        return int.from_bytes(b[i + 1:i + 5], "little"), i + 5
    i = 4
    n, i = varint(raw, i)
    for _ in range(n):
        i += 36; ln, i = varint(raw, i); i += ln + 4
    n, i = varint(raw, i)
    out = []
    for _ in range(n):
        i += 8; ln, i = varint(raw, i); out.append(raw[i:i + ln]); i += ln
    return out

def values_of(raw: bytes) -> list:
    def varint(b, i):
        n = b[i]
        if n < 0xfd: return n, i + 1
        if n == 0xfd: return int.from_bytes(b[i + 1:i + 3], "little"), i + 3
        return int.from_bytes(b[i + 1:i + 5], "little"), i + 5
    i = 4
    n, i = varint(raw, i)
    for _ in range(n):
        i += 36; ln, i = varint(raw, i); i += ln + 4
    n, i = varint(raw, i)
    out = []
    for _ in range(n):
        v = int.from_bytes(raw[i:i + 8], "little"); i += 8; ln, i = varint(raw, i); out.append((v, raw[i:i + ln])); i += ln
    return out

def chunk_of(off, sender):
    msg = P.decode(payload_of(off, sender))
    return I.parse(msg.data)


# ======================================================================== offline
def offline():
    print("--- offline (V1.0 faked)")
    home = tempfile.mkdtemp(prefix="creator-")
    json.dump({"v1": "http://127.0.0.1:9", "v1_home": home, "public_hosts": ["testserver"]},
              open(home + "/site.json", "w"))
    priv, pub = new_key()
    pk = secrets.token_hex(32)
    test_addr = b58check_encode(NETWORKS["test"].pubkeyhash_version, hash160(pub))
    main_addr = b58check_encode(NETWORKS["main"].pubkeyhash_version, hash160(pub))
    op_pk = secrets.token_hex(32)
    _, op_pub = new_key()
    op_main = b58check_encode(NETWORKS["main"].pubkeyhash_version, hash160(op_pub))
    friend = b58check_encode(NETWORKS["test"].pubkeyhash_version, hash160(new_key()[1]))
    json.dump({"address:" + pk: test_addr, "coinkey:" + pk: pub.hex(), "address:main:" + pk: main_addr,
               "coinkey:main:" + pk: pub.hex(), "operator": op_pk, "address:main:" + op_pk: op_main, "unlimited_accounts": [test_addr]},
              open(home + "/settings.json", "w"))
    coins = lambda: [{"txid": hashlib.sha256(bytes([n])).hexdigest(), "vout": 0, "value": 50 * 10 ** 8, "height": 10}
                     for n in range(4)]

    def cmd(_command, **p):
        name = _command
        if name == "chains.list":
            return [{"key": k, "installed": True, "indexes": ["addresses", "ord", "names"]} for k in ("peptest", "pep")]
        if name == "tx.coins":
            return {"addresses": {a: {"confirmed": coins(), "unconfirmed": [], "held_back": []} for a in p["addresses"]}}
        if name == "explorer.ledger_tokens":
            return {"items": [{"id": 3, "name": "Arcade"}]}
        if name == "explorer.ledger_token":
            if int(p["id"]) == 7:
                return {"id": 7, "name": "Points", "type": 1, "managed": 1, "issuer": test_addr}
            raise V1Error("no arcade token")
        if name == "explorer.address":
            return {"ledger_tokens": [{"propertyid": 7, "units": 100}]}
        if name == "explorer.name":
            raise V1Error(f"{p['name']} is not claimed on Pepecoin testnet")
        if name == "ord":
            return {"indexed_to": 1000}
        if name in ("profile", "msg.who"):
            return {}
        raise V1Error(f"faked: {name}")

    def page(path, params=None, timeout=None):
        if path.startswith("address/"):
            return 200, {}
        return 404, None

    def core(chain, method, *params, timeout=None):
        if method == "getrawtransaction":
            return {"confirmations": 1, "vout": []}
        if method == "gettxout":
            return {"value": 1}
        if method == "getblockcount":
            return 1000
        if method == "getrawmempool":
            return []
        if method == "getblockhash":
            return "00" * 32
        if method == "getblock":
            return {"tx": [], "height": params and 1000}
        raise V1Error("Transaction not in mempool")
    from arcadesite.v1 import V1
    V1.cmd = lambda self, _command, _timeout=None, **p: cmd(_command, **p)
    V1.page = lambda self, path, params=None, timeout=None: page(path, params)
    V1.core = lambda self, chain, method, *params, timeout=None: core(chain, method, *params)
    app = create_app(home)
    creator.register(app, app.state.site, app.state.chains)
    account = types.SimpleNamespace(pubkey=pk)
    app.state.signed_in = lambda request: account
    app.state.signed_in_account = lambda request: account
    c = TestClient(app)
    offers = app.state.offers

    # one transaction
    content = b"hello creator " * 3
    r = c.post("/account/inscribe", json={"content": base64.b64encode(content).decode(), "content_type": "text/plain",
                                          "name": "hello.txt"})
    off = r.json()
    check(r.status_code == 200 and off.get("chunks") == 1, f"/account/inscribe one file: an offer {str(off)[:100]}")
    ch = chunk_of(off, test_addr)
    asm = I.Assembly(inscription_id=ch.inscription_id); asm.add(ch)
    man, got = asm.join()
    check(got == content and man.content_type == "text/plain", "its payload reads back as the same inscription")
    r = c.post("/account/inscribe", json={"content": base64.b64encode(content).decode(), "content_type": "text/plain"})
    check(r.status_code == 200, "asked again: offered again (the pile remembers it, no second charge)")

    # a split and its pieces
    big = secrets.token_bytes(20000)
    r = c.post("/account/inscribe", json={"content": base64.b64encode(big).decode(), "content_type": "image/png",
                                          "name": "big.png"})
    off = r.json()
    check(r.status_code == 200 and off.get("split") == "offered" and off["chunks"] > 1,
          f"a big file: a split into {off.get('chunks')} outputs")
    piece_outs = [v for v, s in values_of(bytes.fromhex(off["raw"])) if s == p2pkh_script(test_addr)]
    check(len(piece_outs) >= off["chunks"], "the split pays one output per piece to the account's own address")
    offers.take(off["offer"], pk).done("ab" * 32)
    size, at = off["chunk_len"], off["manifest_len"]
    asm = None
    r = c.post("/account/inscribe", json={"content": base64.b64encode(big).decode(), "content_type": "image/png"})
    check(r.json().get("resumed") and r.json().get("part") == off["part"], "the same file again resumes, not a second split")
    for n in range(off["chunks"]):
        part = big[max(0, n * size - at):(n + 1) * size - at]
        r = c.post("/account/inscribe", json={"part": off["part"], "chunk": n, "content": base64.b64encode(part).decode()})
        po = r.json()
        if r.status_code != 200 or not po.get("offer"):
            check(False, f"piece {n}: {str(po)[:160]}"); break
        ch = chunk_of(po, test_addr)
        asm = asm or I.Assembly(inscription_id=ch.inscription_id); asm.add(ch)
        offers.take(po["offer"], pk).done(f"{n:064x}")
    else:
        man, got = asm.join()
        check(got == big, f"all {off['chunks']} pieces reassemble into the file (manifest checked)")
    bad = c.post("/account/inscribe", json={"part": off["part"], "chunk": 0, "content": base64.b64encode(b"x").decode()})
    check(bad.status_code == 400, "a piece already on the chain is refused")
    un = c.get("/account/inscribe/unfinished").json()
    check(isinstance(un.get("unfinished"), list), "/account/inscribe/unfinished answers")

    # a collection run whose pictures stay in the browser
    pics = [secrets.token_bytes(300) for _ in range(2)]
    items = [{"json": {"name": f"Thing #{i + 1}", "edition": i + 1, "collection": "Things"}, "image": f"{i + 1}.png",
              "size": len(p), "sha256": hashlib.sha256(p).hexdigest(), "content_type": "image/png"}
             for i, p in enumerate(pics)]
    r = c.post("/account/run/plan", json={"chain": "test", "name": "Things", "items": items, "max_supply": "2"})
    plan = r.json()
    check(r.status_code == 200 and plan.get("items") == 2, f"/account/run/plan: a run {str(plan)[:120]}")
    again = c.post("/account/run/plan", json={"chain": "test", "name": "Things", "items": items})
    check(again.status_code == 400 and "still going" in again.text, "one run at a time")
    nxt = c.post("/account/run/piece", json={"run": plan["run"]}).json()
    check(nxt.get("held") == "browser" and nxt.get("piece") == 1, "/account/run/piece: piece 1, held in the browser")
    wrong = c.post("/account/inscribe", json={"run": plan["run"], "piece": 1, "chain": "test",
                                              "content": base64.b64encode(pics[1]).decode(), "content_type": "image/png"})
    check(wrong.status_code == 400 and "not the bytes" in wrong.text, "the wrong picture for a piece is refused")
    r = c.post("/account/inscribe", json={"run": plan["run"], "piece": 1, "chain": "test",
                                          "content": base64.b64encode(pics[0]).decode(), "content_type": "image/png"})
    po = r.json()
    check(r.status_code == 200 and po.get("offer"), f"the run's piece 1: an offer {str(po)[:100]}")
    ch = chunk_of(po, test_addr)
    asm = I.Assembly(inscription_id=ch.inscription_id); asm.add(ch); man, got = asm.join()
    check(got == pics[0] and I.collection_of(man.json) and I.collection_of(man.json)[0] == "Things",
          "it reads back as the picture, with its collection JSON")
    offers.take(po["offer"], pk).done("cd" * 32)
    runs = c.post("/account/run").json()["runs"]
    check(runs and runs[0]["sent"] == 1, "/account/run: one piece sent")
    check(c.post("/account/run/stop", json={"run": plan["run"]}).json().get("status") == "stopped", "/account/run/stop")
    check(c.post("/account/run/retry", json={"run": plan["run"]}).json().get("retried") == 0, "/account/run/retry")
    for path in ("/me/runs", f"/me/run/{plan['run']}"):
        r = c.get(path)
        check(r.status_code == 200 and "Traceback" not in r.text, f"GET {path}: {r.status_code}")
    check(c.post("/account/run/delete", json={"run": plan["run"]}).json().get("forgotten"), "/account/run/delete")

    # tokens
    r = c.post("/account/token/create", json={"chain": "test", "name": "Creator Coin", "supply": "1000",
                                              "units": "divisible"})
    off = r.json()
    want = tokenlib.issuance_payload(name="Creator Coin", divisible=True, managed=False, amount=1000 * 10 ** 8,
                                     category="", subcategory="", url="", data=tokenlib.data_with_icon("", ""))
    got = payload_of(off, test_addr) if r.status_code == 200 else b""
    check(r.status_code == 200 and got[:len(want)] == want and int.from_bytes(got[2:4], "big") == 50,
          f"/account/token/create: a type 50 issuance, unwrapped {str(off)[:80] if r.status_code != 200 else ''}")
    r = c.post("/account/token/create", json={"chain": "test", "name": "Arcade", "supply": "1"})
    check(r.status_code == 400 and "already token #3" in r.text, "a name already taken is refused")
    r = c.post("/account/token/manage", json={"chain": "test", "property_id": 7, "action": "grant", "amount": "5",
                                              "to": friend})
    off = r.json()
    got = payload_of(off, test_addr) if r.status_code == 200 else b""
    outs = outputs_of(bytes.fromhex(off.get("raw", "00" * 10))) if r.status_code == 200 else []
    check(r.status_code == 200 and got.startswith(tokenlib.grant_payload(7, 5, "")[:12])
          and p2pkh_script(friend) in outs, f"/account/token/manage grant: the grant, the recipient's output {str(off)[:80]}")
    r = c.post("/account/token/manage", json={"chain": "test", "property_id": 7, "action": "revoke", "amount": "500"})
    check(r.status_code == 400 and "all it can revoke" in r.text, "revoking more than held is refused")

    # an ord on Pepecoin testnet: commit, reveals, read as its indexer reads them
    body = b"<svg xmlns='http://www.w3.org/2000/svg'>" + b"<!-- " + secrets.token_hex(1200).encode() + b" --></svg>"
    r = c.post("/account/ord/inscribe", json={"chain": "test", "content": base64.b64encode(body).decode(),
                                              "content_type": "image/svg+xml", "name": "x.svg", "pubkey": pub.hex()})
    off = r.json()
    check(r.status_code == 200 and off.get("run") and off["of"] >= 2, f"/account/ord/inscribe: the commit {str(off)[:120]}")
    if r.status_code == 200:
        lock = bytes.fromhex(off["next_lock"])
        check(oi.p2sh(lock) in outputs_of(bytes.fromhex(off["raw"])), "the commit pays the first lock")
        rid = off["run"]
        offers.take(off["offer"], pk).done("ee" * 32)
        scripts = []
        for step in range(off["of"]):
            nx = c.post("/account/ord/inscribe", json={"run": rid}).json()
            if nx.get("done"):
                break
            if not nx.get("offer"):
                check(False, f"reveal {step}: {str(nx)[:160]}"); break
            coin = offers.take(nx["offer"], pk)
            given = coin.unsigned.inputs[0]
            scripts.append(bytes.fromhex(given["envelope"]) + bytes([72]) + b"\x30" * 72
                           + bytes([len(bytes.fromhex(given["redeem"]))]) + bytes.fromhex(given["redeem"])
                           if len(bytes.fromhex(given["redeem"])) < 76 else
                           bytes.fromhex(given["envelope"]) + bytes([72]) + b"\x30" * 72 + b"\x4c"
                           + bytes([len(bytes.fromhex(given["redeem"]))]) + bytes.fromhex(given["redeem"]))
            coin.done(f"{step + 1:064x}")
        state_, ctype, got, _ = ord_rules.read(scripts, oi.rules_of("test").dialect)
        check(state_ == "complete" and ctype == b"image/svg+xml" and got == body,
              f"its {len(scripts)} reveals read back as one complete ord of the file")
        check(c.post("/account/ord/inscribe", json={"run": rid}).json().get("done"), "then: done")
        runs = c.get("/account/ord/runs").json()["runs"]
        check(any(x["run"] == rid and x["done"] for x in runs), "/account/ord/runs lists it, done")
        check(c.post("/account/ord/forget", json={"run": rid}).json().get("ok"), "/account/ord/forget")
    q = c.post("/account/ord/quote", json={"chain": "test", "files": [{"size": 5000, "content_type": "image/png"}]}).json()
    check(q.get("txs", 0) >= 2 and q.get("fees", 0) > 0, f"/account/ord/quote {str(q)[:80]}")
    col = c.post("/account/ord/collection", json={"chain": "doge", "name": "Set", "items": [
        {"id": "ab" * 32 + "i0", "number": 1, "traits": {"Hat": "Red"}}]}).json()
    check(col.get("root") and col.get("parts"), "/account/ord/collection: a root and parts")
    chs = c.get("/account/ord/chains").json()["chains"]
    check({x["chain"] for x in chs} >= {"main", "test"}, f"/account/ord/chains: {[x['chain'] for x in chs]}")
    nm = c.get("/account/ord/name", params={"chain": "test", "name": "bad name"}).json()
    check(nm["valid"] is False and nm["why"], "/account/ord/name: an invalid name says why")
    nm = c.get("/account/ord/name", params={"chain": "test", "name": "creatortest.pepe"}).json()
    check(nm["valid"] and not nm["taken"], f"/account/ord/name: a free name ({nm.get('why') or 'ready'})")
    sp = c.post("/account/ord/split", json={"chain": "test", "lanes": 3, "each": 10 ** 8})
    check(sp.status_code == 200 and sp.json()["vouts"] == [0, 1, 2], "/account/ord/split: three lanes")

    # the operator's fee on mainnet
    r = c.post("/account/inscribe", json={"chain": "main", "content": base64.b64encode(b"tiny").decode(),
                                          "content_type": "text/plain"})
    off = r.json()
    fee = off.get("operator_fee") or {}
    check(r.status_code == 200 and fee.get("address") == op_main
          and (fee["value"], p2pkh_script(op_main)) in values_of(bytes.fromhex(off["raw"])),
          f"mainnet: the operator's fee is an output of the offer ({fee})")
    r = c.post("/account/inscribe", json={"chain": "test", "content": base64.b64encode(b"tiny").decode(),
                                          "content_type": "text/plain"})
    check(r.json().get("operator_fee") is None, "testnet: no operator's fee")

    # pages and the chain cookie
    r = c.get("/create/chain", params={"to": "main", "back": "/create/ord"}, follow_redirects=False)
    check(r.status_code == 303 and "arcade_chain=main" in r.headers.get("set-cookie", ""), "/create/chain sets the cookie")
    for path in ("/create", "/create/name", "/create/ord", "/mintpad/new"):
        r = c.get(path)
        check(r.status_code == 200 and "Traceback" not in r.text, f"GET {path}: {r.status_code}")
    check("Pepecoin" in c.get("/create").text, "/create draws the chosen chain")


# ======================================================================== live
def live(broadcast: bool):
    print("--- live (the dev home's V1.0)")
    cfg = json.load(open(os.path.join(os.path.expanduser(os.environ.get("ARCADE_SITE_DEV", "~/.arcade-site-dev")), "site.json")))
    home = tempfile.mkdtemp(prefix="creator-live-")
    json.dump({**cfg, "public_hosts": ["testserver"]}, open(home + "/site.json", "w"))
    json.dump({"faucet": 100000000}, open(home + "/settings.json", "w"))
    app = create_app(home)
    creator.register(app, app.state.site, app.state.chains)
    c = TestClient(app)
    check(c.get("/healthz").json().get("ok"), "the site reaches V1.0")
    for path in ("/create", "/create/name", "/create/ord", "/mintpad/new", "/me/runs"):
        r = c.get(path, follow_redirects=False)
        check(r.status_code in (200, 303) and "Traceback" not in r.text, f"GET {path} signed out: {r.status_code}")
    v1 = app.state.site.v1
    # Offers from a funded address's real coins, never signed: the operator's address on testnet.
    funded = ""
    for a in ("nWKJgSeAZSmfRCxZnwVgtYyPNowQT5X1Gk", "ncpXrSCQx667v4y92zSWidxs96N5TtPjTE"):
        got = (v1.cmd("tx.coins", addresses=[a], chain="peptest") or {}).get("addresses", {}).get(a) or {}
        if sum(int(x["value"]) for x in got.get("confirmed") or []) > 10 ** 8:
            funded = a; break
    check(bool(funded), f"a funded testnet address to build from: {funded}")
    pk = secrets.token_hex(32)
    _, any_pub = new_key()
    st = app.state.site
    st.set_setting("address:" + pk, funded)
    st.set_setting("coinkey:" + pk, any_pub.hex())         # only to shape Class B outputs: nothing is signed
    account = types.SimpleNamespace(pubkey=pk)
    real_in, real_acct = app.state.signed_in, app.state.signed_in_account
    app.state.signed_in = lambda request: account
    app.state.signed_in_account = lambda request: account
    for path in ("/create", "/create/name", "/create/ord", "/mintpad/new", "/me/runs"):
        r = c.get(path)
        check(r.status_code == 200 and "Traceback" not in r.text, f"GET {path} signed in: {r.status_code}")
    r = c.post("/account/inscribe", json={"content": base64.b64encode(b"creator live test").decode(),
                                          "content_type": "text/plain"})
    check(r.status_code == 200 and r.json().get("offer"), f"/account/inscribe: an offer from V1.0's coins {r.text[:120]}")
    r = c.post("/account/token/create", json={"chain": "test", "name": "Creator Live " + secrets.token_hex(2),
                                              "supply": "10"})
    check(r.status_code == 200 and r.json().get("offer"), f"/account/token/create: an offer {r.text[:120]}")
    r = c.post("/account/token/create", json={"chain": "test", "name": "Arcade", "supply": "10"})
    check(r.status_code == 400 and "already token" in r.text, f"a taken token name is refused (V1.0's tokens) {r.text[:80]}")
    chs = c.get("/account/ord/chains").json()["chains"]
    check(any(x["chain"] == "test" for x in chs), f"/account/ord/chains: {[x['chain'] for x in chs]}")
    nm = c.get("/account/ord/name", params={"chain": "test", "name": "zz" + secrets.token_hex(4) + ".pepe"}).json()
    check(nm["valid"] and not nm["taken"], f"/account/ord/name on V1.0: free ({nm.get('why') or 'ready'})")
    q = c.post("/account/ord/quote", json={"chain": "test", "files": [{"size": 3000, "content_type": "image/png"}]})
    check(q.status_code == 200, f"/account/ord/quote {q.text[:80]}")
    app.state.signed_in, app.state.signed_in_account = real_in, real_acct
    if not broadcast:
        return
    print("--- live broadcast (Pepecoin testnet, a throwaway key)")
    from nacl.signing import SigningKey
    from arcade import accounts, wallet_imports
    key = SigningKey.generate(); pubkey = key.verify_key.encode().hex()
    priv, pub = new_key()
    address = b58check_encode(NETWORKS["test"].pubkeyhash_version, hash160(pub))
    tag = "creatortest" + secrets.token_hex(3)
    ch = c.get("/auth/challenge").json()
    body = {"tag": tag, "pubkey": pubkey, "address": address, "coin_pubkey": pub.hex(),
            "blob": {"sealed": "dGVzdA", "iterations": 600000}, "nonce": ch["nonce"], "origin": "http://testserver",
            "signature": key.sign(accounts.login_message("http://testserver", ch["nonce"])).signature.hex(),
            "address_signature": coin_sign(priv, wallet_imports.proof_digest(pubkey, "test", address, pub.hex())).hex()}
    r = c.post("/signup", json=body); out = r.json()
    check(r.status_code == 200 and out.get("given"), f"sign-up with welcome coins: {str(out)[:120]}")
    if not out.get("given"):
        return
    for _ in range(30):
        if (c.get("/account").json().get("spendable") or 0) >= 10 ** 8: break
        time.sleep(1)
    def sign(off):
        sigs = [(coin_sign(priv, bytes.fromhex(h)) + b"\x01").hex() for h in off["sighashes"]]
        return c.post("/account/sign", json={"offer": off["offer"], "signatures": sigs, "pubkey": pub.hex()})
    text = b"creator test " + secrets.token_hex(4).encode()
    off = c.post("/account/inscribe", json={"content": base64.b64encode(text).decode(), "content_type": "text/plain",
                                            "name": "creator-test.txt"}).json()
    r = sign(off); done = r.json()
    check(r.status_code == 200 and len(done.get("txid") or "") == 64, f"a tiny inscription broadcast: {str(done)[:120]}")
    ordy = b"ord " + secrets.token_hex(4).encode()
    off = c.post("/account/ord/inscribe", json={"chain": "test", "content": base64.b64encode(ordy).decode(),
                                                "content_type": "text/plain", "pubkey": pub.hex()}).json()
    check(bool(off.get("offer")), f"ord commit offered: {str(off)[:120]}")
    if off.get("offer"):
        r = sign(off); check(r.status_code == 200, f"ord commit broadcast: {r.text[:120]}")
        for _ in range(off["of"]):
            nx = c.post("/account/ord/inscribe", json={"run": off["run"]}).json()
            if nx.get("done") or not nx.get("offer"):
                break
            r = sign(nx)
            check(r.status_code == 200 and len(r.json().get("txid") or "") == 64,
                  f"ord reveal {nx.get('step')} broadcast (unpooled is fine): {r.text[:160]}")
        last = c.post("/account/ord/inscribe", json={"run": off["run"]}).json()
        check(last.get("done") and last.get("inscription"), f"the ord is done: {last.get('inscription')}")


import importlib
from arcadesite import v1 as _v1mod
_real = (_v1mod.V1.cmd, _v1mod.V1.page, _v1mod.V1.core)
offline()
_v1mod.V1.cmd, _v1mod.V1.page, _v1mod.V1.core = _real
if "--live" in sys.argv or "--broadcast" in sys.argv:
    live("--broadcast" in sys.argv)
print("ALL OK" if not fails else "%d FAILED" % len(fails))
sys.exit(1 if fails else 0)
