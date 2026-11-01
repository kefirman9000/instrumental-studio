# What's new

## 1.43.0

- **⭐ Make enhanced instrumental**, above the tracks: one click, the whole way. It splits with "Keeps every detail" if the song isn't split yet, repairs it, adds the finishing touches below, then checks that nothing was lost. Undo takes it back; Export to save it.
- **Finishing touches** (in the Repair window, also part of the one click):
  - **Air:** the sung parts lose some top end along with the voice. They get back up to half of what they lost compared with the parts nobody sings (at most +3 dB above 6 kHz).
  - **Level:** choruses sound smaller once the voice is gone. The sung parts come up by part of what they lost against the original (at most +3 dB, never down, never into clipping). It changes over seconds, so nothing pumps.
- **Check nothing was lost** (Repair window): where nobody sings, the instrumental should match the original. It shows how close it is, and flags any lost top end or stereo width, with what to try.
- **"Keeps every detail" split: second opinion on trouble spots.** Where the quick split heard much more singing than the stronger model took out, a second model checks just those seconds, and the singing either one hears there comes out. Without Roformer this costs nothing extra.

## 1.42.0

- **New split type: "Keeps every detail: takes out only the singing (fast)".** Instead of cutting the song into instruments and adding them back, the instrumental is the original with only the singing taken out, so no instrument detail is lost.
  - It's quick: one fast split, then a stronger vocal model runs **only on the parts with singing**. Parts without singing come straight from the original, untouched.
  - The stronger model is Roformer if you've installed it (cleanest), otherwise just the vocal half of the best Demucs model (a quarter of its usual work).
  - **The original's stereo sides are kept.** Singing is taken out of the centre, and out of the sides only where they're clearly singing too (wide backing vocals), so the instruments keep their width and space.
  - The drums, bass and other tracks still work for editing, and they add up exactly to that instrumental.
  - Pick it in the split list, or with **Split again** on a song you already split.

## 1.41.0

- **Clearer names.** *Build the best instrumental* is now **Repair the instrumental** (fixes the split). *Clean instrumental* is now **Rebuild with samples** (plays the song again from its own best material).
- **Step by step.** In the Repair window, **Step by step…** runs each step on its own, on top of the ones you kept, so you can hear what each one does and keep it or undo it.
- **Less muddy / underwater.** The "s" sound removal and the leftover-singing filter are no longer part of the repair (they took out highs and mids), and the steady-noise subtraction (a classic cause of that underwater sound) only runs in Enhance when you ask for it. Repair again (Start over first) to hear the difference.
- **Enhance (no vocals)**, above the tracks: for vinyl rips and instrumentals. Drums are put right and clicks and artifacts turned down, gently, across the whole song, with optional hiss and vinyl noise reduction.
- **Samples: only the common ones.** Exported and extracted samples only include sounds used at least 8 times (choose 4 to 24).
- *Check against the original* stays: it doesn't change the sound at all, it only stops fixes that would put the wrong part in.

## 1.40.0

**Steadier sound** (the level was jumping up and down):
- *Use the original between the vocal lines* and *Turn down the parts where only the singer sings* are now **off by default** (tick them in Build if a song needs them). Switching to the original at every gap, and dipping voice-only parts, made the level go up and down.
- Smart cleanup turns things down by at most 6 dB (was 12).
- **Clean instrumental** no longer switches, dips or mutes anything.
- Songs built before this keep the old result until you build again (or press **Start over**, then build).

**"Make sure every part sounds better" is optional now:** off in Build by default (it's slow), and a **Check no part got worse** button in the Song map runs it afterwards.

**Stop really stops.** A split, build or other job stops at its next step, and a stopped build puts the song back exactly as it was.

## 1.39.1

- **Faster builds.** "Making sure every part sounds better than before" (which takes a build's fixes back out of any part it made worse) now scores sections only; the bar-by-bar scores are worked out once, at the end, and the original song's measurements are reused instead of redone every time. Same check, a fraction of the time.

## 1.39.0

- **Smarter merging of hits, notes, loops and samples.** Every copy is scored by how far it strays from what the copies agree on (singing, noise or a different strike all show up). Copies clearly worse than the best are left out; when the cleanest copy is clearly the best, it's used on its own, untouched. Otherwise only the good copies are merged, the better ones counting more, and the **attack stays the cleanest copy's**, so drums keep their snap (only the body and decay are merged).
- Exported samples say what was done: "cleanest copy (the others were worse)" or "merged from 4 good copies".

## 1.38.0

**Fewer, clearer tools** (nothing you've done to a song is lost):
- **Drums:** one way to repair them: odd hits by their place in the bar, from the merged best hits, plus the Grid for your own edits. The separate "learn the drum pattern" step is gone from Build (the Grid's *Add missing hits* still does it).
- **Vinyl crackle:** one way: clean the recording (best before splitting).
- **Cleanup:** leftover singing, "s" sounds and smart cleanup are one Build step, *Clean up leftovers*.
- **The drag-across-the-timeline menu** shows Compare, Keep as it is and Mute up front; smoothing, clicks, rebuilding, other models, stereo sides and lowering frequencies are under *More tools*.
- **Levels:** stereo sides and lowered frequencies are under *More level tools*.
- **Export:** the main files up front; raw instrumental, report, tempo map, drum MIDI and declicking under *More export options*.

## 1.37.3

- **No more hard cuts to silence from the build.** Parts the app mutes are now turned down 15 dB with smooth fades instead of cut off, and only stretches of 2 seconds or more (short cuts did more harm than good). Mutes you make yourself still mute fully.
- *Turn down the parts where only the singer sings* stays careful; *Be aggressive there* is off by default again (it was cutting too much).
- **Clean instrumental** no longer mutes anything: it rebuilds and cleans, but leaves the muting steps out. Tick them in Build if a song needs them.
- Run the build again on songs built before this to get the gentler version.

## 1.37.2

- **Fix: Clean instrumental (and Build) could lose instruments.** Rebuilding notes and cleaning "what isn't at the note being played" assumed one note at a time, which is only true of the bass. On tracks that hold several instruments (the "other" track of a 4-track split, guitar, piano) it turned the other instruments down. Both now only work on the bass. Run the build again on songs you built since 1.34.

## 1.37.1

- **Split again with different settings.** After a song is split, the split type list stays in step 2 (showing what it was split with), so *Split again* can use another model or other split options.

## 1.37.0

- **✨ Clean instrumental** (above the tracks): one click for the cleanest copy of the song the app can make from its own best material. The drums are played again from merged clean samples at every hit (odd hits put right by their place in the bar), damaged notes are rebuilt as notes, loops replayed from clean copies, leftover noise cleaned up, and the original recording used wherever nobody sings. Then Export. Undo takes the build back (the drum rebuild has its own Undo step, or use the Grid's *Undo rebuild here*).

## 1.36.0

- **"What to do next" is one clear step**, chosen by where the song is: *Split + build* → *Find damage* → *Build the best instrumental* → *Listen to the part the app is least sure about* and *Export*. At most two extras show, and only when they apply (like adding a clean stem for the instrument that lost the most).
- The twenty-odd individual suggestions it used to list (fill in, clear the mud, mute, notch, remove leftover singing, double-check, clicks again...) are gone: Build the best instrumental does all of them, and checks every fix itself.
- **Faster window:** those checks were worked out on every refresh; refreshes should now be noticeably quicker.

## 1.35.1

- **Fix: songs (and their samples folder) could be named "source".** Choosing the app's own copy of a song (the *source* file inside a project folder) now names it after the song; Extract samples also renames such songs.
- **Samples only use the original recording where there's truly no singing** (the vocal track, backing vocals and reverb tails 45 dB down); otherwise they come from the cleaned instrumental.

## 1.35.0

**Smart cleanup** (a new Build step, on by default): the app learns what belongs on each track from the rest of the song and turns down the rest, gently (at most 12 dB, never a hard mute), mainly under the singing.
- **Drums:** each sound's normal attack and decay is learned from its clean hits; anything louder than that between and after hits (clicks, vinyl crackle, smeared consonants, separation chirps) comes down. Real hits off the grid (fills, ghost notes) stay.
- **Pitched parts (bass, keys...):** during each note, whatever isn't at that note's pitch and overtones (vocal bleed singing other notes, artifacts) comes down.
- **Loops:** what a sung copy has that the clean copies don't comes down.
- **Every track:** its steady noise (hiss, vinyl surface, hum), learned from the quietest moments, is reduced everywhere.
- Each track and section is one item in Review, to hear before and after.

**Simpler Review and Song map**
- Review has four filters: **Waiting**, **Used**, **Not used** (each with its reason) and **All**. Tiny fixes you'd hardly hear are hidden (tick *Show small fixes* to see them).
- **What to do next** shows the single most useful step; the other ideas are one click away.
- *Repairability* is now **Song map**: how clean each instrument is in each section, first. The lists of repeated parts, hits and bars are tucked under *advanced*.

## 1.34.0

- **Bass notes rebuilt as notes.** Bass (and other pitched) notes that the singing damaged are made again from clean copies of the *same note* elsewhere in the song, or a nearby note retuned a little when that note is never heard clean. Part of Build the best instrumental (on by default), and each section's notes are one item in Review.
- **Loop-based songs: play each loop again from its clean copies.** Where an instrument repeats a 1-4 bar loop, the copies under the singing are replaced by a merge of its clean copies, instead of being patched. A Build step for electronic and loop-based music (tick it under *Change what it does*).
- **Start over** (next to Undo): back to the song as it was split, with every fix undecided again and levels, kept parts, mutes, cuts, clean tracks and grid edits cleared. Undo brings it all back.

## 1.33.0

**Drums**
- **"Same sound" slider** in the Grid: loose merges more hits into one sound (fewer rows), strict keeps more apart. Your edits follow their sounds into the new grouping.
- **Odd hits are marked** (dashed outline): hits that don't sound much like the rest of their sound.
- **Repair odd hits by their place in the bar:** where a sound plays at the same spot in almost every bar (90%+) and the hit there sounds off, or got grouped as something else, it's rebuilt from the merge of that sound's best hits. Fills before a new section are left alone. Buttons in the Grid (here / whole song), or per sound from its name.
- **The Grid is much quicker:** clicks update it straight away; the tracks and audio catch up when you pause (they used to reload with every click).

**Smarter swaps**
- **Chords must match:** a bar or part only replaces another when the chords around them are the same, not just the sound.
- **Song form first:** Chorus 2 is fixed from Chorus 1 (and bar 5 from bar 5) before anything else in the song.

**Samples**
- **Whole notes** for bass, keys and other pitched parts: each note to where it really ends (pads and long notes come out complete), cleanest copy and merged.
- **Phrases:** the same few notes in the same rhythm, repeated (even shorter than a bar), saved as samples.
- **Extract samples** has a drop box: drop a song straight onto it.

## 1.32.0

- **Fewer wrong swaps.** Parts and bars are only swapped in when they're near-identical (rhythm and notes) and line up with the original closely; alike-but-different parts (a verse and a pre-chorus, two guitar lines) no longer replace each other.
- **Simple build by default:** the best split, the original recording wherever nobody sings, your clean tracks, careful repairs and cleanup. Swapping repeated parts and bars, drum patterns, notches, tone matching and the rest are now extras: tick them under *Change what it does*, or choose **Everything**.
- **Cleaner samples.** Repeating samples only come from copies with no singing over them; loops and samples of the whole instrumental come straight from the original recording where nobody sings (no separation artifacts at all).
- **New split: "Cleanest: three models must agree".** Three models split the song and each instrument keeps only what they agree on, so a model's own artifacts and the singing one lets through drop out. The default once the graphics card works (slow on the processor).
- Extract samples (from 1.31.0) is back on the main line after a history cleanup.

## 1.31.0

- **Extract samples from the start screen.** A new **Extract samples** button: choose a song file (or one of your songs), whether you want the single cleanest copies, merged copies, or both, and how to split it. The app splits it if needed, finds the drum hits, section loops and repeating parts, saves them as WAV files and opens the folder.

## 1.30.9

- **Fix: the Grid window opened empty.** Its drawing code clashed with another part of the app, so the grid of hits never appeared.
- Step 4 no longer has its own clean stems button squeezed over its title; use **+ Clean stems / samples** above the tracks (or drag files onto the window).

## 1.30.8

- **Less noise under the instruments.**
  - *Bring back details from the vocal track* is now **off by default** and much stricter: it ignores the hiss the model leaves (anything near the noise floor) and only brings back parts at least 25 dB loud. It could add a wash of noise on some songs.
  - *Matching the tone under the vocals* may still cut freely, but now lifts at most +2 dB (it was up to +6): lifting there also raised leftover separation noise.
  - To apply this to a song you've already built, run **Build the best instrumental** again (it starts fresh).

## 1.30.7

- **You can always see when the app is working:** a small spinner next to the model light, with what it's doing (a job's progress message, "Working" for anything taking more than a moment, or "Still analysing the song" while background measurements finish).

## 1.30.6

- **Fix: the split type list could be empty** (and other parts of the start screen not fill in). Since 1.27.2, the first request when the app opened left a few bytes unread, which garbled the next one.

## 1.30.5

- **Fix: helpers still tried the graphics card when it can't run** (so the drum split could still fail). On Windows, the way 1.30.4 hid the card from them didn't take effect; now they're told to use the processor directly.

## 1.30.4

- **Fix: splitting the drums (kick, snare, toms, cymbals) crashed** when the graphics card couldn't run it. Helpers now only use the card when it really works, and anything that fails on the card runs again on the processor.
- **Fix: adding a clean track a second time failed** ("Invalid argument" when saving): the old copy was still held open. It's let go first, and the new one is written safely.
- **Fix: Use my graphics card on RTX 50 cards.** A PyTorch that sees the card but can't run on it was taken as working, so the right version was never installed. It now counts as broken and gets replaced (and newer drivers' "CUDA UMD Version" is read correctly). Click **Use my graphics card** once more.
- Fix: a Python 3.13 pre-release bug could stop the model check (and skip bringing back details); it's now harmless.

## 1.30.3

- Wording fix in the message shown when a song you open is already in Songs.

## 1.30.2

- **No more duplicate songs.** Opening or dropping a file that's already in Songs (same contents, whatever its name) opens that song with all your work, instead of making another copy.
- **Remove duplicate songs** (Troubleshooting and reset): cleans up copies made before this update, keeping the one you worked on last.
- Uploaded files are removed once they've been brought into the song (they were piling up in the uploads folder).

## 1.30.1

- **Drag and drop clean stems, loops and samples.** With a split song open, drop files anywhere on the window: the clean stems window opens with them listed and works out what each one is. Drop more onto that window to add them. Hold Shift while dropping to open a new song instead.
- **Easier to find:** a **+ Clean stems / samples** button sits in the timeline toolbar, and step 4's button now shows as soon as the song is split (it used to wait until after Find damage).

## 1.30.0

**Grid editor**
- **Taking a hit out removes only that sound.** Its clean sample is subtracted frequency by frequency, so a cymbal ringing underneath stays (it used to silence the track until the next hit).
- **Louder or softer:** drag a hit up or down (shown as brightness and a %).
- **Copy bars / Paste here:** put a clean bar's hits over a damaged one (short copies repeat to fill); the bars are made again from clean samples.
- **Tighten timing** (50, 75 or 100%) pulls hits toward the grid without making them robotic; the tick under each hit shows how far off it was.
- **Choose what each row plays:** click its name for a merge of its cleanest hits, one particular hit, or your own sample.
- **Loop** the bars you're editing while it plays.
- **Real notes for bass and keys:** pitched tracks get a row per note (C3, D#2...), high to low, like a piano roll.
- **Export MIDI / Import MIDI:** the grid's hits as a MIDI file on the song's tempo, or a MIDI file played with this track's own clean sounds (drums by General MIDI note).

**Clean stem lanes**
- **Automatic timing:** stems that run slightly fast or slow (a different master) are stretched to match when added; **Auto** next to Timing finds the best nudge.
- **Tone match** (on by default): your stem's EQ is matched to the song per section, so the switches can't be heard. Untick *Tone* to use it as it is.
- **Orange marks** where your stem and the split play different things (different notes, a missing part): handy to spot an alternate take or a mislabelled stem.

**Everywhere**
- **Undo** covers lane and grid edits too.
- **Compare** any part: drag across the Sections bar, *Compare: original, split, result* plays it three times in a row.
- **Hold B** to hear the split as it came at the same spot; let go to come back.
- **Clean score per bar** as a thin strip along the top of each lane (red needs work, amber so-so, green clean), after a build or a score check.

## 1.29.0

- **Your clean stems get their own lanes**, right under the instrument they feed (marked ↳), with their own picture of the sound. Green shows where your stem is used and how much.
  - **Drag across the lane** to use it there; **Alt + drag** (or right-drag) to leave it out there.
  - **Click a section** to choose for the whole part: *where the singing damaged it*, *your stem*, *mix* (half and half) or *the split*.
  - **Level** and **Timing** (nudge by milliseconds if the automatic line-up is slightly off), **M** to leave it out everywhere, **S** to hear only your stem, **×** to stop using it.
  - The instrument's own lane always plays the combined result; every change updates it straight away.
- **Grid editor for drums (and any instrument).** The **Grid** button on each lane opens a step-sequencer view on the song's own beat: one row per sound (kick, snare, hats... or repeated notes), one column per 16th, the song's bars and beats marked, a tick for each hit's exact timing.
  - **Click an empty cell** to add that sound there (made from a merge of its cleanest hits); **click a hit** to take it out, again to bring it back; right-click an added hit to remove it. Snap to 1/16 or place freely.
  - **Rebuild these bars from clean samples:** the bars are played again from clean samples at every hit shown, which clears smeared, muddy or bleeding drums.
  - **Add missing hits (pattern)** fills in what the learned drum pattern is sure belongs there.
  - **Play these bars**, move along with ◀ ▶, show 1 to 8 bars at a time. Edits are saved with the song and show up in the lanes and the export.

## 1.28.1

- **Clean stems as part of the build.** The Build window has **Add clean stems first…**: choose your stems or samples, and the app adds them and goes straight on to building with them (they're used before anything else, and every other fix is checked against them). The clean stems window also has *Then build the best instrumental with them*.
- **Aggressive a cappella silencing is now on by default**, to clear leftover background vocals in the parts where only the singer sings. Untick *Be aggressive there* if a soft instrument under the voice gets cut.

## 1.28.0

- **Build the best instrumental, reworked to give a finished result.**
  - **Uses the original recording between the vocal lines**, not just in whole parts without singing: every gap of a second or more where the singer is silent (strict: 45 dB under, with margins for breaths and reverb, and backing vocals count as singing) plays the original, with crossfades. The report says how much of the song is the original.
  - **Settles every option.** Where options overlap or contradict, one is chosen and the rest are put aside with the reason (*another fix was used here*, *the original recording is used here*, *doesn't line up with the original*, *not a close enough match*). They're under Rejected if you want to try one; Review shows the finished result instead of a pile of alternatives.
  - **Never worse:** the clean score of every part is measured before and after; where the build made a part score lower, its fixes there are taken back out.
  - **Building again starts fresh:** what the last build chose and set (fixes, kept parts, mutes, notches, tone, seams) is taken back first, so builds no longer stack on top of each other. Your own decisions stay.
  - **Clear summary:** the clean score before and after, how much of the song is the original recording, then what was done. **Export…** right from the result.
  - The step list is tucked under *Change what it does*: the defaults are the recommended build.

## 1.27.2

- **Tighter security.** The engine listens only on this computer (127.0.0.1), but other programs and other user accounts on the same computer can reach that too. Now nothing can read from it or control it without this session's secret:
  - the secret is made fresh at every start, handed to the engine privately and to the app window in the address fragment (never sent over the network, never written into a page or onto a command line), and removed from the address right away;
  - every request now needs it, including reading data (song lists, audio, spectrograms), not just changes; the audio player uses a strict same-site, script-proof cookie;
  - links to other sites always open in your normal browser, never inside the app window (where they could reach the app's controls);
  - uploads are capped at 4 GB, and idle connections are closed after 2 minutes.
- Unchanged and still in place: other sites are refused, requests must name 127.0.0.1 or localhost (stops DNS-rebinding tricks), the app can't be framed or embedded, *Show folder* only opens the app's own folders and never runs files, and updates are checked against their published checksum before installing.

## 1.27.1

- **Scroll sideways along the song.** When zoomed in, a scrollbar under the tracks moves along the song. Also: the mouse wheel over the Sections bar, Shift + wheel anywhere on the tracks, a sideways trackpad swipe, or drag with the middle mouse button (or hold Space and drag).

## 1.27.0

- **Fast model without a graphics card.** On the processor, each start begins on *Quick draft* (about 4 times faster than Best quality); pick another any time.
- **Time estimates before splitting** ("about 3 min on your processor"), learned from this computer's own splits.
- **Split time and device after splitting:** step 2 says what the split ran on and how long it took.
- **Your clean tracks** list (Levels tab): each clean stem you added, its match and where it's used, with **Remove** (Undo brings its parts back).
- **A cappella silencing joins short pauses** where the singer breathes and nothing else plays, instead of leaving gaps.
- **Note when Python is a test version** on the start screen, with the download link.
- **Match streaming loudness (-14 LUFS)** in Export: measured like Spotify and YouTube do, with a gentle shared limiter (at most 6 dB) so every file stays in step and peaks stay under -1 dB.

## 1.26.0

- **Work it out for me** (the new default in *Add clean stems or a sample…*): drop in any clean files and the app decides what each one is:
  - a **full track** is matched to the instrument it lines up with best (the instrument list now starts on *Auto-detect*) and lined up with the song;
  - a **loop or sample** (half a bar to 8 bars) is found everywhere it plays, on the instrument it sounds like, stretched to each spot's exact length, lined up and level-matched, and used there (only where the singing damaged it, or everywhere);
  - a **one-shot** rebuilds the matching hits, as before.
- **Silence the parts where only the singer sings.** A new Build step finds a cappella moments (the instruments are far under the voice and the voice is nearly all of the original) and silences every instrument track there, since all they hold is leftover singing. On by default, careful. **Be aggressive there** (off by default) also catches shorter, less certain moments. They're listed under Muted parts, where each can be removed.

## 1.25.0

- **Use your clean stems to guide the repairs.** *Add clean stems or a sample…* (step 4) now takes several files at once: the bass, the drums, anything you have. Each is matched to an instrument from its file name (change it if needed) and lined up with the song automatically. For each track choose where to use it: **where the singing damaged it** (the split stays everywhere else) or **the whole track**. Your tracks are kept with the song, and:
  - they're applied right away if you like (Undo takes them back), and come before every other fix on that instrument;
  - every other fix on that instrument is checked against your clean track instead of the full mix, which is much stricter, and fixes that add sound it doesn't have go to *Doesn't fit*;
  - Build the best instrumental has a new first step, *Use your clean tracks*, and the other steps skip the parts they cover.
- **Fix: Use my graphics card on newer cards (RTX 50 series).** 1.23.4 installed a PyTorch build that's too old for the newest cards ("no kernel image is available"). The app now picks the build by card generation, tests it on the card, and if the card rejects it, removes it and tries the next build automatically. The device check also tests the card for real, so it never says the card is used when it isn't.

## 1.24.0

- **Samples are whole loops now, not tiny bits.** Export samples finds what repeats at the length it really repeats at: 1, 2 or 4 bars, always starting on the bar, from the whole instrumental and from each instrument. Rhythm and harmony both have to match (so a chord change makes it a 2- or 4-bar loop), and a loop made of two identical halves is reported at the shorter length. Each comes as the cleanest copy and as a merge of the cleanest copies, named with its length, how often it repeats, the tempo, and where it first plays.
- Pitched parts are no longer exported as split-second "repeated note" clips; drum one-shots stay (only the prominent ones).
- **Blend for repeated bars.** Damaged repeats of a bar can now be rebuilt from a merge of the cleanest repeats (per-frequency median, so a trace of singing or a click that's in only one copy drops out): a Blend button on each repeated bar, **Blend all** in its header, and Build the best instrumental uses the blend whenever there are 3 or more clean repeats.

## 1.23.4

- **Use my graphics card works when Windows sees an NVIDIA card but NVIDIA's own check tool doesn't answer.** The app relied only on that tool (nvidia-smi) to spot an NVIDIA card; now it also asks Windows which cards are installed. If there's an NVIDIA one, it installs the graphics-card version of PyTorch and then tests the card with a real calculation, so it only says it's done when splitting really can use the card.
- **Older NVIDIA cards and drivers are supported:** the app installs the CUDA 12.6 build, which runs on GTX 900 and 10 series cards and on any driver with CUDA 12, instead of the newest builds that need recent ones.
- When the NVIDIA tool doesn't answer, the log says why.

## 1.23.3

- **Use my graphics card now says what card you have.** The app lists the graphics cards in the computer (in Settings and in the log). If it's an NVIDIA card whose driver isn't answering, it says to reinstall the driver. If it's AMD or Intel, it says plainly that the separation model can't use it on Windows, instead of a vague message.
- **Finds NVIDIA drivers in more places** (newer drivers sometimes keep their tools only in the Windows driver store).
- The log now records which app folder is running, and what the graphics card button found.

## 1.23.2

- **Fix: Use my graphics card stopped right away ("Stopped.").** Pressing Stop at any earlier point left a flag set, and the next model download obeyed it as soon as it started. Downloads now start fresh. If the switch was stopped partway, the model shows as not installed; click **Use my graphics card** (or Download) again and it finishes.
- **Running an old backup copy?** If the app is started from inside its *.backup* folder (a copy an update keeps in case it has to go back), the start screen now says so, and **Fix it** copies the current version into the real app folder. Then start the app from there and point any shortcut at it.

## 1.23.1

- **Fix: Use my graphics card didn't switch anything.** When the processor-only PyTorch was already installed, the installer called it "already satisfied" and kept it. It now removes it first and installs the version for your NVIDIA card, then checks the card really works (and says what to do if it doesn't, like updating the driver or restarting).
- **The GPU button is easier to find:** it's also on the start screen, under the separation model, whenever an NVIDIA card is found but not used.
- **Use graphics card (Windows).bat** in the app folder does the same without opening the app; the setup check also offers it.
- **Each split says what it runs on** (graphics card or processor) in its progress message and in the log, and the log notes if the card failed and the processor took over.

## 1.23.0

- **Fix: Repaired didn't play after a split** (since 1.21). Rendering crashed whenever a frequency cut was in place. Every release is now tested end to end on the demo song before it's published, so a break like this can't ship again.
- **Faster:** lowering frequencies (follow and pitch modes) is about 8 times faster, the quality scores and leftover map about 10 times, export about twice as fast.
- **Bring back lost details.** Synths, effects and small parts the split filed as "vocals" are found with one quick model run on the vocal track (once per song) and brought back where they don't move like the singer. A new Build step, on by default.
- **Add a clean track or sample** (step 4). An official stem or session track of one instrument is lined up with the song and offered section by section; a clean hit or note is matched to the sound it is in the song and used to rebuild that sound's damaged and missing hits.
- **Better samples.** Musical 4- and 2-bar loops of each section kind (from the finished instrumental and each instrument, named with section and tempo), only the hits and notes that are a real part of the song, and only riffs that repeat.
- **Fewer vocal-remover passes.** The second vocal-removal pass is now off by default in Build; the focus is on rebuilding from what the split gave.
- **Use my graphics card** in Settings: installs the NVIDIA (CUDA) version of the separation model when you have an NVIDIA card. The setup check warns when one isn't being used.
- **Logs next to the app:** a *logs* folder beside the start files, with *app.log* and *errors.log* (only the problems).

## 1.22.6

- **No Roformer unless you ask for it.** The second vocal-removal pass (and Build the best instrumental, which runs it) now uses the faster standard model; Roformer was used automatically whenever it was installed, which is much slower. Turn it back on in Settings with *Use Roformer for extra passes*. The suggestion to try a different model also stops recommending Roformer splits unless that's on, and the Roformer split options are marked *slow*. Roformer splits you pick yourself work as before.

## 1.22.5

- **Fix: vinyl crackle removal could leave a quiet track full of clicks.** On dense crackle, a few repairs could blow up into loud spikes, heard as clicks, and anything that sets levels by the loudest peak then turned the rest down. Repairs are now never louder than the music around them (a bad one is skipped) and never rebuild more than a few milliseconds at once. If you used it before this update: open **Vinyl crackle…** and choose **Undo crackle removal**, then run it again.

## 1.22.4

- **Tidier steps bar.** The vinyl controls in step 1 are now one small **Vinyl crackle…** button that opens a small window with the strength and the actions (step 1 still says when crackle was removed). *Split and build* is now a smaller **Split + build** next to Split.

## 1.22.3

- **Remove vinyl crackle after splitting.** Once a song is split, step 1 has **Remove from the instruments**: it cleans the clicks and crackle out of each instrument track directly, without splitting again, so repairs and decisions stay. The fixes go to Review under Clicks and smoothing to hear first, and Build the best instrumental applies them with the other click fixes. (*Remove and split again* still cleans the recording itself and re-splits, which is the most thorough.)

## 1.22.2

- **Fix: Remove vinyl crackle wasn't visible.** Its controls didn't fit in step 1 and were hidden behind step 2. They now sit on their own line under *Import*.

## 1.22.1

- **Support the app.** Instrumental Studio is free and open source; if it's useful to you, you can now support it on GitHub Sponsors from the start screen or Settings (it opens in your browser). The repository page has a Sponsor button too.

## 1.22.0

Cleaner instrumentals:
- **Reverb tails.** Leftover singing often rings on as reverb for up to a second after each line. The cuts (Lower frequencies here, notches) and the second vocal-removal pass now follow it; the notches hold the last note sung while it fades.
- **Centre only.** The lead vocal sits in the middle of the mix, most instruments are spread out. Cuts can now act on the middle only, leaving each instrument's stereo sides untouched (on by default for new cuts).
- **Where leftover singing remains.** Pink marks along the top of each instrument's lane show where it still moves like the singer, as it sounds now (Settings to hide them).
- **Bass pattern.** Learn the drum pattern now learns the bass too, and restores bass notes the vocals took, under the same strict rules.
- **Weakest bars.** Score how clean each section is now also lists the weakest single bars, each with Listen, Rebuild and Notch here.

Build the best instrumental includes all of it:
- **Notching is now targeted and on by default:** after everything else, it notches the singer's notes (centre only, with reverb tails) just in the stretches where the leftover map still shows singing, instead of the whole song.
- **Improve weak sections** also tries rebuilding the weakest single bars and keeps a rebuild only if it's clearly cleaner and still fits the original.
- The pattern step covers drums and bass.

For music software (Export):
- **Tempo map and section markers:** a MIDI tempo map that follows the song beat by beat (so it lines up even where the tempo drifts), the sections as markers, Audacity section labels, and the tempo in a text file.
- **Drum pattern as MIDI:** every drum hit as General MIDI drum notes on the song's own tempo map, with loudness as velocity, including the hits the drum pattern restores under the singing.

## 1.21.0

Two stronger ways to remove leftover singing:

- **Second vocal-removal pass on the instruments.** Runs the vocal model (Roformer when installed, otherwise Demucs) again on each instrument track (not the drums) and offers to take out what it finds, but only in bars where someone is singing and what it found moves like the actual singing, so a melody line the model mistakes for a voice is left alone. Each part lands in Review under Vocal leftovers. From the suggestions (*Run it*), and as a step in Build the best instrumental (on by default; it takes a few minutes).
- **Notch the singer's notes.** The app tracks the pitch of the singing moment by moment (checked here: within about 1.5% on low, high and vibrato voices) and makes narrow cuts on exactly those notes and their overtones in the instruments, only while singing, nothing in between. A new mode in *Lower frequencies here* (now the default there), a one-click suggestion for every instrument at -9 dB, and a Build step (off by default, since it can thin an instrument that plays the same notes as the singer).

## 1.20.0

- **Lower frequencies here.** Drag across part of the song and choose an instrument (or all), what to lower (leftover singing 300 Hz-3.5 kHz, "s" and breath sounds 5-10 kHz, harshness 2-5 kHz, mud 150-500 Hz, boom 60-150 Hz, or a custom range), how (**Follow the singer** lowers only the frequencies the vocal is using at each moment, which takes out a ghost vocal and leaves the rest of the instrument alone; **Fixed cut** lowers the whole band), and by how much (-3 to -18 dB). By default it only acts while someone sings. Each one is listed in the Levels tab with Remove, and Undo works.

## 1.19.0

- **Learn the drum pattern** (Repairability tab, and a step in Build the best instrumental). The app learns where each drum sound (kick, snare, hi-hat...) plays in the bar, on a 16th-note grid, from the bars where nobody sings, separately for each kind of section (verses, choruses...). Under the singing, it restores a hit only when it's confident it's the right drum in that place: the pattern has that drum in that slot at least 90% of the time, the **original recording** has an attack there with that drum's sound (the vocals can take a drum out of the split, but it's still in the original), and the drum track is clearly missing it. The hit is rebuilt from a blend of that drum's cleanest hits, lined up to the attack in the original, and only what the vocals took is filled in. Each part lands in Review, checked against the original like every other fix.

## 1.18.0

- **Remove vinyl crackle** (step 1, Import). For vinyl rips: finds the clicks and crackle and rebuilds each from the sound on both sides, so tones carry straight through. **Light** takes only obvious clicks, **Normal** clicks and crackle, **Strong** fainter crackle too. Drum hits, consonants and other real sharp sounds are left alone. Best done before splitting, so the crackle never reaches the instruments; if the song is already split, it's split again with the same settings afterwards. The untouched recording is kept: **Undo crackle removal** brings it back, and choosing another strength always starts from the untouched recording.
- **Export: Remove clicks and crackle from the files**, for songs that were split before cleaning.

## 1.17.1

- The "find damage and build automatically" checkboxes are now buttons: **Split and build** next to Split, and **Import stems and build** under the start screen's cards. Split and Import stems still do just that.

## 1.17.0

- **Improve weak sections** (a new step in Build the best instrumental). Every section and instrument scoring under 75 tries the alternatives: each other model's split, a half-and-half blend with it, and other source parts for a best version. Each is scored against the original, and the one that's clearly cleaner (and still fits the original) is kept; the others are removed. The report says what changed and the clean score before and after.
- **Full tone match.** Each instrument under the singing is matched to the same parts where nobody sings, across the whole frequency range (40 bands), not just the two bands of the earlier fill-in and mud cut. Part of Build the best instrumental's tone step.
- **Smooth seams.** Wherever the original recording takes over from the instrumental (and back), the app measures how different the two sound right there and lengthens the crossfade where the switch would be heard. Also a Build step.
- **Import stems and build automatically.** On the start screen, tick *After importing stems, find the damage and build the best instrumental automatically*. Next to Split there's the same for songs: *Then find damage and build automatically*. You get the finished result to review; Undo takes the build back in one step.
- **Export samples.** In Export (and the Repairability tab): every repeated drum hit, note and loop, as its cleanest single copy, and/or merged from its cleanest copies (noise in any one copy drops out), as WAV files in a samples folder, by instrument. Drum tracks also get their cleanest whole bars.
- **Clearer model list.** Grouped into *4 tracks* and *6 tracks*, each named for what it's for: Best quality (recommended), Quick draft, Fewest gaps, Cleanest vocal removal. The same grouping is used in Try another model.

## 1.16.0

- **Build the best instrumental.** One button (top of the Review tab, and in the Repairability tab) that works through every kind of fix in the right order and applies the result as **one step you can undo**. It first shows its plan, with each step on a checkbox:
  1. Use the original recording where nobody sings
  2. Build each instrument from the cleanest model, bar by bar (if you have other models' splits)
  3. Use the cleanest take of parts that repeat (close matches only)
  4. Rebuild repeated riffs, where no best version covers them
  5. Rebuild drum hits and note starts from a blend of their cleanest copies
  6. Apply the repairs with a close match
  7. Remove leftover singing and "s" sounds; 8. fix clicks
  9. Mute bars that are only leftover singing or noise (measured on the result)
  10. Fill in what's still thin and clear the mud under the vocals (measured on the result)

  **Where fixes overlap, it picks one** instead of stacking them: on drums, blended hits replace ordinary repairs on the same hits (they used to be filled in twice, which sounds thick and muddy); repairs inside a part already replaced by a best version are skipped (they'd be thrown away anyway); fixes inside parts that use the original recording are skipped. **Every fix is checked against the original first**, and anything that adds sound the original doesn't have, or lands off its beat, is left out (or taken out, if it was applied). A short report says what it did.
- Replacing edits now always stack the same way where they overlap: another model's split, then a repeated bar, then a best version on top.
- Undo can now also remove fixes a step created.

## 1.15.2

From a log you sent (thank you):

- **No more long "[pywebview] Error while processing window.native..." lines at start.** The app gave the page a public link to the window, and pywebview tried to walk through every internal part of the Windows window when the page loaded. That link is private now, which also saves a little time at start.
- **"Slow request" times in the log are now real.** They used to include the time a connection sat idle waiting for the window's next request (which is how a request could seem to take 100 minutes while you were away).
- **Closing the window no longer logs "the engine ended (exit code 1)"** as if something went wrong; closing the window stops the engine on purpose.
- The setup check warns if Python is a pre-release (test) version, such as 3.13.0rc3.
- The log shows the pywebview version.

## 1.15.1

- **Fix: nothing appeared when starting on Windows (1.15.0).** The window waited for the engine before showing anything, and on Windows the engine didn't come up. Now the window opens straight away with a "Starting Instrumental Studio" page and the app loads into it once the engine answers.
- **If the engine can't start, the window says so** instead of staying blank: it shows what happened and the end of the log, with **Export log**, **Check and fix setup**, **Show the log file** and **Try again**. All of these work without the engine.
- The engine now keeps an eye on the window program directly (it used a pipe that behaves differently on Windows), and its output goes only to app.log.

## 1.15.0

- **The window can't freeze any more.** The app now runs as two programs: one that only draws the window (and opens file pickers), and an engine that does everything else (opening and decoding songs, splitting, repairs, saving). Before, both shared one Python, which can only do one thing at a time, so while a song opened, the window couldn't even redraw or take a click and Windows treated it as frozen. Now opening a song, or anything else the engine does, leaves the window responsive.
- The engine stops by itself whenever the window closes (or crashes), so nothing is left running in the background. Restart (Troubleshooting, or after an update) starts a fresh copy of both.
- If the engine can't start (something missing or broken), the setup check opens and the reason is in app.log. Lines in app.log now say which part wrote them (window or engine).

## 1.14.6

- **Clear stems.** Double-click **Clear stems (Windows).bat** or **(Mac).command** (Linux: `./clear_stems.sh`) in the app folder to free disk space. It lists how much each song's split uses, then lets you clear either other models' splits only (your songs stay exactly as they are) or songs' whole splits (each song stays in your Songs list with its original recording and goes back to step 1, ready to split again). It asks before deleting, and never touches original recordings or exports. Close the app first.
- **Show folder works with no song open** (it said "No song is open").
- Splitting no longer reads every saved song in full to look for an earlier split of the same file, and never reuses a song whose split was cleared.
- The setup check writes to the log quietly instead of mixing log lines into its report.

## 1.14.5

- **Check and fix setup.** Double-click **Check and fix setup (Windows).bat** or **(Mac).command** (Linux: `./check_setup.sh`) in the app folder. It checks Python, pip, the app's packages, ffmpeg, the Windows app-window component (WebView2), free disk space, the separation model and the extra models, and offers to install whatever is missing (asking first; the big model download is never started without a yes).
- **The app checks before it starts.** If something it needs is missing or broken, it opens the setup check in its own window instead of failing silently.

## 1.14.4

- **Export log.** On the start screen, in Troubleshooting and reset, and on the "taking too long" panel: saves one file (InstrumentalStudio/logs/InstrumentalStudio-log-....zip) with the app's log and details of this computer, to attach when asking for help. No audio or songs are included.
- **The log is always kept now,** however the app was started (it used to be written only without a console), and it records more: each startup step with timings, any request that took over a second, errors in the window, Python errors in any thread, and hard crashes.
- **Freezes are recorded.** If the app stays blocked for more than 3 seconds, the log gets a snapshot of exactly what every part of the app was doing at that moment.

## 1.14.3

The window no longer freezes when you click while the app is starting:

- **The Songs list no longer reads every saved song in full.** Each project file holds every repair and can be tens of MB; the first check at startup parsed them all, and the window couldn't respond until it finished. Songs now keep a tiny summary for the list (older songs are peeked at, never parsed).
- **No background loading while the window is up.** 1.14.2 loaded the analysis libraries a few seconds after start, which also stopped the window responding while it happened. They now load when you open a song, under its progress screen.
- **Open a song works on the first click.** The app's file picker connects a moment after the window appears; a click in that moment used to fall back to a slow browser upload. It now waits for the picker.
- The update check waits 15 seconds after start.

## 1.14.2

Faster start, and the window works while it starts:

- **Use the start screen right away.** "Starting Instrumental Studio" is now a small notice at the bottom instead of a cover over the whole window, and the start screen shows immediately; its lists fill in as soon as the app answers.
- **Only what's needed loads at startup.** The song engine and SciPy (large, and slow to load on Windows where antivirus checks every file) now load in the background a few seconds after the window opens, or the moment you open a song, instead of before the window can appear.
- **Playback is prepared when you press play,** not when a song opens and again after every edit. The app used to render and send the whole mix each time, even if you never listened. The play button pulses while the first play gets ready.

## 1.14.1

Startup and opening songs, no more "something in the background is taking too long":

- **Saving no longer freezes the app.** After every edit the whole project used to be written again (repairs, timeline and analysis, often hundreds of MB) while the song was locked, and several saves could pile up. Now only what changed is written, the song is only held while its state is copied, and quick runs of edits are saved together.
- **Status checks answer instantly.** Each one used to re-scan the disk to see whether the model was installed (slow on Windows, where antivirus inspects every folder read), several times a second at startup. The answer is now kept for 30 seconds.
- **Much less memory when finding drum hits and note starts.** It used to build a ~700 MB table per track at once; now it works in small pieces.
- **Songs open straight away.** The slowest suggestions (per-instrument measurements, repeated hits and notes) are worked out in the background and appear a moment later, with a small *Working out more suggestions* sign while that happens. Find damage now prepares them up front.
- **Something to look at while it loads:** a starting screen as soon as the window opens, and a progress screen while a song opens, instead of a window that looks frozen.
- The graphics-card check (which loads PyTorch) waits a few seconds so the window comes up first.
- The "taking too long" panel now needs three missed checks in a row, not two.

## 1.14.0

- **Double-check against the original.** Every fix is now checked against the original recording: everything it adds must really be in the song at that moment, and its hits must land on the original's beat. This catches the two problems that slipped through before: drums borrowed from a bar where the beat was found slightly off (they flam or land late), and the backing guitar standing in for the lead (or a different part or note). Fixes that don't fit lose their Recommended mark, are never applied by **Apply** or **Apply all** (they wait in Review), and are listed under the new **Doesn't fit** filter. A suggestion takes them all out in one click, and **Check now** re-checks everything, including the beat grid itself.
- **Clear the mud.** The suggestions now measure how much extra low-mid (150 to 500 Hz) each instrument has under the vocals next to the same part where nobody sings, and offer a cut of just that much, only there.
- **Best of several models, bar by bar.** Once you have another model's split (from **Try another model here**), one click scores every bar of every instrument in each split against the original (sound the original doesn't have, leftover singing, missing level) and builds each instrument from the cleanest one in each bar. One item per instrument lands in Review.
- **Sharper note starts.** Best hits now also covers guitar, piano, bass and other: notes and chords the song repeats get their smeared attacks rebuilt from clean copies (just the attack), with Apply, Blend and Add to review like drum hits.
- **Clean score per section.** **Score how clean each section is** (Repairability tab) gives every section a 0 to 100 score, before repairs and now.
- **Verified updates.** Each release now publishes a checksum, and the app refuses to install a download that doesn't match it. (This protects updates from 1.14 onwards.)
- **Update without opening the app.** Double-click **Update Instrumental Studio (Windows).bat**, **Update Instrumental Studio (Mac).command**, or run `./update.sh`.
- **Opening songs no longer looks stuck.** The suggestions list re-measured every instrument after every change; those measurements are now kept until that instrument changes, so big songs (6 tracks, split drums) open and respond much faster. A suggestion that fails to compute is skipped instead of stopping the song from opening, and the Songs list no longer re-reads every project on each refresh.

## 1.13.0

- **Blend the cleanest drum hits.** Each drum sound in the Repairability tab now has a **Blend** button next to **Apply**. Instead of filling in from the single cleanest hit, it uses the 5 cleanest together: each is lined up and level-matched, and only what they have in common is kept, so a cymbal wash or a bit of leftover singing in any one of them drops out. The attack stays as sharp as the cleanest hit's. **Blend all** (Best hits) and **Blend all hits** / **Apply all, blend hits** (at the top) do every sound at once. Blending a sound that was already applied the other way switches it over; Undo switches it back.
- **Smarter muting.** The suggestions now look bar by bar for stretches where an instrument is only the singer's ghost (much quieter than it plays on its own and rising and falling with the vocal, or flagged as leftover singing) or only separation noise, and offer to mute just those bars in one click.
- **Match the original's loudness** when exporting (optional): measured where nobody sings, so the instrumental sits at the same level as the song.
- **Exported instruments add up to the exported mix again.** Files near clipping used to be turned down one at a time, which changed the balance between them. Now every file gets the same adjustment.
- **Safer.** Other web pages open on your computer can no longer send commands to the app: every change needs a secret that only the app's own window has, requests from other sites are refused, and the app can't be shown inside another site. **Show folder** only opens folders inside the InstrumentalStudio folder and never runs a file.

## 1.12.0

- **Rebuild any part.** Drag across part of the song, pick an instrument (or all of them) and choose **Rebuild**: it's rebuilt from the bars where that instrument plays the same thing, even if nothing was flagged there. Every rebuilt bar goes to Review first.
- **Blend two models.** Under **Try another model here**, tick **Blend half and half with the current split** to mix the other model's take with the one you have. Different models leave different artifacts, so a blend often hides both.
- **Use the original's stereo sides.** Lead vocals sit in the middle of a mix, so the sides of the original song are mostly instruments. Drag across a part and choose **Use the original's stereo sides here** to bring back the width and detail the split lost. Listed in the Levels tab (full or half), and Undo takes it back.
- **Leftover "s" sounds.** Find damage (and Clean up this part) now listens for the singer's "s", "sh" and "t" sounds left in the instruments and offers to remove them, under **Vocal leftovers**.
- **Room around the singing.** "Use the original song here" now only takes over where nobody sings, staying 0.3 s clear before singing starts and 0.8 s after it stops, so breaths, pickup notes and reverb tails can't slip into the instrumental. Change both in Settings.

## 1.11.0

- **Apply all repeated parts at once.** The Repairability tab has **Apply all** (or **Add all to Review**) for every best version, drum hit and repeated bar together, plus **Apply all** for each list. Every row now has **Apply** next to **Add to review**, and neither one switches tabs any more. Undo takes a whole batch back in one step.
- **No more hangs at startup or while splitting.** Every background task (model downloads, splitting, installs) is watched: if it shows no progress for 15 minutes it's stopped with an explanation instead of hanging. A **Stop** button and an elapsed timer are on the progress screen.
- **Safe mode.** If a start doesn't finish, the next start skips the update check and the graphics-card check automatically, and says so.
- **Troubleshooting and reset** (Settings, the start screen, or the progress screen): stop what's running, restart the app, clear downloaded model files (fixes a download that got cut off), reinstall the separation model, reset app settings, or close the open song. None of them touch your projects.
- If the app stops answering, a panel offers to restart it.

## 1.10.0

- **Best hits**: finds each repeated drum sound (kick, snare, hi-hat and cymbals, toms), picks its cleanest hit, and fills in what the vocals took from every smeared one, lined up to the sample and matched to how hard it was played.
- **Repeated bars**: riffs and patterns played again and again; damaged repeats are replaced by the cleanest one, matched to the beat and level.
- **Clean up this part**: drag across a glitchy part and get every available fix for it in one go (clicks and gaps, smoothing, rebuilt drum hits and bars), all in Review to hear before applying.

## 1.9.0

- **Try another model on one part.** Drag across a part, choose **Try another model here**, and pick a model and a track (or all tracks). That model's version of the part goes to Review under **Other takes** to compare before applying. The split is kept with the project, so trying it elsewhere is instant. Suggestions offer this for the part that lost the most.
- **Best versions of repeating parts.** When an instrument plays the same part more than once, use its cleanest take every time, matched to the beat and level (Repairability tab).
- **Extra models** (optional downloads on the start screen): cleanest vocal removal with Roformer before Demucs splits the instruments, backing vocals on their own track, and drums split into kick, snare, toms and cymbals.

## 1.8.0

- **Clicks and gaps**: Find damage now also listens for short clicks, pops and dropouts in every instrument, ignoring drum hits, note starts and natural rests. Fixes are listed under **Clicks and smoothing** for you to review.
- **Smooth watery sound**: drag across part of the song and choose **Smooth watery sound here** to calm the warbling separation leaves under the vocals. Each fix is reviewed before it's applied.
- **Mute** everything or one instrument in any part of the song, from the timeline or with the new **M** buttons in the Levels table. Suggestions offer to mute an instrument that's mostly leftover singing in a section.
- **More tracks**: a new **6 tracks, best quality** split gives separate guitar and piano tracks with drums, bass and vocals from the best model. Split options now say how many tracks each gives.
- Hear before and after with all tracks. When reviewing a repair, choose **All tracks** (every instrument as it sounds right now, including your other repairs and levels), **All tracks + vocals**, or **Just** the one instrument. Before and after always differ only by the repair you're looking at. A/B looping uses the same choice, and it's remembered.

## 1.7.1

- No more black command windows popping up on Windows while the app splits songs, installs the model, reads MP3s or checks for updates.
- Start the app on Windows by double-clicking **Instrumental Studio.pyw**: no console window at all. The .bat file now starts it the same way.
- On a Mac, the Terminal window from the start file closes by itself.
- Messages that used to go to the console are saved in *app.log* in the InstrumentalStudio folder.

## 1.7.0

- **Keep parts as they are.** Drag across the Sections bar to select part of the song, then use the original recording there, or keep the split with no repairs. Kept parts are hatched on the timeline and listed in the Levels tab.
- **Suggestions for matching the rest of the song**, at the top of the Review tab: use the original recording where nobody sings, apply the recommended repairs, fill in an instrument that's still thinner under the vocals (measured against the same part elsewhere, lifting only the vocal range, only under the vocals), remove leftover singing, and more. Each is one click and can be undone.

## 1.6.0

- Repairs are never applied without asking. After **Find damage**, every repair waits for you. Strong matches are marked **Recommended**, and **Apply recommended** applies them all in one click.
- Changing a repair's strength or source no longer applies it either; only **Apply** does.
- Projects from earlier versions: repairs that were applied automatically are back to waiting for your decision.
- After you apply or reject a repair, the next one is selected, so you can work down the list.
- Drag a song onto the editor to start a new project with it. The project you were working on stays saved in **Songs**.

## 1.5.0

- Opening a song now just brings it into the editor, where you can play it right away. You decide when to split it and when to look for damage.
- New step bar across the top of the editor: **Import**, **Split into instruments**, **Find damage**, **Review and export**. Each step shows what it does and whether it's ready.
- Choose how to split (best quality, fast, guitar and piano, or both models) right in the Split step. **Split again** and **Find again** let you redo a step.
- Opening songs no longer needs the separation model at all; only the Split step does, and it offers the download if it's missing.
- The demo song walks through every step too.

## 1.4.0

Faster everywhere:

- Finding and repairing damage is about 3x faster (24 s down to 8 s for the demo song on a 2-core computer, and more on computers with more cores): repairs now run in parallel, use a faster FFT, and skip work that was being repeated for every bar.
- Re-opening a song you've already split reuses the earlier split instead of splitting it again.
- Without a graphics card, splitting uses several CPU cores at once, and new songs start on **Fast** separation (about 4x quicker than Best quality). Settings explains the speed of each option.

## 1.3.0

- New **Separation model** panel on the start screen with a status light: red when it isn't installed, amber while downloading, green when ready. It shows the download size and a **Download** button. The same light sits in the top-right corner of the window.
- Starting a project is clearer: **Split a song**, **Import stems** and **Try the demo song**, each marked with whether it needs the model.
- Opening a song no longer starts a download by itself. If the model isn't ready, the app points you to the panel instead.

## 1.2.0

- The separation model is no longer downloaded without asking. The first time you open a song, the app tells you how big the download is and what it will run on, and waits for your OK. You can also start it from the start screen or Settings.
- Drag a song into the window to open it. Drag in a full set of stems (including the vocals) to import them.

## 1.1.0

- The separation model now installs itself. The first time you start the app it downloads and sets it up in the background, choosing the right version for your graphics card (NVIDIA or Apple silicon) automatically. No more terminal commands.
- If the install fails, the start screen says why and offers **Try again**. Settings has **Reinstall separation model**.
- **Check for updates** is now on the start screen too.
- Splitting no longer depends on parts of PyTorch that changed in recent versions, so it works with current Python and PyTorch releases.

## 1.0.1

- After an update, the Mac and Linux start files can still be double-clicked.

## 1.0.0

The first version of Instrumental Studio.

- Turn any song into an instrumental: it's split into drums, bass, other instruments and vocals (or bring stems you already have).
- See exactly where removing the vocals damaged each instrument.
- Damaged spots are rebuilt from the same instrument playing the same part elsewhere in the song.
- Listen before and after, loop A/B, hear only what was added, or hear where it was borrowed from.
- Accept or reject each fix, change how strong it is, or choose a different place to borrow from.
- See how fixable every instrument is in every section of the song.
- Adjust the volume of each instrument, or of one instrument in one section.
- Find and remove vocal that leaked into the instruments.
- Undo, saved projects, and export to WAV, FLAC or MP3.
- Check for updates from inside the app, with optional automatic installs.
