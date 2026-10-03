"""Test corpus: multi-genre songs rendered from MIDI with a sampled GM soundfont.

Every song comes with exact ground truth (beats, downbeats, drum fills, lead
notes/phrases, chord changes, sections). "Live" songs get tempo drift, human
timing, a rubato intro (ballad), room reverb and crowd noise.

Needs: pretty_midi, pyfluidsynth, a GM soundfont (fluid-soundfont-gm). Songs are
cached in tests/.corpus_cache.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field

import numpy as np

SR = 44100
CORPUS_VERSION = "3"   # bump when composition changes so cached renders are refreshed
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".corpus_cache")
SOUNDFONTS = ["/usr/share/sounds/sf2/FluidR3_GM.sf2", "/usr/share/soundfonts/FluidR3_GM.sf2",
              "/usr/share/sounds/sf2/default-GM.sf2"]

KICK, SNARE, SIDESTICK, CLAP, HAT, OPEN_HAT, CRASH, RIDE = 36, 38, 37, 39, 42, 46, 49, 51
TOMS = [50, 48, 47, 45, 43, 41]


def soundfont() -> str | None:
    return next((p for p in SOUNDFONTS if os.path.exists(p)), None)


def available() -> bool:
    try:
        import fluidsynth  # noqa: F401
        import pretty_midi  # noqa: F401
    except Exception:
        return False
    return soundfont() is not None


@dataclass
class Section:
    name: str
    bars: int
    energy: float = 0.7          # 0..1 velocity scale
    drums: str | None = "rock"   # rock | four | funk | halftime | soft | waltz | None
    fill_every: int = 0          # bars; 0 = only before the next section
    bass: bool = True
    chords: int | None = None    # GM program
    lead: int | None = None      # GM program
    lead_style: str = "melody"   # melody | arp | riff
    roll: bool = False           # EDM-style snare roll build through the section
    progression: tuple = (0, 5, 3, 4)   # scale degrees (major key), one per bar


@dataclass
class SongSpec:
    name: str
    bpm: float
    sections: list[Section]
    bpb: int = 4
    key: int = 57                # MIDI root (A3)
    drift: float = 0.0           # tempo drift amplitude (fraction)
    push: float = 0.0            # tempo push in high-energy sections (fraction)
    humanize: float = 0.0        # seconds (std) of note timing jitter
    rubato_intro: bool = False
    live: bool = False
    bass_program: int = 33
    seed: int = 0


MAJOR = [0, 2, 4, 5, 7, 9, 11]


def _chord(key: int, degree: int) -> list[int]:
    notes = [key + MAJOR[(degree + k) % 7] + 12 * ((degree + k) // 7) for k in (0, 2, 4)]
    return notes


def corpus() -> list[SongSpec]:
    return [
        SongSpec("rock_live", 124, [
            Section("intro", 4, 0.5, None, bass=False, chords=29),
            Section("verse", 8, 0.65, "rock", 4, chords=29),
            Section("chorus", 8, 0.95, "rock", 4, chords=29, lead=30),
            Section("verse2", 8, 0.65, "rock", 4, chords=29),
            Section("chorus2", 8, 1.0, "rock", 4, chords=29, lead=30),
        ], drift=0.02, push=0.02, humanize=0.010, live=True, seed=1),
        SongSpec("edm_club", 128, [
            Section("intro", 8, 0.6, "four", 0, chords=89, progression=(5, 3, 0, 4)),
            Section("build", 8, 0.75, None, roll=True, bass=False, chords=89, lead=81, lead_style="arp",
                    progression=(5, 3, 0, 4)),
            Section("drop", 16, 1.0, "four", 8, chords=89, lead=81, lead_style="riff", progression=(5, 3, 0, 4)),
            Section("break", 8, 0.5, None, bass=False, chords=89, progression=(5, 3, 0, 4)),
        ], key=53, bass_program=38, seed=2),
        SongSpec("funk_live", 104, [
            Section("groove", 8, 0.7, "funk", 4, chords=7, progression=(1, 4, 1, 4)),
            Section("horns", 8, 0.9, "funk", 4, chords=7, lead=61, lead_style="riff", progression=(1, 4, 1, 4)),
            Section("solo", 8, 0.85, "funk", 4, chords=7, lead=65, progression=(1, 4, 1, 4)),
            Section("out", 4, 0.9, "funk", 0, chords=7, lead=61, lead_style="riff", progression=(1, 4, 1, 4)),
        ], key=52, drift=0.015, humanize=0.012, live=True, bass_program=36, seed=3),
        SongSpec("ballad_live", 72, [
            Section("intro", 4, 0.4, None, bass=False, chords=0, progression=(0, 4, 5, 3)),
            Section("verse", 8, 0.55, "soft", 0, chords=0, lead=53, progression=(0, 4, 5, 3)),
            Section("chorus", 8, 0.85, "rock", 4, chords=48, lead=53, progression=(3, 4, 0, 5)),
        ], key=55, drift=0.03, humanize=0.015, rubato_intro=True, live=True, seed=4),
        SongSpec("waltz_34", 156, [
            Section("a", 8, 0.6, "waltz", 4, chords=0, lead=73, progression=(0, 3, 4, 0)),
            Section("b", 8, 0.8, "waltz", 4, chords=48, lead=73, progression=(5, 1, 4, 0)),
            Section("a2", 8, 0.65, "waltz", 4, chords=0, lead=73, progression=(0, 3, 4, 0)),
        ], bpb=3, key=60, humanize=0.006, seed=5),
        SongSpec("pop_halftime", 140, [
            Section("verse", 8, 0.6, "halftime", 8, chords=4, lead=80, progression=(0, 5, 3, 4)),
            Section("chorus", 8, 0.95, "rock", 8, chords=4, lead=80, lead_style="riff", progression=(3, 4, 5, 4)),
            Section("verse2", 8, 0.6, "halftime", 8, chords=4, lead=80, progression=(0, 5, 3, 4)),
        ], key=58, seed=6),
    ]


# --------------------------------------------------------------------- tempo
def _beat_times(spec: SongSpec, rng) -> tuple[np.ndarray, list[int]]:
    """Times of every beat (plus one extra for the song end) and section start beats."""
    tempos = []
    starts = []
    b = 0
    for sec in spec.sections:
        starts.append(b)
        for k in range(sec.bars * spec.bpb):
            phase = 2 * np.pi * (b + k) / (16 * spec.bpb)
            t = spec.bpm * (1 + spec.drift * np.sin(phase))
            if sec.energy >= 0.9:
                t *= 1 + spec.push
            tempos.append(t)
        if spec.rubato_intro and sec is spec.sections[0]:
            n = sec.bars * spec.bpb
            walk = np.cumsum(rng.normal(0, 0.025, n))
            tempos[-n:] = list(np.asarray(tempos[-n:]) * (1 + np.clip(walk - walk.mean(), -0.12, 0.12)))
        b += sec.bars * spec.bpb
    ibis = 60.0 / np.asarray(tempos)
    times = np.concatenate([[0.0], np.cumsum(ibis)]) + 0.5  # half a second of lead-in
    return times, starts


# --------------------------------------------------------------------- compose
def compose(spec: SongSpec):
    import pretty_midi
    rng = np.random.default_rng(spec.seed)
    beats, sec_starts = _beat_times(spec, rng)
    bpb = spec.bpb

    def at(beat_pos: float, human: bool = True) -> float:
        t = float(np.interp(beat_pos, np.arange(len(beats)), beats))
        if human and spec.humanize:
            t += float(rng.normal(0, spec.humanize))
        return max(0.0, t)

    drums = pretty_midi.Instrument(0, is_drum=True, name="drums")
    bass = pretty_midi.Instrument(spec.bass_program, name="bass")
    harm: dict[int, pretty_midi.Instrument] = {}
    lead: dict[int, pretty_midi.Instrument] = {}
    truth = {"beats": [], "downbeats": [], "fills": [], "lead_notes": [], "lead_phrases": [],
             "chord_changes": [], "sections": [], "kicks": [], "snares": [], "crashes": []}

    def hit(note, pos, vel, dur=0.1):
        t = at(pos)
        drums.notes.append(pretty_midi.Note(int(np.clip(vel, 1, 127)), note, t, t + dur))
        if vel < 60:      # ghost notes are not lighting hits
            return
        if note == KICK:
            truth["kicks"].append(t)
        if note in (SNARE, CLAP):
            truth["snares"].append(t)
        if note == CRASH:
            truth["crashes"].append(t)

    prev_chord = None
    last_lead_end = -10.0
    for si, sec in enumerate(spec.sections):
        b0 = sec_starts[si]
        truth["sections"].append((float(beats[b0]), sec.name))
        v = 60 + 60 * sec.energy
        next_has_drums = si + 1 < len(spec.sections)
        for bar in range(sec.bars):
            bb = b0 + bar * bpb
            degree = sec.progression[bar % len(sec.progression)]
            chord = _chord(spec.key, degree)
            if chord != prev_chord:
                truth["chord_changes"].append(float(beats[bb]))
                prev_chord = chord
            # ---- drums
            phrase_end = sec.fill_every and (bar + 1) % sec.fill_every == 0
            section_end = bar == sec.bars - 1 and next_has_drums and sec.drums
            fill_len = 0.0
            if sec.drums and (phrase_end or section_end) and bb + bpb < len(beats) - 1:
                # half a beat up to a whole bar, at different speeds: 8ths (slow tom fill),
                # 16ths, sextuplets, 32nd-note rolls
                fill_len = float(rng.choice([0.5, 1, 2, 2, bpb] if bpb == 4 else [0.5, 1, bpb]))
                rate = int(rng.choice([2, 4, 4, 6, 8]))
                if fill_len * rate < 2:
                    rate = 4
            groove_beats = bpb - int(np.ceil(fill_len))
            if sec.drums:
                _groove(sec.drums, bb, groove_beats, bpb, v, hit, rng, first_of_section=(bar == 0))
            if fill_len:
                if fill_len != int(fill_len):        # groove carries on into the partial beat
                    hit(KICK, bb + groove_beats, v * 0.8)
                    hit(HAT, bb + groove_beats, v * 0.5)
                start = bb + bpb - fill_len
                _fill(start, fill_len, rate, v, hit, rng)
                truth["fills"].append((at(start, False), float(beats[bb + bpb]), rate))
            if sec.roll:
                # snare roll that gets denser through the section (EDM build)
                density = [1, 1, 2, 2, 2, 4, 4, 8][min(7, bar * 8 // sec.bars)]  # hits per beat
                steps = bpb * density
                for k in range(steps):
                    hit(SNARE, bb + k * bpb / steps, 50 + 70 * (bar / sec.bars), 0.05)
                if bar == 0:
                    truth["fills"].append((float(beats[bb]), float(beats[b0 + sec.bars * bpb]), 4))
            # ---- bass
            if sec.bass:
                root = chord[0] - 24
                for k in range(bpb * 2):
                    if sec.drums == "funk" and k % 3 == 2:
                        continue
                    t0 = at(bb + k / 2)
                    bass.notes.append(pretty_midi.Note(int(v), root + (12 if k % 4 == 3 else 0), t0, t0 + 0.22))
            # ---- chords
            if sec.chords is not None:
                inst = harm.setdefault(sec.chords, pretty_midi.Instrument(sec.chords, name="harmony"))
                if sec.chords in (7, 29, 4):   # rhythmic comping
                    for k in range(bpb):
                        t0 = at(bb + k)
                        for n in chord:
                            inst.notes.append(pretty_midi.Note(int(v * 0.8), n, t0, t0 + 60 / spec.bpm * 0.8))
                else:                          # sustained
                    t0, t1 = at(bb, False), at(bb + bpb, False)
                    for n in chord:
                        inst.notes.append(pretty_midi.Note(int(v * 0.7), n, t0, t1 - 0.02))
            # ---- lead
            if sec.lead is not None:
                inst = lead.setdefault(sec.lead, pretty_midi.Instrument(sec.lead, name="lead"))
                notes = _lead_bar(sec, bar, bb, bpb, chord, rng)
                for pos, pitch, dur in notes:
                    t0 = at(pos)
                    t1 = at(pos + dur, False) - 0.01
                    if t1 <= t0 + 0.03:
                        t1 = t0 + 0.08
                    inst.notes.append(pretty_midi.Note(int(min(127, v + 10)), pitch, t0, t1))
                    truth["lead_notes"].append(t0)
                    if t0 - last_lead_end > 0.6:
                        truth["lead_phrases"].append(t0)
                    last_lead_end = t1
        # cymbal crash at section starts that have drums
        if sec.drums and si > 0:
            hit(CRASH, b0, v + 10, 0.8)
    end_beat = sec_starts[-1] + spec.sections[-1].bars * bpb
    truth["beats"] = [float(x) for x in beats[:end_beat]]
    truth["downbeats"] = [float(x) for x in beats[:end_beat:bpb]]
    truth["duration"] = float(beats[end_beat]) + 2.0
    truth["bpb"] = bpb
    truth["live"] = spec.live
    stems = {"drums": [drums], "bass": [bass], "harmony": list(harm.values()), "lead": list(lead.values())}
    return stems, truth


def _groove(style, bb, nbeats, bpb, v, hit, rng, first_of_section):
    for k in range(nbeats):
        pos = bb + k
        if style == "rock":
            if k in (0, 2):
                hit(KICK, pos, v)
            if k == 2 and rng.random() < 0.5:
                hit(KICK, pos + 0.5, v * 0.8)
            if k in (1, 3):
                hit(SNARE, pos, v)
            hit(HAT, pos, v * 0.6)
            hit(HAT, pos + 0.5, v * 0.45)
        elif style == "four":
            hit(KICK, pos, v)
            if k in (1, 3):
                hit(CLAP, pos, v * 0.9)
            hit(OPEN_HAT, pos + 0.5, v * 0.5)
        elif style == "funk":
            if k == 0 or (k == 2 and True):
                hit(KICK, pos, v)
            if k == 1:
                hit(KICK, pos + 0.75, v * 0.8)
            if k in (1, 3):
                hit(SNARE, pos, v)
            if k in (0, 2):
                hit(SNARE, pos + 0.75, v * 0.3)  # ghost note
            for q in range(4):
                hit(HAT, pos + q / 4, v * (0.5 if q % 2 == 0 else 0.3))
        elif style == "halftime":
            if k == 0:
                hit(KICK, pos, v)
            if k == 2:
                hit(SNARE, pos, v)
            if k == 3:
                hit(KICK, pos + 0.5, v * 0.7)
            hit(HAT, pos, v * 0.6)
            hit(HAT, pos + 0.5, v * 0.4)
        elif style == "soft":
            if k == 0:
                hit(KICK, pos, v * 0.8)
            if k in (1, 3):
                hit(SIDESTICK, pos, v * 0.7)
            hit(RIDE, pos, v * 0.5)
            hit(RIDE, pos + 0.5, v * 0.35)
        elif style == "waltz":
            if k == 0:
                hit(KICK, pos, v)
            else:
                hit(SNARE, pos, v * 0.5)
            hit(HAT, pos, v * 0.5)


def _fill(start, nbeats, rate, v, hit, rng):
    """A fill of `nbeats` beats at `rate` notes per beat, snare first then down the toms."""
    steps = int(round(nbeats * rate))
    toms = TOMS[:max(2, min(len(TOMS), steps // 2 + 1))]
    roll = rate >= 8
    for q in range(steps):
        frac = q / steps
        if roll:
            note = SNARE if frac < 0.75 else toms[min(len(toms) - 1, int((frac - 0.75) * 4 * len(toms)))]
        else:
            note = SNARE if q < steps // 4 else toms[min(len(toms) - 1, int(frac * len(toms)))]
        hit(note, start + q / rate, v * (0.7 + 0.4 * frac), 0.1)

def _lead_bar(sec: Section, bar: int, bb: int, bpb: int, chord, rng):
    scale_root = chord[0] + 12
    tones = [n + 12 for n in chord] + [chord[0] + 24]
    out = []
    if sec.lead_style == "arp":
        for k in range(bpb * 4):
            out.append((bb + k / 4, tones[k % len(tones)], 0.22))
    elif sec.lead_style == "riff":
        pattern = [(0, 0), (0.75, 2), (1.5, 1), (2.5, 3)] if bpb == 4 else [(0, 0), (1, 2), (2, 1)]
        for pos, i in pattern:
            out.append((bb + pos, tones[i], 0.4))
    else:
        # call-and-response melody: 2 bars of notes, then 2 bars rest
        if (bar // 2) % 2 == 0:
            pos = 0.0
            while pos < bpb - 0.25:
                dur = float(rng.choice([0.5, 1.0, 1.0, 1.5])) if bpb == 4 else float(rng.choice([1.0, 0.5]))
                dur = min(dur, bpb - pos)
                pitch = int(rng.choice(tones)) + int(rng.choice([0, 2, -1])) * (0 if rng.random() < 0.6 else 1)
                out.append((bb + pos, pitch, dur * 0.9))
                pos += dur
    return out


# --------------------------------------------------------------------- render
def _render_instruments(insts, duration: float) -> np.ndarray:
    import pretty_midi
    n = int(duration * SR)
    if not insts or not any(i.notes for i in insts):
        return np.zeros(n, np.float32)
    pm = pretty_midi.PrettyMIDI()
    pm.instruments.extend(insts)
    y = pm.fluidsynth(fs=SR, synthesizer=soundfont())
    out = np.zeros(n, np.float32)
    m = min(n, len(y))
    out[:m] = y[:m]
    return out


def _live_fx(y: np.ndarray, rng) -> np.ndarray:
    from scipy.signal import butter, fftconvolve, sosfilt
    # room reverb: decaying noise impulse response (RT60 ~1.3 s)
    t = np.arange(int(1.6 * SR)) / SR
    ir = rng.standard_normal(len(t)) * np.exp(-6.9 * t / 1.3)
    ir[0] = 0
    ir /= np.sqrt(np.sum(ir ** 2))
    wet = fftconvolve(y, ir)[: len(y)]
    y = 0.8 * y + 0.35 * wet * (np.std(y) / (np.std(wet) + 1e-9))
    # crowd: band-limited noise with slow swells
    noise = rng.standard_normal(len(y))
    sos = butter(2, [300, 3000], btype="band", fs=SR, output="sos")
    crowd = sosfilt(sos, noise)
    swell = np.interp(np.arange(len(y)), np.linspace(0, len(y), 12), rng.uniform(0.3, 1.0, 12))
    crowd *= swell * np.std(y) * 0.12 / (np.std(crowd) + 1e-9)
    # gentle gain riding by the sound engineer
    ride = 1 + 0.08 * np.sin(2 * np.pi * np.arange(len(y)) / SR / 23.0)
    return (y + crowd) * ride


def render(spec: SongSpec, cache: bool = True):
    """Returns (mix stereo float32, {stem: mono}, truth)."""
    key = hashlib.sha1((CORPUS_VERSION + json.dumps(asdict(spec), sort_keys=True, default=str)).encode()
                       ).hexdigest()[:12]
    path = os.path.join(CACHE, f"{spec.name}_{key}.npz")
    if cache and os.path.exists(path):
        z = np.load(path, allow_pickle=True)
        stems = {k[5:]: z[k] for k in z.files if k.startswith("stem_")}
        return z["mix"], stems, json.loads(str(z["truth"]))
    insts, truth = compose(spec)
    stems = {name: _render_instruments(i, truth["duration"]) for name, i in insts.items()}
    gains = {"drums": 1.0, "bass": 0.8, "harmony": 0.6, "lead": 0.75}
    mix = sum(stems[k] * gains[k] for k in stems)
    if spec.live:
        mix = _live_fx(mix, np.random.default_rng(spec.seed + 100))
    peak = np.abs(mix).max() + 1e-9
    mix = (mix / peak * 0.89).astype(np.float32)
    stems = {k: (s / peak * 0.89 * gains[k]).astype(np.float32) for k, s in stems.items()}
    stereo = np.stack([mix, mix], axis=1)
    if cache:
        os.makedirs(CACHE, exist_ok=True)
        np.savez_compressed(path, mix=stereo, truth=json.dumps(truth),
                            **{f"stem_{k}": v for k, v in stems.items()})
    return stereo, stems, truth


if __name__ == "__main__":
    import sys
    import soundfile as sf
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    for spec in corpus():
        mix, stems, truth = render(spec)
        sf.write(os.path.join(out, f"{spec.name}.wav"), mix, SR)
        for k, s in stems.items():
            sf.write(os.path.join(out, f"{spec.name}_{k}.wav"), s, SR)
        print(spec.name, f"{truth['duration']:.1f}s", len(truth["beats"]), "beats", len(truth["fills"]), "fills",
              len(truth["lead_phrases"]), "phrases", len(truth["chord_changes"]), "chord changes")
