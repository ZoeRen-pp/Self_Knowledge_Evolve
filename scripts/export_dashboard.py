"""Export dashboard as a self-contained HTML file with embedded data.

Creates a single HTML file that works offline — all API data is baked in as JSON,
no backend needed. Can be opened on any machine to demo the system.

Usage:
    python scripts/export_dashboard.py [--output path/to/output.html]
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "semcore"))
sys.path.insert(0, str(PROJECT_ROOT))

API = "http://localhost:8000"


def fetch_json(path: str, method="GET", body=None) -> dict:
    """Fetch JSON from local API."""
    url = f"{API}{path}"
    if body:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method=method,
        )
    else:
        req = urllib.request.Request(url, method=method)
    try:
        res = urllib.request.urlopen(req, timeout=120)
        return json.loads(res.read())
    except Exception as exc:
        print(f"  WARN: {path} failed: {exc}")
        return {}


def collect_all_data() -> dict:
    """Collect all dashboard data from running API."""
    print("Collecting data from API...")

    data = {}

    # Overview
    print("  pipeline_flow...")
    data["pipeline_flow"] = fetch_json("/api/v1/system/pipeline_flow")

    print("  stats...")
    data["stats"] = fetch_json("/api/v1/system/stats")

    print("  recent_activity...")
    data["recent_activity"] = fetch_json("/api/v1/system/recent_activity?limit=20")

    # Quality
    print("  ontology_quality...")
    data["quality"] = fetch_json("/api/v1/semantic/ontology_quality")

    # Evolution
    print("  candidate_distribution...")
    data["candidate_distribution"] = fetch_json("/api/v1/system/candidate_distribution")

    print("  review list (discovered)...")
    data["review_discovered"] = fetch_json("/api/v1/system/review?type=all&status=discovered&limit=50")

    print("  review list (accepted)...")
    data["review_accepted"] = fetch_json("/api/v1/system/review?type=all&status=accepted&limit=50")

    # History
    print("  stats_history...")
    data["history"] = fetch_json("/api/v1/system/stats/history?hours=24")

    # Knowledge Explorer: pre-fetch a few showcase terms
    showcase_terms = ["BGP", "OSPF", "VXLAN", "MPLS", "SRv6", "EVPN"]
    data["explorer"] = {}
    for term in showcase_terms:
        print(f"  lookup {term}...")
        lookup = fetch_json(f"/api/v1/semantic/lookup?term={term}")
        result = (lookup.get("result") or lookup)
        node = result.get("matched_node") or result.get("node") or {}
        node_id = node.get("node_id")

        entry = {"lookup": lookup, "context": {}}
        if node_id:
            print(f"  context_assemble {node_id}...")
            ctx = fetch_json("/api/v1/semantic/context_assemble", method="POST",
                             body={"node_ids": [node_id], "max_segments": 5, "max_hops": 2})
            entry["context"] = ctx
        data["explorer"][term] = entry

    # Drilldown samples
    drilldown_metrics = [
        "super_nodes", "isolated_nodes", "unused_predicates",
        "single_evidence_weak", "cross_layer_gaps",
    ]
    data["drilldowns"] = {}
    for metric in drilldown_metrics:
        print(f"  drilldown {metric}...")
        data["drilldowns"][metric] = fetch_json(f"/api/v1/system/drilldown/{metric}?limit=30")

    print(f"Data collection complete.")
    return data


def build_offline_html(data: dict) -> str:
    """Read the live dashboard HTML and inject offline data + override fetch calls."""
    html_path = PROJECT_ROOT / "static" / "dashboard.html"
    html = html_path.read_text(encoding="utf-8")

    # Inject data blob and fetch interceptor right before </script>
    data_json = json.dumps(data, ensure_ascii=False, default=str)

    inject = f"""
// ══════════════════════════════════════════════════════════════
// OFFLINE MODE: All data embedded, no backend required
// Exported at: {__import__('datetime').datetime.now().isoformat()}
// ══════════════════════════════════════════════════════════════
const _OFFLINE = true;
const _DATA = {data_json};

// Override fetch to serve from embedded data
const _originalFetch = window.fetch;
window.fetch = async function(url, opts) {{
  url = typeof url === 'string' ? url : url.toString();

  // Pipeline flow
  if (url.includes('/pipeline_flow')) return _jsonResp(_DATA.pipeline_flow);

  // Stats
  if (url.includes('/stats/history')) return _jsonResp(_DATA.history);
  if (url.includes('/stats')) return _jsonResp(_DATA.stats);

  // Quality
  if (url.includes('/ontology_quality')) return _jsonResp(_DATA.quality);

  // Evolution
  if (url.includes('/candidate_distribution')) return _jsonResp(_DATA.candidate_distribution);
  if (url.includes('/recent_activity')) return _jsonResp(_DATA.recent_activity);

  // Review list
  if (url.includes('/review?') || url.match(/\\/review\\?/)) {{
    if (url.includes('accepted')) return _jsonResp(_DATA.review_accepted);
    return _jsonResp(_DATA.review_discovered);
  }}

  // Drilldown
  const drillMatch = url.match(/\\/drilldown\\/([^?]+)/);
  if (drillMatch) {{
    const metric = drillMatch[1];
    if (_DATA.drilldowns[metric]) return _jsonResp(_DATA.drilldowns[metric]);
    return _jsonResp({{result: {{error: '离线模式下无此指标数据'}}}});
  }}

  // Lookup
  const lookupMatch = url.match(/\\/lookup\\?term=([^&]+)/);
  if (lookupMatch) {{
    const term = decodeURIComponent(lookupMatch[1]);
    const entry = _DATA.explorer[term] || _DATA.explorer[term.toUpperCase()];
    if (entry) return _jsonResp(entry.lookup);
    return _jsonResp({{result: {{error: '离线模式下仅支持预置术语: {", ".join(data.get("explorer", {}).keys())}'}}}});
  }}

  // Context assemble
  if (url.includes('/context_assemble')) {{
    try {{
      const body = JSON.parse(opts?.body || '{{}}');
      const nodeId = (body.node_ids || [])[0] || '';
      for (const [term, entry] of Object.entries(_DATA.explorer)) {{
        const n = (entry.lookup?.result?.matched_node || {{}});
        if (n.node_id === nodeId) return _jsonResp(entry.context);
      }}
    }} catch(e) {{}}
    return _jsonResp({{result: {{facts:[], segments:[], reasoning_chain:[]}}}});
  }}

  // Review detail (single candidate)
  if (url.match(/\\/review\\/[a-f0-9-]+$/)) {{
    return _jsonResp({{related_segments: [], segment_count: 0, error: '离线模式下不支持查看单条候选词详情'}});
  }}

  // Fallback
  console.warn('[Offline] Unhandled fetch:', url);
  return _jsonResp({{error: '离线模式下不支持此请求'}});
}};

function _jsonResp(data) {{
  return new Response(JSON.stringify(data), {{
    status: 200, headers: {{'Content-Type': 'application/json'}}
  }});
}}

// Override review actions to show offline notice
const _origApprove = window.approveCandidate;
window.approveCandidate = function() {{ alert('离线模式下不支持审批操作'); }};
window.rejectCandidate = function() {{ alert('离线模式下不支持拒绝操作'); }};
window.mergeSelected = function() {{ alert('离线模式下不支持合并操作'); }};
window.checkSynonymsSelected = function() {{ alert('离线模式下不支持LLM判断'); }};
"""

    # Insert before the closing </script> tag
    html = html.replace("/* ── Init ── */", inject + "\n/* ── Init ── */")

    # Update title to indicate offline
    html = html.replace(
        "<title>电信语义知识库</title>",
        "<title>电信语义知识库 (离线快照)</title>",
    )

    # Add offline badge in header
    html = html.replace(
        "治理驱动、持续演化、来源可溯的知识基础设施",
        "治理驱动、持续演化、来源可溯的知识基础设施 <span style=\"background:#ff9800;color:#000;padding:2px 8px;border-radius:4px;font-size:0.7rem;margin-left:8px;\">离线快照</span>",
    )

    return html


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Export dashboard as offline HTML")
    parser.add_argument("--output", default=None, help="Output file path")
    args = parser.parse_args()

    output_path = args.output or str(PROJECT_ROOT / "exports" / f"dashboard-snapshot.html")
    output_dir = Path(output_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    data = collect_all_data()
    html = build_offline_html(data)

    Path(output_path).write_text(html, encoding="utf-8")
    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    print(f"\nExported to: {output_path}")
    print(f"File size: {size_mb:.1f} MB")
    print(f"Open in any browser — no backend needed.")


if __name__ == "__main__":
    main()
