"""Which addresses the site will pay: the original arcade's check (arcade/web/app.py _check_address), copied whole
because that file is the old web app itself. Kept identical on purpose."""
from __future__ import annotations

from arcade.config import NETWORKS, unpayable_versions
from arcade.script import b58check_decode, segwit_decode


def _check_address(address: str, *, mainnet: bool, segwit_ok: bool = False) -> str | None:
    """Return a human-readable complaint about `address`, or None if it is fine.

    An address carries its chain in the version byte, so a mainnet address pasted
    into the testnet field is detectable -- and worth detecting, because the two
    look similar enough to confuse and the consequences differ enormously.

    What counts as detectable moved on 2026-09-25, and the direction matters.
    The question used to be "is this a mainnet-shaped address", answered from
    every params object in the file -- which on the day it was written meant a
    Dogecoin `D…` address passed the check on a Pepecoin mainnet send, and the
    coins would have gone somewhere this chain cannot even see. A version byte
    identifies a *family*, not a chain: Dogecoin testnet and Pepecoin testnet
    share 113, and this regtest shares 111 with Litecoin testnet. So the check is
    now "is this an address on one of the two chains this arcade moves coins
    on", which is the only question a send can act on.
    """
    # `mainnet` is a chain or a bool. A CHAIN is exact (2026-10-13): Dogecoin is
    # a real-coin chain too, and "mainnet" alone would let a Pepecoin address
    # through on a Dogecoin send. A bool keeps its old meaning, Pepecoin's.
    network = getattr(mainnet, "network", None)
    if network in ("test", "regtest"):
        wanted = (NETWORKS["test"], NETWORKS["regtest"])
    elif network:
        # The chain's own params: in production NETWORKS[network], and on a test chain
        # standing in for it, that chain's (2026-10-13).
        wanted = (getattr(mainnet, "params", None) or NETWORKS[network],)
    else:
        wanted = (NETWORKS["main"],) if mainnet else (NETWORKS["test"], NETWORKS["regtest"])
    word = (getattr(mainnet, "label", "") or "").lower() or ("mainnet" if mainnet else "testnet")
    # Litecoin's other address kinds, said for what they are (2026-10-18; they read as "the checksum
    # does not match" before). MWEB first: its prefix also starts like a bech32 one.
    lowered = (address or "").strip().lower()
    if lowered.startswith(("ltcmweb1", "tmweb1")):
        return ("that is a Litecoin MWEB (private) address. This arcade cannot pay one: coins sent "
                "there could not be seen or spent from here. Ask for an L… or M… address.")
    for params in NETWORKS.values():
        hrp = getattr(params, "bech32_hrp", None)
        if hrp and lowered.startswith(hrp + "1"):
            # A coin send pays native segwit v0 (2026-10-20, tbuuol): `segwit_ok` is that caller saying it
            # builds the output (`_pay_script`). Test chains first; real coins too since 2026-11-07 (SadFrog: a
            # DigiByte Core or exchange address is dgb1q... by default, and a send refused to pay it). Paying one is
            # only an output script, OP_0 <program>, checked by segwit_decode; RECEIVING on SegWit stays test-only
            # (wallet_js ADDRESS_KINDS). Taproot is not paid yet.
            if segwit_ok and params.name in {p.name for p in wanted}:
                version, program = segwit_decode(hrp, lowered)
                if version == 0:
                    return None
                if version is None:
                    return "that does not look like an address (the checksum does not match)."
                return (f"that is a {params.label} Taproot address ({hrp}1p…). This arcade pays legacy, "
                        "P2SH and native SegWit addresses for now; ask for one of those.")
            return (f"that is a {params.label} SegWit or Taproot address ({hrp}1…). This arcade pays "
                    f"{params.label}'s legacy addresses for now; ask for one of those.")
    try:
        version, payload = b58check_decode(address)
    except HTTPException:
        raise          # a rejected form is a 400, not an error page
    except Exception:
        return "that does not look like an address (the checksum does not match)."
    if len(payload) != 20:
        return "that is not a 20-byte address."
    for p in wanted:
        if version == p.scripthash_version and version in unpayable_versions(p):
            # Bellscoin's P2SH byte is Dogecoin's P2PKH "D" (2026-10-22): the string cannot say which
            # script it means, and txbuild reads 30 as a key hash, so a script hash paid that way is lost.
            return (f"that looks like a {p.label} script (P2SH) address, which shares its first letter and "
                    f"version with another chain's ordinary addresses. This arcade cannot tell them apart "
                    f"safely, so it pays {p.label}'s ordinary addresses only; ask for one of those.")
    if any(version in (p.pubkeyhash_version, p.scripthash_version) for p in wanted):
        return None
    other = [p.name for p in NETWORKS.values()
             if version in (p.pubkeyhash_version, p.scripthash_version)]
    if other:
        named = NETWORKS[other[0]].label if NETWORKS[other[0]].addon else other[0]
        return (f"that is a {named} address, not a "
                f"{word} one.")
    return ("that address is not one this arcade can pay -- it belongs to a "
            "chain this node does not run, and coins sent there cannot come "
            "back.")
