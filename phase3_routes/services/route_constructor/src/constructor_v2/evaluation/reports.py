from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.constructor_v2.common import ensure_parent_dir


def write_json(path: Path, payload: Any) -> None:
    ensure_parent_dir(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Constructor V2 Benchmark",
        "",
        f"- Scenario: {report.get('scenario', {}).get('name', 'default')}",
        f"- Valhalla URL: {report.get('scenario', {}).get('valhalla_base_url', 'n/a')}",
        f"- Generated: {report['generated_at']}",
        f"- Routes tested: {report['summary']['route_count']}",
        f"- Improved routes: {report['summary']['improved_routes']}",
        f"- Ambiguous routes: {report['summary']['ambiguous_routes']}",
        "",
        "## Aggregate",
        "",
        f"- Current avg order agreement: {report['summary']['avg_current_order_agreement']:.3f}",
        f"- V2 avg order agreement: {report['summary']['avg_v2_order_agreement']:.3f}",
        f"- Current avg confidence: {report['summary']['avg_current_confidence']:.1f}",
        f"- V2 avg confidence: {report['summary']['avg_v2_confidence']:.1f}",
        "",
        "## Topology",
        "",
    ]
    for topology, topo_summary in sorted(report["summary"].get("topology_summary", {}).items()):
        lines.extend(
            [
                f"- {topology}: routes={topo_summary['route_count']} improved={topo_summary['improved_routes']} "
                f"ambiguous={topo_summary['ambiguous_routes']} manual_review={topo_summary['manual_review_routes']} "
                f"current_agree={topo_summary['avg_current_order_agreement']:.3f} "
                f"v2_agree={topo_summary['avg_v2_order_agreement']:.3f} "
                f"current_conf={topo_summary['avg_current_confidence']:.1f} "
                f"v2_conf={topo_summary['avg_v2_confidence']:.1f} "
                f"recommendation={topo_summary['recommendation']}",
            ]
        )
    lines.extend(
        [
            "",
        "## Routes",
        "",
            "| Route | Bucket | Topology | Current agreement | V2 agreement | Current conf | V2 conf | Improved | Method |",
            "| --- | --- | --- | ---: | ---: | --- | --- | --- | --- |",
        ]
    )
    for route in report["routes"]:
        lines.append(
            "| {route} | {bucket} | {topology} | {curr_agree:.3f} | {v2_agree:.3f} | {curr_conf} | {v2_conf} | {improved} | {method} |".format(
                route=route["route"],
                bucket=route["bucket"],
                topology=route["topology"],
                curr_agree=route["current_vs_reference"]["order_agreement"],
                v2_agree=route["v2_vs_reference"]["order_agreement"],
                curr_conf=route["current_confidence"]["label"],
                v2_conf=route["v2_confidence"]["label"],
                improved="yes" if route["improved"] else "no",
                method=route["selected_method"],
            )
        )
    return "\n".join(lines) + "\n"


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    ensure_parent_dir(path)
    path.write_text(render_markdown(report))


def write_text(path: Path, text: str) -> None:
    ensure_parent_dir(path)
    path.write_text(text)
