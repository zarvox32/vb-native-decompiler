# vb-native-decompiler

vb-native-decompiler turns Visual Basic 5 and 6 programs compiled to native code into readable Visual
Basic source code. It then checks every procedure of that source against the original machine code.

It is also a library of apps that have been decompiled with it. For each one, `apps/<name>/project`
holds the settings and notes needed to do it again: which files you need, how to unpack them, what is
left out of the source, and how well the result was verified. The programs and their decompiled source
are not included. You supply your own copy of an app, and the tools produce its source.

Everything runs with Python 3 on Windows. The programs themselves are never executed: packed programs
are unpacked in an emulator, and the machine code is checked in an emulator too.

This file is the short version. `documentation.md` has the full instructions: every command, how to
read the checker's results, how to investigate a mismatch, and tips.

## The library

* `apps/troopanum`: Troopanum, an audio game by Daniel Zingaro and Justin Daubenmire, in five
  versions: 1.5, 1.6, SP 1.6, Classic and 2.0. In all five, every procedure passes the checker, apart
  from the few left out of the source on purpose, which are not compared.

Everything for one app is in its folder, `apps/<name>`:

* `project`: the settings (`project.json`), the notes (`README.md`) and any helpers the settings name.
* `utils` (some apps only): specialised tools that change the data of the compiled app, for example
  Troopanum 2.0's sound mod builder. The app's notes describe them.
* `compiled`: your copy of the programs.
* `work`: unpacked images and files extracted from archives.
* `source`: the decompiled source, one folder per program.

Only `project` and `utils` are in git.

## Setup

Run every command from the repository's top folder.

```
python -m pip install --target lib -r requirements.txt
python -m pip install --target emu speakeasy-emulator
python -I patch_speakeasy.py
```

`lib` holds pefile, capstone and comtypes. `emu` holds the Speakeasy Windows emulator and Unicorn, used
to unpack MoleBox and by the checker; `patch_speakeasy.py` applies the small fixes they need. Both
folders are installed packages and can be recreated at any time.

## Decompiling an app from the library

1. Read the app's notes, `apps/<name>/project/README.md`. They list the files you need, with checksums
   to confirm you have the same build.
2. Copy the programs into `apps/<name>/compiled` as the notes describe, for example
   `apps/troopanum/compiled/Troopanum 1.6/troop.exe`.
3. If a program is packed, unpack it with the commands in the notes.
4. Decompile. The source goes to `apps/<name>/source/<program>`:

   ```
   python -I build_sourcecode.py --project troopanum
   python -I build_sourcecode.py --project troopanum --code-only --game "Troopanum 1.6"
   ```

5. Check it against the machine code, if you want to see that for yourself:

   ```
   python -I vbcheck/check.py "Troopanum 1.6"
   ```

Each build compares what the app's omission rules left out of the code with the record in its project
file. If they differ, the published code is not replaced and the differences are listed;
`--record-omissions` accepts a deliberate change and stores it as the new record.

## Adding an app to the library

1. Put your copy of the programs in `apps/<name>/compiled`, and write `apps/<name>/project/project.json`:
   which programs there are, what to leave out of the source, and what the checker needs to know.
2. Decompile and check them, and fix what the checker finds.
3. Write the notes, `apps/<name>/project/README.md`, so that someone else with the same programs can do
   it again.

`documentation.md`, section 4 ("The library"), describes the project file and what the notes should
cover.

## Unpacking MoleBox

`molebox_extract.py` runs a MoleBox-packed program in an emulator until the real program is in memory,
and saves that as `image.bin`, which is what gets decompiled. It also writes the names of the files in
the program's `.bsc` archive (`--list`), or extracts them through the emulated packer (`--extract`;
slow, but works on compressed archives). `bsc_split.py` splits an archive that only stores WAV files,
XOR-encoded, in seconds. Each app's notes give the exact commands for its programs.

## Verifying the decompiled source (vbcheck)

For each procedure, the checker runs the decompiled VB source in an interpreter and the original machine
code in an emulator (Unicorn), from the same random starting states and against the same scripted
"world" (calls to other procedures, DirectX and VB objects, files, timers, DoEvents, which also changes
form variables at random). It compares everything the two sides do: each call and its arguments, the
form variables at every event, the final state, the return value and the run-time errors. A procedure
"passes" only if every run matches.

It also measures how much of the original machine code the runs reached ("blocks 40/45"). After the
first runs, it aims new runs at branches not taken yet, using the constants and strings the machine
code compares with, and the array elements and record fields the decompiled source compares with
constants (`If m_230(6).fC <> 0`). These hints only choose test inputs: a wrong one costs coverage,
never correctness. Code that was never reached is not verified; `--show` lists those blocks.

A run where the emulation itself cannot go on (a pointer that is not an object the checker created, an
unreadable Variant) counts as "machine-problem", never as a VB error. Otherwise it could pass for a
matching error the source raises for another reason.

## Decompiler notes

* VB executables keep no names for local variables, module or form variables, or private procedures.
  They are named after their storage: `var_XX` (stack), `m_XXX` (form object offset), `g_XXXX`
  (global address), `Proc_XXXXXX` (code address), `rXX` for values the compiler kept in a CPU
  register. Event handlers, controls, string literals and DirectX and VB members are real.
* `vbdec/main.py` ends with `unplaced statements N`: a self-check that every lifted statement made it
  into the structured output. Anything but 0 is a decompiler bug (the procedures are listed).
* An app's omission rules say what to leave out of the published source. Set `VBDEC_NOEXCLUDE=1` to
  get everything.
* The DECOMPILER_NOTES.txt written next to each decompiled program lists the compile options visible
  in the machine code (integer overflow, array bounds and floating-point error checks). Without
  overflow checks, Integer arithmetic wraps around; without bounds checks, out-of-range array indexes
  are not caught. Decompiled code is only equivalent to the original under the same options.
* Without overflow checks, the compiler sometimes computes Integer arithmetic in 32-bit registers (a
  `lea` chain for `n * 10000`, `neg eax`), so the result does not wrap. The decompiled source then
  writes `CLng(n) * 10000`: that is what the machine code computes. Where only the low 16 bits are
  kept (stored into an Integer), no CLng is needed, because the result is the same either way.

## What is what

* `apps/`: the library, one folder per app (see "The library" above). An app's `utils` folder, where
  there is one, has tools for changing its compiled data.
* `build_sourcecode.py`: decompiles every program of an app into its `source` folder.
* `vbdec/`: the decompiler. `main.py` is the command-line driver; `project.py` reads project files and
  compares and stores omission records.
* `vbcheck/`: the checker. `check.py` is the driver.
* `molebox_extract.py`, `bsc_split.py`, `patch_speakeasy.py`: unpacking. `unpack_emu.py` is a simpler
  unpacker that only dumps the memory image.
* `typelibs.json` and `tlbdump.py`: the type library dump used to name COM members.
  `python tlbdump.py typelibs.json` regenerates it with the type libraries of every app in the library.
  It needs comtypes, the 32-bit system DLLs, and Microsoft's VB6 type library, `VB6.OLB`, which is not
  included: `documentation.md`, section 11, says where to get it. Using the tools does not need it.
* `vbinfo.py`, `vbdis.py`, `disraw.py`: quick VB header and disassembly viewers.
* `lib/`, `emu/`: installed Python packages (not in git).
