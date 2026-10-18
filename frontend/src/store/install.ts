import { create } from "zustand";

/** The browser's "install this app" offer (Chrome/Edge on Android and desktop). It fires once, early, so it has to be
 *  caught by a listener that is mounted from the start (components/InstallCapture) and kept here for the install screen. */
export interface InstallOffer {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

interface InstallState {
  offer: InstallOffer | null;
  installed: boolean;
  set: (offer: InstallOffer | null) => void;
  markInstalled: () => void;
}

export const useInstall = create<InstallState>((set) => ({
  offer: null,
  installed: false,
  set: (offer) => set({ offer }),
  markInstalled: () => set({ offer: null, installed: true }),
}));
