/* arcade.svc for app pages on this website: the same page API as Arcade V1.0 (docs/SVC.md), for the AI model
   marketplace (service "models"). A visitor's phone has no helper of its own, so `local` is this website's arcade's
   helper, which reads aloud (speak_at) and answers (chat_at) for every visitor within a daily allowance. Nothing
   else of the helper is open here, and `ask` (in the player's own name) needs the Arcade app.

     arcade.svc.offers(service)       -> [{service, arcade, here, tag, name, offer}]
     arcade.svc.local("models", body) -> the helper's answer to {what: "speak_at" | "chat_at", ...} */
(function () {
  'use strict';
  window.arcade = window.arcade || {};
  if (window.arcade.svc) return;
  function json(r) { return r.json().then(function (v) { if (!r.ok || (v && v.ok === false)) throw new Error((v && v.error) || 'refused'); return v; }); }
  window.arcade.svc = {
    offers: function (service) {
      return fetch('/r/svc/offers?service=' + encodeURIComponent(service || ''), { cache: 'no-store' }).then(json).then(function (v) { return v.offers || []; });
    },
    ask: function () { return Promise.reject(new Error('asking a service in your own name needs the Arcade app')); },
    local: function (service, body) {
      return fetch('/r/svc/local', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ service: service, body: body || {} }) })
        .then(json).then(function (v) { return v.result; });
    }
  };
})();
