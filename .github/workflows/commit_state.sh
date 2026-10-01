# Commit step for both writers: the session loop (mstr_loop.yml, read once at loop start) and the 5 minute run (mstr_gap.yml,
# which reads it into memory first, because the reset below can rewrite this file while bash is still reading it).
# No rebase: state is newest-wins and the ledger is unioned with main by merge_ledger.py.
# Exits 1 when three pushes fail, so the loop keeps its state dirty and tries again; the 5 minute run treats that as non-fatal.
set -u
git config user.name  "btc-monitor"
git config user.email "monitor@noreply"
FILES="mstr_state.json mstr_ledger.json"
PINE="mstr_gap_lag.pine mstr_projected.pine mstx_projected.pine"
KEEP=$(mktemp -d)
for f in $FILES; do [ -f "$f" ] && cp "$f" "$KEEP/$f"; done
# The pine files carry only sync_pine's edits to their input defaults, so those go over as a patch onto main's copy. Copying the
# whole file reverted a pine code push that landed while a run was going (10/1, 1d38e010 over 4ee47b21).
# No context lines: each hunk is one input line, found by its own label, so a code edit next to it or above it does not block it.
git diff -U0 HEAD -- $PINE > "$KEEP/pine.patch"
for i in 1 2 3; do
  git fetch -q origin main
  git reset -q --hard origin/main
  for f in $FILES; do [ -f "$KEEP/$f" ] && cp "$KEEP/$f" "$f"; done
  if [ -s "$KEEP/pine.patch" ]; then
    git apply --unidiff-zero "$KEEP/pine.patch" || echo "the indicator default edits do not apply to main's pine files; the next check redoes them"
  fi
  if [ -f "$KEEP/mstr_ledger.json" ]; then
    git show origin/main:mstr_ledger.json > "$KEEP/remote_ledger.json" 2>/dev/null || echo "[]" > "$KEEP/remote_ledger.json"
    python merge_ledger.py "$KEEP/remote_ledger.json" "$KEEP/mstr_ledger.json" mstr_ledger.json
  fi
  for f in $FILES $PINE; do [ -f "$f" ] && git add "$f"; done
  if git diff --staged --quiet; then echo "nothing to commit"; exit 0; fi
  git commit -q -m "mstr state: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if git push -q origin HEAD:main; then echo "state pushed on try $i"; exit 0; fi
  echo "push raced another run, retrying ($i of 3)"
done
echo "state not pushed after 3 tries"
exit 1
