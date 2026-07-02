#!/usr/bin/env python
"""Pseudonymize generalBIMlog logs BEFORE they leave a participant's machine (GDPR).

    python anonymize_logs.py --user-label user_B [--src DIR] [--out DIR]

- Replaces `user_name` with the given label, `document_title` / `project_guid` with stable hashes.
- Scrubs the REAL user name from EVERY string field (it leaks into e.g. "{3D - firstname.lastnameXXX}"
  view names), plus any Windows user-profile paths.
- Writes an anonymized COPY to --out (default: .\anonymized\) — the source logs are never modified.
- Writes anonymize_mapping.json (pseudonym -> original) next to the output: KEEP PRIVATE, do not share.
- Self-check: after writing, greps the output for every original sensitive string and refuses to finish
  if any remains.

Stdlib only — runs on any machine with Python 3.8+ (no installs).
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path


def default_src() -> list[Path]:
    appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    base = Path(appdata) / "Autodesk" / "Revit" / "Addins"
    return sorted(base.glob("*/RevitLogger/Logs/eventlog")) if base.exists() else []


def _hash(value: str, salt: str, prefix: str) -> str:
    return prefix + "_" + hashlib.sha1((salt + value).encode("utf-8")).hexdigest()[:6]


def scrub(obj, replacements: dict[str, str]):
    """Recursively replace every occurrence of each sensitive string in all string values."""
    if isinstance(obj, str):
        out = obj
        for real, pseudo in replacements.items():
            if real and real in out:
                out = out.replace(real, pseudo)
        return out
    if isinstance(obj, dict):
        return {k: scrub(v, replacements) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, replacements) for v in obj]
    return obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-label", required=True, help="pseudonym for this participant, e.g. user_B")
    ap.add_argument("--src", default="", help="eventlog folder (default: auto-detect all Revit versions)")
    ap.add_argument("--out", default="anonymized", help="output folder for the anonymized copy")
    a = ap.parse_args()

    srcs = [Path(a.src)] if a.src else default_src()
    srcs = [s for s in srcs if s.exists()]
    if not srcs:
        print("no eventlog folders found — pass --src")
        return 2
    out_root = Path(a.out)
    out_root.mkdir(parents=True, exist_ok=True)

    salt = hashlib.sha1(os.urandom(16)).hexdigest()
    sensitive: dict[str, str] = {}          # real -> pseudonym (built as we encounter values)
    win_user = getpass.getuser()
    sensitive[win_user] = a.user_label      # windows account name can appear in paths/strings

    # sensitive-field registration: walk any parsed structure, note the values of these keys wherever
    # they appear (generalBIMlog writes BOTH shapes: whole-file JSON docs and JSONL session files).
    FIELD_PREFIX = {"user_name": None, "userName": None,
                    "document_title": "project", "documentTitle": "project", "projectName": "project",
                    "project_guid": "guid", "projectGUID": "guid"}

    def register(obj) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in FIELD_PREFIX and isinstance(v, str) and v and v not in sensitive:
                    pfx = FIELD_PREFIX[k]
                    sensitive[v] = a.user_label if pfx is None else _hash(v, salt, pfx)
                register(v)
        elif isinstance(obj, list):
            for v in obj:
                register(v)

    n_files = n_recs = 0
    for src in srcs:
        out_dir = out_root / src.parent.parent.parent.name  # <ver>/
        out_dir.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.glob("*.json*")):
            text = f.read_text(encoding="utf-8")
            doc = None
            try:
                doc = json.loads(text)                      # whole-file JSON document
            except ValueError:
                pass
            if isinstance(doc, (dict, list)):
                register(doc)
                out_text = json.dumps(scrub(doc, sensitive), indent=1, ensure_ascii=False)
                n_recs += len(doc) if isinstance(doc, list) else 1
            else:                                           # JSONL: one record per line
                lines_out = []
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, (dict, list)):
                        continue
                    register(rec)
                    lines_out.append(json.dumps(scrub(rec, sensitive), ensure_ascii=False))
                    n_recs += 1
                out_text = "\n".join(lines_out) + "\n"
            (out_dir / f.name).write_text(out_text, encoding="utf-8")
            n_files += 1

    # self-check: no original sensitive string may remain anywhere in the output
    leaked = []
    for real in sensitive:
        if len(real) < 4:                    # avoid false positives on tiny strings
            continue
        for f in out_root.rglob("*.json*"):
            if real in f.read_text(encoding="utf-8"):
                leaked.append((real, str(f)))
    if leaked:
        print("FAILED self-check — original values still present:", leaked[:5])
        return 1

    # OUTSIDE the hand-over folder, so zipping/sharing the folder can never include the real names
    mapping_path = out_root.parent / (out_root.name + "_PRIVATE_mapping.json")
    mapping_path.write_text(json.dumps({v: k for k, v in sensitive.items()}, indent=2, ensure_ascii=False),
                            encoding="utf-8")
    print(f"OK: {n_files} file(s), {n_recs} record(s) -> {out_root}")
    print(f"PRIVATE mapping (do NOT share): {mapping_path}")
    print("Hand over ONLY the anonymized folder (you may inspect it first — plain JSON).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
