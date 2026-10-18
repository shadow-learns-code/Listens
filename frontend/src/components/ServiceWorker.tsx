"use client";

import { useEffect } from "react";

/** Registers the service worker (public/sw.js) in production builds only: during `next dev` a cached shell would hide
 *  your edits. Renders nothing. Add `?nosw` to a URL once to remove the worker and its caches (an escape hatch). */
export default function ServiceWorker() {
  useEffect(() => {
    if (!("serviceWorker" in navigator) || process.env.NODE_ENV !== "production") return;
    if (new URLSearchParams(location.search).has("nosw")) {
      navigator.serviceWorker.getRegistrations().then((rs) => rs.forEach((r) => r.unregister()));
      caches.keys().then((ks) => ks.forEach((k) => caches.delete(k)));
      return;
    }
    navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).catch(() => {
      /* unsupported or blocked (private mode): the app works the same without it */
    });
  }, []);
  return null;
}
