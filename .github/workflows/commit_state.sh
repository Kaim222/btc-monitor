# Commit step for the session loop (mstr_loop.yml), the same logic as the commit step in mstr_gap.yml.
# No rebase: state and the pine files are newest-wins, the ledger is unioned with main by merge_ledger.py.
# A lost race retries; a push that still fails is not fatal, the next commit carries the same files.
set -u
git config user.name  "btc-monitor"
git config user.email "monitor@noreply"
FILES="mstr_state.json mstr_ledger.json mstr_gap_lag.pine mstr_projected.pine mstx_projected.pine"
mkdir -p /tmp/keep
for f in $FILES; do [ -f "$f" ] && cp "$f" "/tmp/keep/$f"; done
for i in 1 2 3; do
  git fetch -q origin main
  git reset -q --hard origin/main
  for f in $FILES; do [ -f "/tmp/keep/$f" ] && cp "/tmp/keep/$f" "$f"; done
  if [ -f "/tmp/keep/mstr_ledger.json" ]; then
    git show origin/main:mstr_ledger.json > /tmp/remote_ledger.json 2>/dev/null || echo "[]" > /tmp/remote_ledger.json
    python merge_ledger.py /tmp/remote_ledger.json /tmp/keep/mstr_ledger.json mstr_ledger.json
  fi
  for f in $FILES; do [ -f "$f" ] && git add "$f"; done
  if git diff --staged --quiet; then echo "nothing to commit"; exit 0; fi
  git commit -q -m "mstr state: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if git push -q origin HEAD:main; then echo "state pushed on try $i"; exit 0; fi
  echo "push raced another run, retrying ($i of 3)"
done
echo "state not pushed after 3 tries; the next commit carries it"
exit 0
