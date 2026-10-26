import base64
import hashlib
import re
from pathlib import Path

EDITOR = Path(__file__).parent.parent / "docs" / "editor.html"


def test_editor_csp_allows_only_its_own_inline_script():
    html = EDITOR.read_text(encoding="utf-8")
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
    assert "default-src 'none'" in csp  # без connect-src/img-src: в сеть страница не ходит
    inline = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(inline.encode("utf-8")).digest()).decode()
    assert f"'sha256-{digest}'" in csp, "встроенный <script> изменился — обнови хеш в CSP"
