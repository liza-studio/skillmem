"""Scheduled maintenance: ``skillmem schedule install|remove|status``.

Ставит два регулярных задания без ручной возни с launchd/schtasks/cron:
  - decay:  ежедневно 04:15 — `skillmem decay --days 14` (Ebbinghaus + lifecycle)
  - export: еженедельно вс 04:30 — `skillmem export-all <data>/backups/vault`

Бэкенды по платформе:
  darwin -> launchd user agents (~/Library/LaunchAgents/com.skillmem.*.plist)
  win32  -> schtasks /Create /SC ... (задачи SkillMem\Decay, SkillMem\\Export)
  linux  -> crontab -l | crontab - (маркер "# skillmem:" на строках)

Оффсайт-бэкапы (ssh и т.п.) — сознательно вне пакета: это личная
инфраструктура, не продукт.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import click

from . import storage as S

DECAY_TIME = (4, 15)
EXPORT_TIME = (4, 30)
EXPORT_WEEKDAY = 0  # воскресенье (launchd: 0=Sunday; cron: 0=Sunday; schtasks: SUN)

_LAUNCHD_LABELS = {"decay": "com.skillmem.decay", "export": "com.skillmem.export"}
_WIN_TASKS = {"decay": r"SkillMem\Decay", "export": r"SkillMem\Export"}
_CRON_MARK = "# skillmem:"


def _skillmem_bin() -> Path:
    """Бинарь skillmem рядом с текущим интерпретатором (venv-safe)."""
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
    """Команда для /TR: пути с пробелами — во внутренних кавычках."""
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
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def _cron_write(lines: list[str]) -> None:
    text = "\n".join(lines) + ("\n" if lines else "")
    proc = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True)
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
# click group
# --------------------------------------------------------------------------- #

_BACKENDS = {
    "darwin": (_launchd_install, _launchd_remove, _launchd_status),
    "win32": (_schtasks_install, _schtasks_remove, _schtasks_status),
    "linux": (_cron_install, _cron_remove, _cron_status),
}


def _backend():
    key = sys.platform if sys.platform in _BACKENDS else "linux"
    return _BACKENDS[key]


@click.group(name="schedule")
def schedule_group() -> None:
    """Регулярные задания: decay (ежедневно 04:15) + export (вс 04:30)."""


@schedule_group.command("install")
def schedule_install() -> None:
    """Поставить/обновить задания под текущую ОС."""
    for line in _backend()[0]():
        click.echo(f"  + {line}")
    click.echo("Готово. Проверка: skillmem schedule status")


@schedule_group.command("remove")
def schedule_remove() -> None:
    """Снять задания skillmem."""
    removed = _backend()[1]()
    for line in removed:
        click.echo(f"  - {line}")
    if not removed:
        click.echo("Nothing to remove.")


@schedule_group.command("status")
def schedule_status() -> None:
    """Показать состояние заданий."""
    for line in _backend()[2]():
        click.echo(f"  {line}")
