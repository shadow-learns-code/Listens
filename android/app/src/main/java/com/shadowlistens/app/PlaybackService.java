package com.shadowlistens.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.drawable.Icon;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.net.Uri;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/**
 * Keeps the app alive and visible while music plays or files upload with the screen off:
 *  - a media notification with cover art and previous / play-pause / next, mirrored to the lock screen and Bluetooth
 *    controls through a MediaSession
 *  - an upload notification with progress and a pause / resume button
 *  - a partial wake lock and Wi-Fi lock for as long as something is actually playing or sending
 *
 * The page does the real work (the audio element, the uploader); this service only mirrors its state and forwards button
 * presses back to it (MainActivity.sendCommand).
 */
public class PlaybackService extends Service {
    private static final String CHANNEL = "playback";
    private static final int NOTIFICATION_ID = 7;
    private static final String ACTION_UPDATE = "com.shadowlistens.app.UPDATE";
    private static final String ACTION_COMMAND = "com.shadowlistens.app.COMMAND";
    private static final long IDLE_STOP_MS = 15 * 60 * 1000L;

    static volatile PlaybackService instance;

    private final Handler main = new Handler(Looper.getMainLooper());
    private JSONObject media;
    private JSONObject upload;
    private MediaSession session;
    private Bitmap artwork;
    private String artworkUrl = "";
    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;
    private final Runnable idleStop = this::goIdle;

    // ------------------------------------------------------------------------------------------------- entry points

    /** `kind` is "media" or "upload". Starts the service only when there is something to show. */
    static void update(Context ctx, String kind, String json) {
        PlaybackService running = instance;
        if (running != null) {
            running.main.post(() -> running.apply(kind, json));
            return;
        }
        try {
            if (!new JSONObject(json).optBoolean("active")) return; // nothing to show and nothing running
        } catch (JSONException e) {
            return;
        }
        Intent i = new Intent(ctx, PlaybackService.class).setAction(ACTION_UPDATE).putExtra("kind", kind).putExtra("json", json);
        try {
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
            else ctx.startService(i);
        } catch (RuntimeException e) {
            // Android refused a background start (the user is not in the app): the page will try again on its next update
        }
    }

    static void stopAll(Context ctx) {
        PlaybackService running = instance;
        if (running != null) running.main.post(running::shutdown);
    }

    // --------------------------------------------------------------------------------------------------- lifecycle

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        NotificationChannel channel = new NotificationChannel(CHANNEL, "Playback and uploads", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("The song that is playing and files that are uploading");
        channel.setShowBadge(false);
        channel.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        getSystemService(NotificationManager.class).createNotificationChannel(channel);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // a service started with startForegroundService() must call startForeground() promptly, whatever happens next
        startForegroundCompat(placeholder(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        if (intent != null && ACTION_COMMAND.equals(intent.getAction())) {
            MainActivity page = MainActivity.current;
            if (page != null) page.sendCommand(intent.getStringExtra("cmd"), 0);
            refresh();
        } else if (intent != null && ACTION_UPDATE.equals(intent.getAction())) {
            apply(intent.getStringExtra("kind"), intent.getStringExtra("json"));
        } else {
            refresh();
        }
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        instance = null;
        main.removeCallbacks(idleStop);
        releaseLocks();
        if (session != null) {
            session.release();
            session = null;
        }
        super.onDestroy();
    }

    // ------------------------------------------------------------------------------------------------------ state

    private void apply(String kind, String json) {
        try {
            JSONObject o = new JSONObject(json);
            if ("upload".equals(kind)) upload = o;
            else media = o;
        } catch (JSONException e) {
            return;
        }
        refresh();
    }

    private boolean mediaActive() {
        return media != null && media.optBoolean("active");
    }

    private boolean uploadActive() {
        return upload != null && upload.optBoolean("active");
    }

    private void refresh() {
        boolean hasMedia = mediaActive();
        boolean hasUpload = uploadActive();
        if (!hasMedia && !hasUpload) {
            shutdown();
            return;
        }
        boolean playing = hasMedia && media.optBoolean("playing");
        boolean sending = hasUpload && !upload.optBoolean("paused");

        updateSession(hasMedia, playing);
        if (hasMedia) loadArtwork(media.optString("artwork"));

        int type = 0;
        if (hasMedia) type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK;
        if (hasUpload) type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
        startForegroundCompat(buildNotification(hasMedia, hasUpload, playing), type);

        if (playing || sending) {
            acquireLocks();
            main.removeCallbacks(idleStop);
        } else {
            releaseLocks();
            main.removeCallbacks(idleStop);
            main.postDelayed(idleStop, IDLE_STOP_MS); // paused for a long time: stop holding the app in the foreground
        }
    }

    /** Paused and idle for a while: keep the notification (so it can be resumed from it) but let the system reclaim resources. */
    private void goIdle() {
        releaseLocks();
        stopForeground(STOP_FOREGROUND_DETACH);
    }

    private void shutdown() {
        main.removeCallbacks(idleStop);
        releaseLocks();
        if (session != null) {
            session.setActive(false);
            session.release();
            session = null;
        }
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    // ----------------------------------------------------------------------------------------------- media session

    private void updateSession(boolean active, boolean playing) {
        if (!active) {
            if (session != null) session.setActive(false);
            return;
        }
        if (session == null) {
            session = new MediaSession(this, "ShadowListens");
            session.setCallback(new MediaSession.Callback() {
                @Override public void onPlay() { send("play", 0); }
                @Override public void onPause() { send("pause", 0); }
                @Override public void onStop() { send("pause", 0); }
                @Override public void onSkipToNext() { send("next", 0); }
                @Override public void onSkipToPrevious() { send("prev", 0); }
                @Override public void onSeekTo(long positionMs) { send("seek", positionMs / 1000.0); }
            });
            session.setSessionActivity(openApp());
        }
        MediaMetadata.Builder m = new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, media.optString("title"))
                .putString(MediaMetadata.METADATA_KEY_ARTIST, media.optString("artist"))
                .putString(MediaMetadata.METADATA_KEY_ALBUM, media.optString("album"))
                .putLong(MediaMetadata.METADATA_KEY_DURATION, media.optLong("durationMs"));
        if (artwork != null) m.putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, artwork);
        session.setMetadata(m.build());

        long actions = PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE | PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_SEEK_TO;
        if (media.optBoolean("hasNext")) actions |= PlaybackState.ACTION_SKIP_TO_NEXT;
        if (media.optBoolean("hasPrev")) actions |= PlaybackState.ACTION_SKIP_TO_PREVIOUS;
        session.setPlaybackState(new PlaybackState.Builder()
                .setActions(actions)
                .setState(playing ? PlaybackState.STATE_PLAYING : PlaybackState.STATE_PAUSED, media.optLong("positionMs"),
                        playing ? 1f : 0f, SystemClock.elapsedRealtime())
                .build());
        session.setActive(true);
    }

    private void send(String command, double argument) {
        MainActivity page = MainActivity.current;
        if (page != null) page.sendCommand(command, argument);
    }

    // ------------------------------------------------------------------------------------------------ notification

    private PendingIntent openApp() {
        Intent i = new Intent(this, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private Notification.Action action(int icon, String label, String command) {
        Intent i = new Intent(this, PlaybackService.class).setAction(ACTION_COMMAND).putExtra("cmd", command);
        PendingIntent pi = PendingIntent.getService(this, command.hashCode(), i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new Notification.Action.Builder(Icon.createWithResource(this, icon), label, pi).build();
    }

    private Notification placeholder() {
        return new Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_stat_music).setContentTitle("Shadow Listens")
                .setContentIntent(openApp()).build();
    }

    private Notification buildNotification(boolean hasMedia, boolean hasUpload, boolean playing) {
        Notification.Builder b = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_music)
                .setContentIntent(openApp())
                .setOnlyAlertOnce(true)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setShowWhen(false);

        String uploadLine = hasUpload ? upload.optString("label", "Uploading") : "";
        boolean uploadPaused = hasUpload && upload.optBoolean("paused");

        if (hasMedia) {
            b.setContentTitle(media.optString("title")).setContentText(media.optString("artist"));
            b.setSubText(hasUpload ? uploadLine + (uploadPaused ? " (paused)" : "") : media.optString("album"));
            if (artwork != null) b.setLargeIcon(artwork);
            b.setOngoing(playing);

            List<Integer> compact = new ArrayList<>();
            int index = 0;
            if (media.optBoolean("hasPrev")) {
                b.addAction(action(R.drawable.ic_prev, "Previous", "prev"));
                compact.add(index++);
            }
            b.addAction(playing ? action(R.drawable.ic_pause, "Pause", "pause") : action(R.drawable.ic_play, "Play", "play"));
            compact.add(index++);
            if (media.optBoolean("hasNext")) {
                b.addAction(action(R.drawable.ic_next, "Next", "next"));
                compact.add(index);
            }
            int[] shown = new int[Math.min(3, compact.size())];
            for (int i = 0; i < shown.length; i++) shown[i] = compact.get(i);
            Notification.MediaStyle style = new Notification.MediaStyle().setShowActionsInCompactView(shown);
            if (session != null) style.setMediaSession(session.getSessionToken());
            b.setStyle(style);
        } else {
            int percent = Math.max(0, Math.min(100, upload.optInt("percent")));
            b.setContentTitle(uploadPaused ? "Upload paused" : "Uploading music").setContentText(uploadLine);
            b.setProgress(100, percent, false).setOngoing(true);
            b.addAction(uploadPaused ? action(R.drawable.ic_play, "Resume", "upload-resume") : action(R.drawable.ic_pause, "Pause", "upload-pause"));
        }
        return b.build();
    }

    private void startForegroundCompat(Notification n, int type) {
        try {
            if (Build.VERSION.SDK_INT >= 29) startForeground(NOTIFICATION_ID, n, type);
            else startForeground(NOTIFICATION_ID, n);
        } catch (RuntimeException e) {
            // not allowed to (re)start the foreground state right now; the next update retries
        }
    }

    // -------------------------------------------------------------------------------------------------- cover art

    private void loadArtwork(String url) {
        if (url == null || url.isEmpty() || url.equals(artworkUrl)) return;
        artworkUrl = url;
        artwork = null;
        new Thread(() -> {
            Bitmap bitmap = download(url);
            main.post(() -> {
                if (bitmap != null && url.equals(artworkUrl)) {
                    artwork = bitmap;
                    if (instance == this) refresh();
                }
            });
        }, "artwork").start();
    }

    private Bitmap download(String url) {
        HttpURLConnection c = null;
        try {
            Uri u = Uri.parse(url);
            boolean testBuild = (getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0;
            if (!"https".equals(u.getScheme()) && !(testBuild && "http".equals(u.getScheme()))) return null;
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(8000);
            c.setReadTimeout(8000);
            try (InputStream in = c.getInputStream()) {
                return BitmapFactory.decodeStream(in);
            }
        } catch (Exception e) {
            return null; // no cover art: the notification simply shows without it
        } finally {
            if (c != null) c.disconnect();
        }
    }

    // ------------------------------------------------------------------------------------------------------ locks

    private void acquireLocks() {
        if (wakeLock == null) {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ShadowListens:playback");
            wakeLock.setReferenceCounted(false);
        }
        if (!wakeLock.isHeld()) wakeLock.acquire(6 * 60 * 60 * 1000L);
        if (wifiLock == null) {
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            int mode = Build.VERSION.SDK_INT >= 29 ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
            wifiLock = wm.createWifiLock(mode, "ShadowListens:wifi");
            wifiLock.setReferenceCounted(false);
        }
        if (!wifiLock.isHeld()) wifiLock.acquire();
    }

    private void releaseLocks() {
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
    }
}
