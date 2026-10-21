"""Synthetic tiny fixtures only. Verify money joins, bounds, and explicit missing-data gates."""

import csv
import json
import subprocess
import sys
from pathlib import Path


def test_seeded_scale_preserves_totals_and_does_not_fake_missing_sources(tmp_path):
    fit = tmp_path / "fit"
    fit.mkdir()
    (fit / "fits.json").write_text(
        json.dumps(
            {
                "userbehavior": {"fitted": False, "user_activity_histogram": None},
                "ecd": {
                    "turn_count_histogram": {"2": 1},
                    "rule_route_first_segment_unverified": {"human": 1},
                },
            }
        )
    )
    (fit / "olist-bootstrap.json").write_text(
        json.dumps(
            {
                "products": [
                    {"source_id": "a", "category": "fixture", "price": 100, "freight": 10},
                    {"source_id": "b", "category": "fixture", "price": 200, "freight": 20},
                ],
                "orders": [
                    {
                        "source_id": "order",
                        "status": "delivered",
                        "created_at": "2018-01-01 00:00:00",
                        "items": [
                            {"product": "a", "price": 100, "freight": 10},
                            {"product": "b", "price": 200, "freight": 20},
                        ],
                    }
                ],
            }
        )
    )
    (fit / "ecd-bootstrap-local.json").write_text(
        json.dumps({"by_length": {"2": [["fixture,one", "fixture\ntwo"]]}})
    )
    outputs = []
    for n in range(2):
        out = tmp_path / ".local" / str(n)
        subprocess.run(
            [
                sys.executable,
                "scripts/synthesize-p2.py",
                "--fit",
                str(fit),
                "--output",
                str(out),
                "--products",
                "3",
                "--users",
                "4",
                "--orders",
                "5",
                "--messages",
                "12",
                "--tickets",
                "1",
            ],
            check=True,
            capture_output=True,
        )
        outputs.append(json.loads((out / "manifest.json").read_text()))
    a, b = outputs
    assert {k: r["sha256"] for k, r in a["files"].items()} == {
        k: r["sha256"] for k, r in b["files"].items()
    }
    assert a["ticket_target_blocked"] and "tickets" not in a["files"]
    assert not a["user_activity_fit_complete"] and not a["phase_p2_accepted"]

    def rows(name):
        return list(csv.reader(Path(a["files"][name]["path"]).open()))

    assert all(row[1] == "\\N" for row in rows("users"))
    assert len(rows("messages")) == 12
    assert any("\n" in r[4] for r in rows("messages"))
    assert all(int(r[3]) == 330 for r in rows("orders"))
    assert len(rows("order_items")) == 10
    assert len(rows("shipments")) == 5
    for ratio in (0.05, 0.2, 0.5):
        out = tmp_path / ".local" / ("proxy-" + str(ratio))
        subprocess.run(
            [
                sys.executable,
                "scripts/synthesize-p2.py",
                "--fit",
                str(fit),
                "--output",
                str(out),
                "--products",
                "3",
                "--users",
                "4",
                "--orders",
                "5",
                "--messages",
                "12",
                "--tickets",
                "100",
                "--owner-approved-ecd-proxy",
                "--waiting-human-ratio",
                str(ratio),
            ],
            check=True,
            capture_output=True,
        )
        with (out / "tickets.csv").open() as stream:
            tickets = list(csv.reader(stream))
        assert sum(t[2] == "waiting_human" for t in tickets) == round(100 * ratio)
        assert all(t[-1] == "规则代理，非人工真实意图分布，首片段说话人未确认" for t in tickets)
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["waiting_human_count"] == round(100 * ratio)
        for name in manifest["files"]:
            meta = json.loads((out / (name + ".provenance.json")).read_text())
            assert meta["intent_proxy_note"] == "规则代理，非人工真实意图分布，首片段说话人未确认"
            assert meta["routing_accuracy_or_evaluation_use"] is False


def test_userbehavior_missing_never_falls_back_to_guessed_zipf(tmp_path):
    fit = tmp_path / "fit.json"
    fit.write_text(json.dumps({"userbehavior": {"fitted": False}}))
    out = tmp_path / ".local" / "events.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/generate-behavior-events.py",
            "--fit",
            str(fit),
            "--users",
            str(tmp_path / "unused.csv"),
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "no uniform/guessed" in result.stderr
    assert not out.exists()
