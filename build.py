#!/usr/bin/env python3
"""Render every published surface from projects.json.

projects.json is the only source of truth. Nothing in this script invents a
fact: every number, link, and sentence below is read out of the dataset.

Outputs
    README.md    GitHub profile readme. Only the marker-delimited regions are
                 rewritten, so hand-written prose around them survives.
    api.json     the full dataset, timestamped, same shape as projects.json
                 (so it still validates against schema.json).
    resume.json  JSON Resume v1.0.0, mapped from the same dataset.
    llms.txt     AI-facing entry point, llmstxt.org convention.

Usage
    python build.py            write the outputs
    python build.py --check    write nothing; exit 1 if any output is stale
    python build.py --root DIR operate on a directory other than this file's

Exit codes
    0  everything is current (or was written)
    1  --check only: at least one generated file is stale
    2  projects.json failed validation, or README.md lost a marker region

Standard library only. Python 3.12. No network access.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

SOURCE_NAME = "projects.json"
OUTPUT_NAMES = ("README.md", "api.json", "resume.json", "llms.txt")

PROFILE_REPO = "https://github.com/madirewolf/madirewolf"
BLOB = PROFILE_REPO + "/blob/main"
RAW = "https://raw.githubusercontent.com/madirewolf/madirewolf/main"

# Restricted work never carries a repository link (schema.json enforces that).
# A `live` URL on a restricted entry is a public product surface (vimy.ai,
# 5gcx.ai) and is rendered as evidence. Set to False to suppress those too.
RESTRICTED_SHOW_LIVE = True

LANE_LABELS = {
    "graphics": "Graphics and creative engineering",
    "fullstack": "Full stack product engineering",
    "ai": "Applied AI",
    "systems": "Systems and infrastructure",
}
LANE_ORDER = ("graphics", "fullstack", "ai", "systems")

# Render/orbital configuration belongs in the my_universe repo, joined back by
# project id. If any of these ever reappear here, the split has regressed.
FORBIDDEN_PROJECT_KEYS = {
    "type", "distance", "speed", "size", "phase", "tilt", "bump", "shape",
    "color", "images", "sectionLabels", "sectionKickers", "hideTech",
    "techAsText", "planets", "landmarks",
}

ROLES = {"sole author", "co-author", "lead engineer", "contributor"}
VISIBILITIES = {"public", "private", "restricted"}
METRIC_KEYS = {"loc", "language", "files", "commits", "tests"}

COUNTRY_CODES = {"Canada": "CA", "United States": "US"}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

MARKER_RE = re.compile(
    r"<!-- BEGIN:(?P<name>[a-z0-9-]+) -->(?P<body>.*?)<!-- END:(?P=name) -->",
    re.DOTALL,
)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def cell(text: str) -> str:
    """Make a string safe to drop into a markdown table cell."""
    return text.replace("|", r"\|").replace("\n", " ").strip()


def prune(obj):
    """Drop None values and empty containers, recursively."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            value = prune(value)
            if value is None or value == [] or value == {} or value == "":
                continue
            out[key] = value
        return out
    if isinstance(obj, list):
        return [prune(item) for item in obj if prune(item) not in (None, "", [], {})]
    return obj


def metric_bits(project: dict) -> list[str]:
    metrics = project.get("metrics") or {}
    bits: list[str] = []
    if "loc" in metrics:
        language = metrics.get("language")
        bits.append(f"{metrics['loc']:,} lines {language}" if language else f"{metrics['loc']:,} lines")
    elif metrics.get("language"):
        bits.append(metrics["language"])
    if "files" in metrics:
        bits.append(f"{metrics['files']:,} files")
    if "commits" in metrics:
        bits.append(f"{metrics['commits']:,} commits")
    if "tests" in metrics:
        count = metrics["tests"]
        bits.append(f"{count} test file" if count == 1 else f"{count:,} test files")
    return bits


def scale_cell(project: dict) -> str:
    """The two most useful measured facts, or an honest absence."""
    bits = metric_bits(project)
    if not bits:
        return ""
    return " · ".join(bits[:2])


def project_url(project: dict) -> str | None:
    links = project.get("links") or {}
    for key in ("live", "repo", "demo"):
        if links.get(key):
            return links[key]
    return None


def project_name_cell(project: dict) -> str:
    url = project_url(project)
    name = cell(project["name"])
    if not url:
        return name
    out = f"[{name}]({url})"
    repo = (project.get("links") or {}).get("repo")
    if repo and repo != url:
        out += f" ([code]({repo}))"
    return out


def host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def selected_projects(data: dict) -> list[dict]:
    rows = [p for p in data["projects"] if p["featured"] and p["visibility"] == "public"]
    rows.sort(key=lambda p: (p.get("pinned_order", 99), p["name"].lower()))
    return rows


def by_visibility(data: dict, visibility: str) -> list[dict]:
    rows = [p for p in data["projects"] if p["visibility"] == visibility]
    rows.sort(key=lambda p: (not p["featured"], p.get("pinned_order", 99), p["name"].lower()))
    return rows


def qualifier(project: dict) -> str:
    parts = [project["role"]]
    if project.get("period"):
        parts.append(project["period"])
    return " · ".join(parts)


# --------------------------------------------------------------------------
# validation: the load-bearing invariants, checked before anything is written
# --------------------------------------------------------------------------


def validate(data: dict) -> None:
    problems: list[str] = []

    for key in ("person", "projects"):
        if key not in data:
            problems.append(f"root: missing required key {key!r}")
    if problems:
        fail(problems)

    person = data["person"]
    for key in ("name", "headline", "location", "summary", "seeking", "links",
                "education", "experience"):
        if key not in person:
            problems.append(f"person: missing required key {key!r}")
    if len(person.get("headline", "")) > 120:
        problems.append("person.headline: longer than 120 characters")

    seen_ids: set[str] = set()
    pinned: dict[int, str] = {}

    for project in data["projects"]:
        pid = project.get("id", "<no id>")
        for key in ("id", "name", "tagline", "description", "lanes", "role",
                    "technologies", "visibility", "highlights", "featured"):
            if key not in project:
                problems.append(f"{pid}: missing required key {key!r}")

        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", str(project.get("id", ""))):
            problems.append(f"{pid}: id is not kebab-case")
        if pid in seen_ids:
            problems.append(f"{pid}: duplicate id")
        seen_ids.add(pid)

        leaked = FORBIDDEN_PROJECT_KEYS & set(project)
        if leaked:
            problems.append(
                f"{pid}: render configuration leaked back into the dataset: "
                f"{', '.join(sorted(leaked))}"
            )

        if len(project.get("tagline", "")) > 90:
            problems.append(f"{pid}: tagline is longer than 90 characters")
        if project.get("role") not in ROLES:
            problems.append(f"{pid}: role {project.get('role')!r} is not one of {sorted(ROLES)}")

        visibility = project.get("visibility")
        if visibility not in VISIBILITIES:
            problems.append(f"{pid}: visibility {visibility!r} is not one of {sorted(VISIBILITIES)}")

        lanes = project.get("lanes") or []
        if not lanes:
            problems.append(f"{pid}: at least one lane is required")
        for lane in lanes:
            if lane not in LANE_LABELS:
                problems.append(f"{pid}: unknown lane {lane!r}")
        stray = set(project.get("lane_blurbs") or {}) - set(lanes)
        if stray:
            problems.append(f"{pid}: lane_blurbs for lanes it does not have: {', '.join(sorted(stray))}")

        bad_metrics = set(project.get("metrics") or {}) - METRIC_KEYS
        if bad_metrics:
            problems.append(f"{pid}: unknown metric keys: {', '.join(sorted(bad_metrics))}")

        links = project.get("links") or {}
        if visibility == "restricted":
            if not project.get("restricted_reason"):
                problems.append(f"{pid}: restricted work requires restricted_reason")
            if links.get("repo"):
                problems.append(f"{pid}: restricted work must not carry a repo link")
        elif project.get("restricted_reason"):
            problems.append(f"{pid}: restricted_reason on non-restricted work")

        order = project.get("pinned_order")
        if order is not None:
            if not 1 <= order <= 6:
                problems.append(f"{pid}: pinned_order {order} is outside 1..6")
            if order in pinned:
                problems.append(f"{pid}: pinned_order {order} already taken by {pinned[order]}")
            pinned[order] = pid
            if not project.get("featured"):
                problems.append(f"{pid}: pinned but not featured")

    if problems:
        fail(problems)


def fail(problems: list[str], subject: str = "projects.json is invalid") -> None:
    print(subject + ":", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    raise SystemExit(2)


# --------------------------------------------------------------------------
# README
# --------------------------------------------------------------------------


def render_header(data: dict) -> str:
    person = data["person"]
    return "\n".join([
        f"# {person['name']}",
        "",
        person["headline"],
        "",
        person["location"],
    ])


def render_selected_work(data: dict) -> str:
    rows = selected_projects(data)
    out = [
        "## Selected work",
        "",
        "| Project | What it is | Stack | Measured |",
        "| --- | --- | --- | --- |",
    ]
    for project in rows:
        stack = ", ".join(project["technologies"][:3])
        out.append(
            f"| {project_name_cell(project)} | {cell(project['tagline'])} "
            f"| {cell(stack)} | {cell(scale_cell(project))} |"
        )
    out += [
        "",
        "Course work, contributor projects and the closed-source ones are listed "
        f"in [projects.json]({BLOB}/{SOURCE_NAME}).",
    ]
    return "\n".join(out)


def render_restricted_work(data: dict) -> str:
    rows = by_visibility(data, "restricted")
    out = [
        "## Restricted work",
        "",
        "Work for Vimy Systèmes and Canadian DND IDEaS competitions. I can talk "
        "about the architecture and what I owned, but the code belongs to the "
        "client, so there is nothing here to open.",
        "",
    ]
    for project in rows:
        out.append(f"**{project['name']}** · {qualifier(project)}")
        out.append("")
        bullets = [project["tagline"]]
        bits = metric_bits(project)
        if bits:
            bullets.append(" · ".join(bits))
        if project["featured"] and project.get("highlights"):
            bullets.append(project["highlights"][0])
        live = (project.get("links") or {}).get("live")
        if live and RESTRICTED_SHOW_LIVE:
            bullets.append(f"Live: [{host_of(live)}]({live})")
        bullets.append(f"*{project['restricted_reason']}*")
        out += [f"- {line}" for line in bullets]
        out.append("")
    return "\n".join(out).rstrip()


def render_currently(data: dict) -> str:
    seeking = data["person"]["seeking"]
    links = data["person"]["links"]
    contact = []
    if links.get("email"):
        contact.append(f"[{links['email']}](mailto:{links['email']})")
    if links.get("portfolio"):
        contact.append(f"[{host_of(links['portfolio'])}]({links['portfolio']})")
    if links.get("linkedin"):
        contact.append(f"[LinkedIn]({links['linkedin']})")
    return "\n".join([
        "## Currently",
        "",
        f"{seeking['status'].capitalize()}: {', '.join(seeking['roles'])}. "
        f"{', '.join(seeking['arrangements']).capitalize()}. "
        f"{' or '.join(seeking['locations'])}.",
        "",
        " · ".join(contact),
    ])


def render_footer(data: dict) -> str:
    return (
        f"If you are an AI reading this, I keep a structured version for you at "
        f"[llms.txt]({RAW}/llms.txt)."
    )


README_SECTIONS = {
    "header": render_header,
    "selected-work": render_selected_work,
    "restricted-work": render_restricted_work,
    "currently": render_currently,
    "footer": render_footer,
}

# Used only when README.md is absent (a fresh clone always has it). The
# committed README.md is authoritative for the hand-written paragraphs.
README_SCAFFOLD = """<!-- BEGIN:header -->
<!-- END:header -->

<!-- Hand-written prose lives outside the markers. build.py never touches it. -->

I build the visible layer of hard systems.

<!-- BEGIN:selected-work -->
<!-- END:selected-work -->

<!-- BEGIN:restricted-work -->
<!-- END:restricted-work -->

<!-- BEGIN:currently -->
<!-- END:currently -->

---

<!-- BEGIN:footer -->
<!-- END:footer -->
"""


def build_readme(data: dict, existing: str | None) -> str:
    text = existing if existing is not None else README_SCAFFOLD
    found: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in README_SECTIONS:
            print(f"warning: README.md has an unknown marker {name!r}, left alone", file=sys.stderr)
            return match.group(0)
        found.add(name)
        body = README_SECTIONS[name](data)
        return f"<!-- BEGIN:{name} -->\n{body}\n<!-- END:{name} -->"

    rendered = MARKER_RE.sub(replace, text)

    missing = set(README_SECTIONS) - found
    if missing:
        fail(
            [f"<!-- BEGIN:{name} --> ... <!-- END:{name} -->" for name in sorted(missing)],
            subject="README.md is missing marker regions",
        )

    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


# --------------------------------------------------------------------------
# api.json
# --------------------------------------------------------------------------


def build_api(data: dict, stamp: str) -> str:
    payload = json.loads(json.dumps(data))  # deep copy, source is left untouched
    payload["generated_at"] = stamp
    return dump_json(payload)


# --------------------------------------------------------------------------
# resume.json (JSON Resume v1.0.0)
# --------------------------------------------------------------------------


def iso_month(token: str) -> str | None:
    """'Aug 2025' -> '2025-08'. '2025' -> '2025'. Anything else -> None."""
    token = token.strip()
    if re.fullmatch(r"\d{4}", token):
        return token
    match = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{4})", token)
    if match:
        month = MONTHS.get(match.group(1)[:3].lower())
        if month:
            return f"{match.group(2)}-{month:02d}"
    return None


def parse_period(period: str | None) -> tuple[str | None, str | None]:
    """Return (startDate, endDate) as ISO8601 partials. endDate is None when
    the period is open ended. Returns (None, None) when nothing parses."""
    if not period:
        return None, None
    period = period.strip()

    match = re.fullmatch(r"(\d{4})\s*[-–]\s*(\d{4})", period)
    if match:
        return match.group(1), match.group(2)

    parts = re.split(r"\s+[-–]\s+", period)
    if len(parts) == 2:
        start = iso_month(parts[0])
        if parts[1].strip().lower() in {"present", "now", "current"}:
            return start, None
        return start, iso_month(parts[1])

    single = iso_month(period)
    return (single, single) if single else (None, None)


def build_resume(data: dict, stamp: str) -> str:
    person = data["person"]
    links = person.get("links", {})
    education = person.get("education", {})

    city = region = country = None
    location_parts = [part.strip() for part in person["location"].split(",")]
    if len(location_parts) == 3:
        city, region, country = location_parts
    elif len(location_parts) == 2:
        city, country = location_parts

    profiles = []
    if links.get("github"):
        profiles.append({
            "network": "GitHub",
            "username": links["github"].rstrip("/").rsplit("/", 1)[-1],
            "url": links["github"],
        })
    if links.get("linkedin"):
        profiles.append({
            "network": "LinkedIn",
            "username": links["linkedin"].rstrip("/").rsplit("/", 1)[-1],
            "url": links["linkedin"],
        })

    work = []
    for job in person.get("experience", []):
        start, end = parse_period(job.get("period"))
        work.append({
            "name": job["company"],
            "position": job["title"],
            "url": job.get("url"),
            "location": job.get("location"),
            "startDate": start,
            "endDate": end,
            "summary": job.get("summary"),
            "highlights": job.get("highlights", []),
        })

    study_type, _, area = education.get("degree", "").partition(" ")
    graduated = iso_month(education.get("graduated", "")) if education.get("graduated") else None
    education_entries = []
    if education:
        education_entries.append({
            "institution": education.get("institution"),
            "area": area or education.get("degree"),
            "studyType": study_type or None,
            "endDate": graduated,
            "score": education.get("gpa"),
            "courses": education.get("coursework", []),
        })

    certificates = []
    if education.get("certificate"):
        certificates.append({
            "name": education["certificate"],
            "date": graduated,
            "issuer": education.get("institution"),
        })

    awards = [
        {"title": honour, "awarder": education.get("institution")}
        for honour in education.get("honors", [])
    ]

    projects = []
    for project in data["projects"]:
        start, end = parse_period(project.get("period"))
        entry = {
            "name": project["name"],
            "description": project["description"],
            "highlights": project.get("highlights", []),
            "keywords": project.get("technologies", []),
            "startDate": start,
            "endDate": end,
            "roles": [project["role"]],
            "type": project["lanes"][0],
        }
        # Restricted and private work has nothing public to link.
        if project["visibility"] == "public":
            url = project_url(project)
            if url:
                entry["url"] = url
        projects.append(entry)

    skills = []
    for lane in LANE_ORDER:
        counts: Counter[str] = Counter()
        for project in data["projects"]:
            if lane in project["lanes"]:
                counts.update(project.get("technologies", []))
        if not counts:
            continue
        keywords = sorted(counts, key=lambda tech: (-counts[tech], tech.lower()))[:14]
        skills.append({"name": LANE_LABELS[lane], "keywords": keywords})

    resume = {
        "$schema": "https://raw.githubusercontent.com/jsonresume/resume-schema/v1.0.0/schema.json",
        "basics": {
            "name": person["name"],
            "label": person["headline"],
            "email": links.get("email"),
            "url": links.get("portfolio"),
            "summary": person["summary"],
            "location": {
                "city": city,
                "region": region,
                "countryCode": COUNTRY_CODES.get(country or ""),
            },
            "profiles": profiles,
        },
        "work": work,
        "education": education_entries,
        "certificates": certificates,
        "awards": awards,
        "skills": skills,
        "projects": projects,
        "meta": {
            "canonical": f"{RAW}/resume.json",
            "version": "v1.0.0",
            "lastModified": stamp,
        },
    }
    return dump_json(prune(resume))


# --------------------------------------------------------------------------
# llms.txt
# --------------------------------------------------------------------------


def llms_project_line(project: dict) -> str:
    url = project_url(project)
    label = project["name"]
    head = f"- [{label}]({url})" if url else f"- {label}"
    facts = [project["tagline"], qualifier(project)]
    bits = metric_bits(project)
    if bits:
        facts.append(" · ".join(bits))
    facts.append("Stack: " + ", ".join(project["technologies"][:8]) + ".")
    if project["visibility"] == "restricted":
        facts.append(f"Restricted, no public repository. {project['restricted_reason']}")
    elif project["visibility"] == "private":
        facts.append("Closed source by choice.")
    return head + ": " + " ".join(part.rstrip(".") + "." for part in facts)


def build_llms(data: dict) -> str:
    person = data["person"]
    links = person.get("links", {})
    seeking = person["seeking"]
    education = person.get("education", {})

    lines: list[str] = [f"# {person['name']}", ""]

    blurb = (
        f"{person['headline']} Based in {person['location']}. "
        f"Also written {person['also_written']}. "
        f"{seeking['status'].capitalize()} for {', '.join(seeking['roles'])}."
    ) if person.get("also_written") else (
        f"{person['headline']} Based in {person['location']}. "
        f"{seeking['status'].capitalize()} for {', '.join(seeking['roles'])}."
    )
    lines += [f"> {blurb}", ""]
    lines += [person["summary"], ""]
    lines += [
        "Everything here is generated from a single dataset, projects.json. "
        "Counts are measured from the repositories themselves; where a project "
        "carries no numbers, nothing was measured and nothing is claimed. "
        "Work marked restricted is real and delivered but has no public "
        "repository to inspect.",
        "",
    ]

    public = [p for p in data["projects"] if p["visibility"] == "public"]
    public.sort(key=lambda p: (not p["featured"], p.get("pinned_order", 99), p["name"].lower()))
    lines.append("## Projects")
    lines.append("")
    lines += [llms_project_line(project) for project in public]
    lines.append("")

    restricted = by_visibility(data, "restricted")
    if restricted:
        lines += ["## Restricted work", ""]
        lines += [llms_project_line(project) for project in restricted]
        lines.append("")

    private = by_visibility(data, "private")
    if private:
        lines += ["## Closed source personal work", ""]
        lines += [llms_project_line(project) for project in private]
        lines.append("")

    lines += ["## Experience", ""]
    for job in person.get("experience", []):
        head = f"- [{job['company']}]({job['url']})" if job.get("url") else f"- {job['company']}"
        lines.append(f"{head}: {job['title']}, {job['period']}. {job['summary']}")
    lines.append("")

    if education:
        lines += ["## Education", ""]
        detail = [
            education.get("degree"),
            education.get("faculty"),
            f"graduated {education['graduated']}" if education.get("graduated") else None,
            f"GPA {education['gpa']}" if education.get("gpa") else None,
            education.get("certificate"),
            ", ".join(education.get("honors", [])) or None,
            f"Capstone: {education['capstone']}" if education.get("capstone") else None,
        ]
        lines.append(f"- {education['institution']}: " + ". ".join(part for part in detail if part) + ".")
        if education.get("coursework"):
            lines.append("- Relevant coursework: " + ", ".join(education["coursework"]) + ".")
        lines.append("")

    lines += ["## What he is looking for", ""]
    lines += [
        f"- Status: {seeking['status']}.",
        f"- Roles: {', '.join(seeking['roles'])}.",
        f"- Arrangements: {', '.join(seeking['arrangements'])}.",
        f"- Locations: {' or '.join(seeking['locations'])}.",
        f"- {seeking['note']}",
        "",
    ]

    lines += ["## Contact", ""]
    label_map = {
        "portfolio": "Portfolio",
        "github": "GitHub",
        "linkedin": "LinkedIn",
        "work": "Company",
        "letterboxd": "Letterboxd",
    }
    if links.get("email"):
        lines.append(f"- [Email](mailto:{links['email']}): {links['email']}")
    for key, label in label_map.items():
        if links.get(key):
            lines.append(f"- [{label}]({links[key]}): {links[key]}")
    lines.append("")

    lines += ["## Optional", ""]
    lines += [
        f"- [projects.json]({RAW}/{SOURCE_NAME}): the source of truth for everything above.",
        f"- [api.json]({RAW}/api.json): the same dataset with a generation timestamp.",
        f"- [schema.json]({RAW}/schema.json): JSON Schema the dataset validates against.",
        f"- [resume.json]({RAW}/resume.json): the same person as JSON Resume v1.0.0.",
        f"- [profile repository]({PROFILE_REPO}): build.py renders every file listed here.",
        "",
    ]

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def render_all(data: dict, stamp: str, existing_readme: str | None) -> dict[str, str]:
    return {
        "README.md": build_readme(data, existing_readme),
        "api.json": build_api(data, stamp),
        "resume.json": build_resume(data, stamp),
        "llms.txt": build_llms(data),
    }


def existing_stamp(root: Path, fallback: str) -> str:
    """Reuse the timestamp already on disk so --check compares content only."""
    api = root / "api.json"
    if api.is_file():
        try:
            stamp = read_json(api).get("generated_at")
        except (ValueError, OSError):
            return fallback
        if isinstance(stamp, str) and stamp:
            return stamp
    return fallback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render portfolio outputs from projects.json.")
    parser.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 if any generated file is stale")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent,
                        help="directory holding projects.json (default: this file's directory)")
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    source = root / SOURCE_NAME
    if not source.is_file():
        print(f"error: {source} not found", file=sys.stderr)
        return 2

    data = read_json(source)
    validate(data)

    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    readme_path = root / "README.md"
    existing_readme = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else None

    if args.check:
        rendered = render_all(data, existing_stamp(root, now), existing_readme)
        stale = []
        for name, text in rendered.items():
            path = root / name
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        if stale:
            print("stale, run `python build.py`: " + ", ".join(stale), file=sys.stderr)
            return 1
        print(f"up to date: {', '.join(OUTPUT_NAMES)}")
        return 0

    # Render once against the timestamp already on disk. Anything that still
    # matches is genuinely unchanged and is left alone, so a rebuild that
    # changes nothing does not churn `generated_at` and does not dirty the
    # working tree. Only files whose content moved get the new timestamp.
    baseline = render_all(data, existing_stamp(root, now), existing_readme)
    fresh: dict[str, str] | None = None
    changed = []
    for name in OUTPUT_NAMES:
        path = root / name
        if path.is_file() and path.read_text(encoding="utf-8") == baseline[name]:
            continue
        if fresh is None:
            fresh = render_all(data, now, existing_readme)
        write_text(path, fresh[name])
        changed.append(name)

    counts = Counter(project["visibility"] for project in data["projects"])
    print(
        f"{len(data['projects'])} projects "
        f"({counts['public']} public, {counts['restricted']} restricted, {counts['private']} private)"
    )
    print("wrote: " + (", ".join(changed) if changed else "nothing, everything was current"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
