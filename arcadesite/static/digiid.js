/* ARCADE ID in the browser: signing in HERE with a scan (arcadesite/digiid.py has the server half and the why).
 * Built on Digi-ID (https://www.digi-id.io), created by the DigiByte community; arcadeid.js is the wallet's side.
 *
 * Turning Digi-ID on (signed in, wallet open): scan with a DigiByte wallet -> the site links that Digi-ID to this
 * account and hands this browser ITS half of a key; the browser makes the other half, seals the twelve words under
 * HKDF(both halves) with AES-GCM, and keeps the sealed copy and its own half in IndexedDB. The site keeps only its
 * half. Signing in with Digi-ID: scan -> the site hands its half back -> the words open here -> the usual seat
 * sign-in (the account key signs the site's challenge) -> the wallet is open, as after a password sign-in.
 */
import * as wallet from "/wallet.js";
import * as signer from "/signin.js";

const DB = "arcade-digiid", SHELF = "copy", ONLY = "me", DEVICE = "arcade-digiid-device";
const enc = new TextEncoder();
const hex = (b) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
const unhex = (h) => new Uint8Array((h.match(/../g) || []).map((x) => parseInt(x, 16)));

/** This browser's Digi-ID device id (made once, kept in localStorage; not a secret on its own). */
export function deviceId() {
  let id = "";
  try { id = localStorage.getItem(DEVICE) || ""; } catch (e) {}
  if (!/^[0-9a-f]{32}$/.test(id)) {
    id = hex(crypto.getRandomValues(new Uint8Array(16)));
    try { localStorage.setItem(DEVICE, id); } catch (e) {}
  }
  return id;
}

function shelf(mode) {
  return new Promise((ok, no) => {
    const open = indexedDB.open(DB, 1);
    open.onupgradeneeded = () => open.result.createObjectStore(SHELF);
    open.onerror = () => no(open.error);
    open.onsuccess = () => ok(open.result.transaction(SHELF, mode).objectStore(SHELF));
  });
}
const awaited = (r) => new Promise((ok, no) => { r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error); });

/** The sealed copy this browser keeps, or null: {tag, pubkey, address, mine, salt, nonce, sealed}. */
export async function kept() {
  try { return (await awaited((await shelf("readonly")).get(ONLY))) || null; } catch (e) { return null; }
}
async function keep(record) { await awaited((await shelf("readwrite")).put(record, ONLY)); }
export async function forgetHere() { try { await awaited((await shelf("readwrite")).delete(ONLY)); } catch (e) {} }

async function keyFrom(mine, theirs, salt) {
  const both = new Uint8Array([...unhex(mine), ...unhex(theirs)]);
  const base = await crypto.subtle.importKey("raw", both, "HKDF", false, ["deriveKey"]);
  return crypto.subtle.deriveKey({name: "HKDF", hash: "SHA-256", salt, info: enc.encode("arcade digi-id copy v1")},
                                 base, {name: "AES-GCM", length: 256}, false, ["encrypt", "decrypt"]);
}

export async function status() {
  try { return await (await fetch(`/digiid/status?device=${deviceId()}`, {cache: "no-store"})).json(); }
  catch (e) { return {on: false}; }
}

async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const said = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(said.detail || "that did not work");
  return said;
}

const CSS = `.digiid-dialog{border:1px solid #8884;border-radius:12px;padding:0;max-width:360px;width:calc(100% - 32px)}
.digiid-box{padding:18px;text-align:center}.digiid-how,.digiid-credit{font-size:13px;opacity:.8}.digiid-qr svg{width:100%;max-width:260px;height:auto;background:#fff}`;
function ensureStyle() {
  if (document.getElementById("digiid-css")) return;
  const s = document.createElement("style"); s.id = "digiid-css"; s.textContent = CSS; document.head.append(s);
}

function loadQr() {
  if (typeof qrcode === "function") return Promise.resolve();
  return new Promise((ok, no) => {
    const s = document.createElement("script"); s.src = "/vendor/qrcode.js"; s.onload = ok; s.onerror = no;
    document.head.append(s);
  });
}

/** Show a Digi-ID code in a dialog and wait for the wallet's scan. Resolves with the poll's answer. */
async function scan(purpose, title, lead) {
  const started = await post("/digiid/start", {purpose, device: deviceId()});
  await loadQr().catch(() => {});
  ensureStyle();
  const dlg = document.createElement("dialog");
  dlg.className = "digiid-dialog";
  dlg.innerHTML = `<form method="dialog" class="digiid-box">
      <h2></h2><p class="digiid-lead"></p>
      <div class="digiid-qr" aria-label="Digi-ID QR code"></div>
      <p class="digiid-how">Scan with any ARCADE wallet (Wallet \u203a ARCADE ID \u203a Scan a code) or a Digi-ID wallet such as DigiByte Go.</p>
      <p><a class="btn small digiid-open">Open in my Digi-ID wallet</a></p>
      <p class="digiid-said" role="status">Waiting for your wallet…</p>
      <p><button class="btn ghost small" value="cancel">Cancel</button></p>
      <p class="digiid-credit">ARCADE ID is built on <a href="https://www.digi-id.io" target="_blank" rel="noopener">Digi-ID</a>, created by the DigiByte community.</p></form>`;
  dlg.querySelector("h2").textContent = title;
  dlg.querySelector(".digiid-lead").textContent = lead;
  dlg.querySelector(".digiid-open").href = started.uri;
  const pic = dlg.querySelector(".digiid-qr");
  try {
    const qr = qrcode(0, "M"); qr.addData(started.uri); qr.make();
    pic.innerHTML = qr.createSvgTag({cellSize: 5, margin: 2, scalable: true});
  } catch (e) { pic.textContent = started.uri; }
  document.body.append(dlg);
  dlg.showModal();
  const said = dlg.querySelector(".digiid-said");
  const until = Date.now() + (started.expires || 300) * 1000;
  try {
    while (dlg.open) {
      if (Date.now() > until) throw new Error("the code expired; try again");
      await new Promise((ok) => setTimeout(ok, 1500));
      if (!dlg.open) break;
      const p = await post("/digiid/poll", {nonce: started.nonce, poll: started.poll});
      if (p.state === "waiting") continue;
      if (p.state === "done") { said.textContent = "Scanned."; return p; }
      throw new Error(p.detail || "the code expired; try again");
    }
    throw new Error("cancelled");
  } finally {
    dlg.close(); dlg.remove();
  }
}

/** Turn Digi-ID on in this browser. The wallet must be open (signed in with the password). */
export async function turnOn(chain) {
  const held = await wallet.opened(chain);
  if (!held || !held.phrase) throw new Error("open your wallet with your password first, then turn on Digi-ID");
  const p = await scan("link", "Turn on ARCADE ID",
    "Scan this with your ID. From then on, this browser signs you in with a scan, no password.");
  const mine = hex(crypto.getRandomValues(new Uint8Array(32)));
  const salt = crypto.getRandomValues(new Uint8Array(16)), nonce = crypto.getRandomValues(new Uint8Array(12));
  const key = await keyFrom(mine, p.half, salt);
  const sealed = await crypto.subtle.encrypt({name: "AES-GCM", iv: nonce}, key, enc.encode(held.phrase));
  const NAMES = {dgb: "DigiByte", main: "Pepecoin", test: "Pepecoin testnet", doge: "Dogecoin", ltc: "Litecoin", btc: "Bitcoin"};
  await keep({tag: p.tag || "", pubkey: held.pubkey, address: p.address, chain: p.chain || "dgb",
              chainName: NAMES[p.chain || "dgb"] || p.chain, mine,
              salt: hex(salt), nonce: hex(nonce), sealed: hex(sealed), at: Date.now()});
  return {address: p.address};
}

/** Turn it off here (and on the site), or everywhere. */
export async function turnOff(everywhere = false) {
  await post("/digiid/off", everywhere ? {all: true} : {device: deviceId()});
  await forgetHere();
}

/** Sign in with Digi-ID: scan, open the words kept here, take the seat as a password sign-in does. */
export async function signIn(chain) {
  const copy = await kept();
  if (!copy) throw new Error("ARCADE ID isn't turned on in this browser: sign in with your password, then turn it on");
  const p = await scan("login", "Sign in with ARCADE ID", "Scan this with the ID you turned it on with" +
                       (copy.chainName ? ` (your ${copy.chainName} ID).` : "."));
  let phrase;
  try {
    const key = await keyFrom(copy.mine, p.half, unhex(copy.salt));
    phrase = new TextDecoder().decode(await crypto.subtle.decrypt({name: "AES-GCM", iv: unhex(copy.nonce)}, key,
                                                                  unhex(copy.sealed)));
  } catch (e) {
    throw new Error("this browser's ARCADE ID copy didn't open; sign in with your password and turn it on again");
  }
  const held = await wallet.everyChain(await wallet.walletFrom(phrase, chain.network, chain.version),
                                       await wallet.chains());
  if (p.pubkey && held.pubkey !== p.pubkey) throw new Error("that ID belongs to another account. Nothing was opened.");
  const challenge = await (await fetch("/auth/challenge")).json();
  const origin = signer.loginOrigin();
  const signature = await held.sign(signer.loginMessage(origin, challenge.nonce));
  await post("/auth/login", {pubkey: held.pubkey, nonce: challenge.nonce, origin, signature: hex(signature), join: false});
  wallet.remember(held.phrase);
  return {tag: copy.tag};
}
