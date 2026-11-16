"""What an account says on the chain: its @tag, its messaging key and profile, its posts and reactions. Each is an
offer the browser signs and /account/sign broadcasts (wallet.py), built exactly as the original builds it
(arcade/web/app.py: _class_c_or_b, _announcement_offer, account_claim/post/react), with coins, tags, profiles and
post authors read from Arcade V1.0.

    POST /account/claim      claim the @tag (the one signed up with, unless another is asked)
    POST /account/announce   publish the messaging key (and every chain's address, D-032)
    POST /account/profile    the same announcement with a picture, a bio, a link
    POST /account/post       a feed post
    POST /account/react      like / dislike / reply / share / edit / delete / tip a post
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from arcade import fees
from arcade import funding as fundinglib
from arcade import inscriptions as inscriptionlib
from arcade import tags as taglib
from arcade import txbuild
from arcade.encoding import MAX_CLASS_B_PAYLOAD, encode_class_b, encode_class_c, max_class_c_payload
from arcade.ledger import AmountError, parse_amount
from arcade.messaging import envelope as envelopelib
from arcade.messaging import feed as feedlib
from arcade.messaging import group
from arcade.messaging import sender as sendermod
from arcade import payload as P
from arcade.script import b58check_decode, b58check_encode

from .v1 import V1Error
from .wallet import CoinDB


def class_c_or_b(chain, address: str, raw: bytes, coin_pubkey: bytes = b"", wrap: bool = True):
    """The outputs that carry a payload: Class C where it fits, else Class B (the original's _class_c_or_b)."""
    payload = P.AnyData(data=raw).encode() if wrap else raw
    if len(payload) <= max_class_c_payload():
        return [(0, txbuild.op_return_script(encode_class_c(payload)))]
    if len(payload) > MAX_CLASS_B_PAYLOAD:
        raise fundinglib.FundingError(f"that is {len(payload)} bytes, and one transaction carries at most "
                                      f"{MAX_CLASS_B_PAYLOAD}.")
    if not coin_pubkey:
        raise fundinglib.FundingError("this account has not told the node its public key, so a Class B payload "
                                      "cannot be built. Sign in again.")
    outputs = [(sendermod.OUTPUT_VALUE, txbuild.p2pkh_script(chain.params.marker, getattr(chain, "params", None)))]
    for grp in encode_class_b(address, coin_pubkey, payload):
        outputs.append((sendermod.OUTPUT_VALUE, txbuild.multisig_script(list(grp.keys), grp.required)))
    return outputs


def register(app, st, chains):
    v1 = st.v1
    offers = app.state.offers
    quota = app.state.quota

    def signed_in_account(request):
        return app.state.signed_in_account(request)

    def coin_pubkey(pubkey: str, chain=None) -> bytes:
        try:
            return bytes.fromhex(str(st.setting(chains.coinkey_key(pubkey, chain or chains.account_chain()), "")
                                     or ""))
        except ValueError:
            return b""

    def any_address(pubkey: str, chain) -> str:
        """An account's address on a chain: the one its browser gave, else the same key's (a real-coin chain shares
        the Pepecoin mainnet key, a test chain the testnet key: coins.js coin types 3 and 1)."""
        said = chains.account_address(pubkey, chain)
        if said:
            return said
        sib = chains.by_network("main" if chain.params.real_coins else "test")
        base = chains.account_address(pubkey, sib) if sib is not None else ""
        if not base:
            return ""
        try:
            return b58check_encode(chain.params.pubkeyhash_version, b58check_decode(base)[1])
        except Exception:                                   # noqa: BLE001
            return ""

    #: Every coin a profile names an address on (2026-11-12, the operator: "profiles must carry an address for every
    #: coin the arcade supports so anyone can send any profile any coin"; no Bitcoin yet), besides Pepecoin and
    #: Dogecoin, which have their own places in the announcement. Coins this site keeps no wallet for (ARCADE) are
    #: named from the same key: one key hash on every real-coin chain (BIP44 type 3), another on every test chain
    #: (type 1), as Arcade V1.0 derives them.
    PROFILE_COINS = {"ltc": True, "dgb": True, "kot": True, "flop": True, "wjk": True, "bel": True, "lky": True,
                     "shic": True, "jkc": True, "arc": True, "ltctest": False, "dgbtest": False, "arctest": False}

    def key_hash(pubkey: str, real: bool) -> bytes:
        sib = chains.by_network("main" if real else "test")
        base = chains.account_address(pubkey, sib) if sib is not None else ""
        try:
            return b58check_decode(base)[1] if base else b""
        except Exception:                                   # noqa: BLE001
            return b""

    def chain_hashes(pubkey: str) -> dict[str, bytes]:
        """The account's address hash on every coin in PROFILE_COINS: the address its browser filed for a coin this
        site lists, else the same key's hash."""
        listed = {one.network: one for one in chains.account_chains()}
        out = {}
        for net, real in PROFILE_COINS.items():
            h = b""
            if net in listed:
                a = any_address(pubkey, listed[net])
                try:
                    h = b58check_decode(a)[1] if a else b""
                except Exception:                           # noqa: BLE001
                    h = b""
            out[net] = h or key_hash(pubkey, real)
        return {net: h for net, h in out.items() if len(h) == 20}

    def profile_missing(address: str) -> list[str]:
        """The coins of PROFILE_COINS the account's published profile names no address on (Arcade V1.0's `linked`);
        [] when it has published none (nothing to update) or V1.0 does not answer."""
        try:
            prof = (v1.cmd("profile", who=address) or {})
        except V1Error:
            return []
        prof = prof.get("profile") or prof
        if not prof.get("stated") and not prof.get("txid"):
            return []
        have = {str(row.get("chain") or "") for row in prof.get("linked") or []}
        return [net for net in PROFILE_COINS if net not in have]

    def doge_hash(pubkey: str) -> bytes:
        doge = chains.by_network("doge")
        a = any_address(pubkey, doge) if doge is not None else ""
        try:
            return b58check_decode(a)[1] if a else b""
        except Exception:                                   # noqa: BLE001
            return b""

    def tag_of(address: str) -> str:
        try:
            return str((v1.page(f"address/{address}")[1] or {}).get("tag") or "")
        except V1Error:
            return ""

    def profile_of(address: str) -> dict:
        try:
            prof = (v1.cmd("profile", who=address) or {}).get("profile") or {}
        except V1Error:
            prof = {}
        return {f: str(prof.get(f) or "") for f in ("pfp", "bio", "url")}

    def build(account, chain, address: str, outputs: list, what: str):
        return fundinglib.build(CoinDB(v1, chain), chain.params, address, outputs, rate=fees.MIN_FEE_PER_KB, what=what)

    def offered(account, chain, unsigned, what: str, **extra) -> JSONResponse:
        offer = offers.add(account.pubkey, chain.network, unsigned, what)
        return JSONResponse({"offer": offer.id, "chain": chain.network, **extra, **unsigned.as_json()})

    def no_address(chain) -> JSONResponse:
        return JSONResponse({"detail": f"this account has no {chain.label} address yet"}, status_code=400)

    # ---------------------------------------------------------------- the @tag
    @app.post("/account/claim")
    def account_claim(request: Request, payload: Any = Body(None)):
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = chains.account_chain()
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            asked = str(said.get("tag", "")).strip().lstrip("@") \
                or (st.vault().by_pubkey(account.pubkey) or {}).get("tag", "")
            wanted = taglib.validate(asked)
            holder = v1.tag_owner(wanted)
            if holder and holder != address:
                raise taglib.TagError(f"@{wanted} is taken.")
            waiting = "" if holder else v1.tag_pending(wanted)
            if waiting and waiting != address:
                raise taglib.TagError(f"someone else's claim for @{wanted} is waiting for a block.")
            unsigned = build(account, chain, address,
                             class_c_or_b(chain, address, taglib.encode(wanted), coin_pubkey(account.pubkey)),
                             f"claim @{wanted}")
            quota(account, "name")
        except (taglib.TagError, fundinglib.FundingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return offered(account, chain, unsigned, f"claim @{wanted}")

    # ---------------------------------------------------------------- the messaging key and the profile
    def announcement(account, chain, address: str, said: dict, *, tag: str, what: str, pfp: str = "",
                     bio: str = "", url: str = ""):
        # One announcement at a time: two parts of a page asking at once (the sign-up's own step and base.html's
        # check) get the offer already waiting rather than two transactions that each cost a fee.
        if what == "publish your messaging key":
            waiting = next((o for o in offers.waiting(account.pubkey) if o.what == what), None)
            if waiting is not None:
                return JSONResponse({"offer": waiting.id, "chain": chain.network, **waiting.unsigned.as_json()})
        try:
            key = bytes.fromhex(str(said.get("key", "")))
            if len(key) != 32:
                raise ValueError("a messaging key is 32 bytes")
            _, our_hash = b58check_decode(address)
            other = b""
            main = chains.by_network("main")
            if main is not None and chains.account_address(account.pubkey, main):
                _, other = b58check_decode(chains.account_address(account.pubkey, main))
            body = envelopelib.build_key_announcement(key, hash160=our_hash, tag=tag, other_hash160=other, pfp=pfp,
                                                      bio=bio, url=url, doge_hash160=doge_hash(account.pubkey),
                                                      chains=chain_hashes(account.pubkey), dd_keys={})
            unsigned = build(account, chain, address,
                             class_c_or_b(chain, address, body, coin_pubkey(account.pubkey)), what)
            quota(account, "name", len(body))
        except (fundinglib.FundingError, envelopelib.EnvelopeError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return offered(account, chain, unsigned, unsigned.what)

    @app.post("/account/announce")
    def account_announce(request: Request, payload: Any = Body(None)):
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = chains.account_chain()
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        return announcement(account, chain, address, said, tag=str(said.get("tag", "")).strip().lstrip("@"),
                            what="publish your messaging key")

    @app.post("/account/profile")
    def account_profile(request: Request, payload: Any = Body(None)):
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = chains.account_chain()
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            tag = tag_of(address) or (st.vault().by_pubkey(account.pubkey) or {}).get("tag") or ""
            now = profile_of(address)
            if "pfp" in said:
                face = inscriptionlib.inscription_in(str(said["pfp"] or ""))
                if said.get("pfp"):
                    if not face:
                        raise ValueError("a profile picture is an inscription on one of these chains: give its id.")
                    code, row = v1.page(f"inscription/{face}")
                    mine = {chains.account_address(account.pubkey, one) for one in chains.account_chains()} - {""}
                    if code != 200 or str((row or {}).get("owner") or "") not in mine:
                        raise ValueError("that piece is not yours to wear. A profile picture is a piece you hold, and "
                                         "it is drawn only while the chain says you hold it.")
            else:
                face = now["pfp"]
            text = " ".join(str(said.get("bio", now["bio"]) or "").split())
            if len(text.encode()) > envelopelib.MAX_ANNOUNCE_BIO:
                raise ValueError(f"a bio is at most {envelopelib.MAX_ANNOUNCE_BIO} characters; this one is "
                                 f"{len(text.encode())}")
            link = str(said.get("url", now["url"]) or "").strip()
            if link and not link.startswith(("https://", "http://")):
                raise ValueError("a link starts with https:// -- it is shown on your profile as words, never as "
                                 "somewhere to click")
            if len(link.encode()) > envelopelib.MAX_ANNOUNCE_URL:
                raise ValueError(f"a link is at most {envelopelib.MAX_ANNOUNCE_URL} characters")
        except (ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return announcement(account, chain, address, said, tag=tag, what="publish your profile", pfp=face or "",
                            bio=text, url=link)

    # ---------------------------------------------------------------- follows (on the chain, as Arcade V1.0 reads them)
    def account_named(who: str) -> str:
        """An account's home-chain address from @tag or the address itself; "" when it names nobody."""
        who = str(who or "").strip()
        home = chains.account_chain()
        if who.startswith("@"):
            try:
                return str(v1.tag_owner(taglib.validate(who.lstrip("@"))) or "")
            except (taglib.TagError, V1Error):
                return ""
        return who if who and not chains.check_address(who, home) else ""

    @app.post("/account/follow")
    def account_follow(request: Request, payload: Any = Body(None)):
        """Follow {who} (on: false unfollows): a feed act on the home chain from this account, offered for its browser
        to sign like a like. Arcade V1.0 counts the newest act between two accounts."""
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = chains.account_chain()
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        on = said.get("on", True) not in (False, "false", 0, "0")
        try:
            them = account_named(str(said.get("who", "")))
            if not them:
                raise ValueError("follow whom? Give an @name or a Pepecoin address")
            if them == address:
                raise ValueError("that is you")
            kind = feedlib.FOLLOW if on else feedlib.UNFOLLOW
            note = feedlib.build(kind, feedlib.follow_target(b58check_decode(them)[1]))
            what = f"{'follow' if on else 'unfollow'} {said.get('who')}"
            unsigned = build(account, chain, address, class_c_or_b(chain, address, note, coin_pubkey(account.pubkey)),
                             what)
            quota(account, "react", len(note))
        except (fundinglib.FundingError, feedlib.FeedError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return offered(account, chain, unsigned, unsigned.what, who=them, on=on)

    # ---------------------------------------------------------------- a profile that names every coin
    def coin_label(net: str) -> str:
        from arcade.config import NETWORKS
        p = NETWORKS.get(net)
        return getattr(p, "label", "") or {"arc": "ARCADE", "arctest": "ARCADE testnet"}.get(net, net)

    @app.get("/account/profile/missing")
    def account_profile_missing(request: Request):
        """The coins this account's published profile names no address on, for the one-time "update your profile" card
        (2026-11-12). `skipped`: the account said "not now" to this same list already."""
        account = signed_in_account(request)
        address = chains.account_address(account.pubkey, chains.account_chain())
        nets = profile_missing(address) if address else []
        said = st.setting(f"profile_coins_skipped:{account.pubkey}", "") or ""
        return JSONResponse({"missing": nets, "labels": [coin_label(n) for n in nets],
                             "skipped": bool(nets) and said == ",".join(nets)})

    @app.post("/account/profile/skip")
    def account_profile_skip(request: Request, payload: Any = Body(None)):
        """"Not now" to the card: remembered for this list of coins, so it is asked once (a coin added later asks again)."""
        account = signed_in_account(request)
        address = chains.account_address(account.pubkey, chains.account_chain())
        nets = profile_missing(address) if address else []
        st.set_setting(f"profile_coins_skipped:{account.pubkey}", ",".join(nets))
        return JSONResponse({"skipped": nets})

    # ---------------------------------------------------------------- the feed
    @app.post("/account/post")
    def account_post(request: Request, payload: Any = Body(None)):
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = chains.account_chain()
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            text = str(said.get("text", "")).strip()
            if not text:
                raise ValueError("say something")
            if not tag_of(address):
                raise ValueError("claim your name first -- it is what your posts appear under, and it is one button.")
            plan = group.plan(group.GroupPost(channel="", nickname="", text=text))
            if plan.transactions != 1:
                raise ValueError("that is too long for one transaction -- put a file in it instead, or say less")
            unsigned = build(account, chain, address,
                             class_c_or_b(chain, address, plan.payloads[0], coin_pubkey(account.pubkey)), "a post")
            quota(account, "post", len(plan.payloads[0]))
        except (fundinglib.FundingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return offered(account, chain, unsigned, "a post")

    def post_author(txid: str) -> str:
        try:
            posts = (v1.cmd("feed", txid=txid) or {}).get("posts") or []
        except V1Error:
            posts = []
        stack = list(posts)
        while stack:
            p = stack.pop()
            if p.get("txid") == txid:
                return str((p.get("author") or {}).get("address") or "")
            stack.extend(p.get("replies") or [])
        return ""

    def pays_at(author: str, chain) -> str:
        """Where the author of a post is paid on `chain`: their tag-chain address there, else what they published."""
        if not author:
            return ""
        if chain.network == "test":
            return author
        try:
            prof = (v1.cmd("profile", who=author) or {}).get("profile") or {}
        except V1Error:
            return ""
        for link in prof.get("linked") or []:
            if str(link.get("chain") or "") in (chain.key, chain.network):
                return str(link.get("address") or "")
        return ""

    app.state.pays_at = pays_at
    app.state.post_author = post_author

    @app.post("/account/react")
    def account_react(request: Request, payload: Any = Body(None)):
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            kind = int(said.get("kind", 0))
        except (TypeError, ValueError):
            return JSONResponse({"detail": "a reaction's kind is a number"}, status_code=400)
        chain = chains.account_chain()
        if kind == feedlib.TIP:
            chain = chains.by_network(str(said.get("chain") or "test")) or chain
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            target = str(said.get("txid", "")).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", target):
                raise ValueError("that is not a transaction id")
            if kind not in feedlib.KINDS:
                raise ValueError("that is not something that can be done")
            text = str(said.get("text", ""))
            if kind == feedlib.TIP_TOKEN and not re.fullmatch(r"[0-9a-f]{64}", text.strip().lower()):
                raise ValueError("a token tip names the token send it was, by its txid")
            note = feedlib.build(kind, target, text)
            outputs = class_c_or_b(chain, address, note, coin_pubkey(account.pubkey, chain))
            paid = 0
            if kind == feedlib.TIP:
                amount = parse_amount(str(said.get("amount", "")), True)
                if amount < fees.dust_limit(chain.params):
                    raise ValueError("a tip that small is dust")
                where = pays_at(post_author(target), chain)
                if not where:
                    raise ValueError(f"there is nowhere to send it: whoever wrote that has not published a "
                                     f"{chain.label} address")
                outputs.append((amount, txbuild.p2pkh_script(where, getattr(chain, "params", None))))
                paid = amount
            unsigned = build(account, chain, address, outputs, feedlib.NAMES.get(kind, "a reaction"))
            quota(account, "react", len(note))
        except (fundinglib.FundingError, AmountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return offered(account, chain, unsigned, unsigned.what, paid=paid)
