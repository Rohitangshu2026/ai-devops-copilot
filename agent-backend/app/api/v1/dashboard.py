"""Server-rendered HTML dashboard for incident history (Phase 8d).

``render_dashboard(incidents)`` returns a complete HTML page as a string.
No external CSS frameworks or build steps are required.
"""
from __future__ import annotations

import html
import json
from typing import Any


def render_dashboard(incidents: list[dict[str, Any]]) -> str:
    """Return a minimal HTML page showing the 50 most recent incidents.

    Features:
    - Table with columns: timestamp, service, error_type, severity,
      confidence, safety_decision, outcome, incident_id
    - Inline JS filter by service and outcome (no build step)
    - Click-to-expand row: shows JSON detail below the clicked row
    """

    def _esc(v: Any) -> str:
        return html.escape(str(v) if v is not None else "")

    rows_html = []
    for inc in incidents:
        row_id = _esc(inc.get("incident_id", ""))
        ts = _esc(inc.get("timestamp", "")[:19].replace("T", " "))
        svc = _esc(inc.get("service", ""))
        err = _esc(inc.get("error_type", ""))
        sev = _esc(inc.get("severity", ""))
        conf = _esc(inc.get("confidence_hint", ""))
        safety = _esc(inc.get("safety_decision", ""))
        outcome = _esc(inc.get("outcome", "unknown"))

        detail_json = _esc(json.dumps(inc, indent=2, default=str))

        severity_class = ""
        if inc.get("severity") in ("critical", "high"):
            severity_class = " class=\"row-high\""
        elif inc.get("outcome") == "resolved":
            severity_class = " class=\"row-resolved\""

        rows_html.append(f"""
        <tr{severity_class}
            data-service="{svc}"
            data-outcome="{outcome}"
            data-id="{row_id}"
            onclick="toggleDetail('{row_id}')">
          <td>{ts}</td>
          <td>{svc}</td>
          <td>{err}</td>
          <td>{sev}</td>
          <td>{conf}</td>
          <td>{safety}</td>
          <td>{outcome}</td>
          <td><code>{row_id[:8]}&hellip;</code></td>
        </tr>
        <tr id="detail-{row_id}" class="detail-row" style="display:none">
          <td colspan="8"><pre class="json-detail">{detail_json}</pre></td>
        </tr>""")

    rows = "\n".join(rows_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI DevOps Copilot — Incident Dashboard</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ font-family: system-ui, sans-serif; margin: 0; padding: 1rem 2rem; background: #f5f5f5; color: #222; }}
    h1 {{ font-size: 1.4rem; margin-bottom: 0.5rem; }}
    .filter-bar {{ display: flex; gap: 1rem; margin-bottom: 1rem; align-items: center; }}
    .filter-bar input, .filter-bar select {{
      padding: 0.35rem 0.6rem; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9rem;
    }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 6px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
    th {{ background: #2d3748; color: #fff; text-align: left; padding: 0.55rem 0.8rem; font-size: 0.85rem; }}
    td {{ padding: 0.45rem 0.8rem; font-size: 0.85rem; border-bottom: 1px solid #eee; vertical-align: top; }}
    tr:hover td {{ background: #eef2ff; cursor: pointer; }}
    tr.row-high td {{ background: #fff5f5; }}
    tr.row-resolved td {{ background: #f0fff4; }}
    .detail-row td {{ background: #fafafa !important; cursor: default; }}
    pre.json-detail {{
      margin: 0; padding: 0.75rem; overflow-x: auto; font-size: 0.78rem;
      background: #1a202c; color: #a0aec0; border-radius: 4px; max-height: 400px;
    }}
    .hidden {{ display: none !important; }}
  </style>
</head>
<body>
  <h1>AI DevOps Copilot — Incident Dashboard</h1>
  <div class="filter-bar">
    <label>Service: <input id="f-service" type="text" placeholder="filter…" oninput="applyFilter()"></label>
    <label>Outcome:
      <select id="f-outcome" onchange="applyFilter()">
        <option value="">all</option>
        <option value="resolved">resolved</option>
        <option value="unknown">unknown</option>
        <option value="unresolved">unresolved</option>
        <option value="partial">partial</option>
      </select>
    </label>
    <span id="count-label" style="color:#666;font-size:.85rem;"></span>
  </div>
  <table id="incidents-table">
    <thead>
      <tr>
        <th>Timestamp</th>
        <th>Service</th>
        <th>Error Type</th>
        <th>Severity</th>
        <th>Confidence</th>
        <th>Safety</th>
        <th>Outcome</th>
        <th>Incident ID</th>
      </tr>
    </thead>
    <tbody id="tbody">
      {rows}
    </tbody>
  </table>
  <script>
    function toggleDetail(id) {{
      var row = document.getElementById('detail-' + id);
      if (!row) return;
      row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
    }}

    function applyFilter() {{
      var svcFilter = document.getElementById('f-service').value.trim().toLowerCase();
      var outcomeFilter = document.getElementById('f-outcome').value;
      var rows = document.querySelectorAll('#tbody tr[data-id]');
      var visible = 0;
      rows.forEach(function(tr) {{
        var svc = (tr.getAttribute('data-service') || '').toLowerCase();
        var out = tr.getAttribute('data-outcome') || '';
        var id  = tr.getAttribute('data-id') || '';
        var show = true;
        if (svcFilter && !svc.includes(svcFilter)) show = false;
        if (outcomeFilter && out !== outcomeFilter) show = false;
        tr.style.display = show ? '' : 'none';
        // also hide its detail row when parent is hidden
        var detail = document.getElementById('detail-' + id);
        if (detail && !show) detail.style.display = 'none';
        if (show) visible++;
      }});
      document.getElementById('count-label').textContent = visible + ' incident(s) shown';
    }}

    // initial count
    applyFilter();
  </script>
</body>
</html>"""
