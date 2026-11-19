// Test only: counts what reaches the viewer page (pieces by kind, from the room streams) and what its video decoders do.
window.__probe = { kinds: {}, made: 0, configured: [], decoded: 0, frames: 0, errors: [] };
(function () {
  const ES = window.EventSource;
  window.EventSource = function (url) {
    const es = new ES(url);
    es.addEventListener("message", function (e) {
      try {
        const d = JSON.parse(e.data), ev = d.ev || d;
        if (ev.type === "mchunk") { const k = atob(ev.b64.slice(0, 8)).charCodeAt(1); __probe.kinds[k] = (__probe.kinds[k] || 0) + 1; }
      } catch (err) {}
    });
    return es;
  };
  const VD = window.VideoDecoder;
  if (VD) window.VideoDecoder = function (init) {
    __probe.made++;
    const d = new VD({ output: (f) => { __probe.frames++; init.output(f); }, error: (e) => { __probe.errors.push(String(e)); init.error(e); } });
    const conf = d.configure.bind(d), dec = d.decode.bind(d);
    d.configure = (c) => { __probe.configured.push(c.codedWidth + "x" + c.codedHeight); return conf(c); };
    d.decode = (c) => { __probe.decoded++; return dec(c); };
    return d;
  };
  if (VD) window.VideoDecoder.isConfigSupported = VD.isConfigSupported.bind(VD);
})();
