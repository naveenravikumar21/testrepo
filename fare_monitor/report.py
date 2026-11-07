"""Monitoring summary, static HTML dashboard and Markdown job summary."""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from .analysis import row_view
from .config import Config
from .messages import arrival_label, fmt_clock, fmt_date, local_time
from .models import Verification, fmt_inr, fmt_minutes


def build_summary(db, config: Config, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    tz = config.trip.timezone
    min_ver = int(config.verification.alert_min_status)
    dates = []
    for d in config.trip.dates:
        ds = d.isoformat()
        rows, run, stale = db.latest_observations(ds, config.verification.stale_after_minutes, now)
        views = [row_view(r) for r in rows]
        eligible = [v for v in views if v["verification"] >= min_ver]
        current_best = min(eligible, key=lambda v: (v["price"], -v["verification"]), default=None)
        best_value = min((v for v in eligible if v.get("value_score") is not None),
                         key=lambda v: v["value_score"], default=None)
        low = db.all_time_low(ds, min_ver)
        trend = [{"t": r["started_at"], "price": r["price"]} for r in db.hourly_lows(ds, min_ver)]
        indicative_trend = [{"t": r["started_at"], "price": r["price"]} for r in db.hourly_lows(ds, 1)]
        insights, _ = db.latest_search_meta("serpapi_google_flights", ds)
        dates.append({
            "date": ds,
            "current_best": current_best,
            "best_value": best_value,
            "stale": stale,
            "searched_at": run["started_at"] if run else None,
            "all_time_low": row_view(low) if low else None,
            "options": views[:12],
            "trend": trend,
            "indicative_trend": indicative_trend,
            "price_insights": (insights or {}).get("price_insights"),
            "record_lows": [dict(r) for r in db.record_lows(ds)],
        })
    fresh_bests = [d["current_best"] for d in dates if d["current_best"] and not d["stale"]]
    overall_best = min(fresh_bests, key=lambda v: v["price"], default=None)

    airlines: dict[str, dict] = {}
    for r in db.airline_comparison(min_ver, since=now - timedelta(days=7)):
        a = airlines.setdefault(r["airline"], {"airline": r["airline"], "by_date": {}, "min": r["min_price"],
                                               "n": 0})
        a["by_date"][r["travel_date"]] = r["min_price"]
        a["min"] = min(a["min"], r["min_price"])
        a["n"] += r["n"]
    last = db.last_run()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_local": now.astimezone(tz).strftime("%d %b %Y %H:%M %Z"),
        "dates": dates,
        "overall_best": overall_best,
        "airlines": sorted(airlines.values(), key=lambda a: a["min"]),
        "last_success": last["started_at"] if last else None,
        "last_success_local": local_time(last["started_at"], tz) if last else None,
        "providers": [dict(p) for p in db.provider_status()],
        "recent_alerts": [dict(a) for a in db.recent_alerts(15)],
        "runs": [dict(r) for r in db.recent_runs(24)],
        "alert_min_status": Verification(min_ver).label,
    }


# ---- Markdown (GitHub Actions job summary) ---------------------------------------------------------
def to_markdown(s: dict, config: Config) -> str:
    out = [f"## ✈️ JAI → COK fare monitor — {s['generated_local']}", ""]
    out += ["| Date | Current lowest | Flight | Times | Duration | Status |", "|---|---|---|---|---|---|"]
    for d in s["dates"]:
        c = d["current_best"]
        if c:
            out.append(f"| {fmt_date(d['date'])} | **{fmt_inr(c['price'])}**{' (stale)' if d['stale'] else ''} | "
                       f"{c['airline']} {c['flights_label']} | {fmt_clock(c['departure_local'])}→{arrival_label(c)} | "
                       f"{fmt_minutes(c['duration_minutes'])}, {c['stops_label']} | "
                       f"{Verification(c['verification']).label} |")
        else:
            out.append(f"| {fmt_date(d['date'])} | — | no eligible fare | | | |")
    ob = s["overall_best"]
    out += ["", f"**Overall cheapest:** {fmt_inr(ob['price'])} on {fmt_date(ob['travel_date'])} "
                f"({ob['airline']} {ob['flights_label']})" if ob else "**Overall cheapest:** —",
            f"**Last successful search:** {s['last_success_local'] or 'never'}", "",
            "| Provider | Date | Last status | Last success | Error |", "|---|---|---|---|---|"]
    for p in s["providers"]:
        out.append(f"| {p['provider']} | {p['travel_date']} | {p['status']} | {p['last_success'] or '—'} | "
                   f"{(p['error'] or '')[:100]} |")
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
    return f'<a href="{_e(o["booking_url"])}" target="_blank" rel="noopener">Open booking site</a>'


def _tile(label: str, o: dict | None, sub: str = "") -> str:
    if not o:
        return f'<div class="tile"><div class="tl">{_e(label)}</div><div class="tv">—</div>' \
               f'<div class="ts">{_e(sub or "no eligible fare yet")}</div></div>'
    return (f'<div class="tile"><div class="tl">{_e(label)}</div><div class="tv">{_e(fmt_inr(o["price"]))}</div>'
            f'<div class="ts">{_e(o["airline"])} {_e(o["flights_label"])} · {_e(fmt_clock(o["departure_local"]))}→'
            f'{_e(arrival_label(o))} · {_e(fmt_minutes(o["duration_minutes"]))} · {_e(o["stops_label"])}</div>'
            f'<div class="ts">{_badge(o["verification"])} {_e(sub)}</div></div>')


def to_html(s: dict, config: Config) -> str:
    tz = config.trip.timezone
    d4 = s["dates"][0] if s["dates"] else None
    tiles = []
    for d in s["dates"]:
        sub = "STALE — last search " + local_time(d["searched_at"], tz) if d["stale"] and d["searched_at"] else ""
        tiles.append(_tile(f"Lowest · {fmt_date(d['date'])}", d["current_best"], sub))
    tiles.append(_tile("Overall lowest (both dates)", s["overall_best"],
                       fmt_date(s["overall_best"]["travel_date"]) if s["overall_best"] else ""))
    lows = "".join(
        f"<li>{_e(fmt_date(d['date']))}: <b>{_e(fmt_inr(d['all_time_low']['price']))}</b> "
        f"({_e(d['all_time_low']['airline'])} {_e(d['all_time_low']['flights_label'])}, "
        f"{_e(local_time(d['all_time_low']['observed_at'], tz))})</li>"
        for d in s["dates"] if d["all_time_low"])

    option_tables = []
    for d in s["dates"]:
        rows = []
        for o in d["options"]:
            bag = " · ".join(x for x in (o.get("cabin_baggage"), o.get("checked_baggage")) if x) or "—"
            rows.append(
                f"<tr><td class='num'>{_e(fmt_inr(o['price']))}</td><td>{_badge(o['verification'])}</td>"
                f"<td>{_e(o['airline'])}<br><span class='muted'>{_e(o['flights_label'])}</span></td>"
                f"<td>{_e(fmt_clock(o['departure_local']))} → {_e(arrival_label(o))}</td>"
                f"<td>{_e(fmt_minutes(o['duration_minutes']))}<br><span class='muted'>{_e(o['stops_label'])}"
                f"{': ' + _e(o['layovers_label']) if o['stops'] else ''}</span></td>"
                f"<td>{_e(bag)}</td><td>{_e(o['source'])}<br><span class='muted'>{_e(o.get('seller') or '')}"
                f"</span></td><td>{_e(local_time(o['observed_at'], tz))}</td><td>{_book_link(o)}</td></tr>")
        insight = ""
        pi = d.get("price_insights")
        if pi and pi.get("price_level"):
            rng = pi.get("typical_price_range") or []
            insight = (f"<p class='muted'>Google Flights says prices for this date are currently "
                       f"<b>{_e(pi['price_level'])}</b>"
                       + (f" (typical {_e(fmt_inr(rng[0]))}–{_e(fmt_inr(rng[1]))})" if len(rng) == 2 else "")
                       + ". Indicative context only.</p>")
        option_tables.append(
            f"<h3>{_e(fmt_date(d['date']))}</h3>{insight}"
            + (f"<p class='muted'>From the search at {_e(local_time(d['searched_at'], tz))}"
               + (" — <b>stale</b>" if d["stale"] else "") + "</p>" if d["searched_at"] else "")
            + ("<div class='scroll'><table><thead><tr><th>Fare</th><th>Status</th><th>Flight</th><th>Times</th>"
               "<th>Duration</th><th>Baggage</th><th>Source</th><th>Observed</th><th>Book</th></tr></thead><tbody>"
               + "".join(rows) + "</tbody></table></div>" if rows else "<p class='muted'>No options stored yet.</p>"))

    airline_rows = "".join(
        f"<tr><td>{_e(a['airline'])}</td>"
        + "".join(f"<td class='num'>{_e(fmt_inr(a['by_date'].get(d.isoformat())))}</td>" for d in config.trip.dates)
        + f"<td class='num'>{a['n']}</td></tr>" for a in s["airlines"])
    date_heads = "".join(f"<th class='num'>{_e(date.fromisoformat(d.isoformat()).strftime('%d %b'))}</th>"
                         for d in config.trip.dates)

    prov_rows = "".join(
        f"<tr><td>{_e(p['provider'])}</td><td>{_e(p['travel_date'])}</td>"
        f"<td><span class='badge {'ok' if p['status'] == 'ok' else 'ind' if p['status'] == 'empty' else 'err'}'>"
        f"{_e(p['status'])}</span></td><td>{_e(local_time(p['started_at'], tz))}</td>"
        f"<td>{_e(local_time(p['last_success'], tz) if p['last_success'] else '—')}</td>"
        f"<td class='num'>{p['result_count']} / {p['kept_count']}</td><td class='muted'>{_e((p['error'] or '')[:160])}</td></tr>"
        for p in s["providers"])

    alert_rows = "".join(
        f"<tr><td>{_e(local_time(a['sent_at'], tz))}</td><td>{_e(a['kind'])}</td><td>{_e(a['travel_date'])}</td>"
        f"<td class='num'>{_e(fmt_inr(a['price']))}</td><td class='num'>{_e(fmt_inr(a['previous_low']))}</td>"
        f"<td>{_e(a['channels'] or '—')}</td></tr>" for a in s["recent_alerts"])

    series = [{"name": fmt_date(d["date"]), "points": d["trend"]} for d in s["dates"]]
    chart_data = json.dumps(series).replace("</", "<\\/")
    table_rows = "".join(
        f"<tr><td>{_e(local_time(p['t'], tz))}</td><td>{_e(fmt_date(d['date']))}</td>"
        f"<td class='num'>{_e(fmt_inr(p['price']))}</td></tr>"
        for d in s["dates"] for p in reversed(d["trend"][-72:]))

    return TEMPLATE.format(
        generated=_e(s["generated_local"]),
        last_success=_e(s["last_success_local"] or "never"),
        min_status=_e(s["alert_min_status"]),
        tiles="".join(tiles),
        lows=lows or "<li>No eligible observations yet.</li>",
        chart_data=chart_data,
        trend_table=table_rows,
        airline_rows=airline_rows or f"<tr><td colspan='{len(config.trip.dates) + 2}' class='muted'>No data yet."
                                     "</td></tr>",
        date_heads=date_heads,
        options="".join(option_tables),
        prov_rows=prov_rows or "<tr><td colspan='7' class='muted'>No searches yet.</td></tr>",
        alert_rows=alert_rows or "<tr><td colspan='6' class='muted'>No alerts sent yet.</td></tr>",
        route=f"{_e(config.trip.origin)} → {_e(config.trip.destination)}",
        stale_minutes=config.verification.stale_after_minutes,
    )


def write_reports(s: dict, config: Config) -> dict[str, Path]:
    out = Path(config.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"html": out / "index.html", "md": out / "summary.md", "json": out / "summary.json"}
    paths["html"].write_text(to_html(s, config), encoding="utf-8")
    paths["md"].write_text(to_markdown(s, config), encoding="utf-8")
    paths["json"].write_text(json.dumps(s, indent=1, default=lambda o: o.__dict__), encoding="utf-8")
    return paths


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JAI–COK Fare Monitor</title>
<style>
:root {{ color-scheme: light; --bg:#f6f6f4; --surface-1:#fcfcfb; --border:#e3e2de; --text-primary:#0b0b0b;
  --text-secondary:#52514e; --muted:#77756f; --grid:#ebeae6; --series-1:#2a78d6; --series-2:#eb6834;
  --ok-bg:#e3f4ea; --ok-fg:#0d6b35; --q-bg:#fdf1d8; --q-fg:#7a5200; --ind-bg:#ecebe8; --ind-fg:#52514e;
  --err-bg:#fbe5e5; --err-fg:#a11d1d; --link:#1c5cab; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ color-scheme: dark; --bg:#111110;
  --surface-1:#1a1a19; --border:#2e2e2c; --text-primary:#f0efec; --text-secondary:#c3c2b7; --muted:#9a998f;
  --grid:#2a2a28; --series-1:#3987e5; --series-2:#d95926; --ok-bg:#12321f; --ok-fg:#7fd6a0; --q-bg:#3a2c0b;
  --q-fg:#f1c25e; --ind-bg:#2a2a28; --ind-fg:#c3c2b7; --err-bg:#3d1515; --err-fg:#f29b9b; --link:#86b6ef; }} }}
:root[data-theme="dark"] {{ color-scheme: dark; --bg:#111110; --surface-1:#1a1a19; --border:#2e2e2c;
  --text-primary:#f0efec; --text-secondary:#c3c2b7; --muted:#9a998f; --grid:#2a2a28; --series-1:#3987e5;
  --series-2:#d95926; --ok-bg:#12321f; --ok-fg:#7fd6a0; --q-bg:#3a2c0b; --q-fg:#f1c25e; --ind-bg:#2a2a28;
  --ind-fg:#c3c2b7; --err-bg:#3d1515; --err-fg:#f29b9b; --link:#86b6ef; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text-primary); font:14px/1.5 system-ui,-apple-system,
  "Segoe UI",Roboto,sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:32px 0 12px; }} h3 {{ font-size:14px; margin:20px 0 6px; }}
.muted {{ color:var(--muted); }} a, .link {{ color:var(--link); }}
.link {{ background:none; border:0; padding:0; font:inherit; cursor:pointer; text-decoration:underline; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; margin-top:16px; }}
.tile, .card {{ background:var(--surface-1); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }}
.tl {{ color:var(--text-secondary); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
.tv {{ font-size:28px; font-weight:650; font-variant-numeric:tabular-nums; margin:2px 0; }}
.ts {{ color:var(--text-secondary); font-size:13px; margin-top:2px; }}
.badge {{ display:inline-block; font-size:11px; font-weight:600; padding:1px 7px; border-radius:999px; }}
.badge.ok {{ background:var(--ok-bg); color:var(--ok-fg); }} .badge.quoted {{ background:var(--q-bg); color:var(--q-fg); }}
.badge.ind {{ background:var(--ind-bg); color:var(--ind-fg); }} .badge.err {{ background:var(--err-bg); color:var(--err-fg); }}
.scroll {{ overflow-x:auto; }} .scroll table {{ min-width:760px; }}
table {{ border-collapse:collapse; width:100%; background:var(--surface-1); font-size:13px; }}
th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--border); vertical-align:top; }}
th {{ color:var(--text-secondary); font-weight:600; font-size:12px; }}
td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.legend {{ display:flex; gap:16px; font-size:13px; color:var(--text-secondary); margin-bottom:6px; }}
.sw {{ display:inline-block; width:14px; height:3px; border-radius:2px; vertical-align:middle; margin-right:6px; }}
#chart {{ position:relative; }} #chart svg {{ width:100%; height:280px; display:block; }}
#tip {{ position:absolute; pointer-events:none; background:var(--surface-1); border:1px solid var(--border);
  border-radius:8px; padding:6px 10px; font-size:12px; box-shadow:0 2px 8px rgba(0,0,0,.12); display:none; white-space:nowrap; }}
details {{ margin-top:8px; }} ul {{ margin:6px 0; padding-left:20px; }}
.note {{ font-size:13px; color:var(--text-secondary); }}
</style></head>
<body><main>
<h1>{route} fare monitor · 4 or 5 Jan 2027 · 1 adult · Economy · one-way</h1>
<div class="muted">Updated {generated} · Last successful search: <b>{last_success}</b> ·
 Alerts and record lows use fares at <b>{min_status}</b> or better</div>
<div class="tiles">{tiles}</div>
<div class="card" style="margin-top:12px"><b>Lowest fares ever observed</b><ul>{lows}</ul></div>

<h2>Historical trend: lowest eligible fare per hourly search</h2>
<div class="card">
<div class="legend" id="legend"></div>
<div id="chart"><div id="tip"></div></div>
<details><summary>Table view</summary><div class="scroll"><table><thead><tr><th>Search time</th><th>Travel date</th>
<th class="num">Lowest fare</th></tr></thead><tbody>{trend_table}</tbody></table></div></details>
</div>

<h2>Airline comparison (lowest eligible fare, last 7 days)</h2>
<div class="scroll"><table><thead><tr><th>Airline</th>{date_heads}<th class="num">Observations</th></tr></thead>
<tbody>{airline_rows}</tbody></table></div>

<h2>Options from the latest search</h2>
<p class="note">VERIFIED = live offer re-priced through a booking API. SELLER-QUOTED = final price quoted by the
named airline/OTA through Google Flights at the time shown. INDICATIVE = search price only; it may change at
checkout and never triggers alerts by default. Searches older than {stale_minutes} minutes are marked stale.</p>
{options}

<h2>Sources and verification status</h2>
<div class="scroll"><table><thead><tr><th>Provider</th><th>Date</th><th>Last status</th><th>Last search</th>
<th>Last success</th><th class="num">Results / kept</th><th>Error</th></tr></thead><tbody>{prov_rows}</tbody></table></div>

<h2>Recent alerts</h2>
<div class="scroll"><table><thead><tr><th>Sent</th><th>Type</th><th>Date</th><th class="num">Fare</th>
<th class="num">Previous low</th><th>Channels</th></tr></thead><tbody>{alert_rows}</tbody></table></div>
<p class="note">This agent never books or pays. Booking links open the seller's site for you to decide.</p>
</main>
<script>
(function () {{
  const series = {chart_data};
  const colors = ["var(--series-1)", "var(--series-2)"];
  const legend = document.getElementById("legend");
  series.forEach((s, i) => {{
    const el = document.createElement("span");
    el.innerHTML = '<span class="sw" style="background:' + colors[i % 2] + '"></span>';
    el.appendChild(document.createTextNode(s.name));
    legend.appendChild(el);
  }});
  const box = document.getElementById("chart"), tip = document.getElementById("tip");
  const pts = series.flatMap(s => s.points.map(p => ({{t: Date.parse(p.t), v: p.price}})));
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  box.appendChild(svg);
  if (!pts.length) {{
    const t = document.createElementNS(NS, "text");
    t.setAttribute("x", 16); t.setAttribute("y", 40); t.setAttribute("fill", "var(--muted)");
    t.textContent = "No eligible observations yet."; svg.appendChild(t); return;
  }}
  const fmt = v => "₹" + Math.round(v).toLocaleString("en-IN");
  const fmtT = t => new Date(t).toLocaleString("en-IN", {{timeZone: "Asia/Kolkata", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit"}});
  function draw() {{
    svg.innerHTML = "";
    const W = box.clientWidth, H = 280, m = {{l: 64, r: W < 600 ? 84 : 92, t: 12, b: 28}};
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    let t0 = Math.min(...pts.map(p => p.t)), t1 = Math.max(...pts.map(p => p.t));
    if (t1 === t0) {{ t0 -= 3600e3; t1 += 3600e3; }}
    let v0 = Math.min(...pts.map(p => p.v)), v1 = Math.max(...pts.map(p => p.v));
    const pad = Math.max((v1 - v0) * 0.15, 200); v0 = Math.max(0, v0 - pad); v1 += pad;
    const x = t => m.l + (t - t0) / (t1 - t0) * (W - m.l - m.r);
    const y = v => m.t + (1 - (v - v0) / (v1 - v0)) * (H - m.t - m.b);
    const mk = (tag, a, parent) => {{ const e = document.createElementNS(NS, tag);
      for (const k in a) e.setAttribute(k, a[k]); (parent || svg).appendChild(e); return e; }};
    for (let i = 0; i <= 4; i++) {{
      const v = v0 + (v1 - v0) * i / 4, yy = y(v);
      mk("line", {{x1: m.l, x2: W - m.r, y1: yy, y2: yy, stroke: "var(--grid)", "stroke-width": 1}});
      mk("text", {{x: m.l - 8, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)"}}).textContent = fmt(v);
    }}
    const nx = W < 600 ? 1 : 3;
    for (let i = 0; i <= nx; i++) {{
      const t = t0 + (t1 - t0) * i / nx;
      mk("text", {{x: x(t), y: H - 8, "text-anchor": i === 0 ? "start" : i === nx ? "end" : "middle", "font-size": 11,
        fill: "var(--muted)"}}).textContent = nx === 1 ? new Date(t).toLocaleDateString("en-IN",
          {{timeZone: "Asia/Kolkata", day: "2-digit", month: "short"}}) : fmtT(t);
    }}
    series.forEach((s, i) => {{
      if (!s.points.length) return;
      const P = s.points.map(p => [x(Date.parse(p.t)), y(p.price)]);
      mk("polyline", {{points: P.map(p => p.join(",")).join(" "), fill: "none", stroke: colors[i % 2],
        "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"}});
      const last = P[P.length - 1];
      mk("circle", {{cx: last[0], cy: last[1], r: 4, fill: colors[i % 2], stroke: "var(--surface-1)", "stroke-width": 2}});
      mk("text", {{x: last[0] + 8, y: last[1] + 4 + (i ? 12 : -4), "font-size": 11, fill: "var(--text-secondary)"}})
        .textContent = s.name.slice(4, 10) + " " + fmt(s.points[s.points.length - 1].price);
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
        '<span class="sw" style="background:' + colors[r.i % 2] + '"></span>' + r.name + ": <b>" + fmt(r.p.price) + "</b>").join("<br>");
      tip.style.display = "block";
      tip.style.left = Math.min(mx + 12, W - tip.offsetWidth - 4) + "px"; tip.style.top = "12px";
    }});
    hit.addEventListener("mouseleave", () => {{ tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); }});
  }}
  draw(); window.addEventListener("resize", draw);
}})();
</script>
</body></html>
"""
