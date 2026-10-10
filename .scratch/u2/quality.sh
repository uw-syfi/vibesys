#!/usr/bin/env bash
cd /mnt/data/shli/vibesys-slurm-u2
for c in "uv run python scripts/check_file_length.py" "uv run python -m scripts.check_purity --base-ref origin/main" "uv run python scripts/check_test_isolation.py" "uv run python -m scripts.check_contract_sot --base-ref origin/main" "uv run python scripts/check_tach.py" "uv run python scripts/check_member_dependencies.py" "uv run python scripts/check_tach_graph.py --check" "python3 scripts/check_doc_links.py" "python3 -m scripts.check_doc_citations" "uv run python scripts/check_lint_waivers.py" "./scripts/check_types.sh"; do
  echo "=== $c"; $c 2>&1 | tail -15; echo "rc=${PIPESTATUS[0]}"
done
echo ALLDONE
