"""Run a command with ./.env loaded into its environment (Eagle host services, docs/operations/eagle_wsl_setup.md).

Uses the project's own parser, so values such as the RA_MODEL_PRICING JSON need no shell quoting. Variables already
set (e.g. by the systemd unit) win over .env. Nothing is printed.
Usage: uv run python infra/host/with_dotenv.py <command> [args...]
"""

import os
import sys

from research_autopilot.config import load_dotenv

if len(sys.argv) < 2:
    sys.exit("usage: with_dotenv.py <command> [args...]")
load_dotenv()
os.execvp(sys.argv[1], sys.argv[1:])
