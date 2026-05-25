import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

_app_dir = Path(__file__).parent
templates = Jinja2Templates(directory=str(_app_dir / "templates"))

_css_path = _app_dir / "static/style.css"
assert _css_path.exists(), f"CSS not found: {_css_path.resolve()}"
_css_hash = hashlib.sha256(_css_path.read_bytes()).hexdigest()[:12]
templates.env.globals["css_version"] = _css_hash
