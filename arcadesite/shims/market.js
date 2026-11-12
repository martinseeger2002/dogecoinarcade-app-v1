/* arcade.market for app pages on this website: the same page API as Arcade V1.0 (docs/MARKET.md, "The page API").
   Reads answer at once; a write files a request the signed-in reader approves on this site's own card and signs in
   their browser, and resolves once it is sent (or refused). A side is {asset: property id | "coin", amount: base units}. */
(function () {
  'use strict';
  window.arcade = window.arcade || {};
  if (window.arcade.market) return;
  function q(args) {
    var s = [];
    for (var k in (args || {})) if (args[k] !== undefined && args[k] !== null) s.push(encodeURIComponent(k) + '=' + encodeURIComponent(args[k]));
    return s.length ? '?' + s.join('&') : '';
  }
  function read(name, args) {
    return fetch('/r/market/' + name + q(args), { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (v) {
      if (v && v.ok === false) throw new Error(v.error || 'refused');
      return v && v.ok ? v.result : v;
    });
  }
  function wait(id, ms) {
    var until = Date.now() + (ms || 15 * 60000);
    return new Promise(function (resolve, reject) {
      (function poll() {
        fetch('/r/send/' + encodeURIComponent(id), { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (v) {
          if (v.status === 'sent') return resolve({ txid: v.txid, request: id, what: v.what, fee: v.fee });
          if (v.status && v.status !== 'pending') return reject(new Error(v.status === 'denied' ? 'not approved' : (v.error || v.status)));
          if (Date.now() > until) return reject(new Error('not answered in time'));
          setTimeout(poll, 1500);
        }).catch(function () { setTimeout(poll, 3000); });
      })();
    });
  }
  function write(op, args) {
    return fetch('/r/market/' + op, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(args || {}) })
      .then(function (r) { return r.json(); })
      .then(function (v) {
        if (!v || !v.id) throw new Error((v && v.error) || 'refused');
        return wait(v.id);
      });
  }
  var api = {
    markets: function (a) { return read('markets', a); },
    book: function (a) { return read('book', a); },
    trades: function (a) { return read('trades', a); },
    candles: function (a) { return read('candles', a); },
    assets: function (a) { return read('assets', a); },
    terms: function (a) { return read('terms', a); },
    mine: function (a) { return read('mine', a); },
    holdings: function (a) { return read('holdings', a); },
    /* {give, get}: an order resting on the book at the price give/get. */
    place: function (a) { return write('place', a); },
    /* {order}: take back one of this wallet's orders. */
    cancel: function (a) { return write('cancel', a); },
    /* {order, amount}: buy `amount` base units from a resting ask, paying its price. */
    take: function (a) { return write('take', a); }
  };
  window.arcade.market = api;
})();
