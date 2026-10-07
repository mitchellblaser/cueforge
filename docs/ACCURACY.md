# Analysis accuracy

Measured with `python -m tests.evaluate` (full mix) and `python -m tests.evaluate --stems`
(drums / bass / harmony / lead imported as Stem tracks) on the test corpus in
`tests/corpus.py`. The same floors are enforced by `tests/test_corpus.py`.

## The test corpus

Six songs composed as MIDI (with band stabs and stops as accent ground truth) and rendered with a sampled General-MIDI soundfont (FluidR3:
real recorded drum kits, basses, pianos, guitars, synths, brass, choir). Every song has
exact ground truth for beats, downbeats, drum fills, lead-line notes/phrases, chord
changes and sections.

| Song | Style | Live features |
|---|---|---|
| rock_live | 124 BPM rock, distorted guitars, guitar lead | ±2% tempo drift, push in choruses, 10 ms human timing, room reverb, crowd |
| edm_club | 128 BPM four-on-the-floor, snare-roll build, saw arp / riff | studio-tight |
| funk_live | 104 BPM syncopated funk, ghost notes, clav, horns, sax solo | drift, 12 ms timing, reverb, crowd |
| ballad_live | 72 BPM ballad, free-time piano intro, choir "vocal" line | rubato intro, ±3% drift, 15 ms timing, reverb, crowd |
| waltz_34 | 3/4 at 156 BPM, piano / strings, flute melody | light humanising |
| pop_halftime | 140 BPM half-time pop, EP, square-wave lead | studio-tight |

**Caveat:** this is rendered audio, not real band recordings. It covers tempo drift,
loose timing, reverb, crowd noise and real instrument samples, but not mic bleed, PA
compression or the sound of a real room. Please test on your own live recordings too.
The thresholds and the threshold learning in the app are there to adapt to your material.

## Results — full mix only

| song | beatF | beatAMLt | downF | meter | accP | accR | hits/min | fillF | fillP | fillR | phraseF | chordF | sectionR | secs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| rock_live | 0.99 | 0.98 | 0.99 | 4 | 0.36 | 0.83 | 11.75 | 0.60 | 0.75 | 0.50 | 0.00 | 0.96 | 1.00 | 17.8 |
| edm_club | 0.99 | 0.99 | 0.99 | 4 | 0.22 | 0.67 | 6.97 | 0.86 | 1.00 | 0.75 | 0.00 | 0.78 | 0.67 | 16.8 |
| funk_live | 0.99 | 0.99 | 0.98 | 4 | 0.23 | 0.60 | 11.64 | 0.80 | 1.00 | 0.67 | 0.00 | 0.93 | 0.67 | 13.7 |
| ballad_live | 0.62 | 0.87 | 0.60 | 4 | 0.21 | 1.00 | 12.18 | 1.00 | 1.00 | 1.00 | 0.00 | 0.88 | 1.00 | 14.5 |
| waltz_34 | 0.99 | 0.99 | 0.98 | 3 | 0.38 | 1.00 | 15.90 | 0.89 | 1.00 | 0.80 | 0.00 | 0.98 | 1.00 | 5.4 |
| pop_halftime | 0.99 | 0.99 | 0.98 | 4 | 0.50 | 0.67 | 5.50 | 1.00 | 1.00 | 1.00 | 0.00 | 0.96 | 0.50 | 8.0 |
| MEAN | 0.93 | 0.97 | 0.92 | 3.83 | 0.32 | 0.79 | 10.66 | 0.86 | 0.96 | 0.79 | 0.00 | 0.91 | 0.81 | – |

## Results — with stems

| song | beatF | beatAMLt | downF | meter | accP | accR | hits/min | fillF | fillP | fillR | phraseF | chordF | sectionR | secs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| rock_live | 1.00 | 0.99 | 0.99 | 4 | 0.46 | 1.00 | 10.91 | 1.00 | 1.00 | 1.00 | 1.00 | 0.75 | 1.00 | 18.8 |
| edm_club | 0.99 | 0.99 | 0.99 | 4 | 0.29 | 0.67 | 5.42 | 1.00 | 1.00 | 1.00 | 0.67 | 0.66 | 0.67 | 15.5 |
| funk_live | 1.00 | 0.99 | 0.98 | 4 | 0.33 | 0.60 | 8.06 | 0.67 | 0.67 | 0.67 | 0.35 | 0.90 | 0.33 | 13.8 |
| ballad_live | 0.61 | 0.80 | 0.16 | 3 | 0.03 | 0.33 | 27.84 | 0.22 | 0.12 | 1.00 | 1.00 | 0.52 | 1.00 | 13.6 |
| waltz_34 | 0.99 | 0.99 | 0.98 | 3 | 0.43 | 1.00 | 13.91 | 1.00 | 1.00 | 1.00 | 1.00 | 0.81 | 1.00 | 5.9 |
| pop_halftime | 0.99 | 0.99 | 0.98 | 4 | 0.40 | 0.67 | 6.87 | 1.00 | 1.00 | 1.00 | 0.89 | 0.94 | 0.50 | 9.2 |
| MEAN | 0.93 | 0.96 | 0.85 | 3.67 | 0.32 | 0.71 | 12.17 | 0.81 | 0.80 | 0.94 | 0.82 | 0.76 | 0.75 | – |

## Column meanings

- **beatF**: beat F-measure (±70 ms). **beatAMLt**: the same, but accepting double/half
  tempo or off-beat lock (use *Grid › Halve/Double tempo* to fix those).
- **downF**: downbeat (bar 1) F-measure. **meter**: detected beats per bar.
- **accP / accR / hits/min**: hits are *accents*, scored against the accents in each song (crashes
  at section starts and fill landings, band stabs off the beat, stops) at the default 60% threshold,
  ±70 ms. accP: share of shown hit suggestions on an accent (slow tom fills and horn riffs that a
  programmer may well light count against it); accR: accents found; hits/min: how many are shown.
  The old detector suggested every kick and snare: 80–160 a minute, about 3% of them on an accent.
- **fillF / fillP / fillR**: *fast* drum fills (16ths, sextuplets, 32nd rolls into a bar line). A suggestion is
  correct if it starts inside a real fast fill. Slow 8th-note tom fills are deliberately not suggested: they
  count neither for nor against.
- **phraseF**: lead-line phrase starts (±150 ms). Full-mix lead lines are capped at 45%
  confidence and hidden by default, so they score 0 here by design. Use stems or Demucs.
- **chordF**: chord changes (±150 ms). **sectionR**: section changes found (±0.5 s).

## Known weak spots

- **Fills on a double-tempo grid** (the ballad): fills must land on a bar line, so a grid at double tempo
  hides them. Use *Halve tempo* and analyse again. With a correct grid the ballad scores 1.00.
- **Very short pickups** (two 16th notes) are not reported as fills.

- **Rubato / free-time passages** (ballad intro): beats are unreliable. Use the
  *Tap-along grid*.
- **Slow songs** may be tracked at double tempo (ballad: 146 vs 72 BPM). Use *Halve tempo*.
- **Hits on a wrong grid** (the ballad's stems, tracked in 3/4): accents are found by comparing
  bars, so a wrong bar length gives too many. Fix the grid first.
- **Spoken cue tracks** aren't in this corpus (it has no voice). Their timing is tested with
  synthetic calls in `tests/test_cuetrack.py`, and word recognition with espeak-ng speech when
  the keyword model is installed. Real cue voices are clearer than espeak.
- **Lead lines without stems** are rough. Import stems, or enable Demucs.

## Gaps and pauses (downbeat F-measure)

Each test song was run again with a gap inserted before it, or a pause in the middle. The
scores are the downbeat F-measure in the music after the gap or pause (70 ms window).

| Song | No gap | 3.7 s silent intro | 6 s noisy intro | 1.37 s pause | 2.5-beat pause | 2-bar stop |
|---|---|---|---|---|---|---|
| rock_live | 0.99 | 0.99 | 0.99 | 0.94 | 0.94 | 0.94 |
| edm_club | 0.99 | 0.99 | 0.79 | 0.95 | 0.95 | 0.95 |
| funk_live | 0.98 | 0.98 | 0.98 | 0.93 | 0.93 | 0.93 |
| waltz_34 | 0.98 | 0.98 | 0.98 | 0.92 | 0.92 | 0.92 |
| pop_halftime | 0.98 | 0.98 | 0.98 | 0.92 | 0.92 | 0.92 |
| ballad_live | 0.66 | 0.66 | 0.56 | 0.16 | 0.16 | 0.17 |

Before this change, a gap put bar 1 near 0 s instead of at the music. A pause that wasn't a
whole number of bars gave 0.00 on one side of it.

The ballad is tracked at double tempo, so its downbeats are only half right even without a
pause. Fix it with *Grid › Halve tempo*, then press **D** on the one.
