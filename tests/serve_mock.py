"""Run the mock Remux on a real port:  python -m tests.serve_mock 3002"""
import sys

import uvicorn

from .mock_remux import MockRemux

if __name__ == "__main__":
    uvicorn.run(MockRemux().app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
