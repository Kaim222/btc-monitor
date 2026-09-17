"""Union this run's ledger with whatever landed on main while it was running, and write the result.

The monitor's state file is newest-wins: two runs that overlap can simply take the later one. The ledger cannot, because
it is append-only and a row dropped is an alert that never happened as far as the record is concerned. A rebase on the
ledger is also wrong: the two sides are not edits of the same line, they are different rows.

Usage: python merge_ledger.py <remote.json> <ours.json> <out.json>
A missing or unreadable file is treated as an empty ledger, so a first run cannot fail here.
Rows are keyed on (kind, time), which is unique: the monitor writes at most one row per kind per minute bar.
"""
import json, sys

def load(path):
    try:
        with open(path) as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []

def merge(remote, ours):
    """Union on (kind, time), field-merging rather than picking a winner.

    Taking the first row seen discarded scoring. score_ledger fills mstr_30m / mstx_30m / mstr_60m / mstx_60m into a row
    minutes after it was written, so the two sides of a race are usually the SAME event at different stages: one scored,
    one not. Whichever arrives first would win and the scores would be lost, quietly, forever. Fields are merged instead,
    and a value that is already filled in is never overwritten with a null.
    """
    by_key = {}
    order = []
    for row in list(remote) + list(ours):
        if not isinstance(row, dict):
            continue
        key = (row.get("kind"), row.get("time"))
        if key not in by_key:
            by_key[key] = dict(row)
            order.append(key)
            continue
        merged = by_key[key]
        for k, v in row.items():
            if v is None:
                continue
            if merged.get(k) is None or k not in merged:
                merged[k] = v
    out = [by_key[k] for k in order]
    out.sort(key=lambda r: str(r.get("time") or ""))
    return out

if __name__ == "__main__":
    remote_path, ours_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    remote, ours = load(remote_path), load(ours_path)
    merged = merge(remote, ours)
    with open(out_path, "w") as f:
        json.dump(merged, f, indent=2)
    print("ledger merge: %d remote + %d local -> %d rows" % (len(remote), len(ours), len(merged)))
