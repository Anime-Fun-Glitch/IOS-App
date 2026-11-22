"""Робить pc/host/splash.html із заставки iOS у anime_app.py (та сама анімація). Запуск: python pc/make_splash.py"""
import re, os
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(root, 'src', 'anisyns', 'anime_app.py'), encoding='utf-8').read()
css = [l for l in src.split('\n') if l.startswith('#splash') or re.match(r'@keyframes sp(Glow|Pop|Rise|Swoosh|Play|Beat|Fade|Run)\b', l)]
css.append('@media (prefers-reduced-motion:reduce){#splash *{animation-duration:.01s!important;animation-delay:0s!important}}')
i = src.index('<div id="splash"'); j = src.index('<div class="ld"></div></div>', i) + len('<div class="ld"></div></div>')
div = re.sub(r' onclick="[^"]*"', '', src[i:j], count=1)
html = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>html,body{margin:0;height:100%;background:#0d0e13;overflow:hidden;cursor:default;user-select:none}\n' + '\n'.join(css) + '</style></head><body>\n' + div +
        '\n<script>requestAnimationFrame(function(){requestAnimationFrame(function(){document.getElementById("splash").classList.add("go")})})</script></body></html>')
open(os.path.join(root, 'pc', 'host', 'splash.html'), 'w', encoding='utf-8').write(html)
print('splash.html', len(html))
