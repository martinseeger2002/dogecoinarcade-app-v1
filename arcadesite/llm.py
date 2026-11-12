"""The AI market (the original's arcade/web/llmweb.py, mounted unchanged): an account's prepaid credit, chat and
pictures from this arcade's own models, API keys (/v1/chat/completions), the Exchange's LLM tab.

llmweb.register(app, state, ...) is given a stand-in `state` that answers from the site and Arcade V1.0:
    settings / home         the site's own (llm, llm_images, llm_pay_to:<net>, operator ... move here as they are)
    chains                  rpc() = V1.0's Core-style RPC on that chain (coin payments are checked there)
    token_index(chain)      property / address_of / transaction (token payments: V1.0's /r/ledgertx record)
    payout_address(chain)   the operator account's address on that chain (no Core wallet here to make new ones)
    mesh                    none: other arcades' models are offered over the mesh (NEEDS-V1: an LLM relay)

The models run where the GPU is: `llm.url` / `llm_images.url` name 127.0.0.1 ports that a service on that machine
forwards here (ssh -R), so the settings move unchanged.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from arcade.config import NETWORKS

from .messages import V1Rpc
from .v1 import V1Error


class _Chain:
    def __init__(self, v1, network: str):
        self.v1, self.network = v1, network
        self.params = NETWORKS[network]
        self.label = getattr(self.params, "label", network)
        self.borrowed = True                                # no Core wallet here: never asked for a new address

    def rpc(self):
        return V1Rpc(self.v1, self.network)


class _TokenIndex:
    """What llmweb asks of the original's token index, from V1.0 (home chain)."""

    def __init__(self, v1):
        self.v1 = v1

    def property(self, pid: int):
        code, p = self.v1.page(f"token/{int(pid)}")
        return p if code == 200 else None

    def address_of(self, name: str) -> str:
        return self.v1.tag_owner(name)

    def transaction(self, txid: str):
        """A token send's ledger record: {valid, property_id, got, gave, amount} (simple send, type 0)."""
        code, r = self.v1.page(f"ledgertx/{txid}")
        if code != 200 or not r:
            return None
        p = str(r.get("payload_hex") or "")
        out = {"valid": bool(r.get("valid")), "got": r.get("reference"), "gave": r.get("sender"),
               "property_id": None, "amount": 0}
        if len(p) >= 32 and int(p[0:8], 16) == 0:
            out["property_id"], out["amount"] = int(p[8:16], 16), int(p[16:32], 16)
        return out


class _State:
    def __init__(self, st, chains):
        self._st, self._chains = st, chains
        self.home = st.home
        self.mesh = None
        self.llm_seller = None
        self.all_chains = [_Chain(st.v1, c.network) for c in chains.account_chains()]
        self._index = _TokenIndex(st.v1)

    def setting(self, name, default=None):
        return self._st.setting(name, default)

    def set_setting(self, name, value):
        self._st.set_setting(name, value)

    def settings(self):
        return self._st.settings()

    @property
    def operator(self) -> str:
        return str(self._st.setting("operator") or "").lower()

    def operator_tag(self) -> str:
        found = self._st.vault().by_pubkey(self.operator) if self.operator else None
        return str((found or {}).get("tag") or "")

    def chain_named(self, network: str):
        for c in self.all_chains:
            if c.network == network:
                return c
        raise ValueError(f"this arcade has no {network} chain")

    def keyed(self, chain) -> bool:
        return True

    def token_index(self, chain):
        return self._index

    def payout_address(self, chain) -> str:
        if not self.operator:
            return ""
        one = self._chains.by_network(chain.network)
        return self._chains.account_address(self.operator, one) if one is not None else ""


def register(app, st, chains):
    from arcade.web import llmweb
    state = _State(st, chains)

    def signed_in(request: Request):
        return app.state.signed_in(request)

    def admin_check(request: Request, write: bool = False):
        account = signed_in(request)
        if account is None or not state.operator or account.pubkey.lower() != state.operator:
            raise HTTPException(403, "the operator's page")
        return account.pubkey

    def name_of(address: str) -> str:
        try:
            return str((st.v1.page(f"address/{address}")[1] or {}).get("tag") or "")
        except V1Error:
            return ""

    def pay_choices(request: Request) -> list[dict]:
        return [{"key": f"{c.network}-coin", "label": c.params.coin, "kind": "coin", "net": c.network, "mine": True,
                 "held": ""} for c in chains.account_chains()]

    llmweb.register(app, state, signed_in=signed_in, account_address=chains.account_address,
                    account_chain=chains.account_chain, admin_check=admin_check, name_of=name_of,
                    pay_choices=pay_choices, render=app.state.render)
    app.state.llm_state = state
