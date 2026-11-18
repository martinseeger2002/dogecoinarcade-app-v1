/* arcade.feed for app pages on this website: the public feed, read only, in the same shape as Arcade V1.0's app
   window (crates/arcaded/src/shims/feed.js there). Posting, likes, replies, tips and follows need the Arcade app.

     arcade.feed.read({order: popular|new, channel, tag, limit, offset}) -> {posts, total}
     arcade.feed.channels()                                             -> the channels and their post counts
     arcade.feed.file(txid)                                             -> a post's picture {type, data (base64)} */
(function () {
  'use strict';
  window.arcade = window.arcade || {};
  if (window.arcade.feed) return;
  function json(r) { return r.json().then(function (v) { if (!r.ok || (v && v.ok === false)) throw new Error((v && v.error) || 'refused'); return v.result; }); }
  function get(path, q) {
    var s = Object.keys(q || {}).filter(function (k) { return q[k] !== undefined && q[k] !== null && q[k] !== ''; })
      .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(q[k]); }).join('&');
    return fetch(path + (s ? '?' + s : ''), { cache: 'no-store' }).then(json);
  }
  function app() { return Promise.reject(new Error('posting, likes, replies, tips and follows need the Arcade app')); }
  window.arcade.feed = {
    read: function (o) { o = o || {}; return get('/r/feed', { order: o.order, channel: o.channel, tag: o.tag, limit: o.limit, offset: o.offset }); },
    channels: function () { return get('/r/feed/channels'); },
    file: function (txid) { return get('/r/feed/file', { txid: txid }); },
    settings: function () { return Promise.resolve({ hide_muted: false, contacts_only: false }); },
    chains: function () { return Promise.resolve([]); },
    me: function () { return Promise.resolve({ address: '', tag: '' }); },
    follows: function () { return Promise.resolve({ followers: [], following: [] }); },
    post: app, act: app, follow: app, mute: app
  };
})();
