"""Met à jour le journal de travail (work-log/) à partir de l'API WakaTime.

Lancé chaque nuit par la GitHub Action `work-log.yml`. Récupère les N derniers
jours (par défaut 7, le plan gratuit WakaTime ne garde que 14 jours), regroupe
les durées en plages de travail et régénère un fichier Markdown par mois.

Variables d'environnement :
  WAKATIME_API_KEY   clé API WakaTime (secret du dépôt)
  WORK_LOG_DAYS      nombre de jours à resynchroniser (défaut 7)
  WORK_LOG_TZ        fuseau horaire d'affichage (défaut Africa/Douala)
  WORK_LOG_HIDE      projets à masquer, séparés par des virgules
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://wakatime.com/api/v1/users/current"
ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "work-log"
DATA_FILE = LOG_DIR / "data.json"
SESSION_GAP_MIN = 15  # deux durées séparées de moins de 15 min = même plage

TZ = ZoneInfo(os.environ.get("WORK_LOG_TZ", "Africa/Douala"))
HIDDEN = {p.strip().lower() for p in os.environ.get("WORK_LOG_HIDE", "").split(",") if p.strip()}
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
          "août", "septembre", "octobre", "novembre", "décembre"]
WEEKDAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


def api_get(path: str, **params: str) -> dict:
    key = os.environ["WAKATIME_API_KEY"].strip()
    query = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{API}/{path}?{query}")
    req.add_header("Authorization", "Basic " + base64.b64encode(key.encode()).decode())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def project_name(name: str) -> str:
    return "(projet privé)" if name.lower() in HIDDEN else name


def fetch_day(day: date) -> dict:
    """Total, répartition par projet/langage et plages de travail d'une journée."""
    summary = api_get("summaries", start=day.isoformat(), end=day.isoformat())["data"][0]
    durations = api_get("durations", date=day.isoformat())["data"]

    sessions: list[dict] = []
    for d in sorted(durations, key=lambda d: d["time"]):
        start, end = d["time"], d["time"] + d["duration"]
        proj = project_name(d.get("project") or "inconnu")
        if sessions and start - sessions[-1]["end"] <= SESSION_GAP_MIN * 60:
            last = sessions[-1]
            last["end"] = max(last["end"], end)
            if proj not in last["projects"]:
                last["projects"].append(proj)
        else:
            sessions.append({"start": start, "end": end, "projects": [proj]})

    projects: dict[str, float] = {}
    for p in summary["projects"]:
        name = project_name(p["name"])
        projects[name] = projects.get(name, 0) + p["total_seconds"]

    return {
        "total_seconds": summary["grand_total"]["total_seconds"],
        "projects": projects,
        "languages": {l["name"]: l["total_seconds"] for l in summary["languages"][:5]},
        "sessions": [
            {
                "start": datetime.fromtimestamp(s["start"], TZ).strftime("%H:%M"),
                "end": datetime.fromtimestamp(s["end"], TZ).strftime("%H:%M"),
                "projects": s["projects"],
            }
            for s in sessions
            if s["end"] - s["start"] >= 60  # ignore les plages de moins d'une minute
        ],
    }


def fmt(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60:02d}" if minutes >= 60 else f"{minutes} min"


def render_month(month: str, days: dict[str, dict]) -> str:
    year, m = map(int, month.split("-"))
    total = sum(d["total_seconds"] for d in days.values())
    worked = [k for k, d in days.items() if d["total_seconds"] > 0]

    by_project: dict[str, float] = {}
    for d in days.values():
        for p, s in d["projects"].items():
            by_project[p] = by_project.get(p, 0) + s

    lines = [
        f"# Journal de travail — {MONTHS[m - 1]} {year}",
        "",
        f"**Total : {fmt(total)}** sur {len(worked)} jour(s) travaillé(s)"
        + (f" · moyenne {fmt(total / len(worked))} / jour" if worked else ""),
        "",
        "_Généré automatiquement depuis WakaTime (temps de code dans VS Code)._",
        "",
        "## Par projet",
        "",
        "| Projet | Temps |",
        "|---|---|",
        *[f"| {p} | {fmt(s)} |" for p, s in sorted(by_project.items(), key=lambda x: -x[1])],
        "",
        "## Par jour",
        "",
        "| Jour | Temps | Plages de travail | Projets |",
        "|---|---|---|---|",
    ]
    for key in sorted(days, reverse=True):
        d = days[key]
        if d["total_seconds"] <= 0:
            continue
        day = date.fromisoformat(key)
        periods = ", ".join(f"{s['start']}–{s['end']}" for s in d["sessions"]) or "—"
        projects = ", ".join(p for p, _ in sorted(d["projects"].items(), key=lambda x: -x[1]))
        lines.append(f"| {WEEKDAYS[day.weekday()]} {day:%d/%m} | {fmt(d['total_seconds'])} | {periods} | {projects} |")
    return "\n".join(lines) + "\n"


def render_index(data: dict[str, dict]) -> str:
    months: dict[str, float] = {}
    for key, d in data.items():
        months[key[:7]] = months.get(key[:7], 0) + d["total_seconds"]
    lines = [
        "# Journal de travail",
        "",
        "Temps passé à coder dans VS Code, mesuré par [WakaTime](https://wakatime.com) "
        "et mis à jour chaque nuit par une GitHub Action.",
        "",
        "| Mois | Temps total |",
        "|---|---|",
    ]
    for month in sorted(months, reverse=True):
        y, m = map(int, month.split("-"))
        lines.append(f"| [{MONTHS[m - 1]} {y}]({month}.md) | {fmt(months[month])} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    data: dict[str, dict] = json.loads(DATA_FILE.read_text("utf-8")) if DATA_FILE.exists() else {}

    today = datetime.now(TZ).date()
    for i in range(int(os.environ.get("WORK_LOG_DAYS", "7"))):
        day = today - timedelta(days=i)
        data[day.isoformat()] = fetch_day(day)
        print(f"{day}: {fmt(data[day.isoformat()]['total_seconds'])}")

    data = {k: data[k] for k in sorted(data)}
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")

    for month in {k[:7] for k in data}:
        days = {k: v for k, v in data.items() if k.startswith(month)}
        (LOG_DIR / f"{month}.md").write_text(render_month(month, days), "utf-8")
    (LOG_DIR / "README.md").write_text(render_index(data), "utf-8")


if __name__ == "__main__":
    main()
