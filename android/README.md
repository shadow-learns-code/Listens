# Shadow Listens for Android

A native app (framework `WebView`, no third-party libraries, minSdk 26, targetSdk 35) that opens the live site full screen and adds
what a browser cannot: playback with the screen off, a media notification with buttons, the file picker, and a retrying
offline page. See the main README ("Android app") for how it talks to the site.

```
app/src/main/java/com/shadowlistens/app/
  MainActivity.java     the WebView, insets, file picker, JS dialogs, back button, injects assets/native-shim.js
  NativeBridge.java     window.NativeApp for the page (updatePlayback, updateUpload, retry, version)
  PlaybackService.java  foreground service: MediaSession, notification, wake + Wi-Fi locks, upload progress
app/src/main/assets/
  native-shim.js        works with any site version: reads the page's player, presses its buttons
  offline.html          shown when the site cannot be reached (retries by itself)
app/src/debug/          debug builds only: allows http://localhost for testing against a local stack
```

## Build

Needs JDK 21, Gradle 8.10 and the Android SDK with platform 35 and build-tools 35 (`ANDROID_HOME`, `JAVA_HOME`).

```
cd android
VERSION_CODE=3 VERSION_NAME=2.0.1 gradle assembleRelease     # app/build/outputs/apk/release/app-release.apk
VERSION_CODE=3 VERSION_NAME=2.0.1 gradle assembleDebug       # debuggable: WebView inspectable over USB, --es url <start url>
adb install -r app/build/outputs/apk/release/app-release.apk
```

Raise `VERSION_CODE` for every build you hand out; Android refuses to install a lower one over a higher one. The APK the site offers
(Profile > Get the app) is `frontend/public/app/shadow-listens.apk`; copy a new release there and push.

## The signing key

`keystore.properties` (gitignored, with the `.jks` it points to) holds the key both builds are signed with. **Back it up.**
Android only installs an update over an app signed with the same key, so losing it means everyone must uninstall first. Never commit it.

## Testing on a phone

USB debugging on, `adb devices` shows the phone. A debug build started with
`adb shell am start -n com.shadowlistens.app/.MainActivity --es url http://localhost:8099/login` and `adb reverse tcp:8099 tcp:8099`
loads a local stack. For the live site, `adb forward tcp:9222 localabstract:webview_devtools_remote_<pid>` (debug build) lets Playwright
`connectOverCDP("http://localhost:9222")` inspect the page and press buttons. The notification shows in `adb shell dumpsys notification`
and `dumpsys media_session`; `adb shell cmd media_session dispatch pause|play|next|previous` presses its buttons like a headset would.
