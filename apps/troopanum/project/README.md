# Troopanum

Troopanum is an audio game by Daniel Zingaro and Justin Daubenmire. This app decompiles five versions
of it into Visual Basic 6 source. All five decompile completely, and every procedure that is published
passes the checker (results below).

Everything for Troopanum is in `apps/troopanum`:

* `project`: the part in git.
  * `project.json`: the settings, including the record of what is left out of each version.
  * `sounds.py`: the sound sorter.
  * `README.md`: these notes.
* `compiled`: your copy of the games.
* `work`: the unpacked images and the files extracted from the archives.
* `source`: the decompiled source.

The commands below are run from the repository's top folder, after the setup in the main `README.md`.


## The versions

The program names (used with `--game` and by the checker) are the folder names:

* **Troopanum 1.5** (2002). Sounds are loose WMA, WAV and MIDI files next to the program. The only
  version compiled with integer overflow and array bounds checks.
* **Troopanum 1.6** (2002).
* **Troopanum SP 1.6**: the Spanish edition of 1.6, with Spanish documentation and recordings. Its code
  differs from 1.6 (129 procedures against 123).
* **Classic Troopanum** (2011, blindsoftware.com). Its game code is byte-identical to Troopanum 1.6's;
  the re-release packed it with MoleBox and moved the sounds into the archive `cltr02.bsc`. The MIDI
  music is still loose.
* **Troopanum 2.0** (2008, BSC Games; its documentation says version 2.0.2). Packed with MoleBox, with
  the sounds in the archive `tr20.bsc`. Built without optimisation, unlike the others.

Only `troop.exe` is decompiled. The helper programs that come with the games (`config.exe`,
`register.exe`, and in 2.0 `joystick.exe`) are not.


## The files you need

Copy each game's whole installed folder into `apps/troopanum/compiled`, with exactly these folder names:

* `apps/troopanum/compiled/Troopanum 1.5`
* `apps/troopanum/compiled/Troopanum 1.6`
* `apps/troopanum/compiled/Troopanum SP 1.6`
* `apps/troopanum/compiled/Classic Troopanum`
* `apps/troopanum/compiled/Troopanum 2.0`

The build takes the program, the loose sound files, and the documents `troopanum.txt` and `stats.txt`
from there. You can work with only some of the versions: give `--game` to build or check just those.

The settings and results here are for the files below. To check yours, run for example
`certutil -hashfile "apps/troopanum/compiled/Troopanum 1.6/troop.exe" SHA256` and compare the result. A
different build of a version decompiles to different code, and its omission record will not match.

* Troopanum 1.5, `troop.exe`: 200,704 bytes, SHA-256
  2dfd5aa04919434068d5f6e9121a7659b63e3977ab7672bd7778a64f48b90042
* Troopanum 1.6, `troop.exe`: 196,608 bytes, SHA-256
  d32f14974e43b4b40f91590301572c0e0fe06fb6e7f57999e5c9ac11cc87108a
* Troopanum SP 1.6, `troop.exe`: 196,608 bytes, SHA-256
  f8b832678d42cc73898229ccb0ec2ed8aec4a951a296acb3e6516116fa24df5b
* Classic Troopanum, `troop.exe`: 188,416 bytes, SHA-256
  9dc8684c869043c0c6eb651634d3d1640fc0be4468a65ba03985872c39a4a403
* Classic Troopanum, `cltr02.bsc`: 28,296,255 bytes, SHA-256
  9d538f2aae167deb342632fd2ab76f8d82d670d88691eee107801a543542bf67
* Troopanum 2.0, `troop.exe`: 307,712 bytes, SHA-256
  ab2381d1f7fed5e73dd8d8d0f0c4264fdd7f9d949a95f10651f0ab45a769caf1
* Troopanum 2.0, `tr20.bsc`: 63,148,508 bytes, SHA-256
  0ec54be8b8c8bc8d70e7ef92d7276841014824595414a7973cf7baed06c265e3


## Unpacking Classic and 2.0

Both are packed with MoleBox, so they must be unpacked before they can be decompiled or checked. The
results go to `apps/troopanum/work`, where `project.json` expects them.

Classic Troopanum's archive is compressed, so every file is read out through the emulated packer
(`--extract`). This takes about an hour, and gives `image.bin` and 162 files in `files`:

```
python -I molebox_extract.py "apps/troopanum/compiled/Classic Troopanum/troop.exe" apps/troopanum/work/classic_box --box "apps/troopanum/compiled/Classic Troopanum/cltr02.bsc" --extract
```

Troopanum 2.0's archive only stores the WAV files one after another, XOR-encoded, so only the names are
listed (`--list`) and `bsc_split.py` cuts the files out in seconds. This gives `image.bin`, `names.txt`
and 269 files in `files`:

```
python -I molebox_extract.py "apps/troopanum/compiled/Troopanum 2.0/troop.exe" apps/troopanum/work/tr20_box --box "apps/troopanum/compiled/Troopanum 2.0/tr20.bsc" --list
python -I bsc_split.py "apps/troopanum/compiled/Troopanum 2.0/tr20.bsc" apps/troopanum/work/tr20_box/names.txt apps/troopanum/work/tr20_box/files
```

For comparison, these are the SHA-256 checksums of the unpacked images:

* `apps/troopanum/work/classic_box/image.bin`:
  f070ea1e0e29d591ffe14b32bbe77726f88fa00d341141ed81732060ce534592
* `apps/troopanum/work/tr20_box/image.bin`:
  db898433895ccc3e2650d871ce3c8426e7fbcbdbd8e69a960c6a6240fa0fcebd


## Decompiling

```
python -I build_sourcecode.py --project troopanum
```

This writes each version's source, documents and sorted sounds to `apps/troopanum/source/<version>`.
Add `--code-only` to leave the sounds alone, or `--game "Troopanum 1.6"` for one version. The code of
all five takes a few minutes.

For every version, the output should include `warnings 0 unplaced statements 0` and
`omissions: as recorded`. If the omissions differ from the record, the code for that version is not
written, and the differences are listed. That usually means your copy is a different build (check the
checksums above) or the tools have changed since the record was made.

Each version's folder gets a DECOMPILER_NOTES.txt with its compile options, what was left out, and how
the names in the source were made.


## What is left out, and why

The source was made for a port of the game. The port needs neither the copy protection nor the old
online score server, so three rules in `project.json` leave these parts out:

* **Licensing and registration**: the SerialCode licensing control (`sc`) and everything that uses it,
  the licence messages and the unlock code. The procedures of the `modSC` module are left as empty
  stubs. Treat the game as registered (the full version): a variable that only the omitted code sets is
  marked as such in its declaration, for example the "registered" flag.
* **Online score posting**: the Internet Transfer control (`inet`), the web addresses and form fields
  of the score server, `InternetGetConnectedState`, and the statements that play the sounds that only
  go with posting scores.
* **The config.dat check**: if `config.dat` (written by `config.exe`) is missing, the game plays "say
  config first" and quits. Only the online score posting reads what is in the file.

What that came to is recorded for each version:

* 1.5, 1.6, SP 1.6 and Classic: 5 procedures left out whole (3 licensing, 1 online score posting,
  1 config.dat), and single statements left out of `Form_Load` and two other procedures.
* 2.0: 6 procedures left out whole (3 licensing, 2 online score posting, 1 config.dat), and single
  statements left out of `Form_Load` and one other procedure.

The checker reports the procedures left out whole as "omitted" and does not compare them. The
statements cut from the other procedures are checked with the rest of those procedures.


## Compile options

* Troopanum 1.5: integer overflow checks and array bounds checks are on.
* All other versions: both are off. Integer arithmetic wraps around silently, and array indexes are
  not checked.
* No version has floating-point error checks: dividing by zero gives infinity instead of an error.

The decompiled code behaves like the original only when compiled with the same options.

Troopanum 2.0 is an unoptimised build. Its code keeps statement numbers (at `[ebp-4]`), which is what
lets the checker model On Error Resume Next for it, and it does its cleanup inline (`push after; ...;
ret`).


## Check results

Check a version with, for example:

```
python -I vbcheck/check.py "Troopanum 1.6"
```

Results of the full check at the default 30 runs. Every procedure that is compared passes:

* Troopanum 2.0: 211 pass, 6 omitted, 91% of machine-code blocks reached.
* Troopanum SP 1.6: 124 pass, 5 omitted, 89%.
* Classic Troopanum: 118 pass, 5 omitted, 88%.
* Troopanum 1.6: 118 pass, 5 omitted, 86%.
* Troopanum 1.5: 116 pass, 5 omitted, 81%.

The machine code not reached (9 to 19 percent, depending on the version) is not verified. It is mostly
rare branches inside large procedures; `--show` lists it. The full check of all five takes about 35
minutes on mains power (2.0 alone about 11). On battery, everything takes about 2.5 times as long.

The checker's scripted world uses the settings in `project.json`: the game folder is `C:\TROOP`, it
starts with a high score table, `options.dat`, `stats.txt` and `speaker setting.dat`, and the test
inputs include strings the game works with ("game", "menu", "play sound", "normal", "speed",
"bonus1").


## Sounds

The build copies each version's sounds into `apps/troopanum/source/<version>/sounds`: the loose files
next to `troop.exe`, plus the files extracted from the archive for Classic and 2.0. 2.0's archive stores
the names in capitals, while the code asks for lower case, so the copies get lower-case names.
`troop2_review.mp3`, a review recording in the 2.0 folder, is not copied.

`sounds.py` then sorts them into folders by use, worked out by reading where the decompiled code loads
and plays each file:

* All versions: ambiance, announcement, enemy, game, item, learn, menu, music, number, scores online,
  test speakers, warning, and unused (files the game never plays).
* 2.0 also has bonus round, gorb, lord vector and moon rock.

Each folder except unused gets a `_description.txt`; the underscore keeps it at the top of the list.
Classic, 1.5, 1.6 and SP 1.6 share one set of folders and descriptions; 2.0 has its own. The 1.x ship
names in the descriptions (varian, zeneeth, tarster, star gazer) come from the Classic Troopanum
manual.

After editing the categories in `sounds.py`, re-sort the existing folders without rebuilding:

```
python -I apps/troopanum/project/sounds.py
```

Give a folder to re-sort somewhere else, for example a copy made with `build_sourcecode.py --out`.


## Type libraries

The games use DirectX 8 for Visual Basic (`dx8vb.dll`), the Internet Transfer control (`msinet.ocx`)
and the SerialCode licensing control (`SerialCode.ocx`). All three are already in `typelibs.json`, so
you only need them installed to regenerate that file.
