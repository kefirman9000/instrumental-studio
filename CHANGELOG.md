# What's new

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
