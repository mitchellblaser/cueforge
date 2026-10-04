# Analysis accuracy

Measured with `python -m tests.evaluate` (full mix) and `python -m tests.evaluate --stems`
(drums / bass / harmony / lead imported as Stem tracks) on the test corpus in
`tests/corpus.py`. The same floors are enforced by `tests/test_corpus.py`.

## The test corpus

Six songs composed as MIDI and rendered with a sampled General-MIDI soundfont (FluidR3:
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

| song | beatF | beatAMLt | downF | meter | hitF | fillF | fillP | fillR | phraseF | chordF | sectionR | secs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| rock_live | 0.97 | 0.95 | 0.97 | 4 | 0.88 | 0.73 | 0.80 | 0.67 | 0.00 | 0.97 | 1.00 | 15.2 |
| edm_club | 0.98 | 0.96 | 0.98 | 4 | 0.89 | 0.86 | 1.00 | 0.75 | 0.00 | 0.80 | 1.00 | 12.7 |
| funk_live | 0.97 | 0.96 | 0.98 | 4 | 0.65 | 0.80 | 1.00 | 0.67 | 0.00 | 0.98 | 0.67 | 11.8 |
| ballad_live | 0.65 | 0.95 | 0.65 | 4 | 0.50 | 0.00 | 0.00 | 0.00 | 0.00 | 0.94 | 0.50 | 12.2 |
| waltz_34 | 0.96 | 0.92 | 0.96 | 3 | 0.43 | 1.00 | 1.00 | 1.00 | 0.00 | 0.98 | 1.00 | 4.2 |
| pop_halftime | 0.97 | 0.94 | 0.96 | 4 | 0.69 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 | 1.00 | 6.8 |
| MEAN | 0.92 | 0.95 | 0.92 | 3.83 | 0.67 | 0.73 | 0.80 | 0.68 | 0.00 | 0.95 | 0.86 |

## Results — with stems

| song | beatF | beatAMLt | downF | meter | hitF | fillF | fillP | fillR | phraseF | chordF | sectionR | secs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| rock_live | 0.98 | 0.96 | 0.97 | 4 | 0.81 | 1.00 | 1.00 | 1.00 | 1.00 | 0.77 | 0.75 | 13.9 |
| edm_club | 0.98 | 0.96 | 0.98 | 4 | 0.81 | 1.00 | 1.00 | 1.00 | 1.00 | 0.65 | 1.00 | 13.0 |
| funk_live | 0.98 | 0.97 | 0.98 | 4 | 0.46 | 0.67 | 0.67 | 0.67 | 0.35 | 0.98 | 0.33 | 11.3 |
| ballad_live | 0.65 | 0.96 | 0.16 | 3 | 0.88 | 0.20 | 0.11 | 1.00 | 1.00 | 0.81 | 1.00 | 11.3 |
| waltz_34 | 0.96 | 0.92 | 0.96 | 3 | 0.70 | 0.91 | 0.83 | 1.00 | 1.00 | 0.86 | 1.00 | 4.6 |
| pop_halftime | 0.97 | 0.94 | 0.96 | 4 | 0.54 | 1.00 | 1.00 | 1.00 | 1.00 | 0.94 | 1.00 | 7.3 |
| MEAN | 0.92 | 0.95 | 0.84 | 3.67 | 0.70 | 0.80 | 0.77 | 0.94 | 0.89 | 0.83 | 0.85 |

## Column meanings

- **beatF**: beat F-measure (±70 ms). **beatAMLt**: the same, but accepting double/half
  tempo or off-beat lock (use *Grid › Halve/Double tempo* to fix those).
- **downF**: downbeat (bar 1) F-measure. **meter**: detected beats per bar.
- **hitF**: kick/snare/crash hits at the default 75% threshold (±50 ms; ghost notes excluded).
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
- **Hits in quiet, busy-cymbal passages** (ride / side-stick ballads, soft waltz) produce
  extra suggestions. Raise the Hits threshold or let the app learn it.
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
