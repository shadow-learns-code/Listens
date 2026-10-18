"""Song mood from the audio fingerprint the worker already stores: no new audio processing, no labelled dataset.

Mood is placed on two axes (Russell's circumplex model, the standard in music-emotion research):

  valence   sad / dark  ...  happy / bright
  arousal   calm / soft ...  energetic / intense

so "sad" is low valence and low arousal, "romantic/calm" is higher valence and low arousal, "upbeat" is high on both,
and "intense" is low valence and high arousal. The estimate uses what music-emotion studies find most predictive and
what librosa gave us (see services/worker/app/audio_features.py for the vector layout):

  valence  key mode (major vs minor, from the chroma means via the Krumhansl-Schmuckler key profiles), plus brightness
           (spectral centroid) and tempo: minor, dark and slow sounds sad, major, bright and quick sounds happy
  arousal  loudness (RMS energy), brightness, percussiveness (zero-crossing rate) and tempo

Each feature is z-scored across the library, so the axes are relative to YOUR songs: "sad" means sadder than the rest
of the library, which is the useful reading for ranking neighbours. It is an estimate, not ground truth (mode is a
weak signal for modal Indian film music, and the beat tracker can halve or double tempo), which is why it is blended
with the other similarity signals rather than used alone.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import emotion

CHROMA = slice(13, 25)
CENTROID, ZCR = 32, 33

# Krumhansl-Kessler key profiles (C major / C minor); rotated to test all 12 keys.
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

MIN_TRACKS = 5  # z-scores need some spread to mean anything


@dataclass(frozen=True)
class Mood:
    valence: float  # z-score across the library: negative = sadder/darker
    arousal: float  # z-score across the library: negative = calmer/softer
    # score for each mood in emotion.MOOD_NAMES (Love, Break-up, Sad, ...), in standard deviations above the library average
    scores: tuple[float, ...] | None = None

    @property
    def tags(self) -> list[str]:
        """This song's strongest moods by name (up to three, each clearly above average), best first."""
        return emotion.tags(np.array(self.scores)) if self.scores else []

    @property
    def label(self) -> str:
        return label(self.valence, self.arousal)


def mode_score(chroma) -> float:
    """-1 (clearly minor) .. +1 (clearly major): how much better the best major key fits the chroma than the best minor."""
    c = np.asarray(chroma, dtype=float)
    if c.size != 12 or not np.isfinite(c).all() or c.std() < 1e-9:
        return 0.0

    def best(profile: np.ndarray) -> float:
        return max(float(np.corrcoef(c, np.roll(profile, k))[0, 1]) for k in range(12))

    return float(np.clip((best(_MAJOR) - best(_MINOR)) * 4.0, -1.0, 1.0))


def _z(values: np.ndarray) -> np.ndarray:
    std = values.std()
    return (values - values.mean()) / (std if std > 1e-9 else 1.0)


def compute_moods(raw: dict, ratings: dict | None = None) -> dict:
    """{track id: Mood} from {track id: (fingerprint vector, tempo_bpm or None, energy or None)}.
    Tracks without a fingerprint are left out; tempo/energy fall back to the library average when missing.

    `ratings` ({track id: {emotion: predicted rating}}, from listens_common.emotion) is optional. A song that has them blends
    the learned model with the lighter signals; a song without them is judged by the lighter signals alone."""
    ids = [i for i, (vec, _, _) in raw.items() if vec is not None and len(vec) >= 34]
    out: dict = {}
    if len(ids) >= MIN_TRACKS:
        vecs = np.array([raw[i][0] for i in ids], dtype=float)
        tempo = np.array([raw[i][1] or np.nan for i in ids], dtype=float)
        energy = np.array([raw[i][2] or np.nan for i in ids], dtype=float)
        # tempo on a log scale: 60 -> 120 bpm is the same step as 90 -> 180; energy likewise (loudness is ratio-like)
        tempo = np.log2(np.where(np.isnan(tempo), np.nanmean(tempo) if not np.isnan(tempo).all() else 100.0, tempo))
        energy = np.log(np.where(np.isnan(energy), np.nanmean(energy) if not np.isnan(energy).all() else 0.05, energy))

        mode = np.array([mode_score(v[CHROMA]) for v in vecs])
        bright = _z(vecs[:, CENTROID])
        perc = _z(vecs[:, ZCR])
        t, e = _z(tempo), _z(energy)

        valence = 0.50 * _z(mode) + 0.25 * bright + 0.25 * t
        arousal = 0.40 * e + 0.25 * bright + 0.20 * t + 0.15 * perc
        valence, arousal = _z(valence), _z(arousal)
        scores = emotion.scores_from_axes(np.stack([valence, arousal], axis=1))
        for k, i in enumerate(ids):
            out[i] = Mood(float(valence[k]), float(arousal[k]), tuple(float(x) for x in scores[k]))

    rated = [i for i, r in (ratings or {}).items() if r]
    if len(rated) >= MIN_TRACKS:
        try:
            dims = emotion.dimensions()
        except emotion.ModelMissing:
            return out
        matrix = np.array([[float(ratings[i].get(d, 0.0)) for d in dims] for i in rated])
        learned = emotion.mood_scores(matrix, dims)
        va = emotion.valence_arousal(matrix, dims)
        w = emotion.LEARNED_WEIGHT
        for k, i in enumerate(rated):
            if i in out:  # blend with the lighter signals
                m = out[i]
                blended = tuple(float(w * learned[k][j] + (1 - w) * m.scores[j]) for j in range(len(emotion.MOOD_NAMES)))
                out[i] = Mood(float(w * va[k][0] + (1 - w) * m.valence), float(w * va[k][1] + (1 - w) * m.arousal), blended)
            else:  # scored but no fingerprint stored: the learned model alone
                out[i] = Mood(float(va[k][0]), float(va[k][1]), tuple(float(x) for x in learned[k]))
    return out


SIGMA = 1.0  # how far apart (in z-score units) two moods can be before they stop counting as the same mood


def match(a: Mood, b: Mood) -> float:
    """0..1: 1 for the same mood, falling smoothly with distance on the valence/arousal plane. When both songs have mood
    scores, how alike their named moods are (a Break-up song is closer to a Sad one than to a Party one) counts equally."""
    d2 = (a.valence - b.valence) ** 2 + (a.arousal - b.arousal) ** 2
    plane = math.exp(-d2 / (2 * SIGMA**2))
    if a.scores and b.scores:
        x, y = np.array(a.scores), np.array(b.scores)
        denom = float(np.linalg.norm(x) * np.linalg.norm(y))
        if denom > 1e-9:
            named = ((float(x @ y) / denom + 1.0) / 2.0) ** 2  # cosine -> 0..1, sharpened so only close moods score high
            return round(0.5 * plane + 0.5 * named, 12)  # rounded so a song matches itself exactly
    return plane


def label(valence: float, arousal: float) -> str:
    """A human word for the quadrant, with a neutral band so a middling song is not forced into a mood."""
    if abs(valence) < 0.3 and abs(arousal) < 0.3:
        return "balanced"
    if valence < 0:
        return "melancholic" if arousal < 0 else "intense"
    return "calm" if arousal < 0 else "upbeat"


# What the listener sees and types. The internal quadrant labels stay descriptive; these are the everyday words.
MOOD_NAMES = {"melancholic": "Sad", "upbeat": "Happy", "calm": "Calm", "intense": "Intense", "balanced": "Balanced"}
_ALIASES = {
    "melancholic": ("sad", "melancholic", "melancholy", "emotional", "heartbreak", "heartbroken", "blue", "gloomy", "sorrow", "pain"),
    "upbeat": ("happy", "upbeat", "energetic", "fun", "party", "dance", "joy", "joyful", "cheerful", "feel good", "feelgood"),
    "calm": ("calm", "chill", "relax", "relaxing", "soft", "peaceful", "romantic", "romance", "love", "soothing", "mellow"),
    "intense": ("intense", "angry", "dark", "aggressive", "powerful", "hype", "mass", "epic"),
    "balanced": ("balanced", "neutral", "medium"),
}


def resolve_mood(text: str) -> str | None:
    """The quadrant label for a word a listener might type ("sad", "chill", "party"), or None if it is not a mood."""
    t = " ".join(text.casefold().split())
    for label_, words in _ALIASES.items():
        if t in words:
            return label_
    return None


def strength(m: Mood) -> float:
    """How strongly a song sits in its mood (distance from the library's middle): the best examples sort first."""
    return math.hypot(m.valence, m.arousal)


# The moods a listener picks from, and the other words they might type for each.
MOOD_ALIASES = {
    "Love": ("love", "romantic", "romance", "lovely"),
    "Break-up": ("break-up", "breakup", "break up", "heartbreak", "heartbroken", "heart break", "pain", "painful"),
    "Sad": ("sad", "melancholic", "melancholy", "emotional", "blue", "gloomy", "sorrow", "crying", "depressing"),
    "Pleasant": ("pleasant", "calm", "chill", "relax", "relaxing", "soft", "peaceful", "soothing", "mellow", "easy"),
    "Happy": ("happy", "upbeat", "joy", "joyful", "cheerful", "feel good", "feelgood", "fun"),
    "Party": ("party", "dance", "energetic", "energy", "club"),
    "Mass": ("mass", "intense", "aggressive", "powerful", "hype", "epic", "angry", "dark", "hero", "heroic"),
    "Vibe": ("vibe", "vibes", "cool", "groove", "groovy", "stylish"),
    "Nostalgic": ("nostalgic", "nostalgia", "retro", "old", "classic", "memories"),
    "Devotional": ("devotional", "spiritual", "bhakti", "god", "prayer", "divine", "mystical"),
    "Motivational": ("motivational", "motivation", "inspiring", "inspirational", "workout", "gym", "hopeful"),
}


def resolve_mood_name(text: str) -> str | None:
    """The mood (Love, Break-up, Sad, ...) for a word a listener might type ("heartbreak", "chill", "mass"), or None."""
    t = " ".join(text.casefold().split())
    for name, words in MOOD_ALIASES.items():
        if t == name.casefold() or t in words:
            return name
    return None


def score_for(m: Mood, name: str) -> float:
    return m.scores[emotion.MOOD_NAMES.index(name)] if m.scores else 0.0
