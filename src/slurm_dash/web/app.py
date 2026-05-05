import json
import os
from pathlib import Path
import threading
import webbrowser

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Slurm Dash", docs_url=None, redoc_url=None)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["tojson"] = lambda v, **_: json.dumps(v, default=str)
templates.env.filters["basename"] = lambda p: os.path.basename(p)

from .routes import router  # noqa: E402  (after templates is defined)
app.include_router(router)


def run_web_server(host: str = "127.0.0.1", port: int = 7860) -> None:
    import uvicorn
    url = f"http://{host}:{port}"
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Slurm Dash  →  {url}  (Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
