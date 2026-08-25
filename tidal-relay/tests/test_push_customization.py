from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CUSTOMIZER = ROOT / "eremia-web" / "customize.py"


def load_customizer():
    spec = importlib.util.spec_from_file_location("eremia_customize", CUSTOMIZER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {CUSTOMIZER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    customizer = load_customizer()
    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        web = repo / "web"
        web.mkdir()

        (web / "sw.js").write_text(
            '''const AI_NAME = "Claude";\n'''
            '''const CACHE = "companion-v1";\n'''
            '''const PRECACHE = [\n];\n'''
            '''self.addEventListener("push", (e) => {\n'''
            '''  let d = {};\n'''
            '''  const title = d.title || AI_NAME;\n'''
            '''  const body = d.body || "new";\n'''
            '''  const tag = "message";\n'''
            '''  e.waitUntil(\n'''
            '''    self.registration.showNotification(title, {\n'''
            '''      body,\n'''
            '''      tag,\n'''
            '''      renotify: true,\n'''
            '''      icon:  "./icon-192.png",\n'''
            '''      badge: "./icon-192.png",\n'''
            '''      vibrate: [80, 40, 80],\n'''
            '''      data: { url: d.url || "./" },\n'''
            '''    })\n'''
            '''  );\n'''
            '''});\n''',
            encoding="utf-8",
        )

        index_text = '''function openStream(){
  if (es){ try{ es.close(); }catch(e){} es = null; }
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || !secret) return;
  backfill();
  refreshContextStatus();
  if (!es || es.readyState !== 1 || Date.now() - lastStreamEventAt > STREAM_STALE_MS) openStream();
});'''
        index_text = customizer.customize_background_stream(index_text)
        customizer.customize_worker(web)

        worker_text = (web / "sw.js").read_text(encoding="utf-8")
        assert "function suspendHiddenStream()" in index_text
        assert "if (!USE_MOCK && document.hidden)" in index_text
        assert 'document.visibilityState !== "visible"' in index_text
        assert 'document.visibilityState !== "visible" || !secret' not in index_text
        assert index_text.count("suspendHiddenStream();") == 2
        assert index_text.rstrip().endswith("});")
        assert 'const CACHE = "eremia-hinoki-v6";' in worker_text
        assert worker_text.count('"./icon-192.webp"') == 3

    print("push customization tests passed")


if __name__ == "__main__":
    main()
