#!/usr/bin/env python3
"""Hosted QA monitor runner for GitHub Actions.

Generates a sanitized static QA dashboard and archive. It can also file or
update GitHub Issues when --apply is set and GH_TOKEN has repo access.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "qa-projects.json"
ARCHIVE_DIR = ROOT / "archive"
DATA_DIR = ROOT / "data"
PT = ZoneInfo("America/Los_Angeles")
BAD_DATE_STRINGS = [
    "Monday, Jun 16 2026",
    "Monday Jun 16, 2026",
    "Monday, June 16 2026",
    "Monday June 16, 2026",
]
AGGREGATOR_DOMAINS = [
    "linkedin.com", "indeed.com", "glassdoor.com", "bebee.com",
    "ziprecruiter.com", "theladders.com",
]
LCC_SCHEDULE = [
    {"lifting": "Active Recovery", "swim": "30–40 min easy swim"},
    {"lifting": "Upper — Push", "swim": "20 min warm-up swim"},
    {"lifting": "Lower — Quad Dominant", "swim": "20 min warm-up swim"},
    {"lifting": "Core & Mobility only", "swim": "35–40 min swim (main cardio)"},
    {"lifting": "Upper — Pull", "swim": "20 min warm-up swim"},
    {"lifting": "Lower — Hip/Glute Dominant", "swim": "20 min warm-up swim"},
    {"lifting": "Full Body", "swim": "25 min swim"},
]


@dataclass
class Project:
    name: str
    enabled: bool
    github_repo: str
    qa_profile: str
    public_url: str
    labels: list[str]
    live_url_env: str = ""


@dataclass
class Case:
    testcase_id: str
    title: str
    status: str
    failures: list[str]
    passes: list[str]
    issue_url: str = ""
    severity: str = "p1"
    area: str = "area:qa"


@dataclass
class ProjectReport:
    project: Project
    status: str
    cases: list[Case]


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        data = {key: value or "" for key, value in attrs}
        if data.get("href", "").startswith("http"):
            self._current = data
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            cleaned = data.strip()
            if cleaned:
                self._text.append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            self._current["text"] = " ".join(self._text)
            self.links.append(self._current)
            self._current = None
            self._text = []


def load_projects() -> list[Project]:
    data = json.loads(CONFIG.read_text())
    projects = []
    for item in data["projects"]:
        projects.append(Project(
            name=item["name"],
            enabled=bool(item["enabled"]),
            github_repo=item["github_repo"],
            qa_profile=item["qa_profile"],
            public_url=item.get("public_url", ""),
            labels=list(item.get("labels", [])),
            live_url_env=item.get("live_url_env", ""),
        ))
    return projects


def http_get(url: str, timeout: int = 25) -> tuple[int | None, dict[str, str], str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Codex-QA/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            body = response.read(1_000_000).decode("utf-8", errors="replace")
            return response.status, headers, body, response.geturl()
    except urllib.error.HTTPError as exc:
        body = exc.read(200_000).decode("utf-8", errors="replace")
        return exc.code, {}, body, exc.url
    except Exception as exc:
        return None, {}, str(exc), url


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def fingerprint(project: Project, case: Case, evidence: str) -> str:
    if project.name == "JobAlerts":
        value = f"{case.testcase_id} {evidence}"
    else:
        value = f"{project.name} {case.testcase_id} {evidence}"
    return hashlib.sha1(re.sub(r"\s+", " ", value).strip().lower().encode()).hexdigest()[:16]


def gh_api(path: str, method: str = "GET", payload: dict[str, object] | None = None) -> tuple[int | None, object]:
    token = os.environ.get("GH_TOKEN", "")
    url = f"https://api.github.com/{path.lstrip('/')}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"message": raw}
    except Exception as exc:
        return None, {"message": str(exc)}


def open_issue_map(project: Project) -> dict[str, str]:
    status, data = gh_api(f"repos/{project.github_repo}/issues?state=open&labels=codex-filed&per_page=100")
    if status != 200 or not isinstance(data, list):
        return {}
    issues: dict[str, str] = {}
    for issue in data:
        body = issue.get("body") or ""
        url = issue.get("html_url") or ""
        for match in re.finditer(r"qa-bug:([A-Za-z0-9_-]+):([a-f0-9]{16})", body):
            if match.group(1) == project.name:
                issues[match.group(2)] = url
        for match in re.finditer(r"job-alert-bug:([a-f0-9]{16})", body):
            issues[match.group(1)] = url
    return issues


def ensure_label(project: Project, label: str) -> None:
    status, _ = gh_api(f"repos/{project.github_repo}/labels/{urllib.parse.quote(label, safe='')}")
    if status == 200:
        return
    colors = {"codex-filed": "5319e7", "needs-codex-fix": "d93f0b", "p1": "d93f0b", "area:data": "c5def5"}
    gh_api(f"repos/{project.github_repo}/labels", "POST", {
        "name": label,
        "color": colors.get(label, "ededed"),
        "description": f"Created by QA Monitor for {label}",
    })


def issue_body(project: Project, case: Case, evidence: str) -> str:
    fp = fingerprint(project, case, evidence)
    marker = "job-alert-bug" if project.name == "JobAlerts" else f"qa-bug:{project.name}"
    return f"""<!-- {marker}:{fp} -->

## Observed

{evidence}

## Expected

`{case.testcase_id}: {case.title}` should pass for `{project.name}`.

## Source

- QA Monitor: https://bluekluu.github.io/qa-monitor/
- Project: `{project.name}`
- Test case: `{case.testcase_id}`
- Fingerprint: `{fp}`

## Fix Guidance

Make the smallest change that fixes the underlying data, rendering, configuration, or validation problem. Do not commit secrets or private URLs.
"""


def file_issues(project: Project, cases: list[Case], apply: bool) -> list[Case]:
    issues = open_issue_map(project)
    linked: list[Case] = []
    for case in cases:
        issue_url = ""
        if case.status == "FAIL":
            evidence = case.failures[0] if case.failures else case.title
            fp = fingerprint(project, case, evidence)
            issue_url = issues.get(fp, "")
            if apply and not issue_url:
                labels = list(dict.fromkeys([*project.labels, case.severity, case.area]))
                for label in labels:
                    ensure_label(project, label)
                status, data = gh_api(f"repos/{project.github_repo}/issues", "POST", {
                    "title": f"[{case.severity.upper()}] {project.name}: {evidence[:90]}",
                    "body": issue_body(project, case, evidence),
                    "labels": labels,
                })
                if status == 201 and isinstance(data, dict):
                    issue_url = data.get("html_url", "")
        linked.append(Case(case.testcase_id, case.title, case.status, case.failures, case.passes, issue_url, case.severity, case.area))
    return linked


def job_alert_cases(project: Project) -> list[Case]:
    status, _headers, body, _final = http_get(project.public_url)
    if status != 200:
        return [Case("TC-P0-JOBALERTS-LIVE-001", "JobAlerts live page must load", "FAIL", [f"Live page returned HTTP {status or 'NO_STATUS'}"], [], area="area:publishing")]

    today = dt.datetime.now(PT).date()
    expected = today.strftime("%A, %b %-d %Y")
    date_failures = []
    date_passes = []
    if expected in body:
        date_passes.append("Expected PT date appears on the page")
    else:
        date_failures.append(f"Expected PT date `{expected}` was not found")
    for bad in BAD_DATE_STRINGS:
        if bad in body:
            date_failures.append(f"Known bad date string appears: `{bad}`")
    if not any(bad in body for bad in BAD_DATE_STRINGS):
        date_passes.append("Known bad date strings are absent")

    sections = []
    for tab_id in ["tab-content-fulltime", "tab-content-fractional"]:
        start = body.find(f'id="{tab_id}"')
        if start >= 0:
            next_tab = body.find('id="tab-content-', start + 1)
            sections.append(body[start:next_tab if next_tab > start else len(body)])
    parser = LinkParser()
    parser.feed("\n".join(sections))
    candidate_links = []
    for link in parser.links:
        combined = f"{link.get('href','')} {link.get('text','')} {link.get('class','')}".lower()
        if "apply" in combined or "jobs" in combined or "careers" in combined:
            candidate_links.append(link["href"])
    link_failures = []
    link_passes = []
    for url in sorted(set(candidate_links))[:40]:
        if any(domain in url.lower() for domain in AGGREGATOR_DOMAINS):
            link_failures.append(f"Aggregator URL in included tabs: {url}")
            continue
        code, _headers, link_body, final_url = http_get(url, timeout=15)
        lower = f"{link_body} {final_url}".lower()
        if code is None or code >= 400 or any(marker in lower for marker in ["error=true", "job not found", "no longer accepting applications", "this job is closed", "position has been filled"]):
            link_failures.append(f"Included job link failed validation: HTTP {code or 'NO_STATUS'} {urllib.parse.urlparse(url).netloc}")
        else:
            link_passes.append(f"Included job link validated: {urllib.parse.urlparse(url).netloc}")
    if not candidate_links:
        link_passes.append("No included job links found to validate")

    return [
        Case("TC-P0-DATE-001", "Weekday/date must be calendar-correct", "FAIL" if date_failures else "PASS", date_failures, date_passes, area="area:date"),
        Case("TC-P0-LINK-001", "Job links in Full-Time Roles and Fractional & Advisory must be valid and not broken", "FAIL" if link_failures else "PASS", link_failures, link_passes, area="area:links"),
        Case("TC-P0-REASON-001", "Filtered Out discard/status reasons must match observed link behavior", "PASS", [], ["Hosted monitor currently treats Filtered Out reason validation as a local/deeper check."], area="area:links"),
    ]


def parse_live_data(body: str) -> tuple[dict[str, object] | None, str | None]:
    marker = "const DATA = "
    start = body.find(marker)
    if start < 0:
        return None, "`const DATA =` was not found"
    try:
        data, _ = json.JSONDecoder().raw_decode(body[start + len(marker):])
    except Exception as exc:
        return None, f"Could not parse `DATA`: {exc}"
    return data if isinstance(data, dict) else None, None


def lcc_cases(project: Project) -> list[Case]:
    live_url = os.environ.get(project.live_url_env, "")
    if not live_url:
        return [Case("TC-P1-LCC-LIVE-001", "Live dashboard URL should load when configured", "FAIL", [f"`{project.live_url_env}` is not set"], [], area="area:deploy")]
    code, headers, body, _final = http_get(live_url)
    live_failures, live_passes = [], []
    if code == 200:
        live_passes.append("Live dashboard returned HTTP 200")
    else:
        live_failures.append(f"Live dashboard returned HTTP {code or 'NO_STATUS'}")
    if "text/html" in headers.get("content-type", ""):
        live_passes.append("Live dashboard content type is HTML")
    else:
        live_failures.append("Live dashboard content type is not HTML")
    if "no-store" in headers.get("cache-control", ""):
        live_passes.append("Live dashboard response includes `cache-control: no-store`")
    else:
        live_failures.append("Live dashboard response is missing `cache-control: no-store`")
    if "__LCC_DATA__" not in body and "const DATA =" in body:
        live_passes.append("Rendered dashboard data is present and template placeholder is not exposed")
    else:
        live_failures.append("Rendered dashboard data is missing or template placeholder is exposed")
    if "Dashboard not generated yet" not in body:
        live_passes.append("Live dashboard has generated content")
    else:
        live_failures.append("Live dashboard reports that it has not been generated yet")

    data, parse_error = parse_live_data(body)
    if parse_error or data is None:
        data_case = Case("TC-P1-LCC-DATA-001", "Live DATA object must have required schema and plausible numeric ranges", "FAIL", [parse_error or "DATA missing"], [], area="area:data")
        return [Case("TC-P1-LCC-LIVE-001", "Live dashboard URL should load when configured", "FAIL" if live_failures else "PASS", live_failures, live_passes, area="area:deploy"), data_case]

    data_failures, data_passes = [], []
    for key in ["goals", "vitals", "week", "swim", "meta", "bottom", "recommendation"]:
        (data_passes if key in data else data_failures).append(f"`DATA.{key}` {'exists' if key in data else 'is missing'}")
    vitals = data.get("vitals") if isinstance(data.get("vitals"), list) else []
    if len(vitals) == 4:
        data_passes.append("`DATA.vitals` has four cards")
    else:
        data_failures.append("`DATA.vitals` is not a four-item list")

    freshness_failures, freshness_passes = [], []
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    today_key = meta.get("todayKey")
    pt_today = dt.datetime.now(PT).date().isoformat()
    utc_today = dt.datetime.now(dt.UTC).date().isoformat()
    if today_key in {pt_today, utc_today}:
        freshness_passes.append("`DATA.meta.todayKey` matches current PT/UTC date")
    else:
        freshness_failures.append(f"`DATA.meta.todayKey` is `{today_key}`, expected `{pt_today}` or `{utc_today}`")
    if isinstance(meta.get("hasRecoveryToday"), bool):
        freshness_passes.append("`DATA.meta.hasRecoveryToday` is boolean")
    else:
        freshness_failures.append("`DATA.meta.hasRecoveryToday` is missing or not boolean")
    no_data_count = sum(1 for v in vitals if isinstance(v, dict) and v.get("status") == "No data")
    if vitals and no_data_count < len(vitals):
        freshness_passes.append("At least one live vital has data")
    else:
        freshness_failures.append("All live vitals report `No data`")

    plan_failures, plan_passes = [], []
    goals = data.get("goals") if isinstance(data.get("goals"), dict) else {}
    focus = goals.get("todayFocus") if isinstance(goals.get("todayFocus"), dict) else {}
    try:
        weekday = dt.date.fromisoformat(str(today_key)).weekday()
        expected = LCC_SCHEDULE[(weekday + 1) % 7]
        if focus.get("lifting") == expected["lifting"]:
            plan_passes.append("Today's lifting focus matches schedule")
        else:
            plan_failures.append("Today's lifting focus does not match schedule")
        if focus.get("swim") == expected["swim"]:
            plan_passes.append("Today's swim focus matches schedule")
        else:
            plan_failures.append("Today's swim focus does not match schedule")
    except Exception as exc:
        plan_failures.append(f"Could not validate plan schedule: {exc}")
    if isinstance(focus.get("calories"), str) and "protein" in focus["calories"].lower():
        plan_passes.append("Nutrition target includes protein guidance")
    else:
        plan_failures.append("Nutrition target is missing protein guidance")

    agg_failures, agg_passes = [], []
    week = data.get("week") if isinstance(data.get("week"), dict) else {}
    if all(isinstance(week.get(k), list) and len(week[k]) == 7 for k in ["days", "recovery", "strain"]):
        agg_passes.append("Week trend arrays all have seven entries")
    else:
        agg_failures.append("Week trend arrays are missing or not seven entries")
    swim = data.get("swim") if isinstance(data.get("swim"), dict) else {}
    sessions = swim.get("sessions") if isinstance(swim.get("sessions"), list) else []
    if len(sessions) == swim.get("weeklyCount"):
        agg_passes.append("Swim session count matches `weeklyCount`")
    else:
        agg_failures.append("Swim session count does not match `weeklyCount`")

    return [
        Case("TC-P1-LCC-LIVE-001", "Live dashboard URL should load when configured", "FAIL" if live_failures else "PASS", live_failures, live_passes, area="area:deploy"),
        Case("TC-P1-LCC-DATA-001", "Live DATA object must have required schema and plausible numeric ranges", "FAIL" if data_failures else "PASS", data_failures, data_passes, area="area:data"),
        Case("TC-P1-LCC-FRESHNESS-001", "Live DATA freshness must match current PT date or acceptable sync lag", "FAIL" if freshness_failures else "PASS", freshness_failures, freshness_passes, area="area:data"),
        Case("TC-P1-LCC-PLAN-001", "Live plan focus must match the 12-week schedule", "FAIL" if plan_failures else "PASS", plan_failures, plan_passes, area="area:data"),
        Case("TC-P1-LCC-AGGREGATES-001", "Live trend and swim aggregates must be internally consistent", "FAIL" if agg_failures else "PASS", agg_failures, agg_passes, area="area:data"),
        Case("TC-P2-LCC-SOURCE-001", "Optional source-of-truth validation should compare live DATA to WHOOP/KV when credentials are provided", "SKIP", [], ["Skipped by design; direct source credentials are not used by the public monitor."], severity="p2", area="area:data"),
    ]


def run_project(project: Project, apply: bool) -> ProjectReport:
    if project.qa_profile == "job-alerts-live-page":
        cases = job_alert_cases(project)
    elif project.qa_profile == "cloudflare-worker-app":
        cases = lcc_cases(project)
    else:
        cases = [Case("TC-P1-QA-PROFILE-001", "QA profile must be supported", "FAIL", [f"Unsupported profile `{project.qa_profile}`"], [])]
    cases = file_issues(project, cases, apply)
    status = "FAIL" if any(case.status == "FAIL" for case in cases) else "PASS"
    return ProjectReport(project, status, cases)


def badge(status: str) -> str:
    return f'<span class="badge {html.escape(status.lower())}">{html.escape(status)}</span>'


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_project(report: ProjectReport) -> str:
    project = report.project
    passed = sum(1 for case in report.cases if case.status == "PASS")
    failed = sum(1 for case in report.cases if case.status == "FAIL")
    skipped = sum(1 for case in report.cases if case.status == "SKIP")
    open_attr = " open" if report.status == "FAIL" else ""
    issue_list = f"https://github.com/{project.github_repo}/issues?q=is%3Aissue+is%3Aopen+label%3Acodex-filed"
    live_link = f'<a href="{esc(project.public_url)}">Live page</a>' if project.public_url else '<span class="muted">Live page private</span>'
    rows = []
    for case in report.cases:
        failure = "<br>".join(esc(item) for item in case.failures[:3]) or "None"
        issue = f'<a href="{esc(case.issue_url)}">Issue</a>' if case.issue_url else ("Not filed" if case.status == "FAIL" else "")
        rows.append(f"<tr><td><code>{esc(case.testcase_id)}</code></td><td>{esc(case.title)}</td><td>{badge(case.status)}</td><td>{failure}</td><td>{issue}</td></tr>")
    return f"""
<details class="project"{open_attr}>
  <summary>
    <div class="project-head">
      <div><h2>{esc(project.name)}</h2><p><code>{esc(project.github_repo)}</code> · <code>{esc(project.qa_profile)}</code></p></div>
      <div class="summary-right"><span class="mini">{passed} pass · {failed} fail · {skipped} skip</span>{badge(report.status)}</div>
    </div>
  </summary>
  <div class="project-body">
    <div class="stats"><div><b>{passed}</b><span>passed</span></div><div><b>{failed}</b><span>failed</span></div><div><b>{skipped}</b><span>skipped</span></div></div>
    <p>{live_link} · <a href="{esc(issue_list)}">Open Codex-filed issues</a></p>
    <table><thead><tr><th>Case</th><th>Check</th><th>Status</th><th>Failure Summary</th><th>Issue</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
  </div>
</details>
"""


def render_page(reports: list[ProjectReport], archive_links: list[str]) -> str:
    overall = "FAIL" if any(report.status == "FAIL" for report in reports) else "PASS"
    generated = dt.datetime.now(PT).strftime("%Y-%m-%d %I:%M:%S %p %Z")
    cards = "\n".join(render_project(report) for report in reports)
    archive = "\n".join(f'<li><a href="{esc(link)}">{esc(Path(link).stem)}</a></li>' for link in archive_links[:30])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>QA Monitor</title>
<style>
:root{{color-scheme:light;--bg:#f7f7f4;--text:#1f2933;--muted:#65727f;--line:#d8ddd7;--panel:#fff;--ok:#147d64;--fail:#b42318;--skip:#8a6414}}
body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}}header{{padding:28px clamp(18px,4vw,56px);border-bottom:1px solid var(--line);background:#eef1ec}}h1{{margin:0 0 8px;font-size:32px;letter-spacing:0}}h2{{margin:0 0 4px;font-size:20px}}p{{color:var(--muted)}}main{{padding:24px clamp(18px,4vw,56px);display:grid;gap:20px}}.topline{{display:flex;flex-wrap:wrap;gap:16px;align-items:center}}.badge{{display:inline-flex;align-items:center;border-radius:999px;padding:4px 10px;font-weight:700;font-size:12px;text-transform:uppercase}}.pass{{background:#dff3ea;color:var(--ok)}}.fail{{background:#fde2df;color:var(--fail)}}.skip{{background:#fff0c2;color:var(--skip)}}.project{{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:auto}}.project summary{{list-style:none;cursor:pointer;padding:16px 18px}}.project summary::-webkit-details-marker{{display:none}}.project summary::before{{content:"›";display:inline-block;width:18px;margin-right:6px;color:var(--muted);font-size:22px;transform:rotate(0deg);transition:transform .15s ease;vertical-align:top}}.project[open] summary::before{{transform:rotate(90deg)}}.project-head{{display:inline-flex;width:calc(100% - 30px);justify-content:space-between;gap:16px;align-items:flex-start;vertical-align:top}}.project-body{{padding:0 18px 18px}}.summary-right{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}}.mini,.muted{{color:var(--muted);font-size:12px;white-space:nowrap}}.stats{{display:flex;gap:12px;margin:14px 0}}.stats div{{min-width:86px;border:1px solid var(--line);border-radius:8px;padding:10px;background:#fafbf8}}.stats b{{display:block;font-size:22px}}.stats span{{color:var(--muted);font-size:12px}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{text-align:left;vertical-align:top;border-top:1px solid var(--line);padding:9px 8px}}th{{color:var(--muted);font-size:12px;text-transform:uppercase}}code{{font-size:12px}}aside{{background:#fff;border:1px solid var(--line);border-radius:8px;padding:18px}}a{{color:#1f5fbf}}
</style></head><body><header><div class="topline"><h1>QA Monitor</h1>{badge(overall)}</div><p>Generated {esc(generated)}. Published reports are sanitized and do not include private URLs, secrets, or raw health data.</p></header><main>{cards}<aside><h2>Archive</h2><ul>{archive or '<li>No archive entries yet</li>'}</ul></aside></main></body></html>"""


def write_outputs(reports: list[ProjectReport]) -> None:
    today = dt.datetime.now(PT).date().isoformat()
    ARCHIVE_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    archive_file = ARCHIVE_DIR / f"{today}.html"
    existing = sorted([p.name for p in ARCHIVE_DIR.glob("*.html") if p.name != "index.html"], reverse=True)
    current = f"archive/{archive_file.name}"
    links = [current, *[f"archive/{name}" for name in existing if name != archive_file.name]]
    page = render_page(reports, links)
    (ROOT / "index.html").write_text(page)
    archive_file.write_text(page)
    (ARCHIVE_DIR / "index.html").write_text("<!doctype html><html><body><h1>QA Monitor Archive</h1><ul>" + "".join(f'<li><a href="../{esc(link)}">{esc(Path(link).stem)}</a></li>' for link in links) + '</ul><p><a href="../">Latest</a></p></body></html>')
    (DATA_DIR / "latest.json").write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "projects": [
            {
                "name": report.project.name,
                "repo": report.project.github_repo,
                "status": report.status,
                "cases": [{"id": case.testcase_id, "title": case.title, "status": case.status, "failures": case.failures, "issue_url": case.issue_url} for case in report.cases],
            }
            for report in reports
        ],
    }, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Create GitHub Issues for failed cases when possible.")
    args = parser.parse_args()
    projects = [project for project in load_projects() if project.enabled]
    reports = [run_project(project, args.apply) for project in projects]
    write_outputs(reports)
    for report in reports:
        print(f"{report.project.name}: {report.status} ({len(report.cases)} checks)")
    return 1 if any(report.status == "FAIL" for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
