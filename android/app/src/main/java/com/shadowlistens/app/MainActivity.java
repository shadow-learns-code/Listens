package com.shadowlistens.app;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.JsPromptResult;
import android.webkit.JsResult;
import android.webkit.PermissionRequest;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import android.widget.FrameLayout;

import org.json.JSONObject;

/**
 * Shadow Listens for Android: a real app window around the Shadow Listens site (the framework WebView, not Chrome).
 *
 *  - the site is the app's content, so it is always the latest version and shares one codebase with the web app
 *  - the native side adds what a web page cannot do on its own: a proper file picker for uploads, a media notification
 *    with lock-screen controls, playback and uploads that keep running with the screen off (PlaybackService), and the
 *    system Back button
 */
public class MainActivity extends Activity {
    static final String SITE = "https://shadow-listens.shadow-listens.workers.dev/";
    static final String SITE_HOST = "shadow-listens.shadow-listens.workers.dev";
    private static final int REQ_FILES = 1001;
    private static final int REQ_NOTIFICATIONS = 1002;
    private static final int BACKGROUND = 0xFF0C0D11;

    /** The running activity, so the playback service can send notification button presses to the page. */
    static volatile MainActivity current;

    /** Debuggable (test) builds can be pointed at another address with `--es url ...`; the release app only ever opens the site. */
    private boolean debuggable;
    private String home = SITE;
    private String siteHost = SITE_HOST;

    private FrameLayout root;
    private WebView web;
    private ValueCallback<Uri[]> chooser;
    private boolean showingOffline;
    /** assets/native-shim.js: mirrors the page's media session into the notification (see that file) */
    private String shim;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        current = this;
        debuggable = (getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
        shim = readAsset("native-shim.js");

        if (Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(false);
        getWindow().setStatusBarColor(BACKGROUND);
        getWindow().setNavigationBarColor(BACKGROUND);

        root = new FrameLayout(this);
        root.setBackgroundColor(BACKGROUND);
        // The page is laid out between the system bars (and above the keyboard), so nothing hides under them.
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            int l, t, r, b;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets i = insets.getInsets(
                        WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                l = i.left; t = i.top; r = i.right; b = i.bottom;
            } else {
                l = insets.getSystemWindowInsetLeft(); t = insets.getSystemWindowInsetTop();
                r = insets.getSystemWindowInsetRight(); b = insets.getSystemWindowInsetBottom();
            }
            v.setPadding(l, t, r, b);
            return Build.VERSION.SDK_INT >= 30 ? WindowInsets.CONSUMED : insets.consumeSystemWindowInsets();
        });

        web = createWebView();
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        if (saved != null) web.restoreState(saved);
        else web.loadUrl(urlFrom(getIntent()));
    }

    private String urlFrom(Intent intent) {
        if (debuggable && intent != null && intent.getStringExtra("url") != null) {
            home = intent.getStringExtra("url");
            siteHost = Uri.parse(home).getHost();
            return home;
        }
        Uri data = intent == null ? null : intent.getData();
        return data != null && SITE_HOST.equals(data.getHost()) && "https".equals(data.getScheme()) ? data.toString() : SITE;
    }

    private WebView createWebView() {
        WebView w = new WebView(this);
        w.setBackgroundColor(BACKGROUND);
        w.setOverScrollMode(WebView.OVER_SCROLL_NEVER);

        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true); // sign-in and settings live in localStorage
        s.setMediaPlaybackRequiresUserGesture(false); // the next song must start by itself
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setAllowFileAccess(false); // the bundled offline page (file:///android_asset) is always allowed
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setUserAgentString(s.getUserAgentString() + " ShadowListensApp/2.0");

        CookieManager.getInstance().setAcceptCookie(true);
        // Remote debugging (chrome://inspect) only for debuggable builds
        if (debuggable) WebView.setWebContentsDebuggingEnabled(true);

        w.setWebViewClient(new SiteClient());
        w.setWebChromeClient(new SiteChrome());
        w.addJavascriptInterface(new NativeBridge(this), "NativeApp");
        return w;
    }

    // ----------------------------------------------------------------------------------------------- page loading

    private boolean isSite(Uri u) {
        boolean secure = "https".equals(u.getScheme()) || (debuggable && "http".equals(u.getScheme()));
        return secure && siteHost.equals(u.getHost());
    }

    private class SiteClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri u = request.getUrl();
            if (isSite(u) || "file".equals(u.getScheme()) || "about".equals(u.getScheme())) return false;
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, u)); // other websites open in the browser
            } catch (ActivityNotFoundException ignored) {
                // nothing can open it: ignore the tap
            }
            return true;
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame() && !showingOffline) {
                showingOffline = true;
                view.loadUrl("file:///android_asset/offline.html");
            }
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (url != null && url.startsWith("http")) {
                showingOffline = false;
                // makes the notification and lock-screen buttons work with whichever version of the site is live
                if (isSite(Uri.parse(url)) && shim != null) view.evaluateJavascript(shim, null);
            }
        }

        @Override
        public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
            // the page's renderer crashed or was reclaimed: start a fresh WebView instead of taking the whole app down
            root.removeView(web);
            web.destroy();
            web = createWebView();
            root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
            web.loadUrl(home);
            return true;
        }
    }

    private String readAsset(String name) {
        try (InputStream in = getAssets().open(name)) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
            return out.toString(StandardCharsets.UTF_8.name());
        } catch (Exception e) {
            return null; // without it the app still works; only the notification needs a site that reports to the app itself
        }
    }

    void reload() {
        showingOffline = false;
        web.loadUrl(home);
    }

    // ------------------------------------------------------------------------------------- file picker + dialogs

    private class SiteChrome extends WebChromeClient {
        /** The upload page's file input: the system document picker, several songs at once. */
        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
            if (chooser != null) chooser.onReceiveValue(null);
            chooser = callback;
            Intent intent;
            boolean audio = false;
            for (String t : params.getAcceptTypes()) if (t != null && (t.startsWith("audio") || t.startsWith(".m") || t.startsWith(".f"))) audio = true;
            if (audio) {
                intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                intent.putExtra(Intent.EXTRA_MIME_TYPES, new String[] {"audio/*", "application/ogg"});
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
            } else {
                intent = params.createIntent();
            }
            try {
                startActivityForResult(intent, REQ_FILES);
            } catch (ActivityNotFoundException e) {
                chooser = null;
                return false;
            }
            return true;
        }

        @Override
        public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
            new AlertDialog.Builder(MainActivity.this).setMessage(message)
                    .setPositiveButton(android.R.string.ok, (d, i) -> result.confirm())
                    .setOnCancelListener(d -> result.cancel()).show();
            return true;
        }

        @Override
        public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
            new AlertDialog.Builder(MainActivity.this).setMessage(message)
                    .setPositiveButton(android.R.string.ok, (d, i) -> result.confirm())
                    .setNegativeButton(android.R.string.cancel, (d, i) -> result.cancel())
                    .setOnCancelListener(d -> result.cancel()).show();
            return true;
        }

        @Override
        public boolean onJsPrompt(WebView view, String url, String message, String defaultValue, JsPromptResult result) {
            EditText input = new EditText(MainActivity.this);
            input.setText(defaultValue);
            new AlertDialog.Builder(MainActivity.this).setMessage(message).setView(input)
                    .setPositiveButton(android.R.string.ok, (d, i) -> result.confirm(input.getText().toString()))
                    .setNegativeButton(android.R.string.cancel, (d, i) -> result.cancel())
                    .setOnCancelListener(d -> result.cancel()).show();
            return true;
        }

        @Override
        public void onPermissionRequest(PermissionRequest request) {
            request.deny(); // the app needs no camera, microphone or location
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILES && chooser != null) {
            Uri[] result = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
            chooser.onReceiveValue(result);
            chooser = null;
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    // ------------------------------------------------------------------------------------------------ notifications

    /** Android 13+ asks before a notification may show. Asked once, when the first song starts playing. */
    void ensureNotificationPermission() {
        if (Build.VERSION.SDK_INT < 33) return;
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) return;
        SharedPreferences prefs = getSharedPreferences("app", MODE_PRIVATE);
        if (prefs.getBoolean("askedNotifications", false)) return;
        prefs.edit().putBoolean("askedNotifications", true).apply();
        runOnUiThread(() -> requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFICATIONS));
    }

    /** A notification button was pressed: tell the page (play, pause, next, previous, seek, upload-pause, ...). */
    void sendCommand(String command, double argument) {
        String js = "window.__listensNative && window.__listensNative.onCommand(" + JSONObject.quote(command) + "," + argument + ")";
        runOnUiThread(() -> web.evaluateJavascript(js, null));
    }

    // --------------------------------------------------------------------------------------------------- lifecycle

    @Override
    public void onBackPressed() {
        if (web.canGoBack() && !showingOffline) web.goBack();
        else moveTaskToBack(true); // like a music app: leave, but keep playing
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String url = urlFrom(intent);
        if (!home.equals(url) || !SITE.equals(url)) web.loadUrl(url);
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    // onPause deliberately does NOT call web.onPause(): that would freeze the page's timers and media while the screen is
    // off, which is exactly when a music app must keep going (and when an upload must keep sending).

    @Override
    protected void onDestroy() {
        if (current == this) current = null;
        if (isFinishing()) PlaybackService.stopAll(this);
        root.removeAllViews();
        web.destroy();
        super.onDestroy();
    }
}
