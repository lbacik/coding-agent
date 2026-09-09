"""Standalone helper for scripts/verify-l2-fixtures.sh: proves the `services`
entry this fixture's project-profile.yml declares is reachability-checked
for real, against a real listener, rather than only round-tripped through
the parser. Lives beside the fixture rather than in src/coding_agent so it
has no bearing on the package the image installs.
"""

import os
from pathlib import Path

from coding_agent.profile.parser import parse_profile_yaml
from coding_agent.profile.services import SocketServiceProber, check_service

profile_path = Path(__file__).parent / "docs" / "agents" / "project-profile.yml"
profile = parse_profile_yaml(profile_path.read_text(encoding="utf-8"))
(service,) = profile.services
result = check_service(service, os.environ, SocketServiceProber())
print(f"service-reachable: {result is None}")
