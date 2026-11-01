# vb-native-decompiler: documentation

vb-native-decompiler turns Visual Basic 5 and 6 programs compiled to native code into readable Visual
Basic source code, and checks that source against the original machine code. It is also a library of
apps that have been decompiled with it: their settings and notes are in `apps`, so anyone with a copy of
one of those apps can decompile it again. The programs and their decompiled source are not part of the
repository.

This document covers:

* how to run each tool;
* how the pieces fit together;
* how the library is organised, and how to add an app to it;
* how to check and debug the decompiled code;
* tips and pitfalls.

`README.md` is the short version.

Everything runs with Python 3 on Windows. The programs themselves are never executed: packed programs
are unpacked in an emulator, and the machine code is checked in an emulator too.

Run every command below from the repository's top folder. The commands use an app from the library
(Troopanum and its five versions) wherever a name is needed.


## Contents

1. Quick start
2. Setup
3. How the pieces fit together
4. The library
5. Unpacking MoleBox-packed programs
6. Decompiling
7. Verifying the decompiled code
8. Investigating a mismatch
9. Debugging helpers
10. Sounds
11. Type libraries
12. Tips and pitfalls
13. Known limits
14. Folder map


## 1. Quick start

Put your copy of an app's programs where its notes say (`apps/<name>/project/README.md`), then
decompile every program of the app. The source goes to `apps/<name>/source`:

```
python -I build_sourcecode.py --project troopanum
```

Regenerate the code of one program only, leaving its sounds alone:

```
python -I build_sourcecode.py --project troopanum --code-only --game "Troopanum 1.6"
```

Each build compares what the app's omission rules left out of the code with that program's record in
the project file. If it differs, the published code is not replaced and the differences are listed.
To accept a deliberate change and store it as the new record:

```
python -I build_sourcecode.py --project troopanum --code-only --record-omissions
```

Check one program's decompiled code against its machine code. Every procedure is tested 30 times or
more:

```
python -I vbcheck/check.py "Troopanum 1.6"
```

The program names the tools accept are the ones in the project files. An app's notes list them.


## 2. Setup

The tools need three groups of Python packages, installed into folders inside the repository rather
than into Python itself:

```
python -m pip install --target lib -r requirements.txt
python -m pip install --target emu speakeasy-emulator
python -I patch_speakeasy.py
```

* `lib` gets pefile (reads Windows executables), capstone (disassembler) and comtypes (type
  libraries).
* `emu` gets the Speakeasy Windows emulator and the Unicorn CPU emulator (version 1.0.2) that comes
  with it. Speakeasy unpacks MoleBox; Unicorn also runs the machine code in the checker.
  `patch_speakeasy.py` fixes a few things in both for modern Python and for the MoleBox runtime. It is
  safe to run more than once.

Both folders can be deleted and reinstalled at any time; they are listed in `.gitignore`.

A packed program must be unpacked (section 5) before it can be decompiled or checked.


## 3. How the pieces fit together

The work happens in this order:

1. **Unpacking** (packed programs only). `molebox_extract.py` runs the packer in an emulator until the
   original program is in memory. It saves that memory as `image.bin`, plus the names of the files in
   the program's data archive.
2. **Decompiling.** `vbdec/main.py` reads the program's VB metadata (forms, controls, procedures, API
   declarations), turns each procedure's x86 machine code into statements, cleans them up, rebuilds
   If / Do / For / Select Case, and writes .frm, .bas and .vbp files.
3. **Verifying.** `vbcheck/check.py` decompiles the program again, then runs every procedure twice:
   * once by interpreting the decompiled VB source;
   * once by emulating the original machine code.

   It compares everything the two do.
4. **Publishing.** `build_sourcecode.py` runs the decompiler for every program of the app into
   `apps/<name>/source/<program>`, checks what was left out against the record, and copies the
   program's documents. Unless told `--code-only`, it also copies and sorts the sounds.

The decompiler's stages, in the order a procedure goes through them:

1. `cfg.py` splits the machine code into basic blocks and recognises compiler idioms: overflow traps,
   jump tables, the FDIV bug check, cleanup subroutines.
2. `lift.py` and `x86sem.py` run each block symbolically. Registers and stack slots hold expressions
   instead of numbers, so `mov ax, [esi+0x86a]; inc ax` becomes `m_86A + 1`.
3. `runtime.py` knows what each VB runtime function (`__vba*`, `rtc*`) means: string concatenation,
   Variant arithmetic, conversions, file I/O.
4. `simplify.py` removes temporaries, rebuilds array indexing and With blocks, and splits reused stack
   slots into separate variables.
5. `structure.py` turns the block graph into If / Do / For / Select Case.
6. `emit.py` chooses types and names, writes declarations and record types, applies the app's
   omission rules, and prints the VB code.


## 4. The library

The library is the `apps` folder. It has one folder per app: a program, or a set of related programs
such as the versions of one game. Everything for an app is in its folder, `apps/<name>`:

* `project`: in git.
  * `project.json`: the settings. Which programs there are, what to leave out of the published
    source, a record of what that came to for each program, and what the checker needs to know.
  * `README.md`: the notes. Everything someone with their own copy of the programs needs to decompile
    them again (see "What the notes cover" below).
  * any helpers the settings name, for example a sound sorter (section 10).
* `utils`: in git, and only in some apps. Specialised tools that change the data of the compiled app
  rather than decompile it, for example Troopanum 2.0's sound mod builder, which writes a new sound
  archive for the original game. Nothing in the settings refers to them; the app's notes describe
  each one.
* `compiled`: your copy of the programs. The app's notes say exactly which files go where.
* `work`: unpacked images and files extracted from archives (section 5).
* `source`: the decompiled source, one folder per program.

All paths in a project file are relative to the app's folder.

The tools find the app themselves: by the program name you give (`check.py "Troopanum 1.6"`), or
because it is the only one in the library. `--project NAME` (an app's folder name in `apps`, or the
path of a project file) chooses one explicitly.

### What a project file contains

`project.json` is a plain text file in JSON format; `vbdec/project.py` reads it for the other tools.
The values in brackets are Troopanum's.

* `name` and `about`: the app's name and a description.
* `compiled_folder` and `source_folder`: where the compiled programs are, and where the published source
  goes. Leave them out to use the app's `compiled` and `source` folders, as Troopanum does.
* `games`: one entry per program, by name. Each has:
  * `exe`: the executable, inside `compiled_folder`.
  * `image`: for a packed program, the unpacked memory image to decompile instead
    (`work/tr20_box/image.bin`). `image_base` (in hexadecimal, default 400000) is its load address.
  * `sounds`: folders of extracted sound files to copy, for programs whose sounds are in an archive
    (`work/tr20_box/files`).
  * `label` and `note`: text for the DECOMPILER_NOTES.txt file.
  * `omissions`: the record of what the omission rules left out of this program (see below). The
    tools write this part; don't edit it by hand.
* `documents`: files next to each executable to copy into the published source (`troopanum.txt` and
  `stats.txt`).
* `skip_sound_files`: loose sound files not to copy (a review recording).
* `sound_sorter`: a Python file with a `sort_sounds(game, folder)` function that sorts the copied
  sounds (`project/sounds.py`). Without it, the sounds stay in one folder.
* `vbp_references`: `Reference=` lines for the .vbp file, for type libraries the programs use
  (DirectX 8 for Visual Basic).
* `type_libraries`: extra type libraries for `tlbdump.py` to dump (section 11).
* `checker`: what the checker's scripted world needs to know:
  * `app_path`: the folder `App.Path` returns (`C:\TROOP`).
  * `files`: data files found in that folder at the start of a run, with their contents (the high
    scores, options and statistics files).
  * `strings`: strings the programs work with, added to the checker's test inputs ("game", "menu",
    "play sound" and others).
* `omission_rules`: what to leave out of the published source (next part).
* `omission_reason`: the words used in the output to say why ("not needed for the port").
* `omission_note`: extra lines for DECOMPILER_NOTES.txt about the omissions.

### Omission rules

Each rule has a `label` (the reason shown in the output, for example "licensing / registration"), and
any of these:

* `controls`: names of controls. Statements that use them are left out.
* `patterns`: regular expressions. Statements containing a matching string literal are left out.
* `apis`: names of API functions. Statements that call them are left out.
* `strong`: regular expressions. A procedure containing a matching string is left out whole.
* `whole_module`: true or false. When every procedure of a module touches this rule, the whole
  module is left out (for example, Troopanum's licensing module).

Statements that call a procedure left out for the same reason are left out too. A procedure where 30%
or more of the statements would be left out is left out whole. Without a project file (running
`main.py` on its own, without `--project`), nothing is left out.

### The omission record and the double check

Every time a program is decompiled with its project file, the tools work out exactly what the rules
left out:

* which procedures were left out whole, and why;
* for every other procedure, how many statements were left out, and why.

They compare that with the program's `omissions` record in the project file:

* **The same:** the build says "omissions: as recorded" and goes on.
* **Different:** the build lists every difference. For example: "Proc_41FD90 is now omitted whole
  (online score posting); the record does not have it", or "Form_Load: 1 statement(s) left out as
  configuration (config.dat), the record says 3". The published code of that program is not replaced:
  decompiling happens in a temporary folder first. The build ends with "NOT UPDATED".
* **No record yet:** the build says so and goes on.

A difference means a change to the decompiler, or to the rules, changed what gets cut. Look at the
listed procedures. If the change is intended, store it as the new record:

```
python -I build_sourcecode.py --project troopanum --code-only --record-omissions
```

The record is then saved in the project file, so the next run (by you, by someone else from the
library, or after a long break) knows exactly what to leave out and checks it again.

The checker decompiles without leaving anything out. Statements cut from a procedure that is otherwise
published are therefore checked with the rest of it. Procedures left out whole are reported as
"omitted" and not compared. The checker prints a note when the procedures the rules would leave out
differ from the record.

### What the notes cover

An app's notes, `project/README.md`, should let someone who has the programs, but has never seen this
repository, decompile them and know what they got. They cover:

* what the programs are: name, authors, versions and years;
* the files needed for each program, with sizes and SHA-256 checksums of the executables and archives,
  so a reader can tell whether their copy is the same build;
* where to put them in the app's `compiled` folder;
* the exact unpacking commands, for packed programs, and how long they take;
* the build and check commands, and what their output should end with;
* what the omission rules leave out, and why;
* the compile options of each program (section 6);
* the check results: runs, passes and how much machine code was reached;
* the formats of the sound, music and other media files, for each program:
  * how many files of each kind, and where they are (loose, or in which archive);
  * the container and codec (PCM WAV, WMA, MP3, Ogg Vorbis, MIDI...), with the sample rate, channels
    and bits per sample, and the bit rate of compressed audio;
  * how the program loads and plays them (which API, as which file names, with which buffer flags);
  * any step outside the program that changes them first, for example an installer or configuration
    program that converts compressed files into the format the program reads;
* anything particular to these programs: how they differ from each other, what their sounds and data
  files are for, and quirks met while decompiling them.

### Adding an app to the library

1. Make a folder `apps/<name>/project` with a `project.json`. The quickest way is to copy an existing
   app's `project.json` and change it: give the new programs' names and executables, and replace or
   empty everything that belongs to the old programs: `omission_rules`, `checker`, `documents`,
   `skip_sound_files`, `sound_sorter`, `vbp_references`, `type_libraries`, and the `omissions` records.
2. Put your copy of the programs in `apps/<name>/compiled`.
3. Make sure `typelibs.json` covers the type libraries (ActiveX controls, DirectX...) the programs use
   (section 11).
4. Decompile and check, and fix what the checker finds (section 8):

   ```
   python -I build_sourcecode.py --project NAME --code-only
   python -I vbcheck/check.py "PROGRAM NAME"
   ```

5. Write the notes, `apps/<name>/project/README.md`.

Expect gaps the first time: section 13 lists what the tools do not handle yet.


## 5. Unpacking MoleBox-packed programs

MoleBox wraps a program: the real program is compressed inside the executable, and its data files can
be in a separate archive (`.bsc`). `molebox_extract.py` runs the packer in an emulator and writes the
unpacked program to `image.bin` and the archive's file names to `names.txt`:

```
python -I molebox_extract.py <exe> <output folder> --box <archive.bsc> [--extract | --list]
```

* `--list`: only write the archive's file names.
* `--extract`: also read every file out of the archive through the emulated packer. This works for
  compressed archives, but is slow (about an hour for Troopanum Classic's).

An archive that just stores its WAV files one after another, XOR-encoded, can be split much faster by
`bsc_split.py`, using the names from `--list`. It finds the XOR key from the first file's "RIFF"
signature:

```
python -I bsc_split.py <archive.bsc> <output folder>/names.txt <output folder>/files
```

Use a folder in the app's `work` folder as the output folder, one per packed program. The program's
`image` and `sounds` in the project file point at the results, so `build_sourcecode.py` and the checker
find them automatically. An app's notes give the exact commands for its packed programs.

`testproc.py` and `asmrange.py` accept a `.bin` image in place of an executable (they recognise the
extension), and `main.py` takes one with `--image`.

`unpack_emu.py <exe> <dump> <code start> <code end>` is a simpler unpacker: it runs a packed program in
the emulator until execution reaches the given code range, and dumps the memory image. It does not read
archives.


## 6. Decompiling

`build_sourcecode.py` is the normal way to decompile (section 1). Its options:

* `--project NAME`: the app to build. It can be left out when `--game` names a program, or when the
  library has only one app.
* `--game NAME`: only this program.
* `--code-only` or `--sounds-only`: rebuild only the code, or only the sounds (section 10).
* `--out FOLDER`: write somewhere other than the app's `source` folder.
* `--record-omissions`: store what was left out as each program's new record (section 4).

To decompile one executable by hand into a folder of your choice:

```
python -I vbdec/main.py "apps/troopanum/compiled/Troopanum 1.6/troop.exe" C:\temp\out16
python -I vbdec/main.py apps/troopanum/work/tr20_box/image.bin C:\temp\out20 --image apps/troopanum/work/tr20_box/image.bin
```

Options of `main.py`:

* `--image FILE`: decompile an unpacked memory image instead of the executable. The first argument is
  then ignored but still required.
* `--base HEX`: load address of the image (default 400000).
* `--label TEXT` and `--note TEXT`: text for the DECOMPILER_NOTES.txt file.
* `--debug`: print Python tracebacks for procedures that fail.
* `--project NAME` and `--game NAME`: apply that app's omission rules, and compare what they left
  out with the program's record (section 4). If it differs, the differences are listed and the exit
  status is 3. Without `--project`, nothing is left out.
* `--record-omissions`: store what was left out as the program's record instead of comparing.

The last line printed ends with `warnings N unplaced statements N`. Both must be 0. A statement that
was lifted but did not make it into the structured output is a decompiler bug, and the procedure is
listed.

### Names in the output

VB executables keep no names for local variables, form variables or private procedures. The output
names them after where they live:

* `Proc_41C510`: a procedure, by its code address.
* `m_86A`: a form variable, by its offset in the form object.
* `g_4A10`: a global variable, by its address.
* `var_14`: a local variable, by its stack offset.
* `rAX`, `rDI`: a value the compiler kept in a CPU register across several statements.
* `tmp1`: a value the machine computed once and used later.
* `udt_m_354` and its fields `f0`, `f22`: a record (Type) type, named after the array that uses it.
  Fields are named after their byte offset.

Event handlers (`Form_Load`, `Timer1_Timer`), controls, string literals, API declarations and DirectX
member names are real, because the executable stores them.

### What is left out on purpose

The app's omission rules decide what is left out of the published source, and its record says what
that came to for each program; every build checks it (section 4). Each app's notes say what its
rules leave out and why. Set the environment variable `VBDEC_NOEXCLUDE=1` to get everything.

### Compile options

Each program's DECOMPILER_NOTES.txt lists the compile options found in its machine code:

* Without integer overflow checks, Integer arithmetic wraps around silently (32767 + 1 gives -32768).
* Without array bounds checks, array indexes are not checked.
* Without floating-point error checks, dividing by zero gives infinity instead of an error.

The decompiled code is only equivalent to the original when compiled with the same options.
`CLng(...)` in the output is deliberate. Without overflow checks, the compiler sometimes does Integer
arithmetic in 32-bit registers (for example `n * 10000` as a chain of `lea` instructions). That result
does not wrap, and `CLng(n) * 10000` is what reproduces it in VB. Where only the low 16 bits are kept
(stored into an Integer), no CLng is needed, because the result is the same either way.


## 7. Verifying the decompiled code

```
python -I vbcheck/check.py "Troopanum 2.0"
python -I vbcheck/check.py "Troopanum 2.0" --proc Proc_43D160 --proc Proc_4207D0 --show
```

### What it does

For each procedure, the checker builds random starting states: form variables, parameters, files and
objects. It then runs the procedure twice from the same state:

* once in its own VB6 interpreter, which reads the decompiled source;
* once in a CPU emulator (Unicorn), which runs the original machine code.

Both sides talk to the same scripted "world". Calls to other procedures, DirectX, VB objects, files,
timers, Rnd and DoEvents are recorded as events and answered from a script. DoEvents and procedure
calls also change form variables at random, as other code would.

A scripted call can return a value, change form variables, and Set a new object into a ByRef object
parameter (a sound loader, for example). In the source, that object reaches the variable only when it
is declared with exactly the parameter's type; otherwise VB passes a temporary copy.

The two runs must match exactly: every event and its arguments, the form variables at every event, the
final state, the return value and any run-time error.

### Options

* `--runs N`: random runs per procedure before the coverage search starts (default 30). The search
  stops when nothing new is reached, and never goes past 10 times N runs. Use 10 for a quick look and
  30 or more for a real check.
* `--proc NAME`: check only this procedure. Can be given several times.
* `--seed N`: a different set of random states (default 0). The same seed always gives the same runs.
* `--events N`: the most events one run may produce before it is stopped (default 200).
* `--show`: on a mismatch, print both event logs; for every procedure, list the machine-code blocks
  never reached.
* `--out FOLDER`: where to write the decompiled project it checks (default: a temporary folder). It is
  the full project, including the omitted parts.
* `--project NAME`: the project to use, when the program's name alone does not find it (section 4). The
  checker takes the program's executable, its omission rules, and its data files, program folder and
  test strings from there.

Setting the environment variable `VBCHECK_TRACE=1` prints a Python traceback for every run-time error
on the source side. That shows exactly which statement raised it.

### Reading the results

Each procedure gets one line, for example:

```
Proc_437FB0                  pass               runs 56  blocks 83/92 error:28 return:1 stop:27
```

* The status comes first.
* `runs` is how many runs were made.
* `blocks 83/92` means 83 of the procedure's 92 machine-code blocks were reached by at least one run.
* The rest counts how the runs ended:
  * `return`: normally.
  * `error`: a VB run-time error, the same on both sides.
  * `stop`: the run reached its event limit (`--events`, for example a loop waiting for a key that
    never comes), or the code ran End or unloaded the form. Both sides stop at the same point.
  * `out-of-bounds`: an index outside an array (see below).

The statuses:

* **pass**: every run matched.
* **MISMATCH**: a difference. The report gives the first point where the two sides part ways: the
  event that differs, or the variables that differ before it, or the different endings. It is either a
  decompiler bug or a gap in the checker; section 8 shows how to tell.
* **omitted**: a procedure left out of the published source whole, on purpose (section 4). It is not
  compared.
* **unsupported-source** and **invalid-source**: the interpreter cannot run or parse that source. Treat
  these as checker gaps to fix.
* **machine-problem**: the emulator could not run the original code. Examples are an emulated runtime
  function that is missing, or a pointer that is not an object the checker created. This is never
  counted as a VB error. Otherwise it could pass for a matching error that the source raises for a
  different reason.

Runs where the source uses an index outside an array are counted as out-of-bounds and not compared,
because in a program without bounds checks the machine reads whatever memory is there. In a program
with bounds checks, an out-of-range index is error 9 on both sides and is compared.

### Coverage: what is and is not verified

Only code that some run reached is verified. After the random runs, the checker aims new runs at blocks
not reached yet:

* It takes the variable a branch tests and sets it to the value it compares with. The value comes from
  the machine code's conditions and jump tables, and from the decompiled source's conditions on array
  elements and record fields (`If m_230(6).fC <> 0`).
* It starts from a saved state whose run reached that branch, so the conditions around it already
  hold.
* It gives every unreached block one try before a second try for any of them.

These hints only choose inputs. A wrong hint costs coverage, never correctness.

`--show` lists the unreached blocks by address. To see what they are, use `asmrange.py` (section 9).

Each app's notes give its check results.


## 8. Investigating a mismatch

A typical session, with a Troopanum 2.0 procedure.

1. **See the details.** Run just that procedure with `--show`:

   ```
   python -I vbcheck/check.py "Troopanum 2.0" --proc Proc_437FB0 --show --out C:\temp\chk20
   ```

   The report says where the two sides first differ. With `--out`, the checked source is in
   `C:\temp\chk20\frmMain.frm`.

2. **Read the decompiled procedure** in that file, around the statement that produced the event.

3. **Read the machine code** of the same spot. `asmrange.py` prints it with import names and string
   literals filled in:

   ```
   python -I vbdec/asmrange.py apps/troopanum/work/tr20_box/image.bin 4387B0 438830
   ```

   To find where an import or a string is used inside a procedure, for example where Proc_41C510 calls
   Randomize: `asmrange.py apps/troopanum/work/tr20_box/image.bin find rtcRandomize 41C510`.

4. **Look at the lifted statements before clean-up.** `testproc.py --raw` prints each block's
   statements as the lifter produced them, then the final VB code:

   ```
   python -I vbdec/testproc.py apps/troopanum/work/tr20_box/image.bin 437FB0 --raw
   ```

   `testproc.py` decompiles only the procedures you name. Facts that come from other procedures (the
   types of a called procedure's parameters, for example) are only complete in a full decompile.

5. **Decide who is wrong.** If the machine code does something the source does not, fix the
   decompiler. If the source is right and the checker models VB or the runtime wrongly, fix the checker.
   Examples of both kinds, from the Troopanum games:
   * Decompiler: a 32-bit multiply written as Integer arithmetic. The machine did `lea` chains and
     stored all 32 bits.
   * Decompiler: `0` instead of `Nothing` for a DirectX object parameter.
   * Decompiler: the compiler computed `p2 - p1 + 1` (which can overflow with overflow checks on)
     before the Timer call, while the source computed it after.
   * Checker: `Abs(-32768)` raised overflow in the interpreter. The compiled code (without overflow
     checks) just gives -32768.

6. **Check the fix on that procedure**, then with other seeds (`--seed 1`, `--seed 2`) to make sure it
   was not luck.

7. **Run the full check on every program in the library.** A fix for one program often changes the
   others too.

Fixes are easiest to make and to undo as small script files that make exact text replacements and fail
when the text is not found.


## 9. Debugging helpers

Addresses are hexadecimal, without `0x`. `testproc.py` and `asmrange.py` take either an executable or
an unpacked `.bin` image; the others need a real executable (or, for `disraw.py`, a raw dump and its
load address).

* `vbdec/testproc.py <exe> <address|all> [--raw] [--time]`: decompile the named procedures (several
  addresses separated by commas) and print them. `--raw` also prints the lifted statements per block.
  `--time` lists the slowest procedures.
* `vbdec/asmrange.py <exe> <start> <end>`: disassemble an address range with import names and strings.
  `vbdec/asmrange.py <exe> find <import-or-text> [procedure address]` finds where an import or a string
  literal is used.
* `vbdec/callsites.py <exe> [name pattern] [examples per import]`: example call sites of runtime
  functions, with the instructions around them. Useful when adding support for a new runtime function.
* `vbinfo.py <exe>`: the VB project header: objects, whether the code is native or P-code.
* `vbdis.py <exe> <address> [count]`: quick disassembly.
* `disraw.py <dump> <base> <address> <count>`: disassembly from a raw memory dump.


## 10. Sounds

`build_sourcecode.py` (without `--code-only`) copies each program's sounds into
`apps/<name>/source/<program>/sounds`:

* the loose sound files next to the executable (WAV, WMA, MIDI, MP3 and OGG), minus the app's
  `skip_sound_files`;
* the files in the program's `sounds` folders, for sounds extracted from an archive (section 5). When
  an archive stores every name in capitals, the copies get lower-case names, so they also work on
  systems where case matters.

If the project names a `sound_sorter`, its `sort_sounds(game, folder)` function then sorts them, for
example into folders by use with a description of each sound. Without one, they stay in one folder.
The app's notes describe its sorter, and give the formats of the sounds (section 4, "What the notes
cover"). The copies are not converted: a file the program only reads after something else has converted
it (the WMA files of Troopanum 1.5, 1.6 and SP 1.6, for example) is copied as it was shipped.

`build_sourcecode.py --sounds-only` copies and sorts the sounds again without touching the code.


## 11. Type libraries

`typelibs.json` describes the COM libraries the tools know. The decompiler uses it to name members of
DirectX, VB and other objects from vtable offsets, and both decompiler and checker use it for parameter
types. It is shared by the whole library.

`typelibs.json` is part of the repository, so decompiling and checking never need the libraries
themselves. Regenerating it does: it needs comtypes, the 32-bit system DLLs, and `VB6.OLB` (below).
Then run:

```
python tlbdump.py typelibs.json
```

The VB6 runtime, VB6.OLB (forms and controls) and OLE Automation are always included, plus the
`type_libraries` of every app in the library. `--project NAME` takes only that app's instead.
Paths of more libraries can follow on the command line.

### Getting VB6.OLB

`VB6.OLB` is Microsoft's type library for Visual Basic 6 forms and controls. It is Microsoft's file, so
it is not included here. Put a copy at `untrusted_vb6olb/VB6.OLB`; that folder is listed in
`.gitignore`. Either of these works:

* From a Visual Basic 6 installation: it is in the `VB98` folder, usually
  `C:\Program Files (x86)\Microsoft Visual Studio\VB98\VB6.OLB`.
* From the Semi VB Decompiler project on GitHub, which includes it as
  `Semi VB Decompiler/data/VB6.OLB`:

  ```
  curl -L --create-dirs -o untrusted_vb6olb/VB6.OLB "https://github.com/VBGAMER45/Semi-VB-Decompiler/raw/master/Semi%20VB%20Decompiler/data/VB6.OLB"
  ```

`typelibs.json` matches the copy of 295,514 bytes with this SHA-256 checksum:
4109ee488c9e930f8884280526b4c36ffc7cee03329cac6a9b8f5864b15c8453. To check yours:
`certutil -hashfile untrusted_vb6olb/VB6.OLB SHA256`.


## 12. Tips and pitfalls

* **Run Python with `-I`** (isolated mode). This keeps Python from loading code from the current
  folder or from user settings. That matters because some folders here hold files that came from the
  internet (`untrusted_vb6olb`) or out of programs' archives. Keep downloaded or extracted files in
  their own folders, and keep scripts elsewhere.
* **Check your copy against the checksums in the app's notes.** A different build of the same
  program decompiles to different code, and its omission record will not match.
* **Do not edit the tools while a long check is running.** The checker starts a new Python process for
  each program, so programs checked after an edit would use different code from those before it, and
  the results would be mixed.
* **Battery power slows everything about 2.5 times.** Plug the laptop in for full checks. The first
  line of each program's result shows how long decompiling took.
* **Check at the default 30 runs before trusting a result.** Ten runs is fine while working, but more
  runs reach more code, and find errors that ten runs miss.
* **A pass covers only the code that was reached.** Look at the `blocks` numbers and `--show` before
  concluding that a procedure is right everywhere.
* **The same error number on both sides is not proof of equal behaviour by itself.** The checker
  compares the events up to the error too. And the emulator reports its own limitations as
  machine-problem, not as VB errors.
* **Keep the vbdec and vbcheck files' line endings** (some are CRLF, some LF) when editing them with
  scripts.
* **Shell here-documents in this environment drop a backslash followed by a newline**, which joins
  lines. Write patch scripts to a file instead of pasting them into the shell.
* **The tools never run the programs.** Unpacking happens in an emulator, and checking runs single
  procedures in an emulator against a scripted world.


## 13. Known limits

* **Names and comments are gone** from the executables and cannot be recovered. Everything named
  `Proc_`, `m_`, `var_` and `tmp` is synthetic.
* **Code the checker never reached is not verified.** It is mostly rare branches inside large
  procedures. Each app's notes give how much was reached.
* **Procedures left out whole are not verified**, and are not in the published source.
* **Unused parameters** have no type information in the procedure itself. Their passing mode comes
  from the callers, and their type is a best guess (it does not affect behaviour).
* **On Error Resume Next is only modelled for programs built without optimisation**, because only their
  code keeps the statement numbers this needs. The machine side resumes where the compiler's own
  statement table says the next statement starts; the last entry of that table is the procedure's
  exit.
* **The source matches the original only when compiled with the same options** (section 6).
* **Windows API functions:** only those the library's programs use are known (about 15). For others,
  the decompiler guesses the number of arguments and cannot write the Declare line; the checker stops
  with machine-problem when one is called. Add them to `KNOWN_DECLARES` in `vbdec/main.py`,
  `API_ARG_BYTES` in `vbdec/decompiler.py`, and `api_call` in `vbcheck/machine.py`.
* **VB runtime functions:** only those the library's programs use are understood (`vbdec/runtime.py`)
  and modelled (`vbcheck/mxruntime.py`). `main.py` lists any it meets without a handler.
* **Format():** only the patterns the library's programs use.
* **Forms and classes:** the checker handles one form per program. Several forms, or class modules,
  would need more work.
* **P-code:** only programs compiled to native code are supported.
* **Compiler settings:** the idioms come from the builds in the library: optimised and
  unoptimised. Other settings may produce code patterns not seen yet.


## 14. Folder map

* `apps/`: the library, one folder per app (section 4). For example `apps/troopanum`:
  * `project/`: `project.json` (the settings: games, omission rules and records, checker data),
    `README.md` (the notes) and `sounds.py` (the sound sorter).
  * `utils/`: `tr20_soundmod.py`, which builds a Troopanum 2.0 sound archive with your own sounds.
  * `compiled/`: your copy of the games (not in git).
  * `work/`: unpacked images and extracted archives (not in git).
  * `source/`: the decompiled source (not in git).
* `build_sourcecode.py`: decompiles an app's programs into its `source` folder.
* `molebox_extract.py`, `bsc_split.py`, `unpack_emu.py`, `patch_speakeasy.py`: unpacking.
* `vbdec/`: the decompiler.
  * `main.py`: the command-line driver.
  * `project.py`: reads project files, compares and stores omission records.
  * `vbmeta.py`: VB project structures (objects, controls, events, declares, procedures,
    error-handler tables).
  * `instdata.py`: a form's variable table: strings, objects, fixed-size arrays and their bounds.
  * `pe.py`: executable and memory-image reader.
  * `cfg.py`, `lift.py`, `x86sem.py`, `runtime.py`, `ir.py`, `simplify.py`, `structure.py`, `emit.py`,
    `frm.py`, `tlb.py`, `decompiler.py`: the stages described in section 3.
  * `testproc.py`, `asmrange.py`, `callsites.py`: debugging helpers.
* `vbcheck/`: the checker.
  * `check.py`: the driver and the comparison.
  * `vbparse.py` and `vbinterp.py`: the VB6 parser and interpreter.
  * `vbvalues.py`: VB values and arithmetic.
  * `world.py`: the scripted world.
  * `machine.py`, `mxmem.py` and `mxruntime.py`: the emulated machine and its model of the VB runtime
    functions.
* `typelibs.json`, `tlbdump.py`: type libraries. `untrusted_vb6olb/` is where your copy of `VB6.OLB`
  goes (not in git; section 11).
* `vbinfo.py`, `vbdis.py`, `disraw.py`: quick viewers.
* `lib/`, `emu/`: installed Python packages (not in git). `emu` holds Speakeasy and Unicorn.
