# Ladybird Updater for Windows

This project is independent of the Ladybird Browser Initiative.

This app takes care of the repetitive parts of keeping a native Windows build of Ladybird up to date. Open the app and click **Check now** to look for a newer commit in the official Ladybird repository. You can follow the build log and see how long it has been running while you keep using the browser.

The updater tests a local page and the Ladybird website over HTTPS before staging a build. Once Ladybird is closed, it can install the tested build and keep the previous one available under **Restore previous version**. A failed build leaves the current browser in place.

## What you need

- 64-bit Windows, Python 3 with tkinter, and Git.
- Visual Studio C++ Build Tools, Clang-CL, CMake, Ninja, Rust, and the prerequisites for the Ladybird revision you want to build.
- A Ladybird checkout on a short path and a separate, writable browser folder containing `bin` and `share`.

Native Windows builds are experimental. Check the upstream instructions if a newer commit needs different tools. [Ladybird build instructions](https://github.com/LadybirdBrowser/ladybird/blob/master/Documentation/BuildInstructionsLadybird.md#windows)

## Set it up

Clone or extract the updater **outside** the Ladybird browser folder. If you already built Ladybird and copied it into a separate browser folder, skip the first block and set `$source`, `$runtime`, and `$revision` to your own values.

Build Ladybird for the first time from an x64 Visual Studio Developer PowerShell.

```powershell
$source = 'C:\src\ladybird'
$runtime = Join-Path $env:LOCALAPPDATA 'Programs\Ladybird'
git -c core.longpaths=true clone https://github.com/LadybirdBrowser/ladybird.git $source
if ($LASTEXITCODE) { throw 'The Ladybird checkout could not be cloned.' }

Push-Location $source
try {
    python Meta\ladybird.py build --preset Release --cc clang-cl --cxx clang-cl ladybird
    if ($LASTEXITCODE) { throw 'Ladybird failed to build.' }
    $revision = git rev-parse HEAD
    if (Test-Path -LiteralPath $runtime) { throw 'Choose an empty runtime location.' }
    New-Item -ItemType Directory -Path $runtime | Out-Null
    Copy-Item -LiteralPath 'Build\release\bin','Build\release\share' -Destination $runtime -Recurse
} finally {
    Pop-Location
}
```

Run setup from the updater directory using the full commit hash that produced the browser in `$runtime`.

```powershell
.\setup.ps1 -Source $source -Installation $runtime -Revision $revision
```

Open **Ladybird Updater** from Start, or run `python updater.py` from the updater folder. Add `-Schedule` to the setup command if you want Windows to check on Sundays at 4 p.m. local time. While you are signed in, a second task checks every 15 minutes whether a staged update can be installed.

## If your build needs a different setup

Setup stores your local paths in `config.json`. Add tool directories to `extra_path` if they are outside your user `PATH`. Set `vcvars` if you use a specific `vcvars64.bat`, or set `jobs` to a positive number to control build parallelism. Setup also accepts `-Python` and `-Git` if you have more than one installation.

The default `build.py` calls Ladybird's native Release build helper. You can point `build_script` at your own Python wrapper if your machine needs a workaround. The wrapper should build the configured checkout into `Build\release`, stream its output to `build.log` in the updater folder, and return a nonzero exit code if the build fails.

Keep the checkout clean so the updater can fast-forward it. If you restore the previous build, the updater skips the reverted commit until a newer one appears upstream. Your browser profiles live outside the folders it swaps.
