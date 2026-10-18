/* Runs inside the app's WebView after a page loads (MainActivity.onPageFinished).
 *
 * It makes the notification / lock screen / headset buttons work with ANY version of the website. Android's WebView has no
 * navigator.mediaSession, so the site cannot tell the phone what is playing; instead this reads the player the site draws
 * (its bar at the bottom: song, artist, cover, the <audio> element and the previous / play / next buttons), reports it to
 * window.NativeApp, and presses those same buttons when a notification button is tapped.
 *
 * Newer versions of the site talk to NativeApp themselves (NativeBridge.tsx replaces window.__listensNative); this script
 * then steps aside, so nothing is reported twice. Safe to run more than once per page. */
(function () {
  if (window.__shadowShim || !window.NativeApp) return;
  window.__shadowShim = true;

  var lastKey = "", lastSentPos = 0, lastSentAt = 0;

  function siteManaged() {
    return !!(window.__listensNative && !window.__listensNative.shim);
  }
  function q(id) {
    return document.querySelector('[data-testid="' + id + '"]');
  }
  function audio() {
    return document.querySelector("audio");
  }
  function text(el) {
    return el ? (el.textContent || "").trim() : "";
  }

  function build() {
    var title = q("now-title");
    var bar = q("player-bar");
    if (!title || !bar) return { active: false }; // nothing is loaded in the player
    var a = audio();
    var img = bar.querySelector("img");
    var next = q("next"), prev = q("prev");
    var duration = a && isFinite(a.duration) ? a.duration : 0;
    return {
      active: true,
      title: text(title),
      artist: text(title.nextElementSibling),
      album: "",
      artwork: img && (img.currentSrc || img.src) ? new URL(img.currentSrc || img.src, location.href).href : "",
      playing: !!a && !a.paused && !a.ended,
      positionMs: a && isFinite(a.currentTime) ? Math.round(a.currentTime * 1000) : 0,
      durationMs: Math.round(duration * 1000),
      hasNext: !!next && !next.disabled,
      hasPrev: !!prev && !prev.disabled,
    };
  }

  /* Send when the song or its state changes, when the position jumps (a seek), or as a slow resync. The phone moves the
     progress bar by itself in between, so there is no need to report every tick. */
  function report(force) {
    if (siteManaged()) return;
    var r = build();
    var now = Date.now();
    var key = JSON.stringify([r.active, r.title, r.artist, r.artwork, r.playing, r.durationMs, r.hasNext, r.hasPrev]);
    var expected = lastSentPos + (r.playing ? now - lastSentAt : 0);
    var jumped = Math.abs((r.positionMs || 0) - expected) > 2000;
    if (force || key !== lastKey || jumped || now - lastSentAt > 20000) {
      lastKey = key;
      lastSentPos = r.positionMs || 0;
      lastSentAt = now;
      try {
        window.NativeApp.updatePlayback(JSON.stringify(r));
      } catch (e) {}
    }
  }

  var pending = 0;
  function soon() {
    if (pending) return;
    pending = setTimeout(function () { pending = 0; report(false); }, 150);
  }

  // the audio element lives for the whole session, but be ready for the page to replace it
  ["play", "pause", "playing", "ended", "seeked", "durationchange", "loadedmetadata", "emptied"].forEach(function (ev) {
    document.addEventListener(ev, soon, true); // media events do not bubble, so listen while they travel down
  });
  // a new song, or the player bar appearing / disappearing, changes the page text
  new MutationObserver(soon).observe(document.body, { childList: true, subtree: true, characterData: true });
  setInterval(function () { report(false); }, 5000);

  // notification / lock screen buttons -> the site's own buttons
  function press(id) {
    var b = q(id);
    if (b && !b.disabled) b.click();
  }
  window.__listensNative = {
    shim: true,
    onCommand: function (command, argument) {
      var a = audio();
      if (command === "play") { if (a && a.paused) press("play-toggle"); }
      else if (command === "pause") { if (a && !a.paused) press("play-toggle"); }
      else if (command === "next") press("next");
      else if (command === "prev") press("prev");
      else if (command === "seek") { if (a && isFinite(argument)) a.currentTime = argument; }
      setTimeout(function () { report(true); }, 250);
    },
  };

  report(true); // a song may already be playing when the page (re)loads
})();
