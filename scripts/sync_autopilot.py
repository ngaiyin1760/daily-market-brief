"""Sync the Outlook Autopilot rules into this site.

Reads outlook-autopilot/rules/auto_mark_list.md and writes
data/autopilot_rules.json — the data the Autopilot tab renders from.
Run it after the rules list changes, then commit the JSON.

Usage:
    python scripts/sync_autopilot.py
    python scripts/sync_autopilot.py --rules /path/to/auto_mark_list.md
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "autopilot_rules.json"
DEFAULT_RULES = ROOT.parent / "outlook-autopilot" / "rules" / "auto_mark_list.md"
DEFAULT_LAST_RUN = ROOT.parent / "outlook-autopilot" / "state" / "last_run.json"

SECTIONS = {
    "exact sender addresses": "exact",
    "patterns": "patterns",
    "always keep unread": "whitelist",
    "learned senders": "learned",
}


def parse_rules(path: Path) -> dict:
    """Parse the markdown rules file into JSON-able data."""
    import re
    email_re = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
    backtick_re = re.compile(r"`([^`]+)`")

    exact, whitelist, learned, patterns = [], [], [], []
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("##"):
            low = line.lower()
            current = None
            for title, key in SECTIONS.items():
                if title in low:
                    current = key
                    break
            continue
        if current is None or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        emails = [e.lower() for e in email_re.findall(line)]
        if not emails and current != "patterns":
            continue
        name = cells[0] if len(cells) > 1 else ""
        addr = emails[0] if emails else ""
        notes = cells[2] if len(cells) > 2 else ""
        if current == "exact":
            exact.append({"address": addr, "name": name, "notes": notes})
        elif current == "whitelist":
            whitelist.append({"address": addr, "name": name, "notes": notes})
        elif current == "learned":
            learned.append(addr)
        elif current == "patterns":
            # Only the Pattern column is real — Notes may mention variants.
            found = backtick_re.findall(cells[0])
            if found:
                patterns.append(found[0])

    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "auto_read_exact": sorted(exact, key=lambda s: s["address"]),
        "auto_read_patterns": patterns,
        "whitelist": sorted(whitelist, key=lambda s: s["address"]),
        "learned": sorted(set(a for a in learned if a)),
    }


def read_token_status(path: Path):
    """Read outlook-autopilot's state/last_run.json into a health summary.

    Returns None when the file is missing/unreadable (page shows a hint then).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    accounts = raw.get("accounts") or {}
    return {
        "finished_at": raw.get("finished_at") or raw.get("started_at") or "",
        "accounts": {
            label: {
                "status": rec.get("status", "unknown"),
                "error": rec.get("error", ""),
                "unread": rec.get("unread", 0),
                "marked": rec.get("marked", 0),
            }
            for label, rec in accounts.items()
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync Outlook Autopilot rules into this site")
    ap.add_argument("--rules", default=str(DEFAULT_RULES),
                    help="path to auto_mark_list.md")
    ap.add_argument("--last-run", default=str(DEFAULT_LAST_RUN),
                    help="path to outlook-autopilot state/last_run.json")
    args = ap.parse_args()

    rules_path = Path(args.rules)
    if not rules_path.exists():
        print(f"ERROR: rules file not found: {rules_path}", file=sys.stderr)
        return 1

    data = parse_rules(rules_path)
    data["token_status"] = read_token_status(Path(args.last_run))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}")
    print(f"  exact senders : {len(data['auto_read_exact'])}")
    print(f"  patterns      : {len(data['auto_read_patterns'])}")
    print(f"  learned       : {len(data['learned'])}")
    print(f"  whitelist     : {len(data['whitelist'])}")
    if data["token_status"]:
        print(f"  token health  : " + ", ".join(
            f"{k}={v['status']}" for k, v in data["token_status"]["accounts"].items()))
    else:
        print("  token health  : no last_run.json found")
    print("\nCommit and push to publish the update.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
