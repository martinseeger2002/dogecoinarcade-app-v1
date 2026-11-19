/* Person cutout for the media layer: room.media.start({video: true, cutout: 'blur' | 'composite' | 'transparent'}).

   The same file runs in an Arcade V1.0 app window (crates/arcade-desktop/dist) and in a website's viewer (served by
   the site at /cutout/). It runs only there, never in the app's page: the page still gets no camera frames.

   - A small person-segmentation network (MediaPipe's selfie segmenter, landscape, 256x144, Apache-2.0; see
     cutout-LICENSE.txt) runs here, in a worker when the browser gives one (this same file), else on the page:
     plain JavaScript, with its 1x1 convolutions in a little WebAssembly (SIMD) where the browser and the page's
     policy allow it. No WebGL, nothing fetched but this file and cutout-model.bin from where this file came
     from, and no frame leaves the computer except as the call's own encoded picture.
   - The mask comes out at 256x144, smoothed over time, softened at the edge, and is stretched over the picture.
     Masks come as fast as the computer allows (at most fps a second); a worker that runs slowly gives way to
     the page, and below minFps the cutout turns off and says why.
   - blur: the background behind the person is blurred. composite: the sender's own shared screen goes behind
     the person. transparent: the picture that goes is the blurred one, and beside it goes the mask, small (128x72,
     about 1 KB), so a viewer that knows masks draws the person alone, and one that does not shows the blurred
     picture.

   A mask on the wire is a media piece of kind 5 (as media.js and media_pieces.js: 0xA1, kind, flags, seq, time,
   part, parts, width, height, bytes). Time is the time of the camera picture it goes with. The bytes: 1 (the
   format), then runs over the rows: a byte (value << 4 | n), value 0-15 (0 background, 15 person), a run of n + 1
   when n < 15, else of 16 + a number in 7-bit groups that follows.

   ArcadeCutout.start(opt) -> a cutter. opt: {track (the camera's), mode, screen (a screen track or null),
     base (where cutout.js and the model are), fps, most (the longer side), minFps (masks a second below which it
     gives up, 5), segment (tests: a function of an ImageData at 256x144 that gives a Uint8Array of 0-255),
     onstate (fn({mode, want, why})), onframe (fn())}.
   cutter: canvas (the picture, opaque), alpha (the person on nothing, in transparent mode), track() (the canvas
     as a track), mask() (the latest mask to send, or null), setMode, setScreen, stats(), stop().
   ArcadeCutout.KIND_MASK, encodeMask, decodeMask, maskFor(list, t), paintMasked(canvas, source, mask), and stub (tests
   only: a segment function every cutter of this page uses). */
(function (root) {
  "use strict";
  const IN_W = 256, IN_H = 144, SEND_W = 128, SEND_H = 72;
  const MIN_FPS = 5, SLOW_AFTER = 4000, KIND_MASK = 5;
  const MODES = ["blur", "composite", "transparent"];

  // ---------------- the network ----------------
  function half(h) {
    const s = h & 0x8000 ? -1 : 1, e = (h >> 10) & 0x1f, f = h & 0x3ff;
    if (e === 0) return s * f * Math.pow(2, -24);
    if (e === 31) return f ? NaN : s * Infinity;
    return s * (1 + f / 1024) * Math.pow(2, e - 15);
  }
  // The 1x1 convolution in WebAssembly with SIMD (scripts/cutout_kernels.wat, built by scripts/cutout_wasm.js), used
  // where the browser runs it and the page's policy lets it: about three times as fast as the JavaScript below,
  // which is the same arithmetic and runs everywhere else.
  const KERNELS = "AGFzbQEAAAABCwFgB39/f39/f38AAgoBAW0DbWVtAgABAwIBAAcJAQVjb252MQAACo0FAYoFAwt/BHsCfSACQQJ0IQwgAkF8cUECdCENQQAhBwJAA0AgByAETw0BIAEgByAMbGohCiAFIAcgA2xBAnRqIQsgBiAHQQJ0aioCACEWIBb9EyESQQAhCQJAA0AgCSANTw0BIAogCWogEv0LBAAgCUEQaiEJDAALCwJAA0AgCSAMTw0BIAogCWogFjgCACAJQQRqIQkMAAsLQQAhCAJAA0AgCEEEaiADSw0BIAsgCEECdGr9CQIAIRIgCyAIQQJ0av0JAgQhEyALIAhBAnRq/QkCCCEUIAsgCEECdGr9CQIMIRUgACAIIAxsaiEOIA4gDGohDyAPIAxqIRAgECAMaiERQQAhCQJAA0AgCSANTw0BIAogCWogCiAJav0ABAAgEiAOIAlq/QAEAP3mAf3kASATIA8gCWr9AAQA/eYBIBQgECAJav0ABAD95gH95AEgFSARIAlq/QAEAP3mAf3kAf3kAf0LBAAgCUEQaiEJDAALCwJAA0AgCSAMTw0BIAogCWogCiAJaioCACAS/R8AIA4gCWoqAgCUIBP9HwAgDyAJaioCAJSSIBT9HwAgECAJaioCAJQgFf0fACARIAlqKgIAlJKSkjgCACAJQQRqIQkMAAsLIAhBBGohCAwACwsCQANAIAggA08NASALIAhBAnRqKgIAIRcgF/0TIRIgACAIIAxsaiEOQQAhCQJAA0AgCSANTw0BIAogCWogCiAJav0ABAAgEiAOIAlq/QAEAP3mAf3kAf0LBAAgCUEQaiEJDAALCwJAA0AgCSAMTw0BIAogCWogCiAJaioCACAXIA4gCWoqAgCUkjgCACAJQQRqIQkMAAsLIAhBAWohCAwACwsgB0EBaiEHDAALCws=";
  let kernelMod;
  function kernels() {
    if (kernelMod !== undefined) return kernelMod;
    kernelMod = null;
    try {
      if (typeof WebAssembly === "object" && KERNELS) kernelMod = new WebAssembly.Module(Uint8Array.from(atob(KERNELS), (c) => c.charCodeAt(0)));
    } catch (err) {}
    return kernelMod;
  }
  // buf: cutout-model.bin. plain: JavaScript only. Every tensor and weight lives in one memory (the WebAssembly's).
  function Net(buf, plain) {
    const u8 = new Uint8Array(buf), dv = new DataView(buf);
    if (String.fromCharCode(u8[0], u8[1], u8[2], u8[3]) !== "ACUT") throw new Error("not a cutout model");
    const n = dv.getUint32(4, true), head = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + n)));
    const at = 8 + n + ((4 - ((8 + n) % 4)) % 4), count = (buf.byteLength - at) / 2;
    // Where everything goes, in floats: the weights, every tensor, and the first step's copied-out planes and weights.
    let size = count;
    const place = (k) => { const off = size; size += (k + 3) & ~3; return off; };
    const tens = head.tensors.map((sh) => place(sh[0] * sh[1] * sh[2]));
    for (const st of head.steps) if (st.op === "conv" && !(st.kh === 1 && st.kw === 1 && st.s === 1)) {
      const o = head.tensors[st.out], k = st.kh * st.kw * st.cin;
      st.colsAt = place(k * o[0] * o[1]); st.w1At = place(st.cout * k);
    } else if (st.op === "tconv") {
      const i = head.tensors[st.in];
      st.quadAt = place(4 * i[0] * i[1]); st.b4At = place(4);
    }
    let heap = null, k1 = null;
    const mod = plain ? null : kernels();
    if (mod) {
      try {
        const mem = new WebAssembly.Memory({ initial: Math.ceil(size * 4 / 65536) + 1 });
        k1 = new WebAssembly.Instance(mod, { m: { mem } }).exports.conv1;
        heap = mem.buffer;
      } catch (err) { k1 = null; }
    }
    if (!heap) heap = new ArrayBuffer(size * 4);
    const w = new Float32Array(heap, 0, count), raw = new Uint16Array(buf, at, count), table = new Float32Array(65536);
    for (let i = 0; i < 65536; i++) table[i] = half(i);
    for (let i = 0; i < count; i++) w[i] = table[raw[i]];
    this.head = head; this.w = w; this.k1 = k1; this.fast = !!k1;
    this.t = head.tensors.map((sh, i) => new Float32Array(heap, tens[i] * 4, sh[0] * sh[1] * sh[2]));
    for (const st of head.steps) {
      if (st.op === "conv" || st.op === "dw") {
        st.W = w.subarray(st.w, st.w + (st.op === "conv" ? st.cout * st.kh * st.kw * st.cin : st.kh * st.kw * st.cin));
        st.B = w.subarray(st.b, st.b + st.cout);
        if (st.colsAt !== undefined) {
          const o = head.tensors[st.out], kk = st.kh * st.kw * st.cin;
          st.cols = new Float32Array(heap, st.colsAt * 4, kk * o[0] * o[1]);
          st.w1 = new Float32Array(heap, st.w1At * 4, st.cout * kk);
          for (let oc = 0; oc < st.cout; oc++) for (let ky = 0; ky < st.kh; ky++) for (let kx = 0; kx < st.kw; kx++) for (let i = 0; i < st.cin; i++)
            st.w1[oc * kk + (i * st.kh + ky) * st.kw + kx] = st.W[((oc * st.kh + ky) * st.kw + kx) * st.cin + i];
        }
      } else if (st.op === "tconv") {
        if (st.cout !== 1) throw new Error("a transposed convolution of one channel only");
        const i = head.tensors[st.in];
        st.W = w.subarray(st.w, st.w + 4 * st.cin); st.B = w.subarray(st.b, st.b + 1);
        st.quad = new Float32Array(heap, st.quadAt * 4, 4 * i[0] * i[1]);
        st.B4 = new Float32Array(heap, st.b4At * 4, 4).fill(st.B[0]);
      }
    }
  }
  // Tensors are kept a channel at a time (all of channel 0, then channel 1, ...): every step is then a few long,
  // simple loops, which is what makes plain JavaScript fast enough for this.
  const ACT = { relu: 1, hswish: 2, sigmoid: 3 };
  function actAll(kind, y, from, to) {
    if (kind === 1) for (let i = from; i < to; i++) { if (y[i] < 0) y[i] = 0; }
    else if (kind === 2) for (let i = from; i < to; i++) { const v = y[i], r = v + 3; y[i] = r <= 0 ? 0 : r >= 6 ? v : v * r / 6; }
    else if (kind === 3) for (let i = from; i < to; i++) y[i] = 1 / (1 + Math.exp(-y[i]));
  }
  // 1x1: each output channel is its bias plus the input channels, weighted; four at a time.
  function conv1(x, y, n, cin, cout, w, b, k1) {
    if (k1) { k1(x.byteOffset, y.byteOffset, n, cin, cout, w.byteOffset, b.byteOffset); return; }
    for (let o = 0; o < cout; o++) {
      const yo = o * n, wo = o * cin, bo = b[o];
      for (let p = 0; p < n; p++) y[yo + p] = bo;
      let i = 0;
      for (; i + 3 < cin; i += 4) {
        const w0 = w[wo + i], w1 = w[wo + i + 1], w2 = w[wo + i + 2], w3 = w[wo + i + 3];
        const x0 = i * n, x1 = x0 + n, x2 = x1 + n, x3 = x2 + n;
        for (let p = 0; p < n; p++) y[yo + p] += w0 * x[x0 + p] + w1 * x[x1 + p] + w2 * x[x2 + p] + w3 * x[x3 + p];
      }
      for (; i < cin; i++) {
        const wv = w[wo + i], xo = i * n;
        for (let p = 0; p < n; p++) y[yo + p] += wv * x[xo + p];
      }
    }
  }
  // One weighted copy of input plane xo into output plane yo, for kernel cell (ky, kx), with stride and padding.
  function tap(x, y, xo, yo, wv, H, W, IH, IW, s, ky, kx, py, px) {
    let lo = Math.ceil((px - kx) / s), hi = Math.floor((IW - 1 + px - kx) / s);
    if (lo < 0) lo = 0;
    if (hi > W - 1) hi = W - 1;
    for (let oy = 0; oy < H; oy++) {
      const iy = oy * s + ky - py;
      if (iy < 0 || iy >= IH) continue;
      const yr = yo + oy * W, xr = xo + iy * IW + kx - px;
      if (s === 1) for (let ox = lo; ox <= hi; ox++) y[yr + ox] += wv * x[xr + ox];
      else for (let ox = lo; ox <= hi; ox++) y[yr + ox] += wv * x[xr + ox * s];
    }
  }
  function convK(st, x, y, H, W, IH, IW, k1) {
    const w = st.W, b = st.B, kh = st.kh, kw = st.kw, s = st.s, cin = st.cin, cout = st.cout, n = H * W, dw = st.op === "dw";
    const py = st.same ? padBefore(H, IH, kh, s) : 0, px = st.same ? padBefore(W, IW, kw, s) : 0, m = IH * IW;
    if (!dw) {
      // A full convolution (the first step only): each kernel cell of each input channel copied out as a plane
      // once, and then it is a 1x1 over those planes.
      const k = kh * kw * cin, cols = st.cols;
      cols.fill(0);
      for (let i = 0; i < cin; i++) for (let ky = 0; ky < kh; ky++) for (let kx = 0; kx < kw; kx++)
        tap(x, cols, i * m, ((i * kh + ky) * kw + kx) * n, 1, H, W, IH, IW, s, ky, kx, py, px);
      conv1(cols, y, n, k, cout, st.w1, b, k1);
      return;
    }
    for (let o = 0; o < cout; o++) {
      const yo = o * n, bo = b[o];
      for (let p = 0; p < n; p++) y[yo + p] = bo;
      for (let ky = 0; ky < kh; ky++) for (let kx = 0; kx < kw; kx++) tap(x, y, o * m, yo, w[(ky * kw + kx) * cin + o], H, W, IH, IW, s, ky, kx, py, px);
    }
  }
  function resize(x, y, C, H, W, IH, IW) {
    const fy = IH / H, fx = IW / W, m = IH * IW, n = H * W;
    for (let oy = 0; oy < H; oy++) {
      const sy = Math.max(0, (oy + 0.5) * fy - 0.5), y0 = Math.floor(sy), y1 = Math.min(y0 + 1, IH - 1), ly = sy - y0;
      for (let ox = 0; ox < W; ox++) {
        const sx = Math.max(0, (ox + 0.5) * fx - 0.5), x0 = Math.floor(sx), x1 = Math.min(x0 + 1, IW - 1), lx = sx - x0;
        const a00 = y0 * IW + x0, a01 = y0 * IW + x1, a10 = y1 * IW + x0, a11 = y1 * IW + x1, yi = oy * W + ox;
        for (let c = 0; c < C; c++) {
          const q = c * m, top = x[q + a00] + (x[q + a01] - x[q + a00]) * lx, bot = x[q + a10] + (x[q + a11] - x[q + a10]) * lx;
          y[c * n + yi] = top + (bot - top) * ly;
        }
      }
    }
  }
  // Each step a small function of its own: engines optimize those better than loops inside one big one.
  function addAll(x, x2, y) { for (let k = 0; k < y.length; k++) y[k] = x[k] + x2[k]; }
  function mulAll(x, x2, y, C, n) {
    if (x2.length === y.length) for (let k = 0; k < y.length; k++) y[k] = x[k] * x2[k];
    else for (let c = 0; c < C; c++) { const v = x2[c], q = c * n; for (let k = q; k < q + n; k++) y[k] = x[k] * v; }   // one value per channel
  }
  function meanAll(x, y, C, n) {
    for (let c = 0; c < C; c++) { let s = 0; const q = c * n; for (let k = q; k < q + n; k++) s += x[k]; y[c] = s / n; }
  }
  // Kernel 2, stride 2, one output channel: each input cell makes a 2x2 block. As a 1x1 into the four cells' planes
  // (st.quad), then laid out.
  function tconv(st, x, y, IH, IW, k1) {
    const m = IH * IW, q = st.quad, W = IW * 2;
    conv1(x, q, m, st.cin, 4, st.W, st.B4, k1);
    for (let d = 0; d < 4; d++) {
      const dy = d >> 1, dx = d & 1, qo = d * m;
      for (let iy = 0; iy < IH; iy++) { const r = (iy * 2 + dy) * W + dx, qr = qo + iy * IW; for (let ix = 0; ix < IW; ix++) y[r + ix * 2] = q[qr + ix]; }
    }
  }
  // TensorFlow's SAME padding: what goes before the first row (the rest goes after the last).
  const padBefore = (out, inn, k, s) => Math.floor(Math.max((out - 1) * s + k - inn, 0) / 2);
  // input: IN_H x IN_W x 3 (rows of RGB, 0-1). The answer: IN_H x IN_W, the person's likelihood.
  Net.prototype.run = function (input) {
    const T = this.t, sh = this.head.tensors, x0 = T[this.head.input], n0 = input.length / 3;
    for (let p = 0; p < n0; p++) { x0[p] = input[p * 3]; x0[n0 + p] = input[p * 3 + 1]; x0[2 * n0 + p] = input[p * 3 + 2]; }
    for (const st of this.head.steps) {
      const x = T[st.in], y = T[st.out], o = sh[st.out], i = sh[st.in], a = ACT[st.act] || 0, op = st.op;
      if (op === "conv" && st.kh === 1 && st.kw === 1 && st.s === 1) conv1(x, y, o[0] * o[1], st.cin, st.cout, st.W, st.B, this.k1);
      else if (op === "conv" || op === "dw") convK(st, x, y, o[0], o[1], i[0], i[1], this.k1);
      else if (op === "relu" || op === "hswish" || op === "sigmoid") { y.set(x); actAll(ACT[op], y, 0, y.length); }
      else if (op === "add") addAll(x, T[st.in2], y);
      else if (op === "mul") mulAll(x, T[st.in2], y, o[2], o[0] * o[1]);
      else if (op === "mean") meanAll(x, y, i[2], i[0] * i[1]);
      else if (op === "resize") resize(x, y, o[2], o[0], o[1], i[0], i[1]);
      else if (op === "tconv") tconv(st, x, y, i[0], i[1], this.k1);
      else throw new Error("unknown step " + op);
      if (a && (op === "conv" || op === "dw")) actAll(a, y, 0, y.length);
    }
    return T[this.head.output];
  };

  // ---------------- from a picture to a mask ----------------
  // One segmenter's state: the network (or a test's stand-in) and the last mask, for smoothing over time.
  function Segmenter(net, stub) {
    this.net = net; this.stub = stub || null; this.prev = null;
    this.input = new Float32Array(IN_W * IN_H * 3);
  }
  // rgba: IN_W x IN_H pixels. The answer: IN_W x IN_H values, 0 (background) to 255 (person).
  Segmenter.prototype.run = function (rgba) {
    const n = IN_W * IN_H;
    let p;
    if (this.stub) {
      const got = this.stub({ data: rgba, width: IN_W, height: IN_H });
      p = new Float32Array(n);
      for (let i = 0; i < n; i++) p[i] = got[i] / 255;
    } else {
      const x = this.input;
      for (let i = 0, j = 0; i < n; i++, j += 4) { x[i * 3] = rgba[j] / 255; x[i * 3 + 1] = rgba[j + 1] / 255; x[i * 3 + 2] = rgba[j + 2] / 255; }
      p = Float32Array.from(this.net.run(x));
    }
    // Over time: a little of the last mask, so the edge does not flicker; sharper in the middle, soft at the edge.
    if (this.prev) for (let i = 0; i < n; i++) p[i] = p[i] * 0.7 + this.prev[i] * 0.3;
    this.prev = p;
    const out = new Uint8Array(n), soft = new Float32Array(n);
    for (let i = 0; i < n; i++) { const v = Math.min(1, Math.max(0, (p[i] - 0.3) / 0.4)); soft[i] = v * v * (3 - 2 * v); }
    for (let yy = 0; yy < IN_H; yy++) for (let xx = 0; xx < IN_W; xx++) {        // a 3x3 average: the feather
      let s = 0, c = 0;
      for (let dy = -1; dy <= 1; dy++) {
        const y2 = yy + dy;
        if (y2 < 0 || y2 >= IN_H) continue;
        for (let dx = -1; dx <= 1; dx++) { const x2 = xx + dx; if (x2 >= 0 && x2 < IN_W) { s += soft[y2 * IN_W + x2]; c++; } }
      }
      out[yy * IN_W + xx] = Math.round(s / c * 255);
    }
    return out;
  };

  // ---------------- masks on the wire ----------------
  function shrink(m, w, h, W, H) {
    const out = new Uint8Array(W * H), fx = w / W, fy = h / H;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      let s = 0, c = 0;
      for (let yy = Math.floor(y * fy); yy < Math.floor((y + 1) * fy); yy++) for (let xx = Math.floor(x * fx); xx < Math.floor((x + 1) * fx); xx++) { s += m[yy * w + xx]; c++; }
      out[y * W + x] = c ? Math.round(s / c) : 0;
    }
    return out;
  }
  function encodeMask(m) {
    const out = [1];
    let i = 0;
    while (i < m.length) {
      const v = Math.round(m[i] / 17);
      let j = i + 1;
      while (j < m.length && Math.round(m[j] / 17) === v) j++;
      const run = j - i;
      if (run <= 15) out.push((v << 4) | (run - 1));
      else {
        out.push((v << 4) | 15);
        let rest = run - 16;
        do { let b = rest & 0x7f; rest >>>= 7; if (rest) b |= 0x80; out.push(b); } while (rest);
      }
      i = j;
    }
    return new Uint8Array(out);
  }
  // body: the bytes after the piece's header. null when they are not a mask of w x h.
  function decodeMask(body, w, h) {
    if (!body || body[0] !== 1 || !(w > 0 && h > 0 && w * h <= 1 << 20)) return null;
    const out = new Uint8Array(w * h);
    let at = 0, i = 1;
    while (i < body.length && at < out.length) {
      const b = body[i++], v = (b >> 4) * 17;
      let run = (b & 15) + 1;
      if ((b & 15) === 15) {
        let rest = 0, shift = 0, c;
        do { c = body[i++]; rest |= (c & 0x7f) << shift; shift += 7; } while (c & 0x80 && i < body.length && shift < 28);
        run = 16 + rest;
      }
      out.fill(v, at, Math.min(out.length, at + run));
      at += run;
    }
    return at >= out.length ? out : null;
  }
  // The mask for the picture of time t: the newest that is not newer than it (else the oldest kept).
  function maskFor(list, t) {
    if (!list || !list.length) return null;
    let best = null;
    for (const m of list) if (m.t <= t && (!best || m.t > best.t)) best = m;
    return best || list[0];
  }
  // A list of masks as they come: kept in time order, the last few.
  function keepMask(list, m, most) {
    list.push(m);
    list.sort((a, b) => a.t - b.t);
    while (list.length > (most || 8)) list.shift();
    return list;
  }

  // ---------------- drawing ----------------
  function canvas(w, h) { const c = document.createElement("canvas"); c.width = w || 2; c.height = h || 2; return c; }
  function maskCanvas(c, m) {
    if (c.width !== m.w || c.height !== m.h) { c.width = m.w; c.height = m.h; }
    const g = c.getContext("2d"), img = g.createImageData(m.w, m.h), d = img.data;
    for (let i = 0, j = 3; i < m.data.length; i++, j += 4) d[j] = m.data[i];
    g.putImageData(img, 0, 0);
    return c;
  }
  // The person of `source` (a video, canvas or frame, w x h) on nothing, into `into`, by mask m ({w, h, data}).
  const scratch = { mask: null };
  function paintMasked(into, source, m, w, h) {
    w = w || source.videoWidth || source.displayWidth || source.width; h = h || source.videoHeight || source.displayHeight || source.height;
    if (!w || !h) return false;
    if (into.width !== w || into.height !== h) { into.width = w; into.height = h; }
    const g = into.getContext("2d");
    g.globalCompositeOperation = "copy";
    g.drawImage(source, 0, 0, w, h);
    if (m) {
      const mc = m.canvas || maskCanvas(scratch.mask || (scratch.mask = canvas()), m);
      g.globalCompositeOperation = "destination-in";
      g.imageSmoothingEnabled = true; g.imageSmoothingQuality = "high";
      g.drawImage(mc, 0, 0, w, h);
    }
    g.globalCompositeOperation = "source-over";
    return true;
  }
  // Canvas filters, if this browser really applies them (one may take the property and draw sharp all the same).
  let filterOk = null;
  function canFilter() {
    if (filterOk !== null) return filterOk;
    filterOk = false;
    try {
      const a = canvas(9, 9), b = canvas(9, 9), ag = a.getContext("2d"), bg = b.getContext("2d", { willReadFrequently: true });
      ag.fillStyle = "#fff"; ag.fillRect(4, 4, 1, 1);
      bg.filter = "blur(2px)"; bg.drawImage(a, 0, 0); bg.filter = "none";
      filterOk = bg.getImageData(2, 4, 1, 1).data[3] > 0;
    } catch (err) {}
    return filterOk;
  }
  // The background: the picture small, blurred, and stretched back. With canvas filters a quarter size, blurred.
  // Without (WebKitGTK takes the property and draws sharp): an eighth size, which every engine averages, softened
  // again here in JavaScript (a few thousand pixels), so the room is soft, not blocky.
  function blurInto(g, src, w, h, small) {
    g.imageSmoothingEnabled = true; g.imageSmoothingQuality = "high";
    const f = canFilter(), k = f ? 4 : 8;
    const sw = Math.max(8, Math.round(w / k)), sh = Math.max(8, Math.round(h / k));
    if (small.width !== sw || small.height !== sh) { small.width = sw; small.height = sh; }
    const sg = small.getContext("2d", { willReadFrequently: !f });
    sg.imageSmoothingEnabled = true; sg.imageSmoothingQuality = "high";
    if (f) { sg.filter = "blur(3px)"; sg.drawImage(src, -6, -6, sw + 12, sh + 12); sg.filter = "none"; }
    else {
      sg.drawImage(src, 0, 0, sw, sh);
      const img = sg.getImageData(0, 0, sw, sh);
      boxBlur(img.data, sw, sh, 2); boxBlur(img.data, sw, sh, 2);
      sg.putImageData(img, 0, 0);
    }
    g.drawImage(small, 0, 0, w, h);
  }
  // A box blur of radius r over RGBA, rows then columns, in place.
  function boxBlur(d, w, h, r) {
    const line = new Float32Array(Math.max(w, h) * 4);
    const pass = (n, count, at) => {
      for (let j = 0; j < count; j++) {
        for (let i = 0; i < n; i++) { const p = at(j, i); line[i * 4] = d[p]; line[i * 4 + 1] = d[p + 1]; line[i * 4 + 2] = d[p + 2]; }
        for (let i = 0; i < n; i++) {
          let r0 = 0, g0 = 0, b0 = 0, c = 0;
          for (let k = Math.max(0, i - r); k <= Math.min(n - 1, i + r); k++) { r0 += line[k * 4]; g0 += line[k * 4 + 1]; b0 += line[k * 4 + 2]; c++; }
          const p = at(j, i); d[p] = r0 / c; d[p + 1] = g0 / c; d[p + 2] = b0 / c;
        }
      }
    };
    pass(w, h, (y, x) => (y * w + x) * 4);
    pass(h, w, (x, y) => (y * w + x) * 4);
  }
  function cover(g, src, sw, sh, w, h) {
    if (!sw || !sh) return false;
    const k = Math.max(w / sw, h / sh), dw = sw * k, dh = sh * k;
    g.drawImage(src, (w - dw) / 2, (h - dh) / 2, dw, dh);
    return true;
  }

  // ---------------- the cutter: a camera in, a cut picture out ----------------
  let modelP = null;
  function model(base) {
    if (!modelP) modelP = fetch(base + "cutout-model.bin").then((r) => { if (!r.ok) throw new Error("no model (" + r.status + ")"); return r.arrayBuffer(); });
    modelP.catch(() => { modelP = null; });
    return modelP;
  }
  // A worker running this file when it can be made, else the same work here (a little slower for the page).
  function engine(base, stub) {
    if (stub) { const sg = new Segmenter(null, stub); return Promise.resolve({ where: "page", run: (rgba) => Promise.resolve(sg.run(rgba)), stop() {} }); }
    return model(base).then((buf) => new Promise((ok) => {
      const here = () => { const sg = new Segmenter(new Net(buf.slice(0))); return { where: "page", run: (rgba) => Promise.resolve(sg.run(rgba)), stop() {} }; };
      const inline = () => ok(here());
      let wk;
      try { wk = new Worker(base + "cutout.js"); } catch (err) { inline(); return; }
      let n = 0, wait = {}, ready = false;
      const fail = () => { if (ready) return; ready = true; try { wk.terminate(); } catch (err) {} inline(); };
      const timer = setTimeout(fail, 8000);
      wk.onerror = fail;
      wk.onmessage = (ev) => {
        const d = ev.data || {};
        if (d.ready) { if (ready) return; ready = true; clearTimeout(timer); ok({ where: "worker", run, stop: () => wk.terminate(), here }); return; }
        if (d.error && !ready) { fail(); return; }
        const w = wait[d.id]; delete wait[d.id];
        if (w) d.mask ? w[0](d.mask) : w[1](new Error(d.error || "no mask"));
      };
      const run = (rgba) => new Promise((res, rej) => { const id = ++n; wait[id] = [res, rej]; wk.postMessage({ id, rgba }, [rgba.buffer]); });
      wk.postMessage({ model: buf.slice(0) });
    }));
  }

  function start(opt) {
    const fps = opt.fps || 20, most = opt.most || 640, base = opt.base || "", minFps = opt.minFps || MIN_FPS;
    const v = document.createElement("video");
    v.muted = true; v.playsInline = true; v.autoplay = true; v.setAttribute("playsinline", "");
    v.style.cssText = "position:fixed;left:-10px;top:-10px;width:2px;height:2px;opacity:0;pointer-events:none";
    (document.body || document.documentElement).appendChild(v);
    v.srcObject = new MediaStream([opt.track]); v.play().catch(() => {});
    // frame: the camera's picture now, drawn once a tick; everything else is drawn from it (drawing a video is the
    // slow part in some engines).
    const out = canvas(), alpha = canvas(), person = canvas(), small = canvas(), seg = canvas(IN_W, IN_H), mc = canvas(), frame = canvas();
    // In the page but out of sight: some browsers capture only a canvas that is in the document.
    out.style.cssText = "position:fixed;left:-10px;top:-10px;width:2px;height:2px;opacity:0;pointer-events:none";
    (document.body || document.documentElement).appendChild(out);
    const sv = document.createElement("video");
    sv.muted = true; sv.playsInline = true; sv.autoplay = true;
    sv.style.cssText = v.style.cssText;
    (document.body || document.documentElement).appendChild(sv);
    const me = {
      canvas: out, alpha, want: MODES.indexOf(opt.mode) >= 0 ? opt.mode : "blur", mode: "", why: "", stopped: false,
      onstate: opt.onstate || null, onframe: opt.onframe || null,
    };
    let eng = null, busy = false, last = null, sendable = null, seq = 0, screen = null, broken = "", track = null;
    let times = [], drawn = [], readyAt = 0;
    const said = () => {
      let mode = me.want, why = "";
      if (broken) { mode = "off"; why = broken; }
      else if (!eng) { mode = "off"; why = "getting ready"; }
      else if (mode === "composite" && !(screen && screen.readyState !== "ended")) { mode = "blur"; why = "no screen shared: blur until one is"; }
      if (mode !== me.mode || why !== me.why) {
        me.mode = mode; me.why = why;
        if (me.onstate) try { me.onstate({ mode, want: me.want, why }); } catch (err) {}
      }
    };
    engine(base, opt.segment || api.stub).then((e) => { if (me.stopped) { e.stop(); return; } eng = e; readyAt = performance.now(); said(); },
                                   (err) => { broken = "the cutout could not start (" + ((err && err.message) || err) + ")"; said(); });
    said();
    function segment() {
      if (busy || !eng || broken || me.stopped || frame.width < 4) return;
      busy = true;
      const g = seg.getContext("2d", { willReadFrequently: true });
      g.drawImage(frame, 0, 0, IN_W, IN_H);
      const rgba = new Uint8ClampedArray(g.getImageData(0, 0, IN_W, IN_H).data.buffer), t0 = performance.now(), at = Math.round(t0);
      // On the page the network runs after this picture is drawn, not before it.
      const ran = eng.where === "worker" ? eng.run(rgba) : new Promise((ok) => setTimeout(ok, 0)).then(() => eng.run(rgba));
      ran.then((m) => {
        busy = false;
        if (me.stopped) return;
        const now = performance.now();
        // The next mask as soon as this one is in (in a worker), at most `fps` a second: not held to the drawing's beat.
        if (eng.where === "worker") setTimeout(segment, Math.max(0, 1000 / fps - (now - t0)));
        times.push(now); while (times.length && now - times[0] > 2000) times.shift();
        me.ms = Math.round(now - t0);
        last = { w: IN_W, h: IN_H, data: m, at, seq: ++seq };
        last.canvas = maskCanvas(mc, last);
        sendable = null;
        // Too slow to keep up with a face: off, and said (blur would be as slow, it needs the same mask).
        if (now - readyAt > SLOW_AFTER && times.length / 2 < Math.max(minFps, fps / 2) && eng.here) {
          // A worker this browser runs slowly (a busy computer starves its lower-priority threads): the same work on
          // the page instead, which then has to keep up on its own.
          const old = eng;
          eng = old.here(); old.stop(); times = []; readyAt = now;
        } else if (now - readyAt > SLOW_AFTER && times.length / 2 < minFps) {
          broken = "this computer is too slow for the cutout (" + (times.length / 2).toFixed(1) + " masks a second)"; said();
        }
      }, (err) => { busy = false; broken = "the cutout stopped (" + ((err && err.message) || err) + ")"; said(); });
    }
    function draw() {
      // Nothing is drawn while the network gets ready or before its first mask: the background it is asked to hide
      // never shows. Off (it could not run) is the camera as it is, and said so.
      if (me.stopped || v.readyState < 2 || !v.videoWidth || (!eng && !broken)) return;
      said();
      const began = performance.now();
      const k = Math.min(1, most / Math.max(v.videoWidth, v.videoHeight));
      const w = Math.max(2, Math.round(v.videoWidth * k / 2) * 2), h = Math.max(2, Math.round(v.videoHeight * k / 2) * 2);
      if (frame.width !== w || frame.height !== h) { frame.width = w; frame.height = h; }
      frame.getContext("2d").drawImage(v, 0, 0, w, h);
      if (me.mode !== "off" && !last) { segment(); return; }
      if (out.width !== w || out.height !== h) { out.width = w; out.height = h; }
      const g = out.getContext("2d");
      if (me.mode === "off") g.drawImage(frame, 0, 0);
      else {
        paintMasked(person, frame, last, w, h);
        const screenOn = me.mode === "composite" && sv.readyState >= 2 && sv.videoWidth;
        if (screenOn) { g.fillStyle = "#000"; g.fillRect(0, 0, w, h); cover(g, sv, sv.videoWidth, sv.videoHeight, w, h); }
        else blurInto(g, frame, w, h, small);
        g.drawImage(person, 0, 0);
        if (me.mode === "transparent") {
          if (alpha.width !== w || alpha.height !== h) { alpha.width = w; alpha.height = h; }
          const ag = alpha.getContext("2d"); ag.globalCompositeOperation = "copy"; ag.drawImage(person, 0, 0); ag.globalCompositeOperation = "source-over";
        }
      }
      const done = performance.now();
      drawn.push(done); while (drawn.length && done - drawn[0] > 2000) drawn.shift();
      me.drawMs = Math.round((me.drawMs || 0) * 0.8 + (done - began) * 0.2);
      me.drawnAt = Math.round(done);
      if (me.onframe) try { me.onframe(); } catch (err) {}
      segment();
    }
    const timer = setInterval(draw, Math.round(1000 / fps));
    me.setMode = (m) => { me.want = MODES.indexOf(m) >= 0 ? m : me.want; said(); draw(); return me.mode; };
    me.setScreen = (t) => {
      screen = t && t.readyState !== "ended" ? t : null;
      sv.srcObject = screen ? new MediaStream([screen]) : null;
      if (screen) sv.play().catch(() => {});
      said();
    };
    if (opt.screen) me.setScreen(opt.screen);
    // The mask to send with the picture now in the canvas (transparent only), small, and the same object until a
    // new one comes (seq tells them apart).
    me.mask = () => {
      if (me.mode !== "transparent" || !last) return null;
      if (!sendable || sendable.seq !== last.seq) {
        const d = shrink(last.data, last.w, last.h, SEND_W, SEND_H);
        sendable = { seq: last.seq, w: SEND_W, h: SEND_H, data: d, bytes: encodeMask(d) };
      }
      return sendable;
    };
    me.track = () => {
      if (!track || track.readyState === "ended") track = out.captureStream ? out.captureStream(fps).getVideoTracks()[0] || null : null;
      return track;
    };
    me.stats = () => ({ mode: me.mode, want: me.want, why: me.why, where: eng ? eng.where : "", masksPerSecond: times.length / 2, ms: me.ms || 0,
                         framesPerSecond: drawn.length / 2, drawMs: me.drawMs || 0 });
    me.stop = () => {
      if (me.stopped) return;
      me.stopped = true;
      clearInterval(timer);
      if (eng) eng.stop();
      if (track) track.stop();
      v.srcObject = null; sv.srcObject = null; v.remove(); sv.remove(); out.remove();
    };
    return me;
  }

  // ---------------- in a worker: this file answers masks ----------------
  if (typeof document === "undefined" && typeof self !== "undefined" && typeof self.postMessage === "function" && typeof importScripts === "function") {
    let sg = null;
    self.onmessage = (ev) => {
      const d = ev.data || {};
      try {
        if (d.model) { sg = new Segmenter(new Net(d.model)); self.postMessage({ ready: true }); return; }
        if (!sg) throw new Error("no model yet");
        const m = sg.run(d.rgba);
        self.postMessage({ id: d.id, mask: m }, [m.buffer]);
      } catch (err) { self.postMessage({ id: d.id, error: String((err && err.message) || err) }); }
    };
    return;
  }
  const api = { stub: null, start, Net, Segmenter, KIND_MASK, MODES, encodeMask, decodeMask, maskFor, keepMask, paintMasked, maskCanvas, IN_W, IN_H };
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ArcadeCutout = api;
})(typeof self !== "undefined" ? self : this);
