/* ARCADE ID, the wallet's side: scan a Digi-ID code and sign in with a key on any chain this wallet holds.
 *
 * Built on Digi-ID (https://www.digi-id.io), created by the DigiByte community: the code (digiid://host/path?x=nonce,
 * &u=1 for plain http), the signed URI and the callback are Digi-ID's own, unchanged. A Digi-ID wallet answers with a
 * DigiByte key; an ARCADE wallet may answer with a key on any of its chains and says which ("chain").
 *
 * Every site gets its own key, never the key that holds coins: SLIP-0013 (hierarchical keys for authentication), the
 * way Digi-ID derives one per site, m/13'/A'/B'/C'/D' from sha256(index || identity), with the identity naming the
 * site, and the chain for an ARCADE ID ("arcadeid://doge@host"; a DigiByte answer uses "digiid://host"). So two
 * sites can't tell they saw the same person, and a signature here can never move a coin.
 *
 * What this signs is fixed: only a well-formed Digi-ID code, after the person has seen the site's address. (The
 * wallet's other signing is fixed-template too: coins.js signWords, 2026-10-26.)
 */
import * as secp from "/vendor/noble-secp256k1.js";
import * as coins from "/coins.js";
import * as signer from "/signin.js";
import * as wallet from "/wallet.js";

export const CREDIT = "ARCADE ID is built on Digi-ID, created by the DigiByte community.";
export const CREDIT_URL = "https://www.digi-id.io";

/** Chain keys an ARCADE ID may answer with, and how they read. Kotia and Wojakcoin are never offered. */
export const CHAIN_NAMES = {main: "Pepecoin", test: "Pepecoin testnet", doge: "Dogecoin", dgb: "DigiByte",
  ltc: "Litecoin", btc: "Bitcoin", bel: "Bellscoin", lky: "Luckycoin", shic: "Shibacoin", jkc: "Junkcoin",
  flop: "Flopcoin", dgbtest: "DigiByte testnet", ltctest: "Litecoin testnet4", arctest: "ARCADE testnet"};
const NOT_OFFERED = new Set(["kot", "wjk"]);
/* Each chain's own Core message prefix where it is known (verifymessage checks it); ARCADE's own otherwise.
   The server (arcadesite/digiid.py CHAIN_MAGIC) takes the same. */
const ARCADE_MAGIC = coins.ARCADE_MAGIC;
const MAGIC = {main: ARCADE_MAGIC, test: ARCADE_MAGIC, doge: "Dogecoin Signed Message:\n",
  dgb: "DigiByte Signed Message:\n", dgbtest: "DigiByte Signed Message:\n", ltc: "Litecoin Signed Message:\n",
  ltctest: "Litecoin Signed Message:\n", btc: "Bitcoin Signed Message:\n"};

const CODE = /^digiid:\/\/([a-z0-9.-]{1,253}(?::[0-9]{1,5})?)(\/[A-Za-z0-9._~\/-]{0,200})\?x=([A-Za-z0-9]{8,128})(&u=1)?$/;

/** A scanned or pasted code: {uri, host, callback, plain} or throws. Only a Digi-ID code is read. */
export function parse(text) {
  const uri = String(text || "").trim();
  const m = CODE.exec(uri);
  if (!m) throw new Error("that isn't a Digi-ID or ARCADE ID code");
  const plain = !!m[4];
  return {uri, host: m[1], plain, callback: (plain ? "http://" : "https://") + uri.slice("digiid://".length)};
}

const le32 = (b, i) => (b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24)) >>> 0;

/** This wallet's key for `host` on `chain` (SLIP-0013): {priv, pubkey, address}. */
export async function keyFor(seed, chain, host, version, index = 0) {
  const identity = chain === "dgb" ? `digiid://${host}` : `arcadeid://${chain}@${host}`;
  const data = new Uint8Array([index & 0xff, (index >> 8) & 0xff, (index >> 16) & 0xff, (index >>> 24) & 0xff,
                               ...new TextEncoder().encode(identity)]);
  const h = new Uint8Array(await crypto.subtle.digest("SHA-256", data));
  const H = coins.HARDENED;
  const node = await coins.derive(seed, [13 + H, le32(h, 0) % H + H, le32(h, 4) % H + H, le32(h, 8) % H + H,
                                         le32(h, 12) % H + H]);
  const pubkey = coins.publicKey(node.key);
  return {priv: node.key, pubkey, address: await coins.address(pubkey, version)};
}

async function signUri(priv, uri, magic) {
  const sig = await secp.signAsync(await coins.messageHash(uri, magic), priv, {lowS: true});
  const out = new Uint8Array(65);
  out[0] = 27 + sig.recovery + 4;
  const be = (n) => { const b = new Uint8Array(32); for (let i = 31; i >= 0; i--) { b[i] = Number(n & 0xffn); n >>= 8n; } return b; };
  out.set(be(sig.r), 1); out.set(be(sig.s), 33);
  return btoa(String.fromCharCode(...out));
}

/** The chains this wallet can answer with: [{network, version, name}], DigiByte (Digi-ID's own) first. */
export async function answerable() {
  const list = (await wallet.chains()).filter((c) => !NOT_OFFERED.has(c.network) && CHAIN_NAMES[c.network]);
  return list.map((c) => ({network: c.network, version: c.version, name: CHAIN_NAMES[c.network]}))
             .sort((a, b) => (b.network === "dgb") - (a.network === "dgb"));
}

/** Answer a code: sign it with this wallet's key for that site on `chain` and post it to the site's callback. */
export async function answer(code, chain, home) {
  const {uri, host, callback} = parse(code);
  const held = await wallet.opened(home);
  if (!held || !held.phrase) throw new Error("open your wallet first (sign in with your password)");
  const c = (await answerable()).find((x) => x.network === chain);
  if (!c) throw new Error("this wallet doesn't hold that chain");
  const seed = await signer.toSeed(held.phrase);
  const key = await keyFor(seed, chain, host, c.version);
  const signature = await signUri(key.priv, uri, MAGIC[chain] || ARCADE_MAGIC);
  const body = {address: key.address, uri, signature};
  if (chain !== "dgb") body.chain = chain;          // a DigiByte answer is a plain Digi-ID answer
  let r;
  try {
    r = await fetch(callback, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
                               mode: "cors", credentials: "omit"});
  } catch (e) {
    // Most Digi-ID sites were made for phone apps and don't let a web page post to them: the browser stops it. This
    // site's arcade passes the answer on instead (v1 id.relay), checked and sent once, to public addresses only.
    r = null;
  }
  if (!r) {
    let relayed;
    try {
      relayed = await fetch("/digiid/relay", {method: "POST", headers: {"Content-Type": "application/json"},
                                              body: JSON.stringify(body), credentials: "same-origin"});
    } catch (e) {
      throw new Error(`${host} didn't answer. Check you're online, and that the code is still on its screen.`);
    }
    const said = await relayed.json().catch(() => ({}));
    if (!relayed.ok) throw new Error(said.detail || `${host} didn't answer`);
    return {host, address: key.address, chain, relayed: true};
  }
  const said = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(said.detail || `${host} said no`);
  return {host, address: key.address, chain};
}
