"""One private Trackio backend; started by the manager, never exposed publicly."""
import sys

import uvicorn
from trackio.server import build_starlette_app_only

if __name__ == "__main__":
    app, _ = build_starlette_app_only()
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), root_path=sys.argv[2])
