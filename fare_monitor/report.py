"""Watch summaries, the static HTML dashboard and the Markdown job summary."""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from .analysis import row_view
from .config import Config
from .messages import arrival_label, fmt_clock, fmt_date, local_time, price_history_summary
from .models import Verification, fmt_inr, fmt_minutes
from .watches import watch_from_row


def watch_infos(db, config: Config, watch, run_id: int | None, errored: set | None = None,
                dates: list[date] | None = None) -> list[dict]:
    """Per-date picture for a watch: cheapest found in the latest search, confirmed price in this run,
    lowest confirmed ever, Google price insights, trend."""
    min_ver = int(config.verification.alert_min_status)
    out = []
    for d in dates or watch.dates:
        ds = d.isoformat()
        search, rows = db.latest_route_search(watch.origin, watch.destination, ds)
        views = [row_view(r) for r in rows if r["stops"] <= watch.max_stops
                 and (not watch.airlines or all(fn.split(" ")[0] in watch.airlines
                                                for fn in json.loads(r["flight_numbers"])))]
        confirmed = None
        if run_id is not None:
            c = db.run_observations(run_id, watch.origin, watch.destination, ds, min_ver, watch.max_stops,
                                    watch.airlines)
            confirmed = row_view(c[0]) if c else None
        low = db.all_time_low(watch.origin, watch.destination, ds, min_ver, watch.max_stops, watch.airlines)
        meta = json.loads(search["meta_json"]) if search and search["meta_json"] else {}
        out.append({
            "date": ds,
            "cheapest": views[0] if views else None,
            "confirmed": confirmed,
            "all_time_low": row_view(low) if low else None,
            "best_value": min((v for v in views if v.get("value_score") is not None),
                              key=lambda v: v["value_score"], default=None),
            "options": views[:10],
            "searched_at": search["started_at"] if search else None,
            "price_insights": meta.get("price_insights"),
            "error": d in (errored or set()),
            "trend": [{"t": r["started_at"], "price": r["price"], "v": r["verification"]}
                      for r in db.lows_per_run(watch.origin, watch.destination, ds, 1, watch.max_stops,
                                               watch.airlines)],
        })
    return out


def build_summary(db, config: Config, now: datetime | None = None, usage_line: str = "") -> dict:
    now = now or datetime.now(timezone.utc)
    tz = config.tz
    today = now.astimezone(tz).date()
    min_ver = int(config.verification.alert_min_status)
    watches = []
    for row in sorted(db.watches(), key=lambda r: (-r["active"], -int(r["id"]) if str(r["id"]).isdigit() else 0)):
        w = watch_from_row(row)
        infos = watch_infos(db, config, w, None)
        for info in infos:
            info["stale"] = bool(info["searched_at"]) and \
                now - datetime.fromisoformat(info["searched_at"]) > timedelta(days=config.verification.stale_after_days)
        airlines: dict[str, dict] = {}
        ds = [d.isoformat() for d in w.dates]
        if ds:
            for r in db.airline_comparison(w.origin, w.destination, ds, 1):
                a = airlines.setdefault(r["airline"], {"airline": r["airline"], "by_date": {}, "min": r["min_price"]})
                a["by_date"][r["travel_date"]] = r["min_price"]
                a["min"] = min(a["min"], r["min_price"])
        lows = [i["all_time_low"] for i in infos if i["all_time_low"]]
        watches.append({
            "watch": w, "active": bool(row["active"]), "last_checked": row["last_checked"],
            "infos": infos, "airlines": sorted(airlines.values(), key=lambda a: a["min"]),
            "best_confirmed": min(lows, key=lambda o: o["price"]) if lows else None,
            "alerts": [dict(a) for a in db.recent_alerts(10, w.id)],
            "future": any(d >= today for d in w.dates),
        })
    last = db.last_run()
    if usage_line:
        db.kv_set("usage_line", usage_line)
    else:
        usage_line = db.kv_get("usage_line", "")
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_local": now.astimezone(tz).strftime("%d %b %Y %H:%M IST"),
        "watches": watches,
        "last_success_local": local_time(last["started_at"], tz) if last else None,
        "providers": [dict(p) for p in db.provider_status()],
        "usage_line": usage_line,
        "alert_min_status": Verification(min_ver).label,
    }


# ---- Markdown (GitHub Actions job summary) ---------------------------------------------------------
def to_markdown(s: dict, config: Config) -> str:
    out = [f"## ✈️ Fare monitor — {s['generated_local']}", "", s["usage_line"].replace("**", ""), ""]
    active = [w for w in s["watches"] if w["active"]]
    if not active:
        out.append("No active fare watches. Create one: **Issues → New issue → Fare watch**.")
    for entry in active:
        w = entry["watch"]
        out += [f"### #{w.id} {w.route} · {w.frequency} · book-now {fmt_inr(w.target) if w.target else '—'}", "",
                "| Date | Cheapest found | Status | Flight | Lowest confirmed ever |", "|---|---|---|---|---|"]
        for i in entry["infos"]:
            c, low = i["cheapest"], i["all_time_low"]
            out.append(f"| {fmt_date(i['date'])} | {fmt_inr(c['price']) if c else '—'} | "
                       f"{Verification(c['verification']).label if c else ''} | "
                       f"{(c['airline'] + ' ' + c['flights_label'] + ', ' + fmt_clock(c['departure_local'])) if c else ''} | "
                       f"{fmt_inr(low['price']) if low else '—'} |")
        out.append("")
    out += [f"**Last successful search:** {s['last_success_local'] or 'never'}"]
    return "\n".join(out) + "\n"


# ---- HTML dashboard ------------------------------------------------------------------------------
def _e(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def _badge(level: int) -> str:
    v = Verification(level)
    cls = {Verification.VERIFIED: "ok", Verification.SELLER_QUOTED: "quoted", Verification.INDICATIVE: "ind"}[v]
    return f'<span class="badge {cls}">{_e(v.label)}</span>'


def _book_link(o: dict) -> str:
    req = o.get("booking_request")
    if req and req.get("url"):
        fields = "".join(f'<input type="hidden" name="{_e(k)}" value="{_e(v)}">'
                         for k, v in parse_qsl(req.get("post_data") or "", keep_blank_values=True))
        return (f'<form method="post" action="{_e(req["url"])}" target="_blank" rel="noopener">{fields}'
                f'<button type="submit" class="link">Book with {_e(o.get("seller") or "seller")}</button></form>')
    return f'<a href="{_e(o["booking_url"])}" target="_blank" rel="noopener">Open on Google Flights</a>'


def _tile(info: dict, target: float | None, tz) -> str:
    c, low = info["cheapest"], info["all_time_low"]
    pi = info.get("price_insights") or {}
    head = f'<div class="tl">{_e(fmt_date(info["date"]))}</div>'
    if not c:
        return f'<div class="tile">{head}<div class="tv">—</div><div class="ts">no flights found yet</div></div>'
    hit = target and low and low["price"] <= target
    return (f'<div class="tile{" hit" if hit else ""}">{head}<div class="tv">{_e(fmt_inr(c["price"]))}</div>'
            f'<div class="ts">{_badge(c["verification"])} cheapest found · {_e(local_time(info["searched_at"], tz))}'
            f'{" · <b>stale</b>" if info.get("stale") else ""}</div>'
            f'<div class="ts">{_e(c["airline"])} {_e(c["flights_label"])} · {_e(fmt_clock(c["departure_local"]))}→'
            f'{_e(arrival_label(c))} · {_e(fmt_minutes(c["duration_minutes"]))} · {_e(c["stops_label"])}</div>'
            f'<div class="ts">Lowest confirmed ever: <b>{_e(fmt_inr(low["price"]) if low else "—")}</b>'
            f'{" 🎯 under book-now price" if hit else ""}</div>'
            + (f'<div class="ts">Google: prices are <b>{_e(pi["price_level"])}</b></div>' if pi.get("price_level") else "")
            + "</div>")


def _options_table(info: dict, tz) -> str:
    rows = []
    for o in info["options"]:
        bag = " · ".join(x for x in (o.get("cabin_baggage"), o.get("checked_baggage")) if x) or "—"
        rows.append(
            f"<tr><td class='num'>{_e(fmt_inr(o['price']))}</td><td>{_badge(o['verification'])}</td>"
            f"<td>{_e(o['airline'])}<br><span class='muted'>{_e(o['flights_label'])}</span></td>"
            f"<td>{_e(fmt_clock(o['departure_local']))} → {_e(arrival_label(o))}</td>"
            f"<td>{_e(fmt_minutes(o['duration_minutes']))}<br><span class='muted'>{_e(o['stops_label'])}"
            f"{': ' + _e(o['layovers_label']) if o['stops'] else ''}</span></td>"
            f"<td>{_e(bag)}</td><td>{_e(o.get('seller') or '—')}</td><td>{_book_link(o)}</td></tr>")
    if not rows:
        return "<p class='muted'>No options stored yet.</p>"
    return ("<div class='scroll'><table><thead><tr><th>Fare</th><th>Status</th><th>Flight</th><th>Times</th>"
            "<th>Duration</th><th>Baggage</th><th>Seller</th><th>Book</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def _watch_section(entry: dict, config: Config, idx: int) -> str:
    w, tz = entry["watch"], config.tz
    infos = entry["infos"]
    state = "active" if entry["active"] else "closed"
    head = (f"<h2>{_e(w.route)} <span class='muted'>· watch #{_e(w.id)} ({state})</span></h2>"
            f"<p class='muted'>{_e(', '.join(date.fromisoformat(i['date']).strftime('%d %b') for i in infos))} · "
            f"airlines: {_e(w.airlines_label)} · checks {_e(w.days_label if len(w.days) == 7 else w.frequency.lower() + ' (' + w.days_label + ')')} · book-now price "
            f"<b>{_e(fmt_inr(w.target) if w.target else 'not set')}</b> · last checked "
            f"{_e(entry['last_checked'] or 'never')}"
            + (f" · <a href='{_e(w.url)}' target='_blank' rel='noopener'>open watch</a>" if w.url else "") + "</p>")
    tiles = "<div class='tiles'>" + "".join(_tile(i, w.target, tz) for i in infos) + "</div>"
    trend_notes = "".join(f"<li>{_e(fmt_date(i['date']))}: {_e(price_history_summary(i.get('price_insights')))}"
                          "</li>" for i in infos if price_history_summary(i.get("price_insights")))
    trend_notes = (f"<div class='card'><b>Google Flights price trend</b> <span class='muted'>(indicative)</span>"
                   f"<ul>{trend_notes.replace('**', '')}</ul></div>") if trend_notes else ""
    if len(infos) <= 4:
        series = [{"name": fmt_date(i["date"]), "points": i["trend"]} for i in infos]
    else:
        merged: dict[str, float] = {}
        for i in infos:
            for p in i["trend"]:
                merged[p["t"]] = min(merged.get(p["t"], 1e12), p["price"])
        series = [{"name": "Cheapest of all dates", "points": [{"t": t, "price": v} for t, v in sorted(merged.items())]}]
    chart = (f"<div class='card'><b>Cheapest price found per check</b><div class='legend' id='lg{idx}'></div>"
             f"<div class='chart' id='ch{idx}' data-series='{_e(json.dumps(series))}'><div class='tip'></div></div>"
             "</div>")
    airline_rows = "".join(
        f"<tr><td>{_e(a['airline'])}</td>"
        + "".join(f"<td class='num'>{_e(fmt_inr(a['by_date'].get(i['date'])))}</td>" for i in infos)
        + "</tr>" for a in entry["airlines"])
    airlines = ("<h3>Airline comparison <span class='muted'>(cheapest seen, any status)</span></h3><div class='scroll'>"
                "<table><thead><tr><th>Airline</th>"
                + "".join(f"<th class='num'>{_e(date.fromisoformat(i['date']).strftime('%d %b'))}</th>" for i in infos)
                + f"</tr></thead><tbody>{airline_rows}</tbody></table></div>") if airline_rows else ""
    options = "".join(
        f"<details{' open' if n == 0 else ''}><summary>Options for {_e(fmt_date(i['date']))}</summary>"
        f"{_options_table(i, tz)}</details>" for n, i in enumerate(infos))
    grouped: dict = {}
    for a in entry["alerts"]:
        if a["kind"] == "budget":
            continue
        g = grouped.setdefault((a["sent_at"], a["observation_id"]), dict(a, kinds=[]))
        if a["kind"].replace("_", " ") not in g["kinds"]:
            g["kinds"].append(a["kind"].replace("_", " "))
    alerts = "".join(
        f"<tr><td>{_e(local_time(a['sent_at'], tz))}</td><td>{_e(' + '.join(a['kinds']))}</td>"
        f"<td>{_e(fmt_date(a['travel_date']) if a['travel_date'] else '')}</td>"
        f"<td class='num'>{_e(fmt_inr(a['price']))}</td></tr>" for a in grouped.values())
    alerts = ("<h3>Alerts sent</h3><div class='scroll'><table><thead><tr><th>Sent</th><th>Type</th><th>Date</th>"
              f"<th class='num'>Fare</th></tr></thead><tbody>{alerts}</tbody></table></div>") if alerts else ""
    return f"<section>{head}{tiles}{trend_notes}{chart}{airlines}<h3>Latest options</h3>{options}{alerts}</section>"


def to_html(s: dict, config: Config) -> str:
    tz = config.tz
    active = [e for e in s["watches"] if e["active"]]
    closed = [e for e in s["watches"] if not e["active"]]
    sections = "".join(_watch_section(e, config, n) for n, e in enumerate(active + closed[:3]))
    if not sections:
        sections = ("<div class='card'>No fare watches yet. In the repository open <b>Issues → New issue → "
                    "Fare watch</b>, fill in the route and dates, and submit.</div>")
    prov_rows = "".join(
        f"<tr><td>{_e(p['provider'])}</td><td>{_e(p['origin'])}→{_e(p['destination'])} {_e(p['travel_date'])}</td>"
        f"<td><span class='badge {'ok' if p['status'] == 'ok' else 'ind' if p['status'] == 'empty' else 'err'}'>"
        f"{_e(p['status'])}</span></td><td>{_e(local_time(p['started_at'], tz))}</td>"
        f"<td>{_e(local_time(p['last_success'], tz) if p['last_success'] else '—')}</td>"
        f"<td class='muted'>{_e((p['error'] or '')[:160])}</td></tr>" for p in s["providers"][-30:])
    return TEMPLATE.format(
        generated=_e(s["generated_local"]),
        last_success=_e(s["last_success_local"] or "never"),
        usage=_e(s["usage_line"].replace("**", "")),
        min_status=_e(s["alert_min_status"]),
        sections=sections,
        prov_rows=prov_rows or "<tr><td colspan='6' class='muted'>No searches yet.</td></tr>",
    )


def write_reports(s: dict, config: Config) -> dict[str, Path]:
    out = Path(config.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"html": out / "index.html", "md": out / "summary.md"}
    paths["html"].write_text(to_html(s, config), encoding="utf-8")
    paths["md"].write_text(to_markdown(s, config), encoding="utf-8")
    return paths


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flight Fare Watch</title>
<style>
:root {{ color-scheme: light; --bg:#f6f6f4; --surface-1:#fcfcfb; --border:#e3e2de; --text-primary:#0b0b0b;
  --text-secondary:#52514e; --muted:#77756f; --grid:#ebeae6; --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
  --ok-bg:#e3f4ea; --ok-fg:#0d6b35; --q-bg:#fdf1d8; --q-fg:#7a5200; --ind-bg:#ecebe8; --ind-fg:#52514e;
  --err-bg:#fbe5e5; --err-fg:#a11d1d; --link:#1c5cab; --hit:#0d6b35; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ color-scheme: dark; --bg:#111110;
  --surface-1:#1a1a19; --border:#2e2e2c; --text-primary:#f0efec; --text-secondary:#c3c2b7; --muted:#9a998f;
  --grid:#2a2a28; --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --ok-bg:#12321f; --ok-fg:#7fd6a0;
  --q-bg:#3a2c0b; --q-fg:#f1c25e; --ind-bg:#2a2a28; --ind-fg:#c3c2b7; --err-bg:#3d1515; --err-fg:#f29b9b;
  --link:#86b6ef; --hit:#7fd6a0; }} }}
:root[data-theme="dark"] {{ color-scheme: dark; --bg:#111110; --surface-1:#1a1a19; --border:#2e2e2c;
  --text-primary:#f0efec; --text-secondary:#c3c2b7; --muted:#9a998f; --grid:#2a2a28; --s1:#3987e5; --s2:#d95926;
  --s3:#199e70; --s4:#c98500; --ok-bg:#12321f; --ok-fg:#7fd6a0; --q-bg:#3a2c0b; --q-fg:#f1c25e; --ind-bg:#2a2a28;
  --ind-fg:#c3c2b7; --err-bg:#3d1515; --err-fg:#f29b9b; --link:#86b6ef; --hit:#7fd6a0; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text-primary); font:14px/1.5 system-ui,-apple-system,
  "Segoe UI",Roboto,sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:18px; margin:0 0 4px; }} h3 {{ font-size:14px; margin:20px 0 8px; }}
section {{ margin-top:32px; padding-top:20px; border-top:1px solid var(--border); }}
.muted {{ color:var(--muted); font-weight:normal; }} a, .link {{ color:var(--link); }}
.link {{ background:none; border:0; padding:0; font:inherit; cursor:pointer; text-decoration:underline; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; margin-top:12px; }}
.tile, .card {{ background:var(--surface-1); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }}
.card {{ margin-top:12px; }} .tile.hit {{ border-color:var(--hit); }}
.tl {{ color:var(--text-secondary); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
.tv {{ font-size:28px; font-weight:650; font-variant-numeric:tabular-nums; margin:2px 0; }}
.ts {{ color:var(--text-secondary); font-size:13px; margin-top:2px; }}
.badge {{ display:inline-block; font-size:11px; font-weight:600; padding:1px 7px; border-radius:999px; }}
.badge.ok {{ background:var(--ok-bg); color:var(--ok-fg); }} .badge.quoted {{ background:var(--q-bg); color:var(--q-fg); }}
.badge.ind {{ background:var(--ind-bg); color:var(--ind-fg); }} .badge.err {{ background:var(--err-bg); color:var(--err-fg); }}
.scroll {{ overflow-x:auto; }} .scroll table {{ min-width:720px; }}
table {{ border-collapse:collapse; width:100%; background:var(--surface-1); font-size:13px; }}
th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--border); vertical-align:top; }}
th {{ color:var(--text-secondary); font-weight:600; font-size:12px; }}
td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.legend {{ display:flex; flex-wrap:wrap; gap:6px 16px; font-size:13px; color:var(--text-secondary); margin:6px 0; }}
.sw {{ display:inline-block; width:14px; height:3px; border-radius:2px; vertical-align:middle; margin-right:6px; }}
.chart {{ position:relative; }} .chart svg {{ width:100%; height:240px; display:block; }}
.tip {{ position:absolute; pointer-events:none; background:var(--surface-1); border:1px solid var(--border);
  border-radius:8px; padding:6px 10px; font-size:12px; box-shadow:0 2px 8px rgba(0,0,0,.12); display:none; white-space:nowrap; }}
details {{ margin-top:6px; }} summary {{ cursor:pointer; color:var(--text-secondary); padding:4px 0; }}
ul {{ margin:6px 0; padding-left:20px; }} .note {{ font-size:13px; color:var(--text-secondary); }}
</style></head>
<body><main>
<h1>Flight fare watch</h1>
<div class="muted">Updated {generated} · last successful search {last_success} · {usage}</div>
<p class="note">Alerts use prices at <b>{min_status}</b> or better. 🟡 SELLER-QUOTED = final price quoted by the
named airline or agent at the time shown. ⚪ INDICATIVE = search price only (confirmed before any alert).
Add or change trips with the <b>Fare watch</b> issue form in the repository.</p>
{sections}
<section><h2>Sources</h2><div class="scroll"><table><thead><tr><th>Source</th><th>Route / date</th><th>Last status</th>
<th>Last search</th><th>Last success</th><th>Error</th></tr></thead><tbody>{prov_rows}</tbody></table></div>
<p class="note">This tool never books or pays. Booking links open the seller's site for you to decide.</p></section>
</main>
<script>
(function () {{
  const colors = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"];
  const NS = "http://www.w3.org/2000/svg";
  const fmt = v => "\\u20b9" + Math.round(v).toLocaleString("en-IN");
  const fmtT = t => new Date(t).toLocaleString("en-IN", {{timeZone: "Asia/Kolkata", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit"}});
  document.querySelectorAll(".chart").forEach((box, idx) => {{
    const series = JSON.parse(box.dataset.series || "[]");
    const tip = box.querySelector(".tip");
    const legend = document.getElementById("lg" + box.id.slice(2));
    series.forEach((s, i) => {{
      const el = document.createElement("span");
      el.innerHTML = '<span class="sw" style="background:' + colors[i % 4] + '"></span>';
      el.appendChild(document.createTextNode(s.name));
      legend.appendChild(el);
    }});
    const pts = series.flatMap(s => s.points.map(p => ({{t: Date.parse(p.t), v: p.price}})));
    const svg = document.createElementNS(NS, "svg");
    box.appendChild(svg);
    if (!pts.length) {{
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", 8); t.setAttribute("y", 30); t.setAttribute("fill", "var(--muted)");
      t.textContent = "No checks yet."; svg.appendChild(t); return;
    }}
    function draw() {{
      svg.innerHTML = "";
      const W = box.clientWidth, H = 240, m = {{l: 64, r: W < 600 ? 16 : 24, t: 12, b: 28}};
      svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      let t0 = Math.min(...pts.map(p => p.t)), t1 = Math.max(...pts.map(p => p.t));
      if (t1 === t0) {{ t0 -= 43200e3; t1 += 43200e3; }}
      let v0 = Math.min(...pts.map(p => p.v)), v1 = Math.max(...pts.map(p => p.v));
      const pad = Math.max((v1 - v0) * 0.15, 200); v0 = Math.max(0, v0 - pad); v1 += pad;
      const x = t => m.l + (t - t0) / (t1 - t0) * (W - m.l - m.r);
      const y = v => m.t + (1 - (v - v0) / (v1 - v0)) * (H - m.t - m.b);
      const mk = (tag, a) => {{ const e = document.createElementNS(NS, tag);
        for (const k in a) e.setAttribute(k, a[k]); svg.appendChild(e); return e; }};
      for (let i = 0; i <= 4; i++) {{
        const v = v0 + (v1 - v0) * i / 4, yy = y(v);
        mk("line", {{x1: m.l, x2: W - m.r, y1: yy, y2: yy, stroke: "var(--grid)", "stroke-width": 1}});
        mk("text", {{x: m.l - 8, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)"}}).textContent = fmt(v);
      }}
      const nx = W < 600 ? 1 : 3;
      let lastLabel = "";
      for (let i = 0; i <= nx; i++) {{
        const t = t0 + (t1 - t0) * i / nx;
        const label = new Date(t).toLocaleDateString("en-IN", {{timeZone: "Asia/Kolkata", day: "2-digit", month: "short"}});
        if (label === lastLabel) continue;
        lastLabel = label;
        mk("text", {{x: x(t), y: H - 8, "text-anchor": i === 0 ? "start" : i === nx ? "end" : "middle", "font-size": 11,
          fill: "var(--muted)"}}).textContent = new Date(t).toLocaleDateString("en-IN", {{timeZone: "Asia/Kolkata",
          day: "2-digit", month: "short"}});
      }}
      series.forEach((s, i) => {{
        if (!s.points.length) return;
        const P = s.points.map(p => [x(Date.parse(p.t)), y(p.price)]);
        mk("polyline", {{points: P.map(p => p.join(",")).join(" "), fill: "none", stroke: colors[i % 4],
          "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"}});
        P.forEach(p => mk("circle", {{cx: p[0], cy: p[1], r: P.length > 40 ? 0 : 4, fill: colors[i % 4],
          stroke: "var(--surface-1)", "stroke-width": 2}}));
      }});
      const cross = mk("line", {{y1: m.t, y2: H - m.b, stroke: "var(--muted)", "stroke-width": 1, visibility: "hidden"}});
      const hit = mk("rect", {{x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent"}});
      hit.addEventListener("mousemove", ev => {{
        const r = svg.getBoundingClientRect(), mx = ev.clientX - r.left;
        const t = t0 + (mx - m.l) / (W - m.l - m.r) * (t1 - t0);
        const rows = series.map((s, i) => {{
          if (!s.points.length) return null;
          const p = s.points.reduce((a, b) => Math.abs(Date.parse(b.t) - t) < Math.abs(Date.parse(a.t) - t) ? b : a);
          return {{name: s.name, p, i}};
        }}).filter(Boolean);
        const tx = x(Date.parse(rows[0].p.t));
        cross.setAttribute("x1", tx); cross.setAttribute("x2", tx); cross.setAttribute("visibility", "visible");
        tip.innerHTML = "<b>" + fmtT(Date.parse(rows[0].p.t)) + " IST</b><br>" + rows.map(r =>
          '<span class="sw" style="background:' + colors[r.i % 4] + '"></span>' + r.name + ": <b>" + fmt(r.p.price) + "</b>").join("<br>");
        tip.style.display = "block";
        tip.style.left = Math.min(mx + 12, W - tip.offsetWidth - 4) + "px"; tip.style.top = "8px";
      }});
      hit.addEventListener("mouseleave", () => {{ tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); }});
    }}
    draw(); window.addEventListener("resize", draw);
  }});
}})();
</script>
</body></html>
"""
