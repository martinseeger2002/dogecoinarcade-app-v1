"""The chains an account holds coins on, and what the site writes down per account, in the original's keys.

    account_chain()      Pepecoin testnet: where @tags and messages live (V1.0's home chain)
    account_chains()     that first, then every other chain V1.0 keeps with an Addresses index or borrows
    address_key(...)     "address:<pubkey>" / "address:<network>:<pubkey>" (settings.json, as the original)
    welcome(...)         the faucet's gift for a new account, paid from V1.0's own wallet (arcade/faucet.py rules)

Each chain is the original's Params (arcade.config.NETWORKS) plus V1.0's key for it.
"""
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass

from arcade.config import NETWORKS, Params

from .addresses import _check_address
from .v1 import V1, V1Error, chain_key

#: The ARCADE testnet (Arcade V1.0's own chain, 2026-11-14, the operator: "add arcade test net to the
#: app.dogecoinarcade.com"). The original has no such network, so its Params are made here and added to NETWORKS once,
#: so every lookup by network name finds it: P2PKH 0x53 ("a..."), P2SH 0x57, the chain's fee floor (0.001 ARCADE/kB,
#: twice that as the fallback, as V1.0's tx.fee says) and its smallest output (0.001 ARCADE). Test coins: an account's
#: address on it comes from the testnet key (coin type 1), the one its profile already names.
ARCTEST = dataclasses.replace(
    NETWORKS["jkc"], name="arctest", rpc_port=0, p2p_port=0, activation_height=0, pubkeyhash_version=0x53,
    scripthash_version=0x57, other_pubkeyhash_version=None, messaging_start_height=0, min_fee_per_kb=200_000,
    soft_dust=False, dust_limit=100_000, coin="ARCADE", label="ARCADE testnet",
    genesis="8891596abcceae069b4f022a34e78387259e168a581d37817f015af097a1d7c8", auxpow_chain_id=None,
    auxpow_chain_mask=0, pow_algo="", node_dir="", node_conf="", letter="a", family="ARC", net_word="arcade",
    real_coins=False)
NETWORKS.setdefault("arctest", ARCTEST)

#: The original's order of chains in a wallet (its Params order), by original network name.
ORDER = ("test", "main", "doge", "ltc", "dgb", "kot", "flop", "wjk", "bel", "lky", "shic", "jkc", "btc", "ltctest",
         "dgbtest", "arctest")
# Bitcoin (config.BITCOIN, legacy "1..." addresses on BIP44 coin type 0, as Arcade V1.0's wallet) is listed since
# 2026-11-24 (the operator: "enable Bitcoin"). Like every chain here, it shows only when V1.0 keeps it or borrows it.


@dataclass(frozen=True)
class Chain:
    network: str          # the original's name: test, main, doge, ...
    params: Params

    @property
    def key(self) -> str:                                   # V1.0's chain key
        return chain_key(self.network)

    @property
    def label(self) -> str:
        return getattr(self.params, "label", self.network)

    @property
    def is_mainnet(self) -> bool:
        return self.network not in ("test", "regtest", "ltctest", "dgbtest", "arctest")


class Chains:
    def __init__(self, st, v1: V1):
        self.st, self.v1 = st, v1
        self._kept: tuple[float, list[Chain]] | None = None

    def account_chain(self) -> Chain:
        return Chain("test", NETWORKS["test"])

    def account_chains(self) -> list[Chain]:
        """Tag chain first, then each chain V1.0 has installed with its Addresses index, or borrows from arcades that
        share it (V1.0 answers coins, sends and Core calls of a borrowed chain from its lenders, as the original's
        Wallet showed borrowed chains). Asked every 5 minutes."""
        if self._kept and time.time() - self._kept[0] < 300:
            return self._kept[1]
        keys = set()
        try:
            for c in self.v1.cmd("chains.list") or []:
                if (c.get("installed") and "addresses" in (c.get("indexes") or [])) or c.get("borrowed"):
                    keys.add(str(c.get("key")))
        except V1Error:
            pass
        out = [self.account_chain()]
        for net in ORDER[1:]:
            if net in NETWORKS and chain_key(net) in keys:
                out.append(Chain(net, NETWORKS[net]))
        self._kept = (time.time(), out)
        return out

    def by_network(self, network: str) -> Chain | None:
        return next((c for c in self.account_chains() if c.network == network), None)

    # ---------------------------------------------------------------- what is written per account (the original's keys)
    def address_key(self, pubkey: str, chain: Chain) -> str:
        return f"address:{pubkey}" if chain.network == "test" else f"address:{chain.network}:{pubkey}"

    def coinkey_key(self, pubkey: str, chain: Chain) -> str:
        return f"coinkey:{pubkey}" if chain.network == "test" else f"coinkey:{chain.network}:{pubkey}"

    def account_address(self, pubkey: str, chain: Chain) -> str:
        return str(self.st.setting(self.address_key(pubkey, chain), "") or "")

    def address_held_by_another(self, pubkey: str, chain: Chain, address: str) -> bool:
        mine = self.address_key(pubkey, chain)
        prefix = mine[:-len(pubkey)]
        for name, value in self.st.settings().items():
            if value == address and name != mine and name.startswith(prefix) and ":" not in name[len(prefix):]:
                return True
        return False

    def check_address(self, address: str, chain: Chain, segwit_ok: bool = False) -> str | None:
        return _check_address(address, mainnet=chain, segwit_ok=segwit_ok)

    # ---------------------------------------------------------------- welcome coins
    def welcome(self, pubkey: str, address: str, ip: str = "") -> int:
        """The faucet's gift (arcade/faucet.py: its rules, its record in accounts.sqlite), paid by V1.0's own wallet
        with wallet.send. Returns the amount given; raises FaucetError (or V1Error) with the reason when not."""
        from arcade import faucet as faucetlib
        gift = self.st.setting("faucet")
        ceiling = self.st.setting("faucet_daily")
        faucet = faucetlib.Faucet(self.st.accounts(),
                                  gift=faucetlib.GIFT if gift is None else int(gift),
                                  ceiling=faucetlib.DAILY_CEILING if ceiling is None else int(ceiling),
                                  real_coins=bool(self.st.setting("faucet_real_coins", False)))
        given = faucetlib.pour(_V1Chain(self.v1, self.account_chain()), faucet, pubkey, address, ip=ip)
        return given.amount

    #: The ARCADE welcome gift (2026-11-14, the operator: "add the faucet"): 0.1 test ARCADE for each new account, beside
    #: the faucet's Pepecoin testnet gift, so names and profiles can move to ARCADE. Setting "faucet_arcade" (sats).
    ARCADE_GIFT = 10_000_000
    ARCADE_RETRY = 600

    def arcade_address(self, pubkey: str) -> str:
        """The account's ARCADE testnet address: the one its browser filed, else its testnet key's (the same key hash
        as its Pepecoin testnet address, version 0x53), as its profile names it."""
        arc = self.by_network("arctest")
        if arc is None:
            return ""
        said = self.account_address(pubkey, arc)
        if said:
            return said
        from arcade.script import b58check_decode, b58check_encode
        base = self.account_address(pubkey, self.account_chain())
        try:
            return b58check_encode(arc.params.pubkeyhash_version, b58check_decode(base)[1]) if base else ""
        except Exception:                                   # noqa: BLE001
            return ""

    def welcome_arcade(self, pubkey: str, *, new: bool = False) -> str:
        """Give the account its ARCADE gift once: at sign-up (`new`, after the faucet's own gift went, so its limits
        hold), or later for an account whose gift is still owed (the arcade's ARCADE was all maturing then). The txid
        given, or "" (none owed, none to give yet, or not tried again so soon)."""
        done = f"arcade_gift:{pubkey}"
        owed = f"arcade_gift_owed:{pubkey}"
        if self.st.setting(done):
            return ""
        if new:
            self.st.set_setting(owed, 0)
        tried = self.st.setting(owed)
        if tried is None or time.time() - float(tried or 0) < self.ARCADE_RETRY and not new:
            return ""
        self.st.set_setting(owed, time.time())
        arc = self.by_network("arctest")
        to = self.arcade_address(pubkey)
        if arc is None or not to:
            return ""
        gift = self.st.setting("faucet_arcade")
        sats = self.ARCADE_GIFT if gift is None else int(gift)
        if sats <= 0:
            return ""
        out = self.v1.cmd("wallet.send", chain=arc.key, to=to, amount=sats, send=True) or {}
        txid = str(out.get("txid") or "")
        if txid:
            self.st.set_setting(done, txid)
        return txid


    #: Accounts seen today (pubkey -> UTC day) by keep_arcade: once a day each. The mesh faucet keeps its own
    #: once-a-day per address too, so a restart that forgets this asks again for nothing.
    _arcade_day: dict = {}

    def keep_arcade(self, pubkey: str) -> str:
        """A signed-in account's ARCADE, so likes, comments and tips on the ARCADE network never lock it out (Martin
        2026-11-16): once a UTC day, its one-time gift if it never had one (accounts from before the gift), else an
        ask to the mesh faucet (V1.0 faucet.arc) to top its ARCADE address up (to 0.05 ARCADE, arcade-v1 #258).
        What was done: "gift", "asked", or ""."""
        day = int(time.time() // 86400)
        if self._arcade_day.get(pubkey) == day:
            return ""
        self._arcade_day[pubkey] = day
        if not self.st.setting(f"arcade_gift:{pubkey}") and self.st.setting(f"arcade_gift_owed:{pubkey}") is None:
            self.st.set_setting(f"arcade_gift_owed:{pubkey}", 0)
            try:
                if self.welcome_arcade(pubkey):
                    return "gift"
            except Exception:                               # noqa: BLE001  (tried again on a later visit)
                pass
        to = self.arcade_address(pubkey)
        if not to:
            return ""
        try:
            out = self.v1.cmd("faucet.arc", address=to) or {}
        except Exception:                                   # noqa: BLE001
            return ""
        return "asked" if out.get("asked") else ""


class _V1Chain:
    """What arcade/faucet.pour asks of a chain: is_mainnet, and rpc().call("sendtoaddress", address, coins)."""

    def __init__(self, v1: V1, chain: Chain):
        self.v1, self.chain = v1, chain
        self.is_mainnet = chain.is_mainnet
        self.network = chain.network

    def rpc(self):
        return _SendOnly(self.v1, self.chain)


class _SendOnly:
    def __init__(self, v1: V1, chain: Chain):
        self.v1, self.chain = v1, chain

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method: str, *params):
        if method != "sendtoaddress":
            raise V1Error(f"{method} is not something the faucet asks")
        address, coins = params[0], params[1]
        sats = int(round(float(coins) * 100_000_000))
        out = self.v1.cmd("wallet.send", chain=self.chain.key, to=address, amount=sats, send=True)
        txid = str((out or {}).get("txid") or "")
        if not txid:
            raise V1Error("the arcade's wallet did not send")
        return txid
