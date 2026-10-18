#!/usr/bin/env python3
"""Train the song-emotion model (libs/common/listens_common/emotion.py) and save it as data/emotion_model.npz.

Input: an .npz of CLAP fingerprints and listener ratings for rated music clips, with arrays
  emb (n, 512)   the fingerprint of each clip (scripts/embed_emotion_clips.py makes it)
  y   (n, 28)    what listeners rated, one column per emotion
  names (28,)    the emotion names

The clips are the 1,841 excerpts of Cowen et al., "What music makes us feel" (2020), rated on 28 emotions, as published on
Hugging Face (baobaoh/13-dimensions-music-emotions).

    .venv/Scripts/python scripts/train_emotion_model.py emo13_emb.npz

Prints how well the model predicts clips it did not see (5-fold cross-validation) before saving the final model.
"""
import sys
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr
from sklearn.kernel_ridge import KernelRidge
from sklearn.model_selection import KFold

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "libs" / "common" / "listens_common" / "data" / "emotion_model.npz"
ALPHA, GAMMA_SCALE = 1.0, 0.5  # chosen by cross-validation (scripts/... see the table printed with --compare)


def fit(X, Y, alpha=ALPHA, gamma_scale=GAMMA_SCALE):
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd > 1e-9, sd, 1.0)
    Z = (X - mu) / sd
    gamma = gamma_scale / X.shape[1]
    ymean = Y.mean(axis=0)
    model = KernelRidge(alpha=alpha, kernel="rbf", gamma=gamma).fit(Z, Y - ymean)
    return {"mu": mu, "sd": sd, "train": Z, "dual": model.dual_coef_, "ymean": ymean, "gamma": np.float64(gamma)}


def predict(m, X):
    Z = (X - m["mu"]) / m["sd"]
    d2 = (Z**2).sum(1)[:, None] + (m["train"] ** 2).sum(1)[None, :] - 2 * Z @ m["train"].T
    return np.exp(-m["gamma"] * np.maximum(d2, 0)) @ m["dual"] + m["ymean"]


def main() -> None:
    d = np.load(sys.argv[1], allow_pickle=True)
    X, Y, names = d["emb"].astype(np.float64), d["y"].astype(np.float64), [str(n) for n in d["names"]]
    print(f"{len(X)} rated clips, {len(names)} emotions")

    pred = np.zeros_like(Y)
    for tr, te in KFold(5, shuffle=True, random_state=0).split(X):
        pred[te] = predict(fit(X[tr], Y[tr]), X[te])
    rho = np.array([spearmanr(pred[:, j], Y[:, j]).statistic for j in range(len(names))])
    print("\nhow well it predicts what listeners felt, on clips it did not see (Spearman correlation):")
    for j in np.argsort(-rho):
        print(f"  {names[j]:30s} {rho[j]:+.2f}")
    print(f"  mean {rho.mean():+.2f}")

    m = fit(X, Y)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        OUT, mu=m["mu"].astype(np.float32), sd=m["sd"].astype(np.float32), train=m["train"].astype(np.float32),
        dual=m["dual"].astype(np.float32), ymean=m["ymean"].astype(np.float32), gamma=m["gamma"], dims=np.array(names),
    )
    print(f"\nsaved {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
