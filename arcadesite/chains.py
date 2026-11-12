"""The chains an account holds coins on, and what the site writes down per account, in the original's keys.

    account_chain()      Pepecoin testnet: where @tags and messages live (V1.0's home chain)
    account_chains()     that first, then every other chain V1.0 keeps with an Addresses index or borrows
    address_key(...)     "address:<pubkey>" / "address:<network>:<pubkey>" (settings.json, as the original)
    welcome(...)         the faucet's gift for a new account, paid from V1.0's own wallet (arcade/faucet.py rules)

Each chain is the original's Params (arcade.config.NETWORKS) plus V1.0's key for it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from arcade.config import NETWORKS, Params

from .addresses import _check_address
from .v1 import V1, V1Error, chain_key

#: The original's order of chains in a wallet (its Params order), by original network name.
ORDER = ("test", "main", "doge", "ltc", "dgb", "kot", "flop", "wjk", "bel", "lky", "shic", "jkc", "ltctest", "dgbtest")
# Bitcoin (config.BITCOIN) is defined but not listed yet (the operator, 2026-11-12: "no Bitcoin yet").


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
        return self.network not in ("test", "regtest", "ltctest", "dgbtest")


class Chains:
    def __init__(self, st, v1: V1):
        self.st, self.v1 = st, v1
        self._kept: tuple[float, list[Chain]] | None = None

    def account_chain(self) -> Chain:
        return Chain("test", NETWORKS["test"])

    def account_chains(self) -> list[Chain]:
        """Tag chain first, then each chain V1.0 has installed with its Addresses index, or borrows from arcades that
        share it (V1.0 answers coins, sends and Core calls of a borrowed chain from its lenders, as the original's
        Wallet showed borrowed chains: Martin 2026-11-12, "I'm missing a bunch of coin networks"). Asked every 5 minutes."""
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
