# wq#5028: daily fact-taxonomy scan

The `lumen-fact-taxonomy.timer` user timer runs `harness/fact_taxonomy.py scan --write` once a day at 12:40 UTC. The scan refreshes `/home/lumen/foreman/fact_taxonomy.db`, which `lumen_state.py current` and the `--core` VERIFY "fact coverage" line both read.

- To install: `bash ops/wq5028-fact-taxonomy-timer/install.sh`. It runs as the lumen user, which has Linger=yes, so no root is needed.
- To verify: `bash ops/wq5028-fact-taxonomy-timer/install.sh --verify`.
- To roll back: `systemctl --user disable --now lumen-fact-taxonomy.timer`, then remove the two unit files.

The scan only reads minerva.db and only writes the foreman ledger. It never changes `last_confirmed_at`: a formal-scope verdict is not world evidence (QWED #320).
