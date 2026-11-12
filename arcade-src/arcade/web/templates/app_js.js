// arcade.app -- what happens to the app around it (2026-10-24).
//
// On the installed app's home screen a player can minimize an app and come back to it: it keeps running in the
// background until the player closes it, or until the timeout its card asks for runs out (the card's
// "minimized": {"timeout": seconds}; 2026-10-24: "remain in the server and active in a game for five minutes
// before they time out. Other apps could have a different timeout or no timeout"; 15 minutes when the card does not
// say, and a card's 0 is no limit). This tells the app so it can pause,
// save, keep its seat in a room, or say goodbye.
//
// on(fn): fn({state, timeout, closesAt, inShell}) each time it changes. state is
//   "minimized" -- hidden on the home screen; it keeps running. timeout: the seconds it has before it is closed (null:
//     no timeout), closesAt: when, in ms since 1970 (null: never).
//   "restored"  -- back on the screen.
//   "closing"   -- the arcade closes it in a moment (the player closed it, or the timeout ran out): save now.
//   Also dispatched on window as the event "arcade:app" (event.detail is the same object).
// off(fn): stops calling fn.
// state(): resolves the same object now ({state: "open", ...} when nothing has happened yet).
//
// A browser tab never minimizes an app (it has tabs of its own), so outside the home screen only "open" is heard.
//
//   <script src="/r/app.js"></script>
//   arcade.app.on(function (s) {
//     if (s.state === 'minimized') pauseTheMusic();
//     if (s.state === 'restored') resumeTheMusic();
//     if (s.state === 'closing') saveTheGame();
//   });
(function () {
  var seq = 0, waiting = {}, fns = [], last = {state: 'open', timeout: null, closesAt: null, inShell: false};

  function strip(m) { var o = {}; for (var k in m) if (k !== 'arcade' && k !== 'seq' && k !== 'event' && k !== 'ok') o[k] = m[k]; return o; }

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'app') return;
    if (m.event) {
      last = strip(m);
      fns.slice().forEach(function (fn) { try { fn(last); } catch (err) { setTimeout(function () { throw err; }); } });
      try { window.dispatchEvent(new CustomEvent('arcade:app', {detail: last})); } catch (err) {}
      return;
    }
    if (!waiting[m.seq]) return;
    var w = waiting[m.seq];
    delete waiting[m.seq];
    if (m.ok) w.resolve(m); else w.reject(new Error(m.error || 'refused'));
  });

  function ask(body, wait) {
    return new Promise(function (resolve, reject) {
      if (window.parent === window) { reject(new Error('no viewer: open this page in the arcade')); return; }
      var n = ++seq;
      waiting[n] = {resolve: resolve, reject: reject};
      setTimeout(function () {
        if (waiting[n]) { delete waiting[n]; reject(new Error('no viewer: nothing answered; open this page in the arcade')); }
      }, wait);
      body.arcade = 'app';
      body.seq = n;
      window.parent.postMessage(body, '*');
    });
  }

  window.arcade = window.arcade || {};
  window.arcade.app = {
    on: function (fn) { if (typeof fn === 'function') fns.push(fn); return fn; },
    off: function (fn) { fns = fns.filter(function (f) { return f !== fn; }); },
    state: function () {
      return ask({op: 'state'}, 15000).then(function (m) { last = strip(m); return last; });
    }
  };
})();
