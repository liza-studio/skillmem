"""Scheduled maintenance: ``skillmem schedule install|remove|status``.

Sets up two recurring jobs without manual launchd/schtasks/cron plumbing:
  - decay:  daily 04:15 — `skillmem decay --days 14` (Ebbinghaus + lifecycle)
  - export: weekly Sun 04:30 — `skillmem export-all <data>/backups/vault`

Per-platform backends:
  darwin -> launchd user agents (~/Library/LaunchAgents/com.skillmem.*.plist)
  win32  -> schtasks /Create /SC ... (tasks SkillMem\\Decay, SkillMem\\Export)
  linux  -> systemd user timers (~/.config/systemd/user/skillmem-*.{service,timer})
            when `systemctl --user` works; otherwise crontab -l | crontab -
            (lines marked with "# skillmem:"); neither -> explicit error

Offsite backups (ssh etc.) are deliberately out of scope: that is personal
infrastructure, not the product.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import click

from . import storage as S

DECAY_TIME = (4, 15)
EXPORT_TIME = (4, 30)
EXPORT_WEEKDAY = 0  # Sunday (launchd: 0=Sunday; cron: 0=Sunday; schtasks: SUN)

_LAUNCHD_LABELS = {"decay": "com.skillmem.decay", "export": "com.skillmem.export"}
_WIN_TASKS = {"decay": r"SkillMem\Decay", "export": r"SkillMem\Export"}
_CRON_MARK = "# skillmem:"
_SYSTEMD_UNITS = {"decay": "skillmem-decay", "export": "skillmem-export"}


def _skillmem_bin() -> Path:
    """The skillmem binary next to the current interpreter (venv-safe)."""
    exe = "skillmem.exe" if sys.platform == "win32" else "skillmem"
    return Path(sys.executable).parent / exe


def _jobs() -> dict[str, list[str]]:
    bin_ = str(_skillmem_bin())
    vault = str(S.default_data_dir() / "backups" / "vault")
    return {
        "decay": [bin_, "decay", "--days", "14"],
        "export": [bin_, "export-all", vault],
    }


def _log_dir() -> Path:
    d = S.default_data_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# darwin: launchd
# --------------------------------------------------------------------------- #

def _launchd_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _launchd_install() -> list[str]:
    done = []
    for name, argv in _jobs().items():
        label = _LAUNCHD_LABELS[name]
        cal: dict[str, int] = {
            "Hour": (DECAY_TIME if name == "decay" else EXPORT_TIME)[0],
            "Minute": (DECAY_TIME if name == "decay" else EXPORT_TIME)[1],
        }
        if name == "export":
            cal["Weekday"] = EXPORT_WEEKDAY
        plist = {
            "Label": label,
            "ProgramArguments": argv,
            "StartCalendarInterval": cal,
            "RunAtLoad": False,
            "StandardOutPath": str(_log_dir() / f"{name}.log"),
            "StandardErrorPath": str(_log_dir() / f"{name}.err"),
        }
        path = _launchd_plist_path(label)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(plist))
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        subprocess.run(["launchctl", "load", str(path)], capture_output=True)
        done.append(f"{label} -> {path}")
    return done


def _launchd_remove() -> list[str]:
    done = []
    for label in _LAUNCHD_LABELS.values():
        path = _launchd_plist_path(label)
        if path.exists():
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
            path.unlink()
            done.append(f"removed {label}")
    return done


def _launchd_status() -> list[str]:
    out = []
    for label in _LAUNCHD_LABELS.values():
        path = _launchd_plist_path(label)
        loaded = subprocess.run(
            ["launchctl", "list", label], capture_output=True
        ).returncode == 0
        out.append(f"{label}: plist={'yes' if path.exists() else 'no'} loaded={'yes' if loaded else 'no'}")
    return out


# --------------------------------------------------------------------------- #
# win32: schtasks
# --------------------------------------------------------------------------- #

def _win_tr(argv: list[str]) -> str:
    """Command string for /TR: paths with spaces go in inner quotes."""
    quoted = [f'"{a}"' if " " in a else a for a in argv]
    return " ".join(quoted)


def _schtasks_install() -> list[str]:
    done = []
    jobs = _jobs()
    specs = {
        "decay": ["/SC", "DAILY", "/ST", f"{DECAY_TIME[0]:02d}:{DECAY_TIME[1]:02d}"],
        "export": ["/SC", "WEEKLY", "/D", "SUN",
                   "/ST", f"{EXPORT_TIME[0]:02d}:{EXPORT_TIME[1]:02d}"],
    }
    for name, argv in jobs.items():
        task = _WIN_TASKS[name]
        cmd = ["schtasks", "/Create", "/F", "/TN", task, "/TR", _win_tr(argv), *specs[name]]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise click.ClickException(
                f"schtasks failed for {task}: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        done.append(f"{task} ({' '.join(specs[name])})")
    return done


def _schtasks_remove() -> list[str]:
    done = []
    for task in _WIN_TASKS.values():
        proc = subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", task], capture_output=True, text=True
        )
        if proc.returncode == 0:
            done.append(f"removed {task}")
    return done


def _schtasks_status() -> list[str]:
    out = []
    for task in _WIN_TASKS.values():
        exists = subprocess.run(
            ["schtasks", "/Query", "/TN", task], capture_output=True
        ).returncode == 0
        out.append(f"{task}: {'scheduled' if exists else 'absent'}")
    return out


# --------------------------------------------------------------------------- #
# linux: crontab
# --------------------------------------------------------------------------- #

def _cron_read() -> list[str]:
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        # No crontab binary at all (containers, systemd-only minimal distros):
        # an empty crontab, not a traceback. Install still fails loudly via
        # _cron_write / backend selection.
        return []
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def _cron_write(lines: list[str]) -> None:
    text = "\n".join(lines) + ("\n" if lines else "")
    try:
        proc = subprocess.run(
            ["crontab", "-"], input=text, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            "crontab binary not found — install cron, or use systemd user timers"
        ) from exc
    if proc.returncode != 0:
        raise click.ClickException(f"crontab write failed: {proc.stderr.strip()}")


def _cron_install() -> list[str]:
    jobs = _jobs()

    def q(argv: list[str]) -> str:
        return " ".join(f'"{a}"' if " " in a else a for a in argv)

    entries = [
        f"{DECAY_TIME[1]} {DECAY_TIME[0]} * * * {q(jobs['decay'])} "
        f">> {_log_dir() / 'decay.log'} 2>&1 {_CRON_MARK}decay",
        f"{EXPORT_TIME[1]} {EXPORT_TIME[0]} * * {EXPORT_WEEKDAY} {q(jobs['export'])} "
        f">> {_log_dir() / 'export.log'} 2>&1 {_CRON_MARK}export",
    ]
    kept = [l for l in _cron_read() if _CRON_MARK not in l]
    _cron_write(kept + entries)
    return entries


def _cron_remove() -> list[str]:
    current = _cron_read()
    kept = [l for l in current if _CRON_MARK not in l]
    if len(kept) != len(current):
        _cron_write(kept)
        return [f"removed {len(current) - len(kept)} cron entries"]
    return []


def _cron_status() -> list[str]:
    ours = [l for l in _cron_read() if _CRON_MARK in l]
    return ours or ["no skillmem cron entries"]


# --------------------------------------------------------------------------- #
# linux: systemd user timers (default on modern distros)
# --------------------------------------------------------------------------- #

def _systemd_unit_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "systemd" / "user"


def _systemd_available() -> bool:
    """True when a per-user systemd instance is actually reachable."""
    if not shutil.which("systemctl"):
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True
        )
    except OSError:
        return False
    return proc.returncode == 0


def _systemd_run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True
    )


def _systemd_unit_texts(name: str, argv: list[str]) -> tuple[str, str]:
    """(service_text, timer_text) for one job."""
    descr = {
        "decay": "skillmem decay (daily memory maintenance)",
        "export": "skillmem export (weekly vault backup)",
    }[name]
    log = _log_dir() / f"{name}.log"
    service = (
        "[Unit]\n"
        f"Description={descr}\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={shlex.join(argv)}\n"
        f"StandardOutput=append:{log}\n"
        f"StandardError=append:{log}\n"
    )
    if name == "export":
        on_calendar = f"Sun *-*-* {EXPORT_TIME[0]:02d}:{EXPORT_TIME[1]:02d}:00"
    else:
        on_calendar = f"*-*-* {DECAY_TIME[0]:02d}:{DECAY_TIME[1]:02d}:00"
    timer = (
        "[Unit]\n"
        f"Description=Timer for {descr}\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={on_calendar}\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _systemd_install() -> list[str]:
    unit_dir = _systemd_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    done = []
    for name, argv in _jobs().items():
        unit = _SYSTEMD_UNITS[name]
        service, timer = _systemd_unit_texts(name, argv)
        (unit_dir / f"{unit}.service").write_text(service, encoding="utf-8")
        (unit_dir / f"{unit}.timer").write_text(timer, encoding="utf-8")
    proc = _systemd_run("daemon-reload")
    if proc.returncode != 0:
        raise click.ClickException(
            f"systemctl --user daemon-reload failed: {proc.stderr.strip()}"
        )
    for name in _jobs():
        unit = _SYSTEMD_UNITS[name]
        proc = _systemd_run("enable", "--now", f"{unit}.timer")
        if proc.returncode != 0:
            raise click.ClickException(
                f"could not enable {unit}.timer: {proc.stderr.strip()}"
            )
        done.append(f"{unit}.timer -> {unit_dir / (unit + '.timer')}")
    return done


def _systemd_remove() -> list[str]:
    unit_dir = _systemd_unit_dir()
    done = []
    reload_needed = False
    for unit in _SYSTEMD_UNITS.values():
        _systemd_run("disable", "--now", f"{unit}.timer")  # best-effort
        removed_any = False
        for suffix in (".timer", ".service"):
            path = unit_dir / f"{unit}{suffix}"
            if path.exists():
                path.unlink()
                removed_any = True
        if removed_any:
            reload_needed = True
            done.append(f"removed {unit}")
    if reload_needed:
        _systemd_run("daemon-reload")
    return done


def _systemd_status() -> list[str]:
    unit_dir = _systemd_unit_dir()
    out = []
    for unit in _SYSTEMD_UNITS.values():
        present = (unit_dir / f"{unit}.timer").exists()
        active = _systemd_run("is-active", f"{unit}.timer").returncode == 0
        out.append(
            f"{unit}.timer: unit={'yes' if present else 'no'} "
            f"active={'yes' if active else 'no'}"
        )
    return out


# --------------------------------------------------------------------------- #
# click group
# --------------------------------------------------------------------------- #

_BACKENDS = {
    "darwin": (_launchd_install, _launchd_remove, _launchd_status),
    "win32": (_schtasks_install, _schtasks_remove, _schtasks_status),
}


def _linux_backend():
    """systemd user timers when reachable, else cron, else a clear error."""
    if _systemd_available():
        return (_systemd_install, _systemd_remove, _systemd_status)
    if shutil.which("crontab"):
        return (_cron_install, _cron_remove, _cron_status)
    raise click.ClickException(
        "no scheduler available: neither `systemctl --user` responds nor a "
        "`crontab` binary exists. Install cron (or run under a systemd user "
        "session), or run the jobs manually:\n"
        "  skillmem decay --days 14        (daily)\n"
        "  skillmem export-all <dir>       (weekly)"
    )


def _backend():
    if sys.platform in _BACKENDS:
        return _BACKENDS[sys.platform]
    # linux and anything else POSIX-ish: pick by what actually works
    return _linux_backend()


@click.group(name="schedule")
def schedule_group() -> None:
    """Recurring jobs: decay (daily 04:15) + export (Sun 04:30)."""


@schedule_group.command("install")
def schedule_install() -> None:
    """Install/refresh the jobs for the current OS."""
    for line in _backend()[0]():
        click.echo(f"  + {line}")
    click.echo("Done. Check with: skillmem schedule status")


@schedule_group.command("remove")
def schedule_remove() -> None:
    """Remove skillmem's scheduled jobs."""
    removed = _backend()[1]()
    for line in removed:
        click.echo(f"  - {line}")
    if not removed:
        click.echo("Nothing to remove.")


@schedule_group.command("status")
def schedule_status() -> None:
    """Show job status."""
    for line in _backend()[2]():
        click.echo(f"  {line}")
