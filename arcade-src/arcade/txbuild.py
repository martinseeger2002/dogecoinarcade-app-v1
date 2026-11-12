"""Raw transaction construction.

Only what is needed to place our own outputs into a transaction. The node's
wallet then adds inputs and change (`fundrawtransaction`) and signs
(`signrawtransaction`). **This module never sees a private key.**

Why build raw rather than use `createrawtransaction`: that RPC only expresses
outputs as {address: amount} or {"data": hex}, and Class B needs bare multisig
scriptPubKeys, which it cannot represent.
"""

from __future__ import annotations

from .script import OP_CHECKMULTISIG, OP_RETURN, b58check_decode


def varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def push(data: bytes) -> bytes:
    """Minimal push encoding. Non-minimal pushes are non-standard."""
    if len(data) < 0x4C:
        return bytes([len(data)]) + data
    if len(data) <= 0xFF:
        return b"\x4c" + bytes([len(data)]) + data
    if len(data) <= 0xFFFF:
        return b"\x4d" + len(data).to_bytes(2, "little") + data
    return b"\x4e" + len(data).to_bytes(4, "little") + data


#: The script-hash version bytes of the chains an arcade pays (config.py:
#: Pepecoin mainnet 22, its testnet and the regtest 196). None of them is also a
#: pubkey-hash version on those chains, so the byte alone says which script.
#: Litecoin (2026-10-17): 50 ("M") and its legacy 5 ("3") on mainnet, 58 ("Q") on regtest -- without
#: them an M-address got a pubkey-hash script and the coins were lost.
#: DigiByte (2026-10-25): 63 ("S"), no chain's key-hash byte. Without it an S-address got a pubkey-hash script.
#: Kotia's 45 ("K", 2026-10-25) was missing the same way since Kotia was added: _check_address took a Kotia script
#: address and this gave it a key-hash script. 45 is no chain's key-hash byte (Kotia's own is 46).
#: DigiByte testnet's 140 (2026-11-04), no chain's key-hash byte either.
SCRIPTHASH_VERSIONS = frozenset({22, 196, 50, 5, 58, 63, 45, 140})


#: Version bytes that are one chain's key hash and another's script hash, so the byte alone cannot say which script
#: pays it (2026-11-12): 63 is DigiByte's P2SH ("S...") and Shibacoin's P2PKH ("S..." too). An address with one of
#: these is paid only when the chain it is paid on is named (`params`); without it, refused rather than guessed.
AMBIGUOUS_VERSIONS = frozenset({63})


def _scripthash(version: int, address: str, params=None) -> bool:
    if params is not None:
        if version == getattr(params, "pubkeyhash_version", None):
            return False
        if version == getattr(params, "scripthash_version", None):
            return True
    elif version in AMBIGUOUS_VERSIONS:
        raise ValueError(f"address {address!r} is a DigiByte script address or a Shibacoin address: the chain it is "
                         f"paid on must be named. Nothing was built.")
    return version in SCRIPTHASH_VERSIONS


def p2pkh_script(address: str, params=None) -> bytes:
    """The script that pays `address` (on the chain `params` describes, when given). A script-hash address gets
    OP_HASH160 <20> OP_EQUAL (2026-09-29: refereed prize pools live at one).
    Before that date every address got the pubkey-hash script, so coins sent to
    a script-hash address -- which `_check_address` accepts -- would have gone
    to a key nobody holds. On a chain named by `params`, its own key-hash byte is a key hash and its own script-hash
    byte a script hash; any other byte reads as before. A byte two chains read differently (AMBIGUOUS_VERSIONS)
    needs `params`."""
    version, h160 = b58check_decode(address)
    if len(h160) != 20:
        raise ValueError(f"address {address!r} does not contain a 20-byte hash")
    if _scripthash(version, address, params):
        return b"\xa9" + push(h160) + b"\x87"
    return b"\x76\xa9" + push(h160) + b"\x88\xac"


def is_scripthash(address: str, params=None) -> bool:
    """Whether `address` is a script hash (on the chain `params` describes, when given). Without `params`, a byte two
    chains read differently (AMBIGUOUS_VERSIONS) reads as a script hash, as it did before Shibacoin."""
    try:
        version = b58check_decode(address)[0]
    except Exception:                                    # noqa: BLE001
        return False
    if params is None:
        return version in SCRIPTHASH_VERSIONS
    return _scripthash(version, address, params)


def op_return_script(data: bytes) -> bytes:
    return bytes([OP_RETURN]) + push(data)


def multisig_script(pubkeys: list[bytes], required: int = 1) -> bytes:
    """OP_m <key>... OP_n OP_CHECKMULTISIG.

    Only x-of-3 is standard (`policy/policy.cpp:41-49`), so `pubkeys` must hold
    at most three keys.
    """
    if not 1 <= required <= len(pubkeys) <= 3:
        raise ValueError(f"non-standard multisig: {required}-of-{len(pubkeys)}")
    body = b"".join(push(k) for k in pubkeys)
    return bytes([0x50 + required]) + body + bytes([0x50 + len(pubkeys)]) + bytes([OP_CHECKMULTISIG])


def time_field(ntime: int | None) -> bytes:
    """A Peercoin-style chain's `nTime` as it sits after the version (Params.tx_time); b"" for every other chain."""
    return b"" if ntime is None else int(ntime).to_bytes(4, "little")


def build_raw_tx(
    inputs: list[tuple[str, int]],
    outputs: list[tuple[int, bytes]],
    version: int = 1,
    locktime: int = 0,
    ntime: int | None = None,
) -> str:
    """Serialize an unsigned transaction. `outputs` is [(value_sats, script)]. `ntime`: a Peercoin-style
    chain's transaction time (Params.tx_time, Kotia), written after the version; None writes nothing."""
    raw = version.to_bytes(4, "little") + time_field(ntime) + varint(len(inputs))
    for txid, index in inputs:
        raw += bytes.fromhex(txid)[::-1]          # txids display big-endian
        raw += index.to_bytes(4, "little")
        raw += varint(0)                           # empty scriptSig; the wallet signs
        raw += b"\xff\xff\xff\xff"
    raw += varint(len(outputs))
    for value, script in outputs:
        raw += value.to_bytes(8, "little") + varint(len(script)) + script
    raw += locktime.to_bytes(4, "little")
    return raw.hex()
