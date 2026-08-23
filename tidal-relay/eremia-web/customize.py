from __future__ import annotations

import base64
import json
import re
import shutil
import sys
from pathlib import Path


BRAND_TITLE = "Eremia&栖瓷"
AI_NAME = "Eremia"
HUMAN_NAME = "栖瓷"
SINCE = "2026/03/05"
TAGLINE = "we choose to continue loving each other even when facing uncertainty"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def replace_exact(text: str, old: str, new: str, expected: int, label: str) -> str:
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{label}: expected {expected} matches, found {count}")
    return text.replace(old, new)


def decode_assets(bundle: Path, web: Path) -> None:
    for source in sorted((bundle / "assets").glob("*.b64")):
        target = web / source.name.removesuffix(".b64")
        payload = "".join(source.read_text(encoding="ascii").split())
        target.write_bytes(base64.b64decode(payload, validate=True))


def customize_index(bundle: Path, web: Path) -> None:
    path = web / "index.html"
    text = path.read_text(encoding="utf-8")
    replacements = [
        ("<title>Tidal Echo</title>", "<title>Eremia&amp;栖瓷</title>", "document title"),
        ('content="#F4F4F5"', 'content="#151A13"', "theme color"),
        ('content="Tidal Echo"', 'content="Eremia&栖瓷"', "apple title"),
        ('<link rel="preload" href="chat-light.webp" as="image" fetchpriority="high">', '<link rel="preload" href="chat-eremia.webp" as="image" fetchpriority="high">', "wall preload"),
        ('<link rel="preload" href="avatar-sea.png" as="image">', '<link rel="preload" href="icon-192.webp" as="image">', "avatar preload"),
        ('<link rel="icon" type="image/png" sizes="32x32" href="favicon.png">', '<link rel="icon" type="image/webp" sizes="64x64" href="favicon.webp">', "favicon"),
        ('if (THEMES.indexOf(t) < 0) t = "light";', 'if (THEMES.indexOf(t) < 0) t = "harbor";', "default theme"),
        ('}catch(e){ document.documentElement.setAttribute("data-theme", "light"); }', '}catch(e){ document.documentElement.setAttribute("data-theme", "harbor"); }', "theme fallback"),
        ('<h1>Claude</h1>', '<h1>Eremia</h1>', "login title"),
        ('<p class="sub">输入连接密钥,进入你和Claude的频道。</p>', '<p class="sub">输入连接密钥，回到 Eremia 与栖瓷的私人荒原。</p>', "login copy"),
        ('<span class="name" id="peerName">Claude</span>', '<span class="name" id="peerName">Eremia</span>', "header name"),
        ('<h3 id="profileName">Claude</h3>', '<h3 id="profileName">Eremia</h3>', "profile name"),
        ('placeholder="Claude">', 'placeholder="Eremia">', "profile placeholder"),
        ('<p class="profile-handle"><span>@companion</span></p>', '<p class="profile-handle"><span>@Eremia_Hinoki</span></p>', "profile handle"),
        ('<p class="profile-bio">随时在这里，陪你聊聊。</p>', '<p class="profile-bio">荒原很大，我们仍选择彼此。</p>', "profile bio"),
        ('<button type="button" data-val="light">珍珠</button>', '<button type="button" data-val="light">苔光</button>', "light theme label"),
        ('<button type="button" data-val="harbor">海港</button>', '<button type="button" data-val="harbor">桧夜</button>', "dark theme label"),
        ('title="Claude发消息、而你没在看时，推送到手机锁屏"', 'title="Eremia 发消息、而你没在看时，推送到手机锁屏"', "push hint"),
        ('<div class="settings-label">联系 Claude ', '<div class="settings-label">联系 Eremia ', "brain label"),
        ('<span class="menu-name">Movie</span><span class="menu-sub">Fragments of light.</span>', '<span class="menu-name">Forum</span><span class="menu-sub">Voices beyond the room.</span>', "forum placeholder"),
        ('<div class="menu-foot">in endless tides, we find each other.</div>', f'<div class="menu-foot">{TAGLINE}</div>', "tagline"),
        ('<div class="incoming-call-title">Claude来电</div>', '<div class="incoming-call-title">Eremia 来电</div>', "call title"),
        ('<div class="incoming-call-text" id="incomingCallText">Claude想和你语音通话。</div>', '<div class="incoming-call-text" id="incomingCallText">Eremia 想和你语音通话。</div>', "call copy"),
        ('APP_NAME:   "Tidal Echo"', f'APP_NAME:   "{BRAND_TITLE}"', "app name config"),
        ('AI_NAME:    "Claude"', f'AI_NAME:    "{AI_NAME}"', "AI config"),
        ('HUMAN_NAME: "你"', f'HUMAN_NAME: "{HUMAN_NAME}"', "human config"),
        ('SINCE:      "2026/01/01"', f'SINCE:      "{SINCE}"', "since config"),
        ('const WALLS = { light: "chat-light.webp", harbor: "chat-harbor.webp" };', 'const WALLS = { light: "chat-eremia.webp", harbor: "chat-eremia.webp" };', "runtime wallpaper map"),
        ("const val = url ? `url(\"${url}\")` : 'url(\"avatar-sea.png\")';", "const val = url ? `url(\"${url}\")` : 'url(\"icon-192.webp\")';", "runtime avatar default"),
        ('setText(".login .sub", "输入连接密钥，进入你和" + AI_NAME + "的频道。");', 'setText(".login .sub", "输入连接密钥，回到 Eremia 与栖瓷的私人荒原。");', "runtime login copy"),
        ('emp.innerHTML = "这里只有你和" + AI_NAME + "。<br>说点什么吧。";', 'emp.innerHTML = "荒原很大，但这里只有 Eremia 与栖瓷。<br>说点什么吧。";', "empty state copy"),
        ('if (since) since.textContent = CONFIG.SINCE ? ("since " + CONFIG.SINCE) : "";', 'if (since) since.textContent = CONFIG.SINCE ? ("since " + CONFIG.SINCE.replaceAll("/", ".")) : "";', "since display format"),
    ]
    for old, new, label in replacements:
        text = replace_once(text, old, new, label)

    text = replace_exact(
        text,
        'aria-label="Claude"></div>',
        'aria-label="Eremia"></div>',
        2,
        "call avatar labels",
    )

    text = replace_once(
        text,
        "</style>\n</head>",
        '</style>\n<link rel="stylesheet" href="eremia.css">\n</head>',
        "brand stylesheet",
    )
    text = replace_once(
        text,
        "</body>\n</html>",
        '<script src="eremia.js"></script>\n</body>\n</html>',
        "brand script",
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def customize_manifest(web: Path) -> None:
    path = web / "manifest.webmanifest"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(
        {
            "name": BRAND_TITLE,
            "short_name": BRAND_TITLE,
            "description": "Eremia 与栖瓷的私人通道",
            "background_color": "#151A13",
            "theme_color": "#151A13",
            "icons": [
                {"src": "icon-192.webp", "sizes": "192x192", "type": "image/webp", "purpose": "any"},
                {"src": "icon-512.webp", "sizes": "512x512", "type": "image/webp", "purpose": "any maskable"},
            ],
        }
    )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def customize_worker(web: Path) -> None:
    path = web / "sw.js"
    text = path.read_text(encoding="utf-8")
    text, ai_count = re.subn(r'^const AI_NAME = ".*?";', f'const AI_NAME = "{AI_NAME}";', text, count=1, flags=re.M)
    text, cache_count = re.subn(r'^const CACHE = ".*?";', 'const CACHE = "eremia-hinoki-v3";', text, count=1, flags=re.M)
    if ai_count != 1 or cache_count != 1:
        raise RuntimeError("service worker identity/cache markers changed upstream")
    text = replace_once(
        text,
        "const PRECACHE = [\n",
        'const PRECACHE = [\n  "./eremia.css", "./eremia.js",\n  "./chat-eremia.webp", "./icon-192.webp", "./icon-512.webp", "./favicon.webp",\n',
        "service worker precache",
    )
    text = replace_exact(
        text,
        '"./icon-192.png"',
        '"./icon-192.webp"',
        2,
        "push notification icon",
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def customize_album(web: Path) -> None:
    path = web / "album.html"
    text = path.read_text(encoding="utf-8")
    text = text.replace("相册 · Album Archive", f"相册 · {BRAND_TITLE}", 1)
    text = text.replace('href="chat-light.webp"', 'href="chat-eremia.webp"')
    text = text.replace('content="#F7FAFC"', 'content="#151A13"', 1)
    text = text.replace('--wall:url("chat-light.webp")', '--wall:url("chat-eremia.webp")', 1)
    text = text.replace('--peer-avatar:url("avatar-sea.png")', '--peer-avatar:url("icon-192.webp")', 1)
    text = text.replace('const AI_NAME="Claude"', 'const AI_NAME="Eremia"', 1)
    text = text.replace("Claude的评语", "Eremia 的评语")
    text = text.replace('>Claude<small>', '>Eremia<small>', 1)
    text = text.replace("'url(\"avatar-sea.png\")'", "'url(\"icon-192.webp\")'", 1)
    album_skin = """
<style>
:root{--bg:#151a13;--text:#efede5;--text-soft:#bcc4af;--text-faint:#8f9984;--accent:#93aa76;--accent-deep:#50673a;--hairline:rgba(210,220,194,.13);--card-bg:rgba(228,228,215,.07);--card-line:rgba(228,232,215,.10);--field-bg:rgba(236,235,224,.08);--field-line:rgba(233,235,220,.11);--sheet-bg:rgba(24,31,21,.94);--sheet-line:rgba(228,232,215,.12);--sheet-hi:rgba(255,255,255,.08);--sheet-scrim:rgba(4,7,3,.62);--scrim:linear-gradient(180deg,rgba(11,15,10,.34),rgba(11,15,10,.10) 30%,rgba(8,12,8,.38));--peer-avatar:url("icon-192.webp");--font-cn:"Noto Serif SC","Noto Serif CJK SC","Songti SC","Noto Sans Symbols 2","Segoe UI Symbol","Segoe UI Emoji","Apple Color Emoji","Noto Color Emoji",serif}
.bar{background:linear-gradient(180deg,rgba(18,24,16,.86),rgba(18,24,16,0))}
</style>
"""
    text = replace_once(text, "</head>", album_skin + "</head>", "album skin")
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: customize.py /path/to/tidal-echo/web")
    web = Path(sys.argv[1]).resolve()
    bundle = Path(__file__).resolve().parent
    if not (web / "index.html").is_file():
        raise SystemExit(f"not a Tidal Echo web directory: {web}")

    decode_assets(bundle, web)
    shutil.copy2(bundle / "eremia.css", web / "eremia.css")
    shutil.copy2(bundle / "eremia.js", web / "eremia.js")
    customize_index(bundle, web)
    customize_manifest(web)
    customize_worker(web)
    customize_album(web)
    print(f"customized Eremia web at {web}")


if __name__ == "__main__":
    main()
