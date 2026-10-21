"""End-to-end local Collector metrics -> Prometheus -> provisioned Grafana probe."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

p = argparse.ArgumentParser()
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
secrets = dict(
    line.split("=", 1) for line in Path(".local/stack.env").read_text().splitlines() if line
)
with httpx.Client(timeout=5, trust_env=False) as client:
    now = str(time.time_ns())
    body = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "agent-ops-p0-probe"}}
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "p0-connectivity"},
                        "metrics": [
                            {
                                "name": "agent_ops_p0_probe",
                                "sum": {
                                    "aggregationTemporality": 2,
                                    "isMonotonic": True,
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": now,
                                            "startTimeUnixNano": now,
                                            "asInt": "1",
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
    sent = client.post("http://127.0.0.1:14318/v1/metrics", json=body)
    sent.raise_for_status()
    up = False
    metric = False
    for i in range(30):
        r = client.get(
            "http://127.0.0.1:19091/api/v1/query", params={"query": 'up{job="collector"}'}
        )
        r.raise_for_status()
        up = bool(r.json()["data"]["result"]) and all(
            float(row["value"][1]) == 1 for row in r.json()["data"]["result"]
        )
        r = client.get(
            "http://127.0.0.1:19091/api/v1/query", params={"query": "agent_ops_p0_probe_total"}
        )
        r.raise_for_status()
        metric = any(float(row["value"][1]) >= 1 for row in r.json()["data"]["result"])
        if up and metric:
            break
        time.sleep(1)
    dashboard = client.get(
        "http://127.0.0.1:13001/api/datasources/uid/prometheus/health",
        auth=("admin", secrets["GRAFANA_PASSWORD"]),
    )
    dashboard.raise_for_status()
    assert up and metric and dashboard.json()["status"] == "OK"
    versions = {
        "prometheus": client.get("http://127.0.0.1:19091/api/v1/status/buildinfo").json()["data"][
            "version"
        ]
    }
report = {
    "generated_at": datetime.now(UTC).isoformat(),
    "metric_ingest_http": sent.status_code,
    "prometheus_collector_up": up,
    "probe_metric_visible": metric,
    "grafana_datasource_http": dashboard.status_code,
    "grafana_datasource_status": dashboard.json()["status"],
    "versions": versions,
    "model_calls": 0,
    "scope": "connectivity probe only; no production workload/latency claim",
}
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
