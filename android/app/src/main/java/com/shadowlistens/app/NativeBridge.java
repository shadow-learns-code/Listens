package com.shadowlistens.app;

import android.content.Context;
import android.webkit.JavascriptInterface;

import org.json.JSONException;
import org.json.JSONObject;

/**
 * What the web app (running inside this window) can ask the phone to do. Exposed to the page as `window.NativeApp`; the web
 * app checks for it to know it is running inside the Android app. Every method is called on a background thread by the
 * WebView, so each one only hands work to the main thread / the service.
 */
class NativeBridge {
    private final MainActivity activity;
    private final Context app;

    NativeBridge(MainActivity activity) {
        this.activity = activity;
        this.app = activity.getApplicationContext();
    }

    @JavascriptInterface
    public boolean isApp() {
        return true;
    }

    @JavascriptInterface
    public String version() {
        return "2.0.1";
    }

    /** The song that is playing: {active, title, artist, album, artwork, playing, positionMs, durationMs, hasNext, hasPrev}. */
    @JavascriptInterface
    public void updatePlayback(String json) {
        PlaybackService.update(app, "media", json);
        try {
            JSONObject o = new JSONObject(json);
            if (o.optBoolean("active") && o.optBoolean("playing")) activity.ensureNotificationPermission();
        } catch (JSONException ignored) {
            // malformed update: the service ignores it as well
        }
    }

    /** Uploads in progress: {active, paused, label, percent}. Keeps the app alive in the background while files send. */
    @JavascriptInterface
    public void updateUpload(String json) {
        PlaybackService.update(app, "upload", json);
        try {
            if (new JSONObject(json).optBoolean("active")) activity.ensureNotificationPermission();
        } catch (JSONException ignored) {
            // malformed update
        }
    }

    /** The offline page's "Try again" button. */
    @JavascriptInterface
    public void retry() {
        activity.runOnUiThread(activity::reload);
    }
}
