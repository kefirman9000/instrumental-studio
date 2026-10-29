# What's new

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
