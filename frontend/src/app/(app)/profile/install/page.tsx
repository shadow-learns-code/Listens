"use client";

import { useEffect, useState } from "react";
import { SettingsPage, SettingsRow, SettingsSection } from "@/components/SettingsPage";
import { nativeApp } from "@/lib/native.ts";
import { useInstall } from "@/store/install";

/** Where the Android app file is served from (built by android/, copied to public/app/). */
const APK_URL = "/app/shadow-listens.apk";

type Platform = "android" | "ios" | "other";

function detectPlatform(): Platform {
  const ua = navigator.userAgent;
  if (/android/i.test(ua)) return "android";
  if (/iphone|ipad|ipod/i.test(ua) || (/macintosh/i.test(ua) && navigator.maxTouchPoints > 1)) return "ios";
  return "other";
}

function isStandalone(): boolean {
  return window.matchMedia("(display-mode: standalone)").matches || (navigator as unknown as { standalone?: boolean }).standalone === true;
}

/** Get the app on a phone: the installable web app everywhere, the Android app file, and the iPhone steps. */
export default function InstallPage() {
  const offer = useInstall((s) => s.offer);
  const installed = useInstall((s) => s.installed);
  const [platform, setPlatform] = useState<Platform>("other");
  const [standalone, setStandalone] = useState(false);
  const [apkReady, setApkReady] = useState<boolean | null>(null);
  const [appVersion, setAppVersion] = useState<string | null>(null);

  useEffect(() => {
    setAppVersion(nativeApp()?.version() ?? null);
    setPlatform(detectPlatform());
    setStandalone(isStandalone());
    fetch(APK_URL, { method: "HEAD" }).then((r) => setApkReady(r.ok && !/text\/html/.test(r.headers.get("content-type") ?? ""))).catch(() => setApkReady(false));
  }, []);

  const apk = (
    <SettingsSection title="Android app" description="A real app: its own icon, full screen, and playback that keeps going with the screen off, with lock-screen controls.">
      {apkReady === false ? (
        <SettingsRow label="Not available yet" description="The Android app file has not been published for this site." />
      ) : (
        <SettingsRow
          label="Download the Android app"
          description="Open the downloaded file to install it. Android may ask you to allow installs from your browser, just this once."
          action={
            <a
              href={APK_URL}
              download
              data-testid="apk-download"
              className="shrink-0 rounded-full bg-accent px-4 py-2 text-sm font-semibold text-ink active:scale-95"
            >
              Download
            </a>
          }
        />
      )}
    </SettingsSection>
  );

  const web = (
    <SettingsSection title="Install from the browser" description="Adds Shadow Listens to your home screen. It opens full screen like any other app.">
      {installed || standalone ? (
        <SettingsRow label="You're using the app" description="Shadow Listens is installed on this device." />
      ) : offer ? (
        <SettingsRow
          label="Install Shadow Listens"
          action={
            <button
              type="button"
              data-testid="pwa-install"
              onClick={() => offer.prompt()}
              className="shrink-0 rounded-full bg-accent px-4 py-2 text-sm font-semibold text-ink active:scale-95"
            >
              Install
            </button>
          }
        />
      ) : (
        <SettingsRow label="Use your browser's menu" description={platform === "android" ? 'Open the browser menu (⋮) and choose "Install app" or "Add to Home screen".' : 'Look for "Install app" in your browser\'s menu or address bar.'} />
      )}
    </SettingsSection>
  );

  const ios = (
    <SettingsSection title="iPhone and iPad" description="Apple does not allow installing apps outside the App Store, but the web app works the same way.">
      <SettingsRow label="1. Open this site in Safari" description="It has to be Safari, not another browser." />
      <SettingsRow label="2. Tap the Share button" description="The square with an arrow pointing up." />
      <SettingsRow label="3. Choose “Add to Home Screen”" description="Then tap Add. The app appears on your home screen." />
    </SettingsSection>
  );

  if (appVersion) {
    return (
      <SettingsPage title="Get the app">
        <SettingsSection title="Android app">
          <SettingsRow label="You're using the app" description={`Shadow Listens for Android, version ${appVersion}. Music keeps playing with the screen off.`} />
        </SettingsSection>
      </SettingsPage>
    );
  }

  return (
    <SettingsPage title="Get the app">
      {platform === "ios" ? (<>{ios}{web}</>) : platform === "android" ? (<>{apk}{web}</>) : (<>{web}{apk}{ios}</>)}
    </SettingsPage>
  );
}
