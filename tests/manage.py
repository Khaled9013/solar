"""Run tests in a disposable installation; no real accounts or provider logins are used.

This harness mocks only OS ACL mutation, which is unavailable in managed sandboxes.
The actual launcher always applies ACLs and fails closed if that operation fails.
"""
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["SOLAR_HOME"] = str(ROOT / ".solar-test-suite")
os.environ["SOLAR_CONFIG"] = str(ROOT / ".solar-test-suite" / "solar.conf")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "solar.settings")

from solar.config import initialize_storage

with patch("solar.config.private_permissions"):
    initialize_storage()

from django.core.management import execute_from_command_line

execute_from_command_line([sys.argv[0], *sys.argv[1:]])
