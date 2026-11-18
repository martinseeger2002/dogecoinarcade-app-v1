/* arcade.svc for app pages on this website: the same page API as Arcade V1.0 (docs/SVC.md), for the AI model
   marketplace (service "models"). A visitor's phone has no helper of its own, so `local` is this website's arcade's
   helper, which reads aloud (speak_at) and answers (chat_at) for every visitor within a daily allowance. Nothing
   else of the helper is open here.

     arcade.svc.offers(service)       -> [{service, arcade, here, tag, name, offer}]
     arcade.svc.ask(to, service, body) -> {last, answers: [{ev, blob?}]}: only the free reads a service names in its
       offer ("reads": [...]), asked in this website's name; anything else needs the Arcade app
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
    ask: function (to, service, body) {
      return fetch('/r/svc/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ to: to, service: service, body: body || {} }) })
        .then(json).then(function (v) { return { last: v.last, answers: v.answers || [] }; });
    },
    // The site answers at once with a job and works on it; this asks after it until it is done (a voice server that
    // was asleep can take a couple of minutes to wake).
    local: function (service, body) {
      return fetch('/r/svc/local', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ service: service, body: body || {} }) })
        .then(json).then(function (v) {
          if (!v.job) return v.result;
          var until = Date.now() + 6 * 60000, wait = 800;
          return new Promise(function (resolve, reject) {
            (function poll() {
              fetch('/r/svc/job/' + encodeURIComponent(v.job), { cache: 'no-store' }).then(json).then(function (j) {
                if (j.state === 'done') return resolve(j.result);
                if (Date.now() > until) return reject(new Error('no answer in time'));
                wait = Math.min(wait * 1.3, 2500); setTimeout(poll, wait);
              }).catch(reject);
            })();
          });
        });
    }
  };
})();
