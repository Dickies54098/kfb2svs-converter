# kfb2svs-converter

> Batch-convert KFB whole-slide images into QuPath-compatible SVS files — with one-click batch entry and a bundled portable Python runtime.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: Windows x64](https://img.shields.io/badge/Platform-Windows%20x64-blue.svg)](https://github.com/Dickies54098/kfb2svs-converter)
[![Python: 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)

---

## 📖 Overview

`kfb2svs-converter` is a self-contained Windows toolkit that converts digital
pathology slides stored in **KFB format** (produced by KFbio scanners) into
**Aperio-like SVS / TIFF** files that [QuPath](https://qupath.github.io/) can
open directly as a single whole-slide series.

The conversion is a two-step pipeline:

1. **KFbioConverter.exe** (bundled, 32-bit) performs the raw KFB → Aperio-like
   TIFF conversion.
2. A pure-Python post-processor (`kfb_to_qupath_svs.py`) **cleans the TIFF
   metadata** so QuPath sees exactly one WSI series:
   - Writes an explicit `NewSubfileType=0` on every retained pyramid IFD.
   - Removes thumbnail / label / macro IFDs from the reachable TIFF chain.
   - Preserves all original pixel data and pyramid levels.

The package ships with a **bundled Python 3.12 runtime**, so it runs on any
64-bit Windows machine without installing Python.

---

## ✨ Features

- ⚡ **One-click batch entry** — configure two paths in a local INI file and go.
- 🧹 **QuPath-ready output** — no extra thumbnail/label series polluting the
  image list.
- 🔒 **Non-destructive** — source KFB files are never modified; cleaned SVS
  files are written into a separate output folder.
- 🧵 **Parallel conversion** — configurable worker count for HDD vs SSD.
- ⏭️ **Auto-skip existing outputs** — interrupted runs resume gracefully.
- 📊 **CSV report** — every run produces a per-file conversion report.
- 📦 **Fully portable** — bundled Python runtime, no system install required.
- 🛡️ **Atomic publish** — cross-drive staging + atomic move, so the final
  output is never left half-written.

---

## 📁 Project Layout

```
kfb2svs-converter/
├── run_convert.bat          # One-click entry (keep this file unchanged)
├── run_convert.example.ini  # Copy to run_convert.local.ini and edit
├── kfb_to_qupath_svs.py     # Conversion + SVS cleanup logic (pure stdlib)
├── converter/
│   └── x86/
│       ├── KFbioConverter.exe     # KFbio's KFB→SVS converter (32-bit, third-party)
│       ├── ImageOperationLib.dll  # KFbio image library (third-party)
│       ├── turbojpeg.dll          # libjpeg-turbo (BSD)
│       ├── mfc100u.dll            # MSVC 2010 MFC runtime (Microsoft EULA)
│       ├── MSVCR100.dll           # MSVC 2010 CRT (Microsoft EULA)
│       └── msvcp100.dll           # MSVC 2010 C++ runtime (Microsoft EULA)
├── runtime/                 # Portable Python 3.12 runtime (PSF License)
│   ├── python.exe
│   ├── python312.dll
│   ├── Lib/                 # Python standard library
│   └── DLLs/                # Python C extensions
├── LICENSE                  # MIT license (project's own source code)
├── NOTICE                   # Third-party component attributions
└── README.md
```

---

## 🚀 Quick Start

### Option A — Use the bundled portable runtime (recommended, zero-install)

1. **Clone or download** this repository to any local folder, e.g. `D:\kfb2svs-converter`.
2. **Copy** `run_convert.example.ini` to `run_convert.local.ini`.
3. **Edit the two paths** in `run_convert.local.ini`:

   ```ini
   INPUT_DIR=C:\path\to\your\kfb_input
   OUTPUT_DIR=C:\path\to\your\svs_output
   ```

   - `INPUT_DIR` — folder that contains your `.kfb` files.
   - `OUTPUT_DIR` — folder where the cleaned `.svs` files will be written
     (created automatically if it does not exist).

4. **Optional** — adjust the worker count for your storage:

   ```ini
   WORKERS=2
   ```

   Use `2` for HDD/USB (default), or `3`–`4` for a fast NVMe SSD.

5. **Double-click `run_convert.bat`**. Done.

`run_convert.local.ini` is ignored by Git, so machine-specific paths are not
published accidentally. Keeping configuration outside the batch file also
prevents text editors from changing its required Windows CRLF line endings.

### Option B — Use your own Python 3.10+ installation

If you already have Python 3.10 or newer on your system, you don't need the
bundled `runtime/` folder at all:

```powershell
# From the project root:
python kfb_to_qupath_svs.py convert ^
    --input-dir  "C:\path\to\your\kfb_input" ^
    --output-dir "C:\path\to\your\svs_output" ^
    --converter  ".\converter\x86\KFbioConverter.exe" ^
    --workers 2
```

The script uses **only the Python standard library** — no `pip install`
needed.

---

## 🛠️ Command Reference

The script exposes three subcommands:

### `convert` — KFB → cleaned SVS

```
python kfb_to_qupath_svs.py convert
    --input-dir DIR         # Source .kfb folder
    --output-dir DIR        # Destination .svs folder
    --converter PATH        # KFbioConverter.exe path
    --stage-dir DIR         # ASCII-only staging folder (auto: <output>:\kfb2svs_stage)
    --layers N              # Pyramid levels to generate (2-9, default 4)
    --workers N             # Parallel conversions (default 1; HDD: 2, SSD: 3-4)
    --force                 # Re-convert even if output already exists
    --min-pyramid-max-dim N # Drop tiny pyramid IFDs whose longest edge < N (default 0 = off)
```

### `clean` — clean already-converted SVS files (no KFB→SVS step)

```
python kfb_to_qupath_svs.py clean
    --input-dir DIR         # Folder with raw .svs files to clean
    --output-dir DIR        # Folder for cleaned .svs files
```

### `inspect` — print the TIFF/IFD structure of an SVS file

```
python kfb_to_qupath_svs.py inspect path/to/file.svs
```

Useful for diagnosing why QuPath shows multiple series for one slide.

---

## ⚙️ How It Works

KFbioConverter writes a valid Aperio-like TIFF, but its baseline IFD normally
carries `NewSubfileType=2`, while its reduced-resolution IFDs **omit** the
tag. Bio-Formats (the reader QuPath uses) can interpret both the non-zero and
the missing values as label/macro images, which splits a single WSI into
multiple series in QuPath's image list.

`kfb_to_qupath_svs.py` fixes this by:

1. Reading the full TIFF / BigTIFF directory chain (pure-stdlib parser,
   supports both Classic TIFF and BigTIFF).
2. Keeping only the baseline IFD + every IFD whose ImageDescription starts
   with `Aperio Image` (these are the real pyramid levels).
3. Appending a **replacement IFD chain** with `NewSubfileType=0` on every
   page, and pointing the TIFF header at it. All original tag payloads and
   pixel offsets are preserved in place — only the directory chain is
   rewritten.
4. Validating the result: every reachable IFD must have `NewSubfileType=0`,
   be an Aperio page, and have consistent tile/strip arrays.

The original input is **never** edited.

### Staging & atomic publish

KFbioConverter can fail when its output path contains long non-ASCII
components. The script works around this by:

1. Converting into an ASCII-only **staging folder** (`--stage-dir`, default
   `<output drive>:\kfb2svs_stage`) using a unique per-file name.
2. Cleaning the staged SVS in place.
3. **Atomically moving** it into the final output folder. If the staging and
   output folders live on different volumes, it transparently falls back to
   a copy-into-temp + atomic replace on the destination volume, so the
   output is never left half-written.

### Conversion report

Every successful run writes a CSV report to the output folder:

```
qupath_svs_report_YYYYMMDD_HHMMSS.csv
```

Columns: `file, kept_ifds, removed_ifds, removed_associated_ifds,
removed_small_pyramid_ifds, input_mib, output_mib, physically_truncated,
baseline_nst_before, baseline_nst_after`.

---

## 🔍 Requirements

| Component             | Version / Note                                          |
| --------------------- | ------------------------------------------------------- |
| Operating System      | Windows 10 / 11 (64-bit). The converter is 32-bit, so   |
|                       | the OS must support 32-bit apps (Win10/11 x64 do).      |
| Python                | 3.10+ if running the script yourself. The bundled      |
|                       | `runtime/` ships Python 3.12, so no install is needed. |
| KFB source files      | Produced by KFbio scanners.                            |
| Disk space            | Each SVS is roughly the same size as its source KFB.   |
|                       | The staging folder needs room for one in-flight SVS.   |

---

## ⚠️ Important Notes

- **Don't run two batch files against the same output folder** at the same
  time — they will collide on the staging and publish step.
- **Existing outputs are skipped** by default. Pass `--force` to re-convert.
- The bundled VC++ 2010 MFC DLLs (`mfc100u.dll`, `MSVCR100.dll`,
  `msvcp100.dll`) are required because `KFbioConverter.exe` is a 32-bit MFC
  application. You can instead install the *Microsoft Visual C++ 2010 x86
  Redistributable* system-wide and delete those three DLLs.
- The converter writes Chinese characters to stdout on some systems; the
  script captures and re-emits them with `errors="replace"`, so encoding
  issues will not crash a run.

---

## 📜 License & Third-Party Components

This project's own source code (`kfb_to_qupath_svs.py`, `run_convert.bat`,
and documentation) is released under the **MIT License** — see
[LICENSE](LICENSE).

However, this repository also **bundles third-party binaries** that are
**NOT** covered by the MIT license and retain their original licenses:

- **KFbioConverter.exe / ImageOperationLib.dll** — proprietary, © KFbio.
- **libjpeg-turbo (`turbojpeg.dll`)** — BSD 3-Clause.
- **MSVC 2010 MFC/CRT DLLs** — Microsoft EULA.
- **Python 3.12 runtime** — PSF License.

See [NOTICE](NOTICE) for the full attribution and license details for each
bundled component. If you are a vendor and wish to have your binary removed
from this repository, please open an issue and it will be taken down
promptly.

---

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/foo`).
3. Commit your changes with a clear message.
4. Open a pull request describing what and why.

Bug reports and usage questions are equally welcome via Issues.

---

## 🙏 Acknowledgements

- [KFbio](https://www.kfbio.com/) for the KFB format and KFbioConverter.
- [QuPath](https://qupath.github.io/) — open-source bioimage analysis.
- [Bio-Formats](https://www.openmicroscopy.org/bio-formats/) — the reader
  whose strictness made this cleanup necessary.
- [libjpeg-turbo](https://libjpeg-turbo.org/) — fast JPEG decoding.
- The Python Software Foundation for the bundled runtime.

---

## 📧 Contact

Open an issue at
[github.com/Dickies54098/kfb2svs-converter/issues](https://github.com/Dickies54098/kfb2svs-converter/issues)
for bug reports, feature requests, or questions.
