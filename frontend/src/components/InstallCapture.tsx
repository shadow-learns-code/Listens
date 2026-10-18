"use client";

import { useEffect } from "react";
import { type InstallOffer, useInstall } from "@/store/install";

/** Renders nothing: keeps the browser's install offer for the "Get the app" screen. */
export default function InstallCapture() {
  const set = useInstall((s) => s.set);
  const markInstalled = useInstall((s) => s.markInstalled);
  useEffect(() => {
    const onOffer = (e: Event) => {
      e.preventDefault(); // hold it back: the offer is shown on the install screen, not as a browser mini-bar
      set(e as unknown as InstallOffer);
    };
    window.addEventListener("beforeinstallprompt", onOffer);
    window.addEventListener("appinstalled", markInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onOffer);
      window.removeEventListener("appinstalled", markInstalled);
    };
  }, [set, markInstalled]);
  return null;
}
