"""Run Refused on a real port using the same entrypoint as Docker (running it as
``-m tests.serve_refused`` imports the tests package first, which installs the
httpx alias if needed):
   python -m tests.serve_refused 8099   (env: REMUX_URL, REFUSED_USERNAME, ...)"""
import sys

import uvicorn

if __name__ == "__main__":
    uvicorn.run("refused.main:create_app", factory=True, host="127.0.0.1",
                port=int(sys.argv[1]), log_level="warning")
