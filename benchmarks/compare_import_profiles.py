#!/usr/bin/env python3
"""Fail-closed aggregation of two independent Kanto importer profiles."""
import argparse
import json
from pathlib import Path
import statistics


def compare(safe, candidate):
    result = {"status": "FAIL", "safe_sha256": safe.get("pbf_sha256"),
              "candidate_sha256": candidate.get("pbf_sha256"),
              "checks": {}, "time_s": {}}
    checks = result["checks"]
    checks["same_pbf"] = bool(safe.get("pbf_sha256")) and safe["pbf_sha256"] == candidate.get("pbf_sha256")
    checks["same_production_sha"] = safe.get("baseline_sha") == candidate.get("baseline_sha")
    checks["correct_profiles"] = safe.get("profile") == "safe" and candidate.get("profile") == "candidate"
    checks["both_passed"] = safe.get("status") == candidate.get("status") == "PASS"
    checks["two_trials_each"] = len(safe.get("repeats", [])) == len(candidate.get("repeats", [])) == 2
    audits = [r.get("audit") for profile in (safe, candidate)
              for r in profile.get("repeats", [])]
    checks["all_audits_present"] = len(audits) == 4 and all(
        a and a.get("tables") and a.get("samples") for a in audits)
    checks["counts_and_decoded_samples_equal"] = (
        checks["all_audits_present"] and
        all(json.dumps(a, sort_keys=True) == json.dumps(audits[0], sort_keys=True)
            for a in audits))
    for key, value in (("safe", safe), ("candidate", candidate)):
        repeats = value.get("repeats", [])
        if repeats and all("total_import_and_post_s" in x for x in repeats):
            values = [x["total_import_and_post_s"] for x in repeats]
            result["time_s"][key] = {"values": values,
                                      "median": statistics.median(values),
                                      "range": [min(values), max(values)]}
    if "safe" in result["time_s"] and "candidate" in result["time_s"]:
        s, c = result["time_s"]["safe"]["median"], result["time_s"]["candidate"]["median"]
        result["candidate_speedup_percent"] = round((s-c)/s*100, 2) if s else None
    result["status"] = "PASS_EXPLORATORY_ONLY" if all(checks.values()) else "FAIL"
    result["adopt_production"] = False  # 2 trials and sample-only parity are insufficient.
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--safe", type=Path, required=True)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    safe = json.loads(args.safe.read_text())
    candidate = json.loads(args.candidate.read_text())
    result = compare(safe, candidate)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False)+"\n")
    print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    if result["status"] != "PASS_EXPLORATORY_ONLY":
        raise SystemExit("Import profile comparison FAILED; do not interpret speed numbers")


if __name__ == "__main__":
    main()
