#!/usr/bin/env bash
# Every check this repository has: the rule self-test and the unit, broker and launcher tests.
set -euo pipefail
cd "$(dirname "$0")"
export AGENT_GUARD_CONFIG=/nonexistent/agent-guard.json
python3 -B ../bin/agent-guard selftest
python3 -B -W error::ResourceWarning -m unittest -v test_rules test_policy test_hooks test_installer test_broker test_launcher test_prove
