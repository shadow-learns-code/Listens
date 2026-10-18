"""Song emotions from an audio fingerprint, learned from what listeners actually felt.

A pretrained audio model (LAION CLAP) turns a short excerpt of a song into a 512-number fingerprint. A regression
trained on 1,841 music clips that listeners rated on 28 emotions (Cowen et al., "What music makes us feel", 2020; hosted
as baobaoh/13-dimensions-music-emotions) turns that fingerprint into predicted ratings: how romantic, sad, painful,
joyful, energizing, calm, triumphant ... the song feels. Those are then combined into the moods shown in the app.

The fingerprint itself needs the CLAP model (a large download), so it is made on a computer that has it
(scripts/score_moods.py) and stored on the track; everything here needs only numpy, so the web service can use it.

The model file (data/emotion_model.npz) is a kernel ridge regression with an RBF kernel, produced by
scripts/train_emotion_model.py; evaluating it is a few lines of numpy (see `ratings`).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import numpy as np

MODEL_PATH = Path(__file__).with_name("data") / "emotion_model.npz"

# How the 28 rated emotions combine into the moods a listener picks. Weights are a reading of each word ("Break-up" is
# heartache: sad, bittersweet, longing; "Mass" is the heroic, defiant, crowd-pleasing kind of power) and lean on the emotions
# the model predicts reliably. Cross-validation showed that "painful", "erotic" and "goose bumps" cannot be told from audio
# here (r below 0.1), so no mood depends on them.
MOODS: dict[str, dict[str, float]] = {
    "Love": {"romantic/loving": 0.6, "tender/longing": 0.5, "beautiful": 0.5, "compassionate/sympathetic": 0.4, "dreamy": 0.2},
    "Break-up": {"sad/depressing": 0.8, "bittersweet": 0.8, "tender/longing": 0.7, "compassionate/sympathetic": 0.3},
    "Sad": {"sad/depressing": 1.0, "bittersweet": 0.4, "joyful/cheerful": -0.4, "energizing/pump-up": -0.3},
    "Pleasant": {"calm/relaxing/serene": 1.0, "beautiful": 0.7, "dreamy": 0.5, "compassionate/sympathetic": 0.3},
    "Happy": {"joyful/cheerful": 1.0, "amusing": 0.4, "euphoric/ecstatic": 0.4, "energizing/pump-up": 0.3, "sad/depressing": -0.5},
    "Party": {"energizing/pump-up": 1.0, "exciting": 0.8, "euphoric/ecstatic": 0.5, "amusing": 0.2, "calm/relaxing/serene": -0.5},
    "Mass": {"triumphant/heroic": 0.8, "proud/strong": 0.7, "indignant/defiant": 0.6, "energizing/pump-up": 0.6, "awe-inspiring/amazing": 0.5, "sad/depressing": -0.3},
    "Vibe": {"dreamy": 0.6, "energizing/pump-up": 0.5, "entrancing": 0.3, "exciting": 0.3},
    "Nostalgic": {"bittersweet": 1.0, "tender/longing": 0.7, "dreamy": 0.5, "compassionate/sympathetic": 0.3},
    "Devotional": {"transcendent/mystical": 1.0, "calm/relaxing/serene": 0.6, "awe-inspiring/amazing": 0.5, "beautiful": 0.3},
    "Motivational": {"triumphant/heroic": 0.7, "energizing/pump-up": 0.6, "proud/strong": 0.7, "awe-inspiring/amazing": 0.3, "joyful/cheerful": 0.2},
}
MOOD_NAMES = list(MOODS)

# the two classic axes, from the same ratings: pleasant vs unpleasant, and calm vs energetic
VALENCE = {"joyful/cheerful": 1.0, "beautiful": 0.6, "romantic/loving": 0.5, "calm/relaxing/serene": 0.3,
           "sad/depressing": -1.0, "painful": -0.7, "anxious/tense": -0.6, "angry": -0.5, "scary/fearful": -0.5}
AROUSAL = {"energizing/pump-up": 1.0, "exciting": 0.8, "euphoric/ecstatic": 0.6, "triumphant/heroic": 0.5, "angry": 0.4,
           "calm/relaxing/serene": -1.0, "dreamy": -0.5, "tender/longing": -0.3}

# How far each mood leans along (valence: sad..happy, arousal: calm..energetic). Used to turn the lighter signals the worker
# already stores (key, tempo, loudness) into scores for the same moods, so every song has a mood even before it is scored.
AXES = {"Love": (0.4, -0.4), "Break-up": (-0.8, -0.5), "Sad": (-1.0, -0.6), "Pleasant": (0.5, -0.7), "Happy": (1.0, 0.4),
        "Party": (0.3, 1.0), "Mass": (-0.1, 1.0), "Vibe": (0.2, 0.5), "Nostalgic": (-0.2, -0.6), "Devotional": (0.0, -0.8),
        "Motivational": (0.4, 0.8)}
# Share of the learned model when a song has been scored; the rest is the lighter signals. Checked against songs of known
# mood: each alone misses songs the other gets right. The blend is a bet rather than a proven win, so the weight can be changed
# without a code change: set MOOD_LEARNED_WEIGHT (0 = ignore the learned model entirely, 1 = learned model only).
LEARNED_WEIGHT = min(1.0, max(0.0, float(os.environ.get("MOOD_LEARNED_WEIGHT", "0.5"))))

TAG_Z = 0.6  # a mood counts as "this song's mood" when it is at least this many standard deviations above the library
MAX_TAGS = 3


class ModelMissing(RuntimeError):
    pass


class Model:
    """Kernel ridge regression: rating = mean + sum_i weight_i * exp(-gamma * |x - x_i|^2) over the training clips."""

    def __init__(self, z):
        self.mu = z["mu"].astype(np.float64)
        self.sd = z["sd"].astype(np.float64)
        self.train = z["train"].astype(np.float64)  # standardised training fingerprints, (n, 512)
        self.train_sq = (self.train**2).sum(axis=1)
        self.dual = z["dual"].astype(np.float64)  # (n, 28)
        self.ymean = z["ymean"].astype(np.float64)  # (28,)
        self.gamma = float(z["gamma"])
        self.dims = [str(d) for d in z["dims"]]

    def predict(self, fingerprints: np.ndarray) -> np.ndarray:
        x = (np.atleast_2d(np.asarray(fingerprints, dtype=np.float64)) - self.mu) / self.sd
        d2 = (x**2).sum(axis=1)[:, None] + self.train_sq[None, :] - 2.0 * x @ self.train.T
        return np.exp(-self.gamma * np.maximum(d2, 0.0)) @ self.dual + self.ymean


@lru_cache(maxsize=2)
def load_model(path: str | None = None) -> Model:
    p = Path(path) if path else MODEL_PATH
    if not p.exists():
        raise ModelMissing(f"emotion model not found at {p}")
    return Model(np.load(p, allow_pickle=False))


def ratings(fingerprints: np.ndarray, path: str | None = None) -> np.ndarray:
    """Predicted 28 emotion ratings for fingerprint(s): (512,) -> (28,), (n, 512) -> (n, 28)."""
    out = load_model(path).predict(fingerprints)
    return out[0] if np.ndim(fingerprints) == 1 else out


def dimensions(path: str | None = None) -> list[str]:
    return load_model(path).dims


def _z(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    std = values.std(axis=0)
    return (values - values.mean(axis=0)) / np.where(std > 1e-9, std, 1.0)


def _combine(z_ratings: np.ndarray, weights: dict[str, float], dims: list[str]) -> np.ndarray:
    col = {d: j for j, d in enumerate(dims)}
    total = sum(abs(w) for w in weights.values())
    return sum(w * z_ratings[:, col[d]] for d, w in weights.items()) / total


def mood_scores(rating_matrix: np.ndarray, dims: list[str] | None = None) -> np.ndarray:
    """(n songs, 11 moods) scores in standard deviations relative to the songs given (your library): +1 means a song is
    more of that mood than most of your songs. With fewer than 5 songs there is no spread to measure, so scores are 0."""
    dims = dims or dimensions()
    r = np.atleast_2d(rating_matrix)
    if len(r) < 5:
        return np.zeros((len(r), len(MOOD_NAMES)))
    z = _z(r)
    raw = np.stack([_combine(z, MOODS[m], dims) for m in MOOD_NAMES], axis=1)
    return _z(raw)


def valence_arousal(rating_matrix: np.ndarray, dims: list[str] | None = None) -> np.ndarray:
    """(n, 2): valence (sad..happy) and arousal (calm..energetic) as z-scores relative to the songs given."""
    dims = dims or dimensions()
    r = np.atleast_2d(rating_matrix)
    if len(r) < 5:
        return np.zeros((len(r), 2))
    z = _z(r)
    return _z(np.stack([_combine(z, VALENCE, dims), _combine(z, AROUSAL, dims)], axis=1))


def tags(scores_row: np.ndarray) -> list[str]:
    """The names of this song's strongest moods (up to MAX_TAGS, each clearly above the library average), best first."""
    order = np.argsort(-np.asarray(scores_row))[:MAX_TAGS]
    return [MOOD_NAMES[j] for j in order if scores_row[j] >= TAG_Z]


def scores_from_axes(valence_arousal: np.ndarray) -> np.ndarray:
    """(n, 2) valence/arousal z-scores -> (n, 11) mood scores: how far each song lies in the direction of each mood on the
    sad..happy / calm..energetic plane. The directions are unit length, so scores are comparable across moods (not
    standardised per mood: that would make every mood near one corner look equally extreme and hide which fits best)."""
    lean = np.array([AXES[m] for m in MOOD_NAMES], dtype=np.float64)
    lean /= np.linalg.norm(lean, axis=1, keepdims=True)
    return np.atleast_2d(np.asarray(valence_arousal, dtype=np.float64)) @ lean.T


MODEL_VERSION = "cowen28-krr-clap-music-1"  # stored with each song's ratings, so scores from an older model can be spotted


def payload(fingerprint) -> tuple[list[float], dict]:
    """What gets stored on a track for a fingerprint: (the fingerprint as a list, {"ratings": {emotion: rating}, "model": ...})."""
    fp = np.asarray(fingerprint, dtype=np.float64)
    fp = fp / np.linalg.norm(fp)
    r = ratings(fp)
    return [round(float(x), 5) for x in fp], {"ratings": {d: round(float(v), 4) for d, v in zip(dimensions(), r)}, "model": MODEL_VERSION}
