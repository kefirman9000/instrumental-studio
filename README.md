# Instrumental Studio

Turns a song into an instrumental, finds the places where removing the vocals damaged the
instruments underneath, and rebuilds them from the same instrument playing the same part
somewhere else in the song.

## Install (once)

Download the latest release from the [Releases page](https://github.com/kefirman9000/instrumental-studio/releases)
(Source code .zip) and unzip it, or clone it:

```
git clone https://github.com/kefirman9000/instrumental-studio.git
```


1. Install **Python 3.10 or newer** from python.org (on Windows, tick "Add Python to PATH").
2. Install **ffmpeg** (needed for MP3/M4A files):
   - Windows: `winget install ffmpeg`
   - Mac: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`
3. In a terminal, inside this folder:

   ```
   pip install -r requirements.txt
   ```

   For an NVIDIA GPU on Windows/Linux, install the CUDA build of PyTorch first
   (pick your options at pytorch.org), then run the line above. Apple silicon Macs use the GPU automatically.

## Start

- Windows: double-click **Start Instrumental Studio (Windows).bat**
- Mac / Linux: `./start.sh` (or `python3 app.py`)

The app opens in its own window. If pywebview isn't installed it uses a Chrome or Edge app window;
`python app.py --browser` opens it in a normal browser tab instead.

Projects are saved automatically in `~/InstrumentalStudio/projects`, exports go to
`~/InstrumentalStudio/exports`.

## Using it

1. **Open a song** (splits it with Demucs), **use stems you already have** (from UVR, Spleeter,
   LALAL…; select all the files, one must have "vocal" in its name), or **try the demo song**.
2. The **timeline** shows each instrument as a spectrogram.
   - *Damage* view: red marks where the vocal took that instrument's sound.
   - *Repaired* view: amber marks the sound that was filled back in.
   - Bars under each lane are the edits: amber = applied, white = waiting for you, red = no good
     source, pink = leftover vocal found in that instrument.
   - Click an edit to select it; click anywhere else to move the playhead. Ctrl+scroll zooms,
     Shift+scroll moves sideways.
3. **Review** tab: every edit, grouped by section.
   - Play **Before / After / Only what's added / Source / Original song**, solo or in the mix.
   - **Loop A/B** plays before and after in sync; press A and B (or click) to flip between them.
   - **Strength** turns a repair down or up.
   - **Source**: pick another matching place from the list, or **Pick on timeline** and click
     anywhere in the same instrument's lane to borrow from there.
   - Apply / Reject one edit, or everything in a section or instrument at once.
4. **Repairability** tab: a grid of sections × instruments showing how much was damaged and how
   much of it can be rebuilt. Click a cell to review just that part. Double-click a section name
   to rename it.
5. **Levels** tab: loudness of each instrument in each section, with a boost/cut in dB per cell.
   Each lane also has a master level, mute and solo.
6. **Undo** (Ctrl+Z) steps back through any edit or level change.
7. **Export**: repaired instrumental, each instrument on its own, raw instrumental, vocals, and a
   JSON report of every edit, as WAV, FLAC or MP3.

Keyboard: Space play/pause · J/K next/previous edit · Enter apply · Delete reject · A/B flip loop · Esc stop.

**Settings** tab: the confidence above which repairs apply automatically (default 60%), the
cut-off for "no good match", and which separation model new songs use:

| Option | Stems | Notes |
|---|---|---|
| Drums / Bass / Other (best quality) | 4 | `htdemucs_ft`, recommended |
| Drums / Bass / Guitar / Piano / Other | 6 | `htdemucs_6s`, piano stem is weaker |
| Fast | 4 | `htdemucs` |
| Ensemble | 4 | averages two models: least damage, about twice as slow |

## Updates

Settings tab → **Check for updates** looks for a newer release on GitHub, shows what changed, and
**Install update** downloads and installs it, then asks to restart. When an update is ready, a
button also appears in the top bar.

- **Check for updates when the app starts** is on by default.
- **Install updates automatically** is off until you tick it. Once you agree, new versions install
  in the background at start-up; the app restarts on its own if no song is open, otherwise it
  shows **Restart to update**.
- Projects and exports live in `~/InstrumentalStudio` and are never changed by an update. The
  previous version is kept in the app folder under `.backup/<version>`.
- If you installed with `git clone`, updates use `git pull`.

### Publishing a new version (for the maintainer)

1. Bump `__version__` in `studio/__init__.py`.
2. Commit and push, then create a GitHub release whose tag is the version, e.g. `v1.1.0`
   (`gh release create v1.1.0 --notes "What changed"`). The release notes are shown in the app.

## How the repair works

1. Demucs splits the song into instrument stems and vocals.
2. The beat and bar grid are tracked, and the song is cut into sections (intro, verse, chorus…)
   by finding where the harmony and texture change and which sections repeat.
3. Every beat of every instrument is described by its tone (40 frequency bands) and harmony
   (chroma), measured **only in the frequencies the vocal didn't cover**.
4. For each bar where there were vocals, the app searches the whole song for bars where the
   **same instrument** plays the most similar part and was vocal-free where this bar was
   vocal-heavy (intro, breaks, vocal rests, a different melody line in another verse).
5. Damage = places where the vocal dominated *and* the instrument now has less energy than those
   matching bars say it should. That's the red map.
6. The missing sound is filled in from the matching bars: level-matched per frequency band, using
   the median of up to three sources, aligned to the beat, blended so untouched sound is kept, and
   cross-faded at the edges.
7. Leftover vocal in an instrument stem is detected by its spectrum moving in step with the vocal
   stem, and can be removed.

Tested on the built-in demo (where the true, undamaged instruments are known): the piano's
spectral error dropped from 4.9 dB to 1.9 dB and its missing energy from 2.3% to 0.7%. The guitar,
whose picking pattern changes every bar, improved less (7.0 → 6.0 dB error), because no other bar
plays exactly the same notes. Run `python tests/test_engine.py` to repeat the test.

## Limits worth knowing

- Repairs can only rebuild what the song repeats somewhere vocal-free. A unique solo under a
  vocal shows up red ("no good source") and is left alone unless you pick a source yourself.
- If an instrument plays something different in the intro than in the verse (an extra riff, a
  fill), a repair can add it where it doesn't belong. Use **Only what's added** to check, and
  turn the strength down or reject.
- Section names are a guess (the app can confuse verse and chorus). Rename them in the
  Repairability tab.
- Separation runs on your computer: a few minutes per song on a CPU, under a minute on a GPU.
