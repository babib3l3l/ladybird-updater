import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def load_visual_studio(config, environment):
    if environment.get("VSCMD_ARG_TGT_ARCH", "").lower() in {"x64", "amd64"}:
        return
    script = config.get("vcvars")
    if script:
        vcvars = Path(script).resolve(strict=True)
    else:
        program_files = Path(environment.get("PROGRAMFILES(X86)") or environment["PROGRAMFILES"])
        vswhere = program_files / "Microsoft Visual Studio/Installer/vswhere.exe"
        if not vswhere.is_file():
            raise RuntimeError("Install Visual Studio Build Tools or set vcvars in config.json.")
        installations = subprocess.check_output([
            vswhere, "-all", "-prerelease", "-products", "*", "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath",
        ], text=True, errors="replace").splitlines()
        candidates = [Path(item.strip()) / "VC/Auxiliary/Build/vcvars64.bat" for item in installations if item.strip()]
        vcvars = next((item for item in candidates if item.is_file()), None)
        if vcvars is None:
            raise RuntimeError("Visual Studio C++ x64 Build Tools were not found.")
    command = f'cmd.exe /d /c call "{vcvars}" >nul && set'
    output = subprocess.check_output(command, text=True, errors="replace", env=environment)
    for line in output.splitlines():
        name, separator, value = line.partition("=")
        if separator and name:
            environment[name.upper()] = value


def build(config, log):
    source = Path(config["source"]).resolve(strict=True)
    environment = os.environ.copy()
    load_visual_studio(config, environment)
    additional_paths = [str(Path(item).resolve(strict=True)) for item in config.get("extra_path", [])]
    git_directory = str(Path(config["git"]).resolve(strict=True).parent)
    environment["PATH"] = os.pathsep.join([*additional_paths, git_directory, environment.get("PATH", "")])
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["PYTHONUNBUFFERED"] = "1"
    environment["VCPKG_DISABLE_METRICS"] = "1"

    missing = [
        name for name in ("clang-cl", "cmake", "ninja", "cargo")
        if not shutil.which(name, path=environment["PATH"])
    ]
    if missing:
        raise RuntimeError(f"Missing build tools: {', '.join(missing)}. See README.md.")

    jobs = int(config.get("jobs", min(os.cpu_count() or 1, 8)))
    if jobs < 1:
        raise ValueError("jobs must be a positive integer.")
    return subprocess.call([
        sys.executable, "-u", "Meta/ladybird.py", "build", "--preset", "Release",
        "--cc", "clang-cl", "--cxx", "clang-cl", "-j", str(jobs), "ladybird",
    ], cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT)


def main():
    with (ROOT / "build.log").open("w", encoding="utf-8", buffering=1) as log:
        try:
            config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
            return build(config, log)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"Build setup failed: {error}", file=log)
            return 1


if __name__ == "__main__":
    sys.exit(main())




