import argparse
import codecs
import ctypes
from contextlib import contextmanager
from datetime import datetime
import json
import msvcrt
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))
SOURCE = Path(CONFIG["source"])
BUILD = Path(CONFIG["build_script"])
PYTHON = Path(CONFIG["python"])
GIT = Path(CONFIG["git"])
REPOSITORY = "https://github.com/LadybirdBrowser/ladybird.git"
REF = "refs/heads/master"
REVISION = re.compile(r"[0-9a-f]{40}")
MARKER = "ladybird-update.json"
STATE = HERE / "status.json"
LOG = HERE / "update.log"
PAUSE = HERE / "skipped-revision"
NO_WINDOW = subprocess.CREATE_NO_WINDOW
WINDOWS = Path(os.environ["SystemRoot"])
TASKLIST = WINDOWS / "System32/tasklist.exe"
POWERSHELL = WINDOWS / "System32/WindowsPowerShell/v1.0/powershell.exe"


class UpdateError(RuntimeError):
    pass


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def state(message, **values):
    try:
        previous = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    phase = values.get("phase")
    if phase == "checking":
        values.update(update_started=timestamp, update_finished=None)
    elif phase in {"pending", "done", "error"} and previous.get("update_started") and not previous.get("update_finished"):
        values["update_finished"] = timestamp
    if previous.get("message") == message and all(previous.get(key) == value for key, value in values.items()):
        return
    previous.update(values, message=message, timestamp=timestamp)
    write_json(STATE, previous)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"{previous['timestamp']} {message}\n")


@contextmanager
def exclusive():
    with (HERE / "worker.lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def command(arguments, timeout=120, cwd=None):
    result = subprocess.run(
        [str(argument) for argument in arguments], cwd=cwd, timeout=timeout,
        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
    )
    if result.returncode:
        tail = (result.stderr or result.stdout).strip()[-1800:]
        raise UpdateError(f"Command failed ({result.returncode}). {tail}")
    return result.stdout


def git(*arguments):
    return command([
        GIT, "-c", f"safe.directory={SOURCE.as_posix()}", "-c", "core.longpaths=true",
        "-c", "http.sslBackend=openssl", "-c", "credential.interactive=false",
        "-c", "core.hooksPath=NUL", "-C", SOURCE, *arguments,
    ]).strip()


def official_revision():
    value = git("ls-remote", "--exit-code", REPOSITORY, REF).split()
    if len(value) != 2 or value[1] != REF or not REVISION.fullmatch(value[0]):
        raise UpdateError("The official repository did not return a usable revision.")
    return value[0]


def update_sources(revision):
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise UpdateError("The build checkout contains local changes.")
    if git("rev-parse", "HEAD") == revision:
        return
    git("fetch", "--no-tags", "--deepen=256", REPOSITORY, REF)
    if git("rev-parse", "FETCH_HEAD") != revision:
        raise UpdateError("The upstream branch changed during the download. Try again.")
    git("merge", "--ff-only", "FETCH_HEAD")


def validate_runtime(path):
    for relative in ("bin/Ladybird.exe", "bin/RequestServer.exe", "bin/WebContent.exe",
                     "bin/platforms/qwindows.dll", "share/Lagom"):
        if not (path / relative).exists():
            raise UpdateError(f"Missing Ladybird file. {relative}")


def runtime_revision(path):
    value = json.loads((path / MARKER).read_text(encoding="utf-8"))["revision"]
    if not isinstance(value, str) or not REVISION.fullmatch(value):
        raise UpdateError("The installed Ladybird revision is unreadable.")
    return value


def smoke(runtime):
    validate_runtime(runtime)
    exe = runtime / "bin/Ladybird.exe"
    marker = "Ladybird updater rendering test"
    checks = (
        ("data:text/html," + quote(f"<!doctype html><h1>{marker}</h1>"), marker),
        ("https://ladybird.org/", "independent"),
    )
    for url, expected in checks:
        with TemporaryDirectory(prefix="test-", dir=HERE) as profile:
            output = command([exe, "--profile-path", profile, "--headless=text", url], timeout=90, cwd=exe.parent)
        if expected not in output:
            raise UpdateError(f"The test page did not render correctly. {url}")


def browser_open():
    output = command([TASKLIST, "/FI", "IMAGENAME eq Ladybird.exe", "/FO", "CSV", "/NH"], timeout=20)
    return any(line.casefold().startswith('"ladybird.exe",') for line in output.splitlines())


def notify(message):
    try:
        subprocess.Popen(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-File", str(HERE / "notify.ps1"), "-Message", message],
            creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


class Installation:
    def __init__(self, live):
        self.live = live
        self.previous = live.with_name(live.name + ".previous")
        self.pending = live.with_name(live.name + ".pending")
        self.rollback_swap = live.with_name(live.name + ".rollback")
        self.staging = live.with_name(live.name + ".staging")

    def remove(self, path):
        if not path.exists():
            return
        allowed = {self.previous, self.pending, self.staging}
        if path not in allowed or path.resolve() != path.absolute():
            raise UpdateError("An updater working directory was redirected.")
        shutil.rmtree(path)

    def recover(self):
        if not self.live.exists() and self.previous.exists():
            self.previous.rename(self.live)
        if self.rollback_swap.exists() and self.live.exists() and not self.previous.exists():
            self.rollback_swap.rename(self.previous)
        validate_runtime(self.live)

    def stage(self, runtime, revision):
        self.remove(self.staging)
        shutil.copytree(runtime / "bin", self.staging / "bin",
                        ignore=shutil.ignore_patterns("*.pdb", "*.ilk", "*.lib", "*.exp", "vc_redist.*"))
        shutil.copytree(runtime / "share", self.staging / "share")
        smoke(self.staging)
        write_json(self.staging / MARKER, {"revision": revision})
        self.remove(self.pending)
        self.staging.rename(self.pending)

    def apply(self):
        if not self.pending.exists():
            return None
        validate_runtime(self.pending)
        revision = runtime_revision(self.pending)
        if browser_open():
            state("Update ready. Close Ladybird to install it.", phase="pending")
            return None
        self.remove(self.previous)
        self.live.rename(self.previous)
        try:
            self.pending.rename(self.live)
        except OSError:
            self.previous.rename(self.live)
            raise
        state(f"Ladybird was updated ({revision[:8]}).", phase="done", installed=revision)
        notify("Ladybird was updated.")
        return revision

    def rollback(self):
        if browser_open():
            raise UpdateError("Close Ladybird before restoring the previous version.")
        validate_runtime(self.previous)
        removed = runtime_revision(self.live)
        restored = runtime_revision(self.previous)
        PAUSE.write_text(removed, encoding="ascii")
        self.live.rename(self.rollback_swap)
        try:
            self.previous.rename(self.live)
        except OSError:
            self.rollback_swap.rename(self.live)
            raise
        self.rollback_swap.rename(self.previous)
        self.remove(self.pending)
        state(f"Previous version restored ({restored[:8]}).", phase="done", installed=restored)


def check(installation):
    revision = official_revision()
    installed = runtime_revision(installation.live)
    checked = datetime.now().astimezone().isoformat(timespec="seconds")
    state("Upstream revision checked.", installed=installed, checked=checked, available=revision)
    if revision == installed:
        installation.remove(installation.pending)
        state("Ladybird is up to date.", phase="done", failure=None)
        return
    if PAUSE.exists() and PAUSE.read_text(encoding="ascii") == revision:
        state("The revision you rolled back is skipped until upstream advances.", phase="done")
        return
    if installation.pending.exists() and runtime_revision(installation.pending) == revision:
        installation.apply()
        return
    state(f"Building revision {revision[:8]}.", phase="building")
    update_sources(revision)
    with (HERE / "build-launch.log").open("w", encoding="utf-8") as log:
        result = subprocess.run([str(PYTHON), str(BUILD)], stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    if result.returncode:
        raise UpdateError("The Windows build failed. See the build log.")
    if git("rev-parse", "HEAD") != revision:
        raise UpdateError("The source checkout changed during the build.")
    state("Testing page rendering and HTTPS.", phase="testing")
    installation.stage(SOURCE / "Build/release", revision)
    state("The new revision passed its tests.", phase="testing", failure=None)
    installation.apply()


def worker(action, installation):
    if action == "apply" and not any(path.exists() for path in (installation.pending, installation.rollback_swap)) and installation.live.exists():
        return 0
    with exclusive() as acquired:
        if not acquired:
            return 0
        try:
            installation.recover()
            if action == "apply":
                installation.apply()
            elif action == "rollback":
                installation.rollback()
            else:
                state("Checking for updates…", phase="checking")
                check(installation)
        except (OSError, ValueError, KeyError, UpdateError, subprocess.SubprocessError) as error:
            failure = f"Update failed. {error}"
            state(failure, phase="error", failure=failure)
            notify("Ladybird Updater encountered an error. Open the app for details.")
            return 1
    return 0


class BuildLogReader:
    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.header = b""
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def read(self):
        try:
            with self.path.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                header = stream.read(128)
                reset = size < self.offset or not header.startswith(self.header)
                if reset:
                    self.offset = 0
                    self.decoder.reset()
                if self.offset == 0 and size > 96 * 1024:
                    stream.seek(size - 96 * 1024)
                    stream.readline(96 * 1024)
                    self.offset = stream.tell()
                stream.seek(self.offset)
                data = stream.read(128 * 1024)
                self.offset = stream.tell()
                self.header = header
        except OSError:
            return False, ""
        return reset, self.decoder.decode(data).replace("\r", "")


def elapsed_label(report, running, now=None):
    started = report.get("update_started")
    if not started:
        return "Elapsed time  —"
    try:
        start = datetime.fromisoformat(started)
        if running:
            end = now or datetime.now().astimezone()
        else:
            end = datetime.fromisoformat(report.get("update_finished") or report["timestamp"])
        seconds = max(0, int((end - start).total_seconds()))
    except (KeyError, TypeError, ValueError):
        return ""
    hours, remaining = divmod(seconds, 3600)
    minutes, seconds = divmod(remaining, 60)
    title = "Elapsed time" if running else "Last attempt duration"
    return f"{title}  {hours:02d}:{minutes:02d}:{seconds:02d}"


def desktop(installation):
    import tkinter as tk
    from tkinter import ttk

    ctypes.windll.user32.SetProcessDPIAware()
    root = tk.Tk()
    root.title("Ladybird Updater")
    root.geometry("950x635")
    root.minsize(670, 460)
    outer = ttk.Frame(root, padding=24)
    outer.pack(fill="both", expand=True)
    ttk.Label(outer, text="Ladybird Updater", font=("Segoe UI", 17, "bold")).pack(anchor="w")
    summary = tk.StringVar()
    details = tk.StringVar()
    elapsed = tk.StringVar()
    ttk.Label(outer, textvariable=summary, wraplength=600).pack(anchor="w", pady=(14, 6))
    ttk.Label(outer, textvariable=details, wraplength=600).pack(anchor="w")
    ttk.Label(outer, textvariable=elapsed, font=("Consolas", 10)).pack(anchor="w", pady=(4, 0))
    controls = ttk.Frame(outer)
    controls.pack(anchor="w", pady=(12, 0))

    def launch(action):
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--action", action], creationflags=NO_WINDOW)

    check_button = ttk.Button(controls, text="Check now", command=lambda: launch("check"))
    check_button.pack(side="left")
    rollback_button = ttk.Button(controls, text="Restore previous version", command=lambda: launch("rollback"))
    rollback_button.pack(side="left", padx=(8, 0))
    ttk.Button(controls, text="Open logs", command=lambda: os.startfile(HERE)).pack(side="left", padx=(8, 0))

    console_frame = ttk.Frame(outer)
    console_frame.pack(fill="both", expand=True, pady=(16, 0))
    console_frame.rowconfigure(0, weight=1)
    console_frame.columnconfigure(0, weight=1)
    console = tk.Text(
        console_frame, wrap="none", state="disabled", height=10, width=1,
        background="#171a20", foreground="#e7ecf3", selectbackground="#37577f",
        font=("Consolas", 10), padx=10, pady=8, borderwidth=0,
    )
    vertical = ttk.Scrollbar(console_frame, orient="vertical", command=console.yview)
    horizontal = ttk.Scrollbar(console_frame, orient="horizontal", command=console.xview)
    console.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    console.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    reader = BuildLogReader(HERE / "build.log")

    def refresh_console():
        reset, output = reader.read()
        if reset or output:
            follow = reset or console.yview()[1] >= 0.995
            console.configure(state="normal")
            if reset:
                console.delete("1.0", "end")
            if output:
                console.insert("end", output)
                line_count = int(console.index("end-1c").split(".")[0])
                if line_count > 2400:
                    console.delete("1.0", f"{line_count - 2400 + 1}.0")
            console.configure(state="disabled")
            if follow:
                console.see("end")
        root.after(850, refresh_console)

    def refresh():
        try:
            report = json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
        message = report.get("message", "Ready to check for updates.")
        failure = report.get("failure")
        if failure and failure != message:
            message += f"\n\nLast error — {failure}"
        summary.set(message)
        installed = report.get("installed", "–")[:8]
        checked = report.get("checked")
        checked_label = datetime.fromisoformat(checked).astimezone().strftime("%Y-%m-%d %H:%M") if checked else "–"
        details.set(f"Installed revision  {installed}\nLast checked  {checked_label}")
        busy = report.get("phase") in {"checking", "building", "testing"}
        if busy:
            with exclusive() as acquired:
                busy = not acquired
        elapsed.set(elapsed_label(report, running=busy))
        check_button.configure(state="disabled" if busy else "normal")
        rollback_button.configure(state="normal" if installation.previous.exists() and not busy else "disabled")
        root.after(1000, refresh)

    refresh()
    refresh_console()
    if "--ui-test" in sys.argv:
        assert elapsed.get()
        log_file = HERE / "build.log"
        if log_file.exists() and log_file.stat().st_size > 0:
            assert console.get("1.0", "end-1c").strip()
        root.after(800, root.destroy)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("check", "apply", "rollback", "verify"))
    parser.add_argument("--ui-test", action="store_true")
    args = parser.parse_args()
    installation = Installation(Path(CONFIG["installation"]))
    if args.action == "verify":
        smoke(installation.live)
        return 0
    if args.action:
        return worker(args.action, installation)
    desktop(installation)
    return 0


if __name__ == "__main__":
    sys.exit(main())








