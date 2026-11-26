"""Run the real report command on one batch of messages and return what it observably did."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    node, src = sys.argv[1], sys.argv[2]
    messages = json.load(sys.stdin)
    with tempfile.TemporaryDirectory() as work:
        child = subprocess.run([node, f"{src}/bin/report.mjs"], input=json.dumps(messages).encode(),
                               cwd=work, capture_output=True, timeout=3)
        report = json.loads(Path(work, "report.json").read_bytes())
        print(json.dumps({"exit": child.returncode, "classes": report["classes"], "version": report["version"]}))


if __name__ == "__main__":
    main()
