// arcade.groups -- the signed-in player's encrypted group chats.
//
// Asked of the viewer the page is running in, the way arcade.contacts is. The same chats as Messages: what is said here
// shows there too. The keys stay with the viewer: a page gets names and words, never a key. Your card needs
// "groups": true, and the player must turn on Group chats on it. Each call needs their wallet unlocked.
//
//   <script src="/r/groups.js"></script>
//   arcade.groups.list().then(function (r) {            // {me, groups: [{id, name, room, members: [{tag, address}]}]}
//     var g = r.groups[0];
//     arcade.groups.read(g.id).then(function (lines) {  // oldest first: [{txid, when, height, mine, from, text}]
//       lines.forEach(function (l) { console.log(l.from, l.text); });
//     });
//     arcade.groups.send(g.id, 'hello');                // {txid, fee}: one small test-chain transaction, and for ever
//   });
//
// read(id, {after, limit}): `after` is a `when` in seconds, inclusive, so drop txids you have; a line's `height` is 0
// until it is in a block, and `from` is the address that paid for it, which no member can forge.
//
// LIVE. The chain takes seconds. For chat that appears at once, also say it in a realtime room named from `g.room`, which
// only members can work out. Rooms are not encrypted, so seal it first and treat it as a preview of the chain's line:
//
//   arcade.groups.seal(g.id, 'hello').then(function (s) { room.send({s: s}); });
//   room.on('message', function (d) { arcade.groups.open(g.id, d.s).then(show); });   // null: not sealed by this group
//
// create(name, members): after the player taps yes, a new group with those @names or addresses.
// add(id, members): after the player taps yes, lets them in. They can read what has been said under the group's key.
// leave(id): after the player taps yes, one small message says they left. Somebody who stays then makes a new key, so the group's
// `room` changes: read list() again now and then, and follow it. Somebody in the group can ask them back.
// people(): the player's address book, [{address, tag}], for choosing who to make or add a group with.
//
// Rejects with an Error saying why: "off: ...", "no card: ...", "sign in first", "declined", "busy: ...", "too long: ...",
// "not a group of yours", "no viewer ...", or what the chain said.
(function () {
  var seq = 0, waiting = {};

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'groups' || !waiting[m.seq]) return;
    var w = waiting[m.seq];
    delete waiting[m.seq];
    if (m.ok) w.resolve(m);
    else w.reject(new Error(m.error || 'refused'));
  });

  function ask(body, wait) {
    return new Promise(function (resolve, reject) {
      var n = ++seq;
      waiting[n] = {resolve: resolve, reject: reject};
      setTimeout(function () {
        if (waiting[n]) { delete waiting[n]; reject(new Error('no viewer is listening, or the wallet is still locked')); }
      }, wait || 120000);
      body.arcade = 'groups';
      body.seq = n;
      window.parent.postMessage(body, '*');
    });
  }

  window.arcade = window.arcade || {};
  window.arcade.groups = {
    list: function () { return ask({op: 'list'}); },
    read: function (id, o) {
      o = o || {};
      return ask({op: 'read', group: id, after: o.after, limit: o.limit}).then(function (r) { return r.lines; });
    },
    send: function (id, text) { return ask({op: 'send', group: id, text: text}); },
    leave: function (id) { return ask({op: 'leave', group: id}, 600000); },
    seal: function (id, text) { return ask({op: 'seal', group: id, text: text}).then(function (r) { return r.sealed; }); },
    open: function (id, sealed) { return ask({op: 'open', group: id, sealed: sealed}).then(function (r) { return r.text; }); },
    people: function () { return ask({op: 'people'}).then(function (r) { return r.people; }); },
    add: function (id, members) {
      return ask({op: 'add', group: id, members: members}, 600000).then(function (r) { return r.group; });
    },
    save: function (name, bytes) {                       // the frame cannot download: the viewer does
      var data = bytes.buffer ? bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) : bytes;
      return ask({op: 'save', name: name, data: data});
    },
    create: function (name, members) {
      return ask({op: 'create', name: name, members: members}, 600000).then(function (r) { return r.group; });
    }
  };
})();
