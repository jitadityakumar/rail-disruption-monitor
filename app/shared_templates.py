import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")

_css_path = Path("static/style.css")
_css_hash = hashlib.md5(_css_path.read_bytes()).hexdigest()[:12]
templates.env.globals["css_version"] = _css_hash
