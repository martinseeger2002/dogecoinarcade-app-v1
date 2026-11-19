/* Media pieces: this viewer's side of an Arcade V1.0 app window's camera, microphone and screen.

   An app window has no WebRTC. It encodes with WebCodecs (VP8 pictures, Opus sound) and hands the pieces
   to its arcade (realtime.media), which carries them over the arcades' fast lane (V1.0 media.js). With such
   a player this viewer does the same: it decodes their pieces into a canvas and an audio worklet, and hands
   the doors a MediaStream like a WebRTC one (peer.remote, peer.xremote), so everything the doors do with
   tracks -- placing, hearing, volume, frames, levels, spatial sound -- works unchanged. What this player
   shares with them is encoded here and sent as pieces through this site (POST /realtime/media), which
   passes them to its arcade on the seat's token. Players with WebRTC stay on WebRTC.

   A piece on the wire (as V1.0 media.js): 0xA1, kind (1 camera, 2 microphone, 3 screen, 4 screen sound,
   9 a note), flags (1: key frame), seq (u32), time (u32, ms), part, parts, width (u16), height (u16), bytes.
   A note is JSON: {kf: 1} send a key frame, {off: 1} I stopped sending, {screenOff: 1} my screen stopped.

   ArcadePieces(env) -> {heard, drop, level, sync, share, unshare, stop, can, wake}. env:
     post(h, path, body) -> Promise of the answer (the seat's token added by the caller),
     seatLost(h), changed(h, peer) (a peer's tracks changed), event(h, ev) (for the page), notice(text). */
var ArcadePieces = (function () {
  "use strict";
  var KIND = { cam: 1, mic: 2, scr: 3, snd: 4, note: 9 };
  var HEAD = 17, PIECE = 48000, OUT_MOST = 80, BATCH = 24, BATCH_BYTES = 600000;
  var CAM = { most: 640, fps: 20, bitrate: 600000 }, SCR = { most: 1280, fps: 10, bitrate: 1500000 };
  var KEY_EVERY = 2000, GONE_AFTER = 3000, RATE = 48000;

  // The worklets, from a blob: arcade-cap gives the microphone's samples as 20 ms blocks at 48 kHz, whatever the
  // context's own rate; arcade-play plays one player's 48 kHz sound at the context's rate from a small buffer (it
  // starts once 60 ms are in, plays silence when it runs dry, and drops the oldest past 300 ms), as media-worklet.js.
  function worklets() {
    function linear(src, step, state, push) {
      // From one rate to another, carrying the fraction between blocks.
      var pos = state.pos, last = state.last;
      while (pos < src.length) {
        var i = Math.floor(pos), f = pos - i, a = i === 0 ? last : src[i - 1], b = src[i];
        push(a + (b - a) * f);
        pos += step;
      }
      state.pos = pos - src.length;
      state.last = src[src.length - 1];
    }
    class Cap extends AudioWorkletProcessor {
      constructor() { super(); this.buf = new Float32Array(960); this.n = 0; this.st = { pos: 1, last: 0 }; this.step = sampleRate / 48000; }
      process(inputs, outputs) {
        var ch = inputs[0] && inputs[0][0], self = this;
        if (ch && ch.length) linear(ch, this.step, this.st, function (v) {
          self.buf[self.n++] = v;
          if (self.n === 960) { self.port.postMessage(self.buf.slice(0)); self.n = 0; }
        });
        var out = outputs[0] && outputs[0][0];
        if (out) out.fill(0);
        return true;
      }
    }
    registerProcessor("arcade-cap", Cap);
    class Play extends AudioWorkletProcessor {
      constructor() {
        super();
        this.q = []; this.head = 0; this.queued = 0; this.playing = false;
        this.JITTER = 48000 * 0.06; this.MOST = 48000 * 0.3;
        this.step = 48000 / sampleRate; this.pos = 0;
        var self = this;
        this.port.onmessage = function (e) {
          var b = e.data;
          if (!(b instanceof Float32Array) || !b.length) return;
          self.q.push(b); self.queued += b.length;
          while (self.queued > self.MOST && self.q.length > 1) { var old = self.q.shift(); self.queued -= old.length - self.head; self.head = 0; }
        };
      }
      process(inputs, outputs) {
        var out = outputs[0][0];
        if (!this.playing && this.queued >= this.JITTER) this.playing = true;
        var i = 0;
        while (this.playing && i < out.length && this.q.length) {
          var b = this.q[0], at = this.head, f = this.pos;
          var a = b[at], c = at + 1 < b.length ? b[at + 1] : (this.q[1] ? this.q[1][0] : a);
          out[i++] = a + (c - a) * f;
          f += this.step;
          while (f >= 1 && this.q.length) {
            f -= 1; this.head++; this.queued--;
            if (this.head >= this.q[0].length) { this.q.shift(); this.head = 0; }
          }
          this.pos = f;
        }
        if (i < out.length) { out.fill(0, i); if (!this.q.length) this.playing = false; }
        for (var c2 = 1; c2 < outputs[0].length; c2++) outputs[0][c2].set(out);
        return true;
      }
    }
    registerProcessor("arcade-play", Play);
  }

  function b64(u8) { var t = ""; for (var i = 0; i < u8.length; i += 0x8000) t += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(t); }
  function unb64(text) { var t = atob(text), u = new Uint8Array(t.length); for (var i = 0; i < t.length; i++) u[i] = t.charCodeAt(i); return u; }
  function header(kind, key, seq, t, part, parts, w, h) {
    var b = new Uint8Array(HEAD), v = new DataView(b.buffer);
    b[0] = 0xa1; b[1] = kind; b[2] = key ? 1 : 0;
    v.setUint32(3, seq >>> 0); v.setUint32(7, t >>> 0); b[11] = part; b[12] = parts;
    v.setUint16(13, w || 0); v.setUint16(15, h || 0);
    return b;
  }
  function parse(u) {
    if (u.length < HEAD || u[0] !== 0xa1) return null;
    var dv = new DataView(u.buffer, u.byteOffset, u.byteLength);
    return { kind: u[1], key: !!(u[2] & 1), seq: dv.getUint32(3), t: dv.getUint32(7), part: u[11], parts: u[12],
             w: dv.getUint16(13), h: dv.getUint16(15), body: u.subarray(HEAD) };
  }

  // What this browser can do, asked once: WebCodecs and the two codecs, each way.
  var can = null;
  function ask(Cls, conf) {
    if (typeof Cls !== "function" || typeof Cls.isConfigSupported !== "function") return Promise.resolve(false);
    return Cls.isConfigSupported(conf).then(function (r) { return !!(r && r.supported); }, function () { return false; });
  }
  function canDo() {
    if (can) return can;
    var W = typeof window !== "undefined" ? window : {};
    can = Promise.all([
      ask(W.VideoDecoder, { codec: "vp8", codedWidth: 640, codedHeight: 480 }),
      ask(W.AudioDecoder, { codec: "opus", sampleRate: RATE, numberOfChannels: 1 }),
      ask(W.VideoEncoder, { codec: "vp8", width: 640, height: 480, bitrate: CAM.bitrate, framerate: CAM.fps }),
      ask(W.AudioEncoder, { codec: "opus", sampleRate: RATE, numberOfChannels: 1, bitrate: 32000 }),
    ]).then(function (r) {
      var out = { decode: { video: r[0], audio: r[1] }, encode: { video: r[2], audio: r[3] },
                  worklet: typeof W.AudioWorkletNode === "function", canvas: !!(W.HTMLCanvasElement && HTMLCanvasElement.prototype.captureStream) };
      can.now = out;
      return out;
    });
    return can;
  }

  return function ArcadePieces(env) {
    var ctxP = null, ctx = null, rooms = new Set(), watching = null;

    // ---------------- sound: one context, at the device's own rate ----------------
    function audio() {
      if (ctxP) return ctxP;
      var C = window.AudioContext || window.webkitAudioContext;
      if (!C) return (ctxP = Promise.reject(new Error("no Web Audio")));
      ctx = new C({ latencyHint: "interactive" });
      // A phone holds sound until the player touches the page: any touch lets it go.
      var wake = function () { if (ctx.state === "suspended") ctx.resume().catch(function () {}); };
      ["pointerdown", "touchend", "keydown"].forEach(function (t) { window.addEventListener(t, wake, true); });
      var url = URL.createObjectURL(new Blob(["(" + worklets + ")()"], { type: "text/javascript" }));
      ctxP = ctx.audioWorklet.addModule(url).then(function () { wake(); return ctx; });
      return ctxP;
    }
    // A tap in the app's frame gives this page the right to start sound too, but none of its own events: the doors
    // call this on each of the app's media calls, and it is tried again as sound arrives.
    var woke = 0;
    function wake() {
      if (!ctx || ctx.state !== "suspended") return;
      var now = Date.now();
      if (now - woke < 500) return;
      woke = now;
      ctx.resume().catch(function () {});
    }

    // ---------------- receiving ----------------
    function pxOf(h, peer) {
      if (!peer.px) peer.px = { parts: {}, dec: {}, last: {}, level: {}, canvas: {}, track: {}, bad: {}, kfAt: 0 };
      rooms.add(h);
      watch();
      return peer.px;
    }
    function streamOf(peer, slot, make) {
      var k = slot === "cam" || slot === "mic" ? "remote" : "xremote";
      if (!peer[k] && make) peer[k] = new MediaStream();
      return peer[k];
    }
    function setTrack(h, peer, slot, track) {
      var px = peer.px, old = px.track[slot], s = streamOf(peer, slot, !!track);
      if (old === track) return;
      if (old) { try { if (s) s.removeTrack(old); old.stop(); } catch (err) {} }
      if (track) px.track[slot] = track; else delete px.track[slot];
      if (track && s) s.addTrack(track);
      env.changed(h, peer);
    }
    function askKey(h, peer) {
      var px = peer.px, now = Date.now();
      if (now - px.kfAt < 1000) return;
      px.kfAt = now;
      note(h, [peer.p.id], { kf: 1 });
    }
    function heard(h, peer, text) {
      var u; try { u = unb64(text); } catch (err) { return; }
      var x = parse(u);
      if (!x) return;
      var px = pxOf(h, peer), body = x.body;
      if (x.parts > 1) {
        var k = x.kind + "/" + x.seq, got = px.parts[k] || (px.parts[k] = { n: 0, bits: [] });
        if (!got.bits[x.part]) { got.bits[x.part] = body.slice(0); got.n++; }
        if (got.n < x.parts) return;
        delete px.parts[k];
        var all = new Uint8Array(got.bits.reduce(function (a, b) { return a + b.length; }, 0)), at = 0;
        got.bits.forEach(function (b) { all.set(b, at); at += b.length; });
        body = all;
        for (var k2 in px.parts) if (+k2.split("/")[1] < x.seq - 30) delete px.parts[k2];
      }
      if (x.kind === KIND.note) { heardNote(h, peer, body); return; }
      if (x.kind === KIND.cam || x.kind === KIND.scr) heardVideo(h, peer, x.kind === KIND.cam ? "cam" : "scr", x.key, x.t, body, x.w, x.h);
      else if (x.kind === KIND.mic || x.kind === KIND.snd) heardAudio(h, peer, x.kind === KIND.mic ? "mic" : "snd", x.t, body);
    }
    function heardNote(h, peer, body) {
      var o; try { o = JSON.parse(new TextDecoder().decode(body)); } catch (err) { return; }
      if (!o || typeof o !== "object") return;
      if (o.kf) wantKey(h);
      if (o.off) ["cam", "mic", "scr", "snd"].forEach(function (s) { quiet(h, peer, s); });
      if (o.screenOff) ["scr", "snd"].forEach(function (s) { quiet(h, peer, s); });
    }
    // One slot of theirs stopped: its track goes (a new key frame brings it back).
    function quiet(h, peer, slot) {
      var px = peer.px;
      if (!px) return;
      delete px.last[slot];
      var d = px.dec[slot];
      if (d && d.video) d.broken = true;
      setTrack(h, peer, slot, null);
    }
    function canvasOf(px, slot) {
      var c = px.canvas[slot];
      if (!c) {
        c = px.canvas[slot] = document.createElement("canvas");
        c.width = 2; c.height = 2;
        // In the page but out of sight: some browsers capture only a canvas that is in the document.
        c.style.cssText = "position:fixed;left:-10px;top:-10px;width:2px;height:2px;opacity:0;pointer-events:none";
        (document.body || document.documentElement).appendChild(c);
      }
      return c;
    }
    function paintTrack(h, peer, slot) {
      var px = peer.px;
      if (px.track[slot]) return;
      var c = canvasOf(px, slot);
      if (typeof c.captureStream !== "function") return;
      setTrack(h, peer, slot, c.captureStream().getVideoTracks()[0]);
    }
    function paint(h, peer, slot, frame) {
      var px = peer.px;
      if (!px) { frame.close(); return; }
      var c = canvasOf(px, slot), w = frame.displayWidth, hh = frame.displayHeight;
      if (c.width !== w || c.height !== hh) { c.width = w; c.height = hh; }
      c.getContext("2d").drawImage(frame, 0, 0, w, hh);
      frame.close();
      paintTrack(h, peer, slot);
    }
    // What cannot be played here is said where the picture would be (or, sound alone, in a note), never as nothing.
    function unplayable(h, peer, slot, why) {
      var px = peer.px, video = slot === "cam" || slot === "scr";
      if (px.bad[slot]) return;
      px.bad[slot] = why;
      var whose = peer.p && peer.p.tag ? "@" + peer.p.tag + "'s" : "this player's";
      var what = slot === "cam" ? "camera" : slot === "scr" ? "screen" : slot === "mic" ? "microphone" : "screen's sound";
      console.warn("arcade: can't play " + whose + " " + what + ": " + why);
      env.event(h, { type: "media-unplayable", member: peer.p, what: video ? "video" : "audio", screen: slot === "scr" || slot === "snd", why: why });
      if (video) {
        drawSorry(px, slot, "Can't play " + whose + " " + what + " here", why);
        paintTrack(h, peer, slot);
      } else env.notice("Can't play " + whose + " " + what + " in this browser (" + why + ").");
    }
    function drawSorry(px, slot, line, why) {
      var c = canvasOf(px, slot), g = c.getContext("2d");
      if (c.width < 320) { c.width = 640; c.height = 360; }
      g.fillStyle = "#1b1b1f"; g.fillRect(0, 0, c.width, c.height);
      g.fillStyle = "#fff"; g.textAlign = "center"; g.textBaseline = "middle";
      g.font = "600 " + Math.round(c.height / 14) + "px system-ui, sans-serif";
      g.fillText(line, c.width / 2, c.height / 2 - c.height / 18);
      g.fillStyle = "#bbb"; g.font = Math.round(c.height / 20) + "px system-ui, sans-serif";
      g.fillText(why, c.width / 2, c.height / 2 + c.height / 14);
      px.sorry = px.sorry || {};
      px.sorry[slot] = [line, why];
    }
    function heardVideo(h, peer, slot, key, t, body, w, hh) {
      var px = peer.px;
      px.last[slot] = Date.now();
      if (px.bad[slot]) { paintTrack(h, peer, slot); return; }
      if (typeof VideoDecoder !== "function" || typeof EncodedVideoChunk !== "function") { unplayable(h, peer, slot, "this browser has no video decoder (WebCodecs)"); return; }
      if (can && can.now && !can.now.decode.video) { unplayable(h, peer, slot, "this browser can't decode VP8 video"); return; }
      var d = px.dec[slot];
      if (!d || d.dec.state === "closed" || (key && (d.w !== w || d.h !== hh))) {
        if (!key) { askKey(h, peer); return; }
        if (d) { try { d.dec.close(); } catch (err) {} }
        d = px.dec[slot] = { video: true, w: w, h: hh, broken: false, errors: 0, dec: null };
        try {
          d.dec = new VideoDecoder({
            output: function (frame) { if (px.dec[slot] === d) paint(h, peer, slot, frame); else frame.close(); },
            error: function (e) {
              d.broken = true; d.errors++;
              if (d.errors >= 3) unplayable(h, peer, slot, "the video decoder failed (" + ((e && e.message) || e) + ")");
              else askKey(h, peer);
            },
          });
          d.dec.configure({ codec: "vp8", codedWidth: w, codedHeight: hh, optimizeForLatency: true });
        } catch (err) { unplayable(h, peer, slot, "this browser can't decode VP8 video (" + ((err && err.message) || err) + ")"); return; }
      }
      if (d.broken && !key) { askKey(h, peer); return; }
      if (d.dec.decodeQueueSize > 8 && !key) return;
      try { d.dec.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: t * 1000, data: body })); if (key) d.broken = false; }
      catch (err) { d.broken = true; askKey(h, peer); }
    }
    function mono(a) {
      var n = a.numberOfFrames, out = new Float32Array(n);
      try { a.copyTo(out, { planeIndex: 0, format: "f32-planar" }); return out; } catch (err) {}
      // A browser that only copies out its own format: interleaved or 16-bit, the first channel.
      try {
        var ch = a.numberOfChannels || 1, f = String(a.format || "");
        if (f.indexOf("s16") === 0) {
          var s = new Int16Array(f === "s16" ? n * ch : n);
          a.copyTo(s, { planeIndex: 0 });
          for (var i = 0; i < n; i++) out[i] = s[f === "s16" ? i * ch : i] / 32768;
          return out;
        }
        var all = new Float32Array(f === "f32" ? n * ch : n);
        a.copyTo(all, { planeIndex: 0 });
        for (var j = 0; j < n; j++) out[j] = all[f === "f32" ? j * ch : j];
        return out;
      } catch (err2) { return null; }
    }
    function heardAudio(h, peer, slot, t, body) {
      var px = peer.px;
      px.last[slot] = Date.now();
      if (px.bad[slot]) return;
      if (typeof AudioDecoder !== "function" || typeof EncodedAudioChunk !== "function") { unplayable(h, peer, slot, "this browser has no audio decoder (WebCodecs)"); return; }
      if (can && can.now && !can.now.decode.audio) { unplayable(h, peer, slot, "this browser can't decode Opus sound"); return; }
      if (typeof AudioWorkletNode !== "function") { unplayable(h, peer, slot, "this browser has no audio worklets"); return; }
      var d = px.dec[slot];
      if (!d || d.dec.state === "closed") {
        d = px.dec[slot] = { dec: null, node: null, track: null };
        audio().then(function (c) {
          if (px.dec[slot] !== d) return;
          d.node = new AudioWorkletNode(c, "arcade-play", { outputChannelCount: [1] });
          var out = c.createMediaStreamDestination();
          d.node.connect(out);
          d.track = out.stream.getAudioTracks()[0];
        }, function (err) { unplayable(h, peer, slot, String((err && err.message) || err)); });
        try {
          d.dec = new AudioDecoder({
            output: function (a) {
              var buf = mono(a);
              a.close();
              if (!buf || px.dec[slot] !== d) return;
              var sum = 0; for (var i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
              px.level[slot] = Math.max((px.level[slot] || 0) * 0.9, Math.sqrt(sum / buf.length) * 1.5);
              if (!d.node) return;
              wake();
              d.node.port.postMessage(buf, [buf.buffer]);
              if (d.track && px.track[slot] !== d.track) setTrack(h, peer, slot, d.track);
            },
            error: function (e) {
              try { d.dec.close(); } catch (err) {}
              if (px.dec[slot] === d) delete px.dec[slot];
              d.errors = (d.errors || 0) + 1;
              if (d.node) d.node.disconnect();
              unplayable(h, peer, slot, "the audio decoder failed (" + ((e && e.message) || e) + ")");
            },
          });
          d.dec.configure({ codec: "opus", sampleRate: RATE, numberOfChannels: 1 });
        } catch (err) { delete px.dec[slot]; unplayable(h, peer, slot, "this browser can't decode Opus sound (" + ((err && err.message) || err) + ")"); return; }
      }
      if (d.dec.state !== "configured") return;
      try { d.dec.decode(new EncodedAudioChunk({ type: "key", timestamp: t * 1000, data: body })); } catch (err) {}
    }
    // A player whose link went: their decoders, canvases and tracks.
    function drop(h, peer) {
      var px = peer.px;
      if (!px) return;
      for (var slot in px.dec) { var d = px.dec[slot]; try { d.dec.close(); } catch (err) {} if (d.node) try { d.node.disconnect(); } catch (err) {} }
      for (var s in px.track) { try { px.track[s].stop(); } catch (err) {} }
      for (var c in px.canvas) px.canvas[c].remove();
      peer.px = null;
    }
    // Every half second: slots that went quiet, a sorry picture kept fresh, levels falling.
    function watch() {
      if (watching) return;
      watching = setInterval(function () {
        var now = Date.now(), any = false;
        rooms.forEach(function (h) {
          var peers = h.fast ? h.fast.peers : null;
          if (!peers) { rooms.delete(h); return; }
          for (var id in peers) {
            var peer = peers[id], px = peer.px;
            if (!px) continue;
            any = true;
            for (var slot in px.track) if (!px.last[slot] || now - px.last[slot] > GONE_AFTER) quiet(h, peer, slot);
            if (px.sorry) for (var s in px.sorry) if (px.track[s]) drawSorry(px, s, px.sorry[s][0], px.sorry[s][1]);
            for (var l in px.level) px.level[l] *= 0.8;
          }
        });
        if (!any && !rooms.size) { clearInterval(watching); watching = null; }
      }, 500);
    }
    function level(peer, slot) {
      var px = peer && peer.px;
      return px && px.track[slot || "mic"] ? px.level[slot || "mic"] || 0 : 0;
    }

    // ---------------- sending ----------------
    function txOf(h) { return h.px || (h.px = { enc: {}, out: [], busy: false, timer: null, off: false, had: {} }); }
    function piecePeer(h, id) { var p = h.fast && h.fast.peers[id]; return p && p.pieces && p.state === "direct" ? p : null; }
    // Whom a kind goes to now: the players it is shared with who take pieces and whom the fast lane reaches.
    function targets(h, kind) {
      var md = h.media, want = kind === KIND.mic || kind === KIND.snd ? "audio" : "video";
      if (!md || !md.share) return [];
      return Object.keys(md.share).filter(function (id) { return md.share[id][want] && piecePeer(h, id); });
    }
    function sendPiece(h, kind, key, seq, t, bytes, w, hh, to) {
      var tx = txOf(h);
      if (!to.length || tx.off) return;
      var parts = Math.max(1, Math.ceil(bytes.length / PIECE));
      for (var i = 0; i < parts; i++) {
        var body = bytes.subarray(i * PIECE, (i + 1) * PIECE), msg = new Uint8Array(HEAD + body.length);
        msg.set(header(kind, key, seq, t, i, parts, w, hh)); msg.set(body, HEAD);
        tx.out.push({ to: to, b64: b64(msg), kind: kind });
      }
      // Behind: the pictures waiting go (a key frame comes next), sound and notes stay.
      if (tx.out.length > OUT_MOST) {
        tx.out = tx.out.filter(function (x) { return x.kind === KIND.mic || x.kind === KIND.snd || x.kind === KIND.note; });
        wantKey(h);
      }
      pump(h);
    }
    // One request at a time, carrying everything that waits (up to BATCH pieces): a phone far from the site sends
    // a few round trips a second, not one per piece.
    function pump(h) {
      var tx = txOf(h);
      if (tx.busy || !tx.out.length) return;
      var batch = [], bytes = 0;
      while (tx.out.length && batch.length < BATCH && (bytes < BATCH_BYTES || !batch.length)) {
        var x = tx.out.shift(); bytes += x.b64.length; batch.push({ to: x.to, b64: x.b64 });
      }
      tx.busy = true;
      Promise.resolve(env.post(h, "/realtime/media", { pieces: batch })).then(function (j) {
        if (j && !j.ok && /join again|join the room first/.test(j.error || "")) env.seatLost(h);
        // A site or arcade that does not carry them (an older one, or another node's road): none go there.
        else if (j && !j.ok && (j.detail || /fast lane/.test(j.error || ""))) { tx.off = true; tx.out = []; console.warn("arcade: this site does not carry media pieces"); }
      }, function () {}).then(function () { tx.busy = false; pump(h); });
    }
    function note(h, to, o) {
      if (!to.length) return;
      sendPiece(h, KIND.note, false, 0, 0, new TextEncoder().encode(JSON.stringify(o)), 0, 0, to);
    }
    function wantKey(h) {
      var tx = h.px;
      if (tx) ["cam", "scr"].forEach(function (s) { if (tx.enc[s]) tx.enc[s].wantKey = true; });
    }
    function startVideo(h, slot, kind, track, opt) {
      var tx = txOf(h), v = document.createElement("video");
      v.muted = true; v.playsInline = true; v.setAttribute("playsinline", ""); v.autoplay = true;
      v.style.cssText = "position:fixed;left:-10px;top:-10px;width:2px;height:2px;opacity:0;pointer-events:none";
      (document.body || document.documentElement).appendChild(v);
      v.srcObject = new MediaStream([track]); v.play().catch(function () {});
      var c = document.createElement("canvas"), g = c.getContext("2d");
      var e = tx.enc[slot] = { v: v, c: c, track: track, seq: 0, wantKey: true, lastKey: 0, w: 0, h: 0, enc: null, opt: opt, fails: 0, dead: false };
      var make = function (w, hh) {
        try { if (e.enc) e.enc.close(); } catch (err) {}
        e.enc = new VideoEncoder({
          output: function (chunk) {
            var bytes = new Uint8Array(chunk.byteLength); chunk.copyTo(bytes);
            sendPiece(h, kind, chunk.type === "key", e.seq++, Math.round(chunk.timestamp / 1000), bytes, e.w, e.h, targets(h, kind));
          },
          error: function (err) {
            e.enc = null; e.wantKey = true;
            // A browser that says it can encode VP8 and then cannot (some builds): said once, not tried for ever.
            if (++e.fails >= 3 && !e.dead) {
              e.dead = true;
              var why = "this browser can't encode VP8 video (" + ((err && err.message) || err) + ")";
              console.warn("arcade: no pictures go to players without WebRTC: " + why);
              env.event(h, { type: "media-unsendable", what: "video", screen: slot === "scr", why: why });
              env.notice("Your " + (slot === "scr" ? "screen" : "camera") + " can't go to app windows from this browser (" + why + ").");
            }
          },
        });
        e.enc.configure({ codec: "vp8", width: w, height: hh, bitrate: opt.bitrate, framerate: opt.fps, latencyMode: "realtime" });
        e.w = w; e.h = hh; e.wantKey = true;
      };
      e.timer = setInterval(function () {
        var md = h.media;
        if (txOf(h).enc[slot] !== e || e.dead || v.readyState < 2 || !v.videoWidth || !md) return;
        if (slot === "cam" && md.muted && md.muted.video) return;
        if (!targets(h, kind).length) { e.wantKey = true; return; }        // nobody to send to: nothing encoded
        var scale = Math.min(1, opt.most / Math.max(v.videoWidth, v.videoHeight));
        var w = Math.max(2, Math.round(v.videoWidth * scale / 2) * 2), hh = Math.max(2, Math.round(v.videoHeight * scale / 2) * 2);
        try { if (!e.enc || e.enc.state === "closed" || w !== e.w || hh !== e.h) make(w, hh); } catch (err) { return; }
        if (e.enc.encodeQueueSize > 4) return;                               // the encoder is behind: skip a frame
        if (c.width !== w || c.height !== hh) { c.width = w; c.height = hh; }
        g.drawImage(v, 0, 0, w, hh);
        var now = performance.now(), key = e.wantKey || now - e.lastKey > KEY_EVERY;
        var frame = new VideoFrame(c, { timestamp: Math.round(now * 1000) });
        try { e.enc.encode(frame, { keyFrame: key }); if (key) { e.wantKey = false; e.lastKey = now; } } catch (err) { e.wantKey = true; }
        frame.close();
      }, Math.round(1000 / opt.fps));
    }
    function startAudio(h, slot, kind, track) {
      var tx = txOf(h), e = tx.enc[slot] = { track: track, seq: 0, enc: null };
      audio().then(function (c) {
        if (txOf(h).enc[slot] !== e) return;                     // stopped while the sound was getting ready
        var src = e.src = c.createMediaStreamSource(new MediaStream([track]));
        var cap = e.cap = new AudioWorkletNode(c, "arcade-cap");
        var quiet0 = e.quiet = c.createGain(); quiet0.gain.value = 0;
        src.connect(cap); cap.connect(quiet0); quiet0.connect(c.destination);
        e.enc = new AudioEncoder({
          output: function (chunk) {
            var bytes = new Uint8Array(chunk.byteLength); chunk.copyTo(bytes);
            sendPiece(h, kind, true, e.seq++, Math.round(chunk.timestamp / 1000), bytes, 0, 0, targets(h, kind));
          },
          error: function () {},
        });
        e.enc.configure({ codec: "opus", sampleRate: RATE, numberOfChannels: 1, bitrate: kind === KIND.snd ? 96000 : 32000 });
        var t0 = performance.now() * 1000, sent = 0;
        cap.port.onmessage = function (ev) {
          if (txOf(h).enc[slot] !== e || e.enc.state !== "configured" || !targets(h, kind).length) return;
          var data = new AudioData({ format: "f32-planar", sampleRate: RATE, numberOfFrames: 960, numberOfChannels: 1,
                                     timestamp: t0 + sent * 20000, data: ev.data });
          sent++;
          try { e.enc.encode(data); } catch (err) {}
          data.close();
        };
      }).catch(function (err) { console.warn("arcade: no sound goes to players without WebRTC", err); });
    }
    function stopSlot(h, slot) {
      var tx = h.px, e = tx && tx.enc[slot];
      if (!e) return;
      delete tx.enc[slot];
      clearInterval(e.timer);
      try { if (e.enc && e.enc.state !== "closed") e.enc.close(); } catch (err) {}
      try { if (e.v) { e.v.srcObject = null; e.v.remove(); } } catch (err) {}
      try { if (e.src) { e.src.disconnect(); e.cap.disconnect(); e.quiet.disconnect(); } } catch (err) {}
    }
    // What goes now, from what the doors hold (h.media: stream, xstream, screen, share): a slot runs while its
    // track is live and somebody takes it as pieces. Called on every change and twice a second.
    function sources(h) {
      var md = h.media, s = md && md.stream, x = md && md.xstream;
      var live = function (t) { return t && t.readyState !== "ended" ? t : null; };
      return {
        cam: { kind: KIND.cam, track: live(s && md.video ? s.getVideoTracks()[0] : null), opt: md && md.screen ? SCR : CAM },
        mic: { kind: KIND.mic, track: live(s && md.audio ? s.getAudioTracks()[0] : null) },
        scr: { kind: KIND.scr, track: live(x ? x.getVideoTracks()[0] : null), opt: SCR },
        snd: { kind: KIND.snd, track: live(x ? x.getAudioTracks()[0] : null) },
      };
    }
    function sync(h) {
      var tx = txOf(h), src = sources(h), caps = can && can.now;
      canDo();
      if (!tx.timer) tx.timer = setInterval(function () { if (!h.fast && !h.media) { stop(h, true); return; } sync(h); }, 500);
      ["cam", "mic", "scr", "snd"].forEach(function (slot) {
        var want = src[slot], to = targets(h, want.kind), e = tx.enc[slot], video = slot === "cam" || slot === "scr";
        var able = video ? typeof VideoEncoder === "function" && (!caps || caps.encode.video)
                         : typeof AudioEncoder === "function" && typeof AudioWorkletNode === "function" && (!caps || caps.encode.audio);
        if (!want.track || !to.length || !able) {
          if (e) stopSlot(h, slot);
          // Those who had it hear that it stopped at once, not when it goes quiet.
          if (!want.track && tx.had[slot] && tx.had[slot].length) note(h, tx.had[slot], slot === "scr" || slot === "snd" ? { screenOff: 1 } : { off: 1 });
          tx.had[slot] = want.track ? to : [];
          return;
        }
        tx.had[slot] = to;
        if (e && e.track === want.track && (!video || e.opt === want.opt)) return;
        if (e) stopSlot(h, slot);
        if (video) startVideo(h, slot, want.kind, want.track, want.opt); else startAudio(h, slot, want.kind, want.track);
      });
    }
    // Shared with one more player (a new viewer needs a key frame), or no longer.
    function share(h) { wantKey(h); sync(h); }
    function unshare(h, id) {
      if (piecePeer(h, id)) note(h, [id], { off: 1 });
      sync(h);
    }
    // Everything off (quiet: the room itself is going, nobody is told).
    function stop(h, quiet0) {
      var tx = h.px;
      if (!tx) return;
      if (!quiet0) {
        var told = {};
        for (var s in tx.had) (tx.had[s] || []).forEach(function (id) { told[id] = 1; });
        var ids = Object.keys(told).filter(function (id) { return piecePeer(h, id); });
        if (ids.length) { note(h, ids, { off: 1 }); }
      }
      ["cam", "mic", "scr", "snd"].forEach(function (slot) { stopSlot(h, slot); });
      clearInterval(tx.timer); tx.timer = null; tx.had = {};
      if (quiet0) { tx.out = []; h.px = null; }
    }

    return { heard: heard, drop: drop, level: level, sync: sync, share: share, unshare: unshare, stop: stop,
             can: canDo, wake: wake, KIND: KIND, parse: parse };
  };
})();
