"""Server-rendered hub pages, built only from Orbit components (ob- classes in static/bundle.css)."""

from __future__ import annotations

import time
from html import escape
from urllib.parse import quote

THEMES = {"orbit": "voidshift", "default": "plain"}


def e(v) -> str:
    return escape("" if v is None else str(v), quote=True)


def u(v: str) -> str:
    return quote(str(v), safe="")


def _svg(body: str) -> str:
    return ('<svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + body + "</svg>")


# Lucide 1.52.0, copied unchanged (ISC licence).
ICONS = {
    "box": _svg('<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>'),
    "server": _svg('<rect width="20" height="8" x="2" y="2" rx="2" ry="2"/><rect width="20" height="8" x="2" y="14" rx="2" ry="2"/><line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/>'),
    "archive": _svg('<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>'),
    "scroll-text": _svg('<path d="M15 12h-5"/><path d="M15 8h-5"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/><path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/>'),
    "inbox": _svg('<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>'),
    "triangle-alert": _svg('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>'),
    "circle-check": _svg('<circle cx="12" cy="12" r="10"/><path d="m16 9-5.5 5.5L8 12"/>'),
    "circle-x": _svg('<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>'),
    "x": _svg('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
    "menu": _svg('<path d="M4 5h16"/><path d="M4 12h16"/><path d="M4 19h16"/>'),
    "refresh-cw": _svg('<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>'),
    "chevron-down": _svg('<path d="m6 9 6 6 6-6"/>'),
    "log-out": _svg('<path d="m16 17 5-5-5-5"/><path d="M21 12H9"/><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'),
}

NAV = (("stacks", "/", "Stacks", "box"), ("agents", "/agents", "Agents", "server"),
       ("jobs", "/jobs", "Jobs", "scroll-text"), ("repository", "/repository", "Repository", "archive"))

TOASTS = {
    "backup": ("success", "Backup queued", "It runs after any job already in progress on that host."),
    "migrate": ("success", "Migration backup queued", "The stack starts again when the backup is done."),
    "restore": ("success", "Restore queued", "Follow it on the job page."),
    "saved": ("success", "Saved", "The agent uses the new value from now on."),
    "lease": ("success", "Maintenance agent changed", "Prune and repository checks now run there."),
    "error": ("error", "That didn't work", ""),
}


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = max(0, int(time.time() - ts))
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            return f"{s // n} {unit} ago"
    return "just now"


def size(n) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return ""


def status(word: str, kind: str) -> str:
    return f'<span class="ob-status ob-status--{kind}">{e(word)}</span>'


def result_status(state: str | None) -> str:
    return {"ok": status("Done", "up"), "failed": status("Failed", "down"), "running": status("Running", "maint"),
            "queued": status("Queued", "maint")}.get(state or "", status("No run yet", "maint"))


def empty(title: str, body: str) -> str:
    return (f'<div class="ob-empty"><span class="ob-empty__icon">{ICONS["inbox"]}</span>'
            f'<h2 class="ob-h2">{e(title)}</h2><p>{e(body)}</p></div>')


def callout(kind: str, title: str, body: str) -> str:
    icon = {"warning": "triangle-alert", "error": "circle-x", "success": "circle-check"}[kind]
    return (f'<div class="ob-callout ob-callout--{kind}" role="status">{ICONS[icon]}<div class="ob-callout__text">'
            f'<p class="ob-callout__title">{e(title)}</p><p class="ob-callout__body">{e(body)}</p></div></div>')


def toast(key: str | None, detail: str = "") -> str:
    if not key or key not in TOASTS:
        return ""
    kind, title, body = TOASTS[key]
    body = detail or body
    icon = "circle-x" if kind == "error" else "circle-check"
    return (f'<div class="ob-toasts" role="status"><dialog class="ob-toast ob-toast--{kind}" open>{ICONS[icon]}'
            f'<div class="ob-toast__text"><p class="ob-toast__title">{e(title)}</p>'
            f'<p class="ob-toast__body">{e(body)}</p></div><form method="dialog"><button class="ob-btn ob-btn--ghost '
            f'ob-btn--icon ob-btn--sm ob-toast__close" aria-label="Dismiss">{ICONS["x"]}</button></form></dialog></div>')


def head(title: str, theme: str) -> str:
    fonts = '<link rel="preload" href="/static/fonts/ibm-plex-sans-latin-400-normal.woff2" as="font" type="font/woff2" crossorigin>' if theme == "voidshift" else ""
    return (f'<!doctype html><html lang="en" data-theme="{theme}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)} · Backtrack</title>'
            f'{fonts}<link rel="stylesheet" href="/static/tokens.css"><link rel="stylesheet" href="/static/plain.css">'
            f'<link rel="stylesheet" href="/static/bundle.css"><link rel="stylesheet" href="/static/app.css"></head>')


def page(title: str, current: str, body: str, theme_key: str, csrf: str, toast_html: str = "",
         alerts: int = 0) -> str:
    theme = THEMES.get(theme_key, "plain")
    items = []
    tiles = []
    for key, href, label, icon in NAV:
        cur = ' aria-current="page"' if key == current else ""
        items.append(f'<a class="ob-sidenav__item" href="{href}"{cur}>{ICONS[icon]}<span>{label}</span></a>')
        tiles.append(f'<a class="ob-mobilenav__item" href="{href}"{cur}><span class="ob-mobilenav__icon">'
                     f'{ICONS[icon]}</span><span>{label}</span></a>')
    signout = (f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{e(csrf)}">'
               f'<button class="ob-sidenav__item" type="submit">{ICONS["log-out"]}<span>Sign out</span></button></form>')
    brand = '<img src="/static/mark.svg" alt=""><span>backtrack</span>'
    badge = f'<span class="ob-mobilenav__badge">{alerts}</span>' if alerts else ""
    menu_label = f"Open menu, {alerts} alerts" if alerts else "Open menu"
    return (
        head(title, theme)
        + '<body class="ob-body"><div class="ob-shell"><a class="ob-skip" href="#main">Skip to content</a>'
        + f'<nav class="ob-sidenav" aria-label="Main"><div class="ob-sidenav__brand">{brand}</div>'
        + f'<div class="ob-sidenav__group">{"".join(items)}</div>'
        + f'<div class="ob-sidenav__group ob-sidenav__foot">{signout}</div></nav>'
        + f'<div class="ob-shell__main">{body}</div>'
        + f'<nav class="ob-mobilenav" aria-label="Main mobile"><div class="ob-mobilenav__bar">'
        + f'<a class="ob-mobilenav__brand" href="/">{brand}</a>'
        + f'<button class="ob-btn ob-btn--ghost ob-btn--icon ob-mobilenav__toggle" type="button" '
        + f'popovertarget="bt-menu" aria-label="{menu_label}">{ICONS["menu"]}{badge}</button></div>'
        + f'<div class="ob-mobilenav__menu" id="bt-menu" popover><div class="ob-mobilenav__grid">{"".join(tiles)}</div>'
        + f'<div class="ob-mobilenav__bar"><a class="ob-mobilenav__brand" href="/">{brand}</a>'
        + f'<button class="ob-btn ob-btn--ghost ob-btn--icon ob-mobilenav__toggle" type="button" popovertarget="bt-menu" '
        + f'popovertargetaction="hide" aria-label="Close menu">{ICONS["x"]}</button></div></div></nav>'
        + f'</div>{toast_html}<script src="/static/app.js" defer></script></body></html>'
    )


def header(title: str, eyebrow: str = "", desc: str = "", actions: str = "") -> str:
    eb = f'<p class="ob-eyebrow">{e(eyebrow)}</p>' if eyebrow else ""
    d = f"<p>{e(desc)}</p>" if desc else ""
    act = f'<div class="ob-page-header__actions">{actions}</div>' if actions else ""
    return (f'<header class="ob-page-header"><div class="ob-page-header__text">{eb}<h1 class="ob-h1">{e(title)}</h1>'
            f'{d}</div>{act}</header>')


def post_button(action: str, label: str, csrf: str, cls: str = "ob-btn ob-btn--outline", fields: dict | None = None,
                icon: str = "") -> str:
    hidden = "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in (fields or {}).items())
    return (f'<form method="post" action="{e(action)}"><input type="hidden" name="csrf" value="{e(csrf)}">{hidden}'
            f'<button class="{cls}" type="submit">{ICONS.get(icon, "")}{e(label)}</button></form>')


# -- pages --------------------------------------------------------------------------------------------------------

def login(theme_key: str, error: str = "") -> str:
    theme = THEMES.get(theme_key, "plain")
    err = (f'<p class="ob-field__help">{e(error)}</p>' if error else "")
    cls = "ob-field ob-field--error" if error else "ob-field"
    return (head("Sign in", theme) + '<body class="ob-body"><main id="main" class="ob-center"><div class="ob-card">'
            '<div class="ob-stack ob-stack--group"><div class="ob-stack ob-stack--inline">'
            '<h1 class="ob-h1">Sign in to Backtrack</h1></div>'
            '<form class="ob-stack" method="post" action="/login">'
            f'<label class="{cls}"><span class="ob-field__label">Admin password</span>'
            '<input class="ob-input" type="password" name="password" autocomplete="current-password" required autofocus>'
            f'{err}</label><button class="ob-btn ob-btn--primary ob-btn--block" type="submit">Sign in</button></form>'
            '<p class="ob-small ob-muted">The password is UI_PASSWORD in this agent\'s environment.</p>'
            '</div></div></main></body></html>')


def stacks_page(agents: list[dict], snapshot_count: int | None, lease: dict | None, warnings: list[tuple],
                csrf: str) -> str:
    rows = []
    stacks = []
    for a in agents:
        for s in a.get("stacks", []):
            stacks.append((s, a))
    stacks.sort(key=lambda p: p[0]["name"])
    ok = sum(1 for s, _ in stacks if (s.get("last") or {}).get("state") == "ok")
    for s, a in stacks:
        last = s.get("last") or {}
        state = status("Misconfigured", "down") if s.get("error") else result_status(last.get("state"))
        rows.append(
            f'<tr><td><a class="ob-h3 ob-table__link" href="/stacks/{u(s["name"])}">{e(s["name"])}</a></td>'
            f'<td class="ob-code">{e(a["machine"])}</td><td class="ob-muted">{e(s.get("mode") or "–")}</td>'
            f'<td>{e(ago(last.get("time")))}</td><td class="num">{e(size(last.get("data_added")))}</td>'
            f'<td>{state}</td></tr>')
    online = sum(1 for a in agents if a.get("online"))
    metrics = [
        ("Last backups ok", f"{ok}", f"of {len(stacks)}", "success" if stacks and ok == len(stacks) else
         ("warning" if stacks else "")),
        ("Agents online", f"{online}", f"of {len(agents)}", "success" if agents and online == len(agents) else
         "warning" if agents else ""),
        ("Snapshots in the repository", "–" if snapshot_count is None else str(snapshot_count), "", ""),
        ("Maintenance agent", (lease or {}).get("machine") or "None", "", "" if lease else "warning"),
    ]
    cards = "".join(
        f'<div class="ob-card ob-card--dense"><p class="ob-card__label">{e(label)}</p>'
        f'<p class="ob-card__metric{" ob-card__metric--" + tone if tone else ""}">{e(value)}'
        f'{" <small>" + e(ref) + "</small>" if ref else ""}</p></div>' for label, value, ref, tone in metrics)
    table = (f'<div class="ob-table-wrap"><table class="ob-table"><thead><tr><th>Stack</th><th>Machine</th><th>Mode</th>'
             f'<th>Last backup</th><th class="num">Added</th><th>State</th></tr></thead><tbody>{"".join(rows)}'
             f'</tbody></table></div>' if rows else
             empty("No stacks yet", "Stacks appear when a container has the backtrack.mode label."))
    body = (header("Stacks", "fleet") + "".join(callout(*w) for w in warnings[:2])
            + f'<div class="ob-grid ob-grid--4">{cards}</div>'
            + f'<section class="ob-card ob-card--flush">{table}</section>')
    return f'<main id="main" class="ob-page">{body}</main>'


def stack_page(stack: dict, agent: dict, snapshots: list[dict], csrf: str) -> str:
    name = stack["name"]
    last = stack.get("last") or {}
    actions = (post_button(f"/stacks/{u(name)}/backup", "Back up for migration", csrf, fields={"trigger": "pre-migrate"})
               + post_button(f"/stacks/{u(name)}/backup", "Back up now", csrf, "ob-btn ob-btn--primary",
                             fields={"trigger": "manual"}))
    warn = callout("error", "Misconfigured", stack["error"]) if stack.get("error") else ""
    keep = ", ".join(f"{v} {k}" for k, v in (stack.get("keep") or {}).items())
    containers = ", ".join(f'{c["service"] or c["name"]} ({c["state"]})' for c in stack.get("containers", []))
    details = (f'<dl class="ob-dl"><dt>Machine</dt><dd class="ob-code">{e(agent["machine"])}</dd>'
               f'<dt>Mode</dt><dd>{e(stack.get("mode"))}</dd><dt>Data</dt><dd class="ob-code">{e(stack.get("path"))}</dd>'
               f'<dt>Retention</dt><dd>{e(keep)}</dd><dt>Containers</dt><dd>{e(containers or "none")}</dd>'
               f'<dt>Last backup</dt><dd>{e(ago(last.get("time")))} {result_status(last.get("state"))}</dd></dl>')
    snap_rows = "".join(
        f'<tr><td>{e(s["time"][:16].replace("T", " "))}</td><td class="ob-code">{e(s.get("machine") or "–")}</td>'
        f'<td class="ob-muted">{e(s.get("trigger") or "–")}</td><td class="num">{e(size(s.get("data_added")))}</td>'
        f'<td class="ob-code">{e(s["short_id"])}</td></tr>' for s in reversed(snapshots))
    snaps = (f'<div class="ob-table-wrap"><table class="ob-table"><thead><tr><th>Taken</th><th>Machine</th>'
             f'<th>Trigger</th><th class="num">Added</th><th>Snapshot</th></tr></thead><tbody>{snap_rows}</tbody>'
             f'</table></div>' if snapshots else empty("No snapshots yet", "The first one is taken at the next nightly run."))
    options = "".join(f'<option value="{e(s["id"])}">{e(s["time"][:16].replace("T", " "))} · {e(s["short_id"])} · '
                      f'{e(s.get("machine") or "")}</option>' for s in reversed(snapshots))
    restore = (
        f'<section class="ob-section ob-section--card"><div class="ob-section__head"><div><h2 class="ob-h2">Restore</h2>'
        f'<p class="ob-small ob-muted">Stops the stack, keeps the current folder as {e(name)}.pre-restore and starts it again.</p></div></div>'
        f'<form class="ob-card" method="post" action="/stacks/{u(name)}/restore"><div class="ob-stack">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        f'<label class="ob-field"><span class="ob-field__label">Snapshot</span><span class="ob-select">'
        f'<select class="ob-input" name="snapshot">{options}</select>{ICONS["chevron-down"]}</span></label>'
        f'<label class="ob-field"><span class="ob-field__label">Type {e(name)} to confirm</span>'
        f'<input class="ob-input ob-input--mono" name="confirm" autocomplete="off" spellcheck="false" required '
        f'pattern="{e(name)}"></label><div class="ob-cluster ob-cluster--end">'
        f'<button class="ob-btn ob-btn--danger" type="submit">Restore {e(name)}</button></div></div></form></section>'
        if snapshots else "")
    body = (header(name, agent["machine"], actions=actions) + warn
            + f'<div class="ob-panel">{details}</div>'
            + f'<section class="ob-section ob-section--card"><div class="ob-section__head"><div><h2 class="ob-h2">Snapshots</h2>'
            + f'</div></div><div class="ob-card ob-card--flush">{snaps}</div></section>' + restore)
    return f'<main id="main" class="ob-page">{body}</main>'


def agents_page(agents: list[dict], lease: dict | None) -> str:
    rows = []
    for a in agents:
        job = a.get("job")
        ln = a.get("last_nightly") or {}
        holder = (lease or {}).get("machine") == a["machine"]
        rows.append(
            f'<tr><td><a class="ob-h3 ob-table__link" href="/agents/{u(a["machine"])}">{e(a["machine"])}</a></td>'
            f'<td class="ob-code">{e(a.get("version"))}</td>'
            f'<td>{e(job["kind"] + (" " + job["stack"] if job.get("stack") else "")) if job else "Idle"}</td>'
            f'<td>{e(ago(ln.get("finished")))} {result_status(ln.get("state")) if ln else ""}</td>'
            f'<td>{"Maintenance" if holder else "–"}</td>'
            f'<td>{status("Online", "up") if a.get("online") else status("Offline", "down")}</td></tr>')
    table = (f'<div class="ob-table-wrap"><table class="ob-table"><thead><tr><th>Agent</th><th>Version</th>'
             f'<th>Now</th><th>Last nightly</th><th>Role</th><th>State</th></tr></thead><tbody>{"".join(rows)}'
             f'</tbody></table></div>' if rows else
             empty("No agents yet", "Agents appear once they write their presence file to the share."))
    return (f'<main id="main" class="ob-page">{header("Agents", "fleet")}'
            f'<section class="ob-card ob-card--flush">{table}</section></main>')


SETTING_LABELS = {
    "BACKUP_SCHEDULE": ("Nightly backup", "HH:MM, local time"),
    "KEEP": ("Retention", "e.g. daily=7,weekly=4,monthly=6"),
    "PRUNE_SCHEDULE": ("Prune", "Weekday and time, e.g. sun 13:00"),
    "VERIFY_SCHEDULE": ("Monthly check", "Day of month and time, e.g. 1 14:00"),
    "THEME": ("Hub theme", "default or orbit"),
}


def agent_page(agent: dict, settings: list[dict] | None, lease: dict | None, hub_machine: str, csrf: str,
               error: str = "") -> str:
    m = agent["machine"]
    holder = (lease or {}).get("machine") == m
    rows = []
    for s in settings or []:
        if s["key"] not in SETTING_LABELS:
            continue
        title, help_ = SETTING_LABELS[s["key"]]
        if s["locked"]:
            control = f'<span class="ob-code">{e(s["value"])}</span>'
            desc = "From env"
        else:
            control = (f'<form class="bt-autosave ob-cluster ob-cluster--nowrap" method="post" '
                       f'action="/agents/{u(m)}/settings"><input type="hidden" name="csrf" value="{e(csrf)}">'
                       f'<input class="ob-input ob-input--mono" name="{e(s["key"])}" value="{e(s["value"])}" '
                       f'aria-label="{e(title)}" spellcheck="false"><noscript><button class="ob-btn ob-btn--outline '
                       f'ob-btn--sm" type="submit">Save {e(title.lower())}</button></noscript></form>')
            desc = help_ + (" · saved on the agent" if s["source"] == "saved" else "")
        rows.append(f'<div class="ob-setting"><div class="ob-setting__text"><p class="ob-setting__title">{e(title)}</p>'
                    f'<p class="ob-setting__desc">{e(desc)}</p></div><div class="ob-setting__control">{control}</div></div>')
    if settings is None:
        settings_html = empty("Agent unreachable", "Settings show again when the hub can reach this agent.")
    else:
        settings_html = f'<div class="ob-settings">{"".join(rows)}</div>'
    if holder:
        lease_control = f'<span>{status("Holds the lease", "up")}</span>'
    else:
        lease_control = post_button("/repository/lease", "Make maintenance agent", csrf,
                                    "ob-btn ob-btn--outline ob-btn--sm", {"machine": m})
    maint = (f'<div class="ob-settings"><div class="ob-setting"><div class="ob-setting__text">'
             f'<p class="ob-setting__title">Maintenance agent</p><p class="ob-setting__desc">'
             f'{"Assigned by " + e(lease.get("assigned_by")) if holder and lease else "Prunes and checks the shared repository"}'
             f'</p></div><div class="ob-setting__control">{lease_control}</div></div></div>')
    warn = callout("error", "Settings not saved", error) if error else ""
    state = "online" if agent.get("online") else "offline"
    body = (header(m, f"agent · {state}", "Changes save as you make them.") + warn
            + f'<section class="ob-section ob-section--card"><div class="ob-section__head"><div><h2 class="ob-h2">Schedule'
            + f'</h2></div></div>{settings_html}</section>'
            + f'<section class="ob-section ob-section--card"><div class="ob-section__head"><div><h2 class="ob-h2">'
            + f'Repository</h2></div></div>{maint}</section>')
    return f'<main id="main" class="ob-page ob-page--narrow">{body}</main>'


def jobs_page(jobs: list[tuple[str, dict]], unreachable: list[str]) -> str:
    rows = "".join(
        f'<tr><td><a class="ob-h3 ob-table__link" href="/jobs/{u(m)}/{u(j["id"])}">{e(j["kind"])}</a></td>'
        f'<td class="ob-code">{e(m)}</td><td>{e(j.get("stack") or "–")}</td>'
        f'<td>{e(ago(j.get("started") or j.get("created")))}</td>'
        f'<td class="num">{e(_duration(j))}</td><td>{result_status(j.get("state"))}</td></tr>' for m, j in jobs)
    warn = (callout("warning", "Some agents didn't answer", ", ".join(unreachable)) if unreachable else "")
    table = (f'<div class="ob-table-wrap"><table class="ob-table"><thead><tr><th>Job</th><th>Machine</th><th>Stack</th>'
             f'<th>Started</th><th class="num">Took</th><th>State</th></tr></thead><tbody>{rows}</tbody></table></div>'
             if rows else empty("No jobs yet", "Jobs appear after the first scheduled or manual run."))
    return (f'<main id="main" class="ob-page">{header("Jobs", "fleet")}{warn}'
            f'<section class="ob-card ob-card--flush">{table}</section></main>')


def _duration(j: dict) -> str:
    if not j.get("started"):
        return "–"
    end = j.get("finished") or time.time()
    s = int(end - j["started"])
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def job_page(machine: str, job: dict) -> str:
    log = "\n".join(job.get("log") or [])
    err = callout("error", "Failed", job["error"]) if job.get("error") else ""
    refresh = " data-refresh" if job.get("state") in ("queued", "running") else ""
    title = job["kind"] + (f" {job['stack']}" if job.get("stack") else "")
    return (f'<main id="main" class="ob-page"{refresh}>{header(title, machine)}'
            f'<div class="ob-cluster">{result_status(job.get("state"))}<span class="ob-muted">'
            f'{e(ago(job.get("started") or job.get("created")))} · {e(_duration(job))}</span></div>{err}'
            f'<section class="ob-section ob-section--card"><div class="ob-section__head"><div><h2 class="ob-h2">Log</h2>'
            f'</div></div><pre class="ob-panel ob-code bt-log">{e(log) or "No output yet."}</pre></section></main>')


def repository_page(repo: str, snapshot_count: int | None, stack_count: int | None, lease: dict | None,
                    agents: list[dict], csrf: str) -> str:
    lease_dd = (f'<span class="ob-code">{e(lease["machine"])}</span> · assigned by {e(lease["assigned_by"])}, '
                f'renewed {e(ago(lease["renewed_at"]))}' if lease else "None")
    details = (f'<dl class="ob-dl"><dt>Repository</dt><dd class="ob-code">{e(repo)}</dd>'
               f'<dt>Snapshots</dt><dd>{e("–" if snapshot_count is None else snapshot_count)}</dd>'
               f'<dt>Stacks with snapshots</dt><dd>{e("–" if stack_count is None else stack_count)}</dd>'
               f'<dt>Maintenance agent</dt><dd>{lease_dd}</dd></dl>')
    options = "".join(f'<option value="{e(a["machine"])}"{" selected" if lease and lease["machine"] == a["machine"] else ""}>'
                      f'{e(a["machine"])}</option>' for a in agents)
    form = (f'<form class="ob-card" method="post" action="/repository/lease"><div class="ob-stack">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}"><label class="ob-field">'
            f'<span class="ob-field__label">Maintenance agent</span><span class="ob-select"><select class="ob-input" '
            f'name="machine">{options}</select>{ICONS["chevron-down"]}</span>'
            f'<span class="ob-field__help">Only this agent prunes and checks the repository.</span></label>'
            f'<div class="ob-cluster ob-cluster--end"><button class="ob-btn ob-btn--primary" type="submit">'
            f'Assign maintenance</button></div></div></form>')
    return (f'<main id="main" class="ob-page ob-page--narrow">{header("Repository", "shared")}'
            f'<div class="ob-panel">{details}</div>'
            f'<section class="ob-section ob-section--card"><div class="ob-section__head"><div><h2 class="ob-h2">'
            f'Maintenance</h2></div></div>{form}</section></main>')
