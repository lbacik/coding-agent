"""PROTOTYPE (ticket #284): embed workflows.yaml into the self-contained walkthrough page.

    python build_demo.py        # writes workflow-demo.html next to this file
"""

import json
import sys
from pathlib import Path

from validate_prototype import validate_file

HERE = Path(__file__).resolve().parent
cfg, problems = validate_file(HERE / "workflows.yaml")
if problems:
    sys.exit("workflows.yaml is invalid:\n" + "\n".join(problems))
page = (HERE / "demo_template.html").read_text()
page = page.replace("/*__CONFIG__*/null", json.dumps(cfg).replace("</", "<\\/"))
page = page.replace('/*__YAML__*/""', json.dumps((HERE / "workflows.yaml").read_text()).replace("</", "<\\/"))
(HERE / "workflow-demo.html").write_text(page)
print("wrote workflow-demo.html")
