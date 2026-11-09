import base64
import collections
import concurrent.futures
import datetime
import hashlib
import html as htmllib
import json
import mimetypes
import os
import random
import re
import unicodedata
import shutil
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests

MOBILE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)'
DESKTOP_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
# Сторінки запитуємо як «десктопний» браузер: на багатьох сайтах у мобільній версії немає таймера, пропусків інтро тощо
HEADERS = {'User-Agent': DESKTOP_UA}
QUALITIES = ['1080p', '720p', '480p', '360p']
MAX_WORKERS = 12
BASE_DIR = '.'  # папка з відео, library.json і кешем обкладинок
PORT = 8765
HOST = '127.0.0.1'
IOS_APP = False  # true, коли працює всередині iPhone-застосунку (див. app.py): тоді сторінка сама враховує вирізи екрана


def log(msg):
  pass  # у серверному режимі повідомлення ядра не потрібні




# ───────────────────────── Мережевий шар ─────────────────────────
# Один спільний Session (keep-alive), короткі таймаути, повтори, перевірка скасування.

class Cancelled(Exception):
  pass


_tl = threading.local()  # на потік: cancel (Event) і stage (callback для статусу)


def _stage(msg):
  cb = getattr(_tl, 'stage', None)
  if cb:
    try:
      cb(msg)
    except Exception:
      pass


PAUSE = {'on': False}  # загальна пауза завантажень (стан зберігається на диск)


PAUSED_KIDS = set()  # окремі аніме, поставлені на паузу (решта черги качається далі)


def _kpaused(cancel):
  return cancel is not None and getattr(cancel, 'kid', None) in PAUSED_KIDS


def _pause_wait(cancel=None):
  while PAUSE['on'] or _kpaused(cancel):
    if cancel is not None and cancel.is_set():
      raise Cancelled()
    time.sleep(0.4)


def _chk():
  c = getattr(_tl, 'cancel', None)
  if c is not None and c.is_set():
    raise Cancelled()
  if (PAUSE['on'] and getattr(_tl, 'job', False)) or _kpaused(c):
    _pause_wait(c)


def _make_session():
  from requests.adapters import HTTPAdapter
  s = requests.Session()
  ad = HTTPAdapter(pool_connections=24, pool_maxsize=48, max_retries=0)
  s.mount('http://', ad)
  s.mount('https://', ad)
  s.headers.update(HEADERS)
  return s


SESS = _make_session()
class _ASem:
  """Семафор зі змінним лімітом — потрібен, щоб режим «Багатозадачність» вмикався й вимикався без перезапуску."""

  def __init__(self, n):
    self._c = threading.Condition()
    self._n, self._used = n, 0

  def acquire(self, blocking=True, timeout=None):
    end = None if timeout is None else time.time() + timeout
    with self._c:
      while self._used >= self._n:
        if not blocking:
          return False
        rem = None if end is None else end - time.time()
        if rem is not None and rem <= 0:
          return False
        self._c.wait(rem)
      self._used += 1
      return True

  def release(self):
    with self._c:
      self._used = max(0, self._used - 1)
      self._c.notify()

  def set_limit(self, n):
    with self._c:
      self._n = n
      self._c.notify_all()

  def __enter__(self):
    self.acquire()
    return self

  def __exit__(self, *a):
    self.release()


# ліміти одночасних операцій: звичайний (безпечний) режим і режим «Багатозадачність»
_LIM_BASE = {'net': 5, 'browse': 2, 'heavy': 2, 'heavy_bg': 1, 'thumb': 2, 'img': 3, 'jobs': 1, 'seg': 12}
_LIM_MT = {'net': 12, 'browse': 4, 'heavy': 4, 'heavy_bg': 2, 'thumb': 4, 'img': 6, 'jobs': 3, 'seg': 8}


def _lim(k):
  return (_LIM_MT if SETTINGS.get('multitask') else _LIM_BASE)[k]


_NET_SEM = _ASem(5)  # одночасно не більше 5 запитів сторінок/пошуку (у режимі «Багатозадачність» — 12)


def _net_request(method, url, **kw):
  while not _NET_SEM.acquire(timeout=0.5):
    _chk()
  try:
    return SESS.request(method, url, **kw)
  finally:
    _NET_SEM.release()


def http(method, url, retries=2, **kw):
  """requests з таймаутом (6 с на з'єднання, до 15 с на відповідь), повторами й перевіркою скасування."""
  t = kw.pop('timeout', 15)
  ua = getattr(_tl, 'ua', None)
  if ua:
    kw['headers'] = {**(kw.get('headers') or {}), 'User-Agent': ua}
  kw['timeout'] = (6, min(t, 15) if not isinstance(t, tuple) else t[1])
  last = None
  for attempt in range(retries + 1):
    _chk()
    try:
      r = _net_request(method, url, **kw)
      if r.status_code in (502, 503, 504) and attempt < retries:
        time.sleep(0.4 * (attempt + 1))
        continue
      return r
    except (requests.ConnectionError, requests.Timeout) as ex:
      last = ex
      if attempt < retries:
        time.sleep(0.4 * (attempt + 1))
  raise RuntimeError('Сайт не відповідає (' + type(last).__name__ + '). Перевірте інтернет або спробуйте пізніше.')


def http_get(url, **kw):
  return http('GET', url, **kw)


def http_post(url, **kw):
  return http('POST', url, **kw)


def run_cancellable(cmd, cancel=None, timeout=1800):
  """Запускає ffmpeg без stdin, дозволяє скасування. Повертає (код, хвіст stderr)."""
  import tempfile
  with tempfile.TemporaryFile() as err:
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
    t0 = time.time()
    try:
      while True:
        try:
          p.wait(timeout=0.4)
          break
        except subprocess.TimeoutExpired:
          pass
        if (PAUSE['on'] or _kpaused(cancel)) and p.poll() is None:  # пауза: заморожуємо ffmpeg і чекаємо
          import signal
          t_p = time.time()
          try:
            os.kill(p.pid, signal.SIGSTOP)
          except Exception:
            pass
          try:
            while (PAUSE['on'] or _kpaused(cancel)) and p.poll() is None:
              if cancel is not None and cancel.is_set():
                break
              time.sleep(0.4)
          finally:
            try:
              os.kill(p.pid, signal.SIGCONT)
            except Exception:
              pass
          t0 += time.time() - t_p
        if cancel is not None and cancel.is_set():
          p.kill()
          p.wait()
          raise Cancelled()
        if time.time() - t0 > timeout:
          p.kill()
          p.wait()
          return -9, 'ffmpeg завис і був зупинений'
    finally:
      if p.poll() is None:
        p.kill()
    err.seek(0)
    tail = err.read()[-400:].decode('utf8', 'replace').strip()
    return p.returncode, tail
# ───────────────────────── Парсинг сторінки ─────────────────────────

def _devalue(arr):
  """Розпаковує __NUXT_DATA__ (формат devalue: об'єкти посилаються на індекси)."""
  memo = {}

  def res(i):
    if not isinstance(i, int) or isinstance(i, bool):
      return i
    if i < 0:
      return None
    if i in memo:
      return memo[i]
    v = arr[i]
    if isinstance(v, list):
      if v and isinstance(v[0], str):
        tag = v[0]
        if tag in ('Reactive', 'ShallowReactive', 'Ref', 'ShallowRef'):
          memo[i] = None
          memo[i] = res(v[1])
          return memo[i]
        if tag == 'EmptyRef':
          memo[i] = res(v[1])
          return memo[i]
        if tag == 'Date':
          memo[i] = v[1]
          return v[1]
        memo[i] = None  # Set/Map та інше — не потрібно
        return None
      out = []
      memo[i] = out
      out.extend(res(x) for x in v)
      return out
    if isinstance(v, dict):
      out = {}
      memo[i] = out
      for k, x in v.items():
        out[k] = res(x)
      return out
    memo[i] = v
    return v

  return res(0)


def _walk(node, found, seen):
  if id(node) in seen:
    return
  seen.add(id(node))
  if isinstance(node, dict):
    found.append(node)
    for v in node.values():
      _walk(v, found, seen)
  elif isinstance(node, list):
    for v in node:
      _walk(v, found, seen)


def _pick(d, *keys):
  for k in keys:
    if d.get(k):
      return d[k]
  return None


def _num(x):
  if isinstance(x, float) and x.is_integer():
    return int(x)
  return x


def _make_episode(d):
  links = {}
  for label, keys in (
      ('1080p', ('hls1080', 'hls_1080')),
      ('720p', ('hls720', 'hls_720')),
      ('480p', ('hls480', 'hls_480')),
  ):
    u = _pick(d, *keys)
    if u:
      links[label] = htmllib.unescape(u)
  skips = {}
  for key, names in (('opening', ('opening', 'intro')), ('ending', ('ending', 'outro'))):
    v = next((d.get(n) for n in names if isinstance(d.get(n), dict)), None)
    if v:
      st = v.get('startAt', v.get('start_at', v.get('start')))
      en = v.get('endAt', v.get('end_at', v.get('stop', v.get('end'))))
      if isinstance(st, (int, float)) and isinstance(en, (int, float)) and en > st:
        skips[key] = [float(st), float(en)]
  pv = d.get('preview')
  preview = None
  if isinstance(pv, dict):
    preview = pv.get('src') or pv.get('thumbnail') or ((pv.get('optimized') or {}).get('src') if isinstance(pv.get('optimized'), dict) else None)
  elif isinstance(pv, str):
    preview = pv
  return {
      'skips': skips,
      'preview': preview,
      'duration': d.get('duration'),
      'id': d.get('id'),
      'updated': d.get('updatedAt') or d.get('updated_at'),
      'ordinal': _num(d.get('ordinal')),
      'title': d.get('name') or '',
      'links': links,
  }


def _abs(base, u):
  if not u:
    return None
  u = htmllib.unescape(str(u))
  if u.startswith('//'):
    return 'https:' + u
  return urljoin(base, u) if base else u


def _strip_tags(t):
  t = re.sub(r'<br\s*/?>', '\n', t or '', flags=re.I)
  t = htmllib.unescape(re.sub(r'<[^>]+>', ' ', t))
  t = re.sub(r'[ \t\r\f\v]+', ' ', t)
  return re.sub(r'\n\s*\n+', '\n\n', t).strip()


def _to_ts(v):
  """Дата з сайту (ISO-рядок, ['Date', iso], мс/с) → unix-секунди UTC або None."""
  if isinstance(v, list) and len(v) == 2 and v[0] == 'Date':
    v = v[1]
  if isinstance(v, (int, float)) and not isinstance(v, bool):
    return int(v / 1000) if v > 1e11 else int(v)
  if isinstance(v, str) and v:
    try:
      return int(datetime.datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp())
    except Exception:
      if v.isdigit():
        return _to_ts(int(v))
  return None


def _anilib_next(node, episodes):
  """Приблизна дата наступної серії: остання серія + 7 днів (лише для онгоїнгів)."""
  if not isinstance(node, dict) or not node.get('isOngoing', node.get('is_ongoing')):
    return None
  ords = [e['ordinal'] for e in episodes if isinstance(e.get('ordinal'), (int, float))]
  if not ords:
    return None
  last = max(ords)
  total = node.get('episodesTotal') or node.get('episodes_total')
  if isinstance(total, int) and total and last >= total:
    return None
  stamps = [t for t in (_to_ts(e.get('updated')) for e in episodes if e['ordinal'] == last) if t]
  if not stamps:
    return None
  return {'ts': stamps[0] + 7 * 86400, 'ep': _num(last + 1), 'approx': True}


def _anilib_meta(node, base=None):
  """Постер, опис та інше з об'єкта релізу AniLiberty (camelCase і snake_case)."""
  if not isinstance(node, dict):
    return {}
  poster = node.get('poster') or {}
  src = None
  if isinstance(poster, dict):
    opt = poster.get('optimized') or {}
    src = poster.get('src') or (opt.get('src') if isinstance(opt, dict) else None)
  name = node.get('name') if isinstance(node.get('name'), dict) else {}
  genres = [g.get('name') for g in node.get('genres') or [] if isinstance(g, dict) and g.get('name')]
  return {
      'poster': _abs(base or 'https://anilibria.top/', src),
      'description': (node.get('description') or '').replace('\r', '').strip() or None,
      'year': node.get('year'),
      'original': name.get('english'),
      'genres': genres,
  }


def _html_next(html):
  """Таймер до наступної серії зі звичайної сторінки: data-атрибути, JSON, текст «через N днів»."""
  now = time.time()
  pat = re.compile(
      r'(?:next[_-]?(?:episode|ep|release|air|serie)[a-z_-]*|countdown|timer|data-time|data-date)["\'\s:=]+["\']?'
      r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?|\d{10,13})', re.I)
  for m in pat.finditer(html):
    v = m.group(1)
    if re.match(r'\d{4}-', v) and not re.search(r'(Z|[+-]\d{2}:?\d{2})$', v):
      v = v.replace(' ', 'T') + '+03:00'  # без пояса — зазвичай московський час
    ts = _to_ts(v)
    if ts and ts > now - 86400:
      return {'ts': ts, 'ep': None, 'approx': False}
  m = re.search(r'следующ\w+\s+сери\w+[^<]{0,60}?через\s+(?:(\d+)\s*(?:д|дн)\w*)?\s*(?:(\d+)\s*(?:ч|час)\w*)?\s*(?:(\d+)\s*(?:м|мин)\w*)?', html, re.I)
  if m and any(m.groups()):
    d, h, mi = (int(x or 0) for x in m.groups())
    return {'ts': int(now + d * 86400 + h * 3600 + mi * 60), 'ep': None, 'approx': True}
  return None


def _html_meta(html, base):
  """Постер/опис зі звичайної сторінки: JSON-LD, itemprop, og:*."""
  meta = {'poster': None, 'description': None, 'year': None, 'original': None, 'genres': []}
  for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
    try:
      d = json.loads(m.group(1))
    except Exception:
      continue
    if isinstance(d, dict) and d.get('@type') in ('TVSeries', 'Movie', 'TVSeason', 'CreativeWork'):
      img = d.get('image')
      if isinstance(img, list) and img:
        img = img[0]
      meta['poster'] = _abs(base, img) if isinstance(img, str) else None
      meta['description'] = d.get('description') or None
      meta['year'] = d.get('datePublished')
      alt = d.get('alternateName')
      meta['original'] = alt[0] if isinstance(alt, list) and alt else (alt if isinstance(alt, str) else None)
      g = d.get('genre')
      meta['genres'] = [x for x in (g if isinstance(g, list) else [g]) if x]
      break
  meta['next'] = _html_next(html)
  if not meta['poster']:
    # anim-ru та схожі (DLE): og:image — широкий банер, а справжня обкладинка — перше <img class="…poster…">
    m = re.search(r'<img[^>]+class="[^"]*\bxfieldimage\b[^"]*\bposter\b[^"]*"[^>]*?\ssrc="([^"]+)"', html)
    if m:
      meta['poster'] = _abs(base, m.group(1))
  if not meta['poster']:
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if m:
      meta['poster'] = _abs(base, m.group(1))
  if not meta['description']:
    m = re.search(r'<div[^>]*itemprop="description"[^>]*>(.*?)</div>', html, re.S)
    if m:
      meta['description'] = _strip_tags(m.group(1)) or None
  if not meta['description']:
    m = re.search(r'<meta property="og:description" content="([^"]*)"', html)
    if m:
      meta['description'] = htmllib.unescape(m.group(1)) or None
  return meta


def parse_page(html, url=None):
  """Повертає (назва_аніме, список_серій, метадані)."""
  m = re.search(
      r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S
  )
  if not m:
    return None, [], {}
  data = _devalue(json.loads(m.group(1)))
  nodes = []
  _walk(data, nodes, set())

  episodes = {}
  release_name = None
  release_node = None
  alias = None
  try:
    am = re.search(r'/release/([^/?#]+)', urlparse(url or '').path)
    alias = unquote(am.group(1)).lower() if am else None
  except Exception:
    alias = None
  cands = [d for d in nodes if isinstance(d.get('name'), dict) and d['name'].get('main') and 'episodes' in d]
  if alias:  # сторінка може містити й інші релізи (стрічка новинок, схожі) — беремо саме той, чия адреса відкрита
    byal = [d for d in cands if str(d.get('alias') or '').lower() == alias]
    if byal:
      cands = byal
  if cands:
    release_node = cands[0]
    release_name = release_node['name']['main']

  def collect(src):
    for d in src:
      if 'ordinal' in d and _pick(d, 'hls480', 'hls720', 'hls1080', 'hls_480', 'hls_720', 'hls_1080'):
        ep = _make_episode(d)
        if ep['id'] and ep['id'] not in episodes:
          episodes[ep['id']] = ep

  if release_node is not None and isinstance(release_node.get('episodes'), list):
    sub = []
    _walk(release_node['episodes'], sub, set())
    collect(sub)
  if not episodes:  # запасний варіант: усі серії зі сторінки (як раніше)
    collect(nodes)

  if release_name is None:
    t = re.search(r'<title>(.*?)</title>', html, re.S)
    if t:
      parts = [p.strip() for p in htmllib.unescape(t.group(1)).split('|')]
      release_name = parts[-2] if len(parts) >= 3 else parts[0]

  eps = sorted(
      episodes.values(),
      key=lambda e: (e['ordinal'] is None, e['ordinal'] or 0),
  )
  meta = _anilib_meta(release_node)
  if isinstance(release_node, dict) and release_node.get('id'):
    meta['rid'] = release_node.get('id')
  exact = _find_next(release_node) if release_node else None
  meta['next'] = ({'ts': exact, 'ep': (max([e['ordinal'] for e in eps if isinstance(e['ordinal'], (int, float))] or [0]) + 1) or None, 'approx': False}
                  if exact else _anilib_next(release_node, eps))
  return release_name, eps, meta


def fetch_episodes(url):
  """Повертає (назва, серії, тільки_одна_серія: bool, метадані). Спершу як десктопний браузер,
  якщо так нічого не знайшли — ще раз як мобільний."""
  res = err = None
  try:
    res = _fetch_episodes(url)
  except Cancelled:
    raise
  except Exception as ex:
    err = ex
  if res is None or not res[1]:
    _tl.ua = MOBILE_UA
    try:
      res2 = _fetch_episodes(url)
      if res2 and res2[1]:
        return res2
    except Cancelled:
      raise
    except Exception:
      pass
    finally:
      _tl.ua = None
  if res is None:
    raise err
  return res


def _rel_anilib(url, meta):
  """AniLiberty: «Связанное» (франшиза). meta['rid'] — службове поле з parse_page, у бібліотеку не потрапляє."""
  rid = meta.pop('rid', None)
  m = re.search(r'/release/([^/?#]+)', urlparse(url).path)
  if not m:
    return
  host, alias = urlparse(url).netloc, unquote(m.group(1))
  rel = related_cached('a:' + alias, lambda: _anilib_related(host, rid, alias))
  rel = _rel_clean(rel, None)
  rel = [x for x in rel if f'/release/{alias}/' not in x['url']]
  if rel:
    meta['related'] = rel


def _fetch_episodes(url):
  """Повертає (назва, серії, тільки_одна_серія: bool, метадані)."""
  if '/catalog/item/' in urlparse(url).path:
    return _fetch_yummy(url)
  if _is_vost(url):
    return _fetch_vost(url)
  _stage('Читаю сторінку…')
  html = http_get(url, headers=HEADERS, timeout=20).text
  name, eps, meta = parse_page(html, url)
  _rel_anilib(url, meta)

  # Посилання на конкретну серію: /anime/video/episode/<uuid>
  m = re.search(r'/episode/([0-9a-fA-F-]{36})', urlparse(url).path)
  only_one = False
  if m and eps:
    ep_id = m.group(1).lower()
    chosen = [e for e in eps if (e['id'] or '').lower() == ep_id]
    if chosen:
      eps = chosen
      only_one = True

  # Сайти зі вбудованим плеєром (напр. anim-ru.net)
  if not eps:
    res = _fetch_embedded(url, html)
    if res:
      return res

  # Запасний варіант: офіційне API (якщо в HTML даних не знайшли)
  if not eps:
    alias = re.search(r'/release/([^/?#]+)', urlparse(url).path)
    if alias:
      host = urlparse(url).netloc
      api = f'https://{host}/api/v1/anime/releases/{alias.group(1)}'
      j = http_get(api, headers=HEADERS, timeout=20).json()
      name = (j.get('name') or {}).get('main') or alias.group(1)
      meta = _anilib_meta(j, f'https://{host}/')
      for d in j.get('episodes') or []:
        ep = _make_episode(d)
        if ep['links']:
          eps.append(ep)
      eps.sort(key=lambda e: (e['ordinal'] is None, e['ordinal'] or 0))
  return name, eps, only_one, meta


# ─────────── Сайти зі вбудованим плеєром (anim-ru.net та схожі) ───────────

PLAYER_RE = re.compile(r'<iframe[^>]+src="(https?://[^"]*?/player/\d+[^"]*)"', re.I)
EP_LABEL_RE = re.compile(r'(?:сери[яию]|серія|эпизод|episode)\s*(\d+(?:\.\d+)?)', re.I)


def _clean_title(t):
  t = htmllib.unescape(re.sub(r'<[^>]+>', ' ', t or ''))
  t = re.sub(r'\s+', ' ', t).strip()
  t = re.sub(r'\s*\[?\s*\d+\s*[-–]\s*\d+\s+сери\w*(\s+из\s+\d+)?\s*\]?.*$', '', t, flags=re.I)
  t = re.sub(r'\s+(аниме\s+)?смотреть.*$', '', t, flags=re.I)
  t = re.sub(r'\s*\(\d{4}\)\s*$', '', t)
  return t.strip()


def _page_title(html):
  for pat in (
      r'property="og:title"\s+content="([^"]*)"',
      r'<h1[^>]*>(.*?)</h1>',
      r'<title>(.*?)</title>',
  ):
    m = re.search(pat, html, re.S)
    if m and _clean_title(m.group(1)):
      return _clean_title(m.group(1))
  return None


def _decode_player_files(player_html):
  """Дістає {'720p': url, ...} з коду плеєра (file: atob("..."))."""
  text = ''
  m = re.search(r'atob\(\s*["\']([A-Za-z0-9+/=]+)["\']\s*\)', player_html)
  if m:
    try:
      text = base64.b64decode(m.group(1)).decode('utf8', 'replace')
    except Exception:
      text = ''
  if not text:
    text = player_html
  links = {}
  for q, u in re.findall(
      r'\[(\d{3,4})p?\]\s*(https?://[^\s,\[\]"\'<>\\]+?\.m3u8[^\s,\[\]"\'<>\\]*)', text
  ):
    links[f'{q}p'] = u
  if not links:
    urls = re.findall(r'https?://[^\s"\'<>\\]+?\.m3u8[^\s"\'<>\\]*', text)
    if len(urls) == 1:
      links['720p'] = urls[0]
  return links


def _fetch_embedded(url, html):
  """Сторінки аніме з перемикачем серій і iframe-плеєром. None, якщо не підходить."""
  if not PLAYER_RE.search(html) and 'sel_page' not in html:
    return None

  path = urlparse(url).path
  cur = re.search(r'/page-(\d+)-', path)
  cur_page = int(cur.group(1)) if cur else None

  items = []
  for val, label in re.findall(r'<option value="(/page-(?:\d+)-[^"]+)"[^>]*>([^<]*)</option>', html):
    pg = int(re.search(r'/page-(\d+)-', val).group(1))
    m = EP_LABEL_RE.search(label)
    items.append({'page': pg, 'path': val, 'ordinal': _num(float(m.group(1))) if m else pg})
  if not items:  # запасний варіант — посилання пагінації
    seen = set()
    for val, pg in re.findall(r'href="(/page-(\d+)-[^"]+)"', html):
      if val not in seen:
        seen.add(val)
        items.append({'page': int(pg), 'path': val, 'ordinal': int(pg)})
  if not items:  # одна сторінка — одна серія
    items = [{'page': 1, 'path': path, 'ordinal': 1}]
    cur_page = 1

  items.sort(key=lambda i: i['page'])
  only_one = False
  if cur_page is not None:
    chosen = [i for i in items if i['page'] == cur_page]
    if chosen:
      items, only_one = chosen, True

  def resolve(item):
    try:
      if (cur_page is None and item['page'] == 1) or cur_page == item['page']:
        h = html
      else:
        h = http_get(urljoin(url, item['path']), headers=HEADERS, timeout=20).text
      m = PLAYER_RE.search(h)
      if not m:
        return None
      src = htmllib.unescape(m.group(1))
      origin = '{0.scheme}://{0.netloc}'.format(urlparse(src))
      ph = http_get(
          src, headers={**HEADERS, 'Referer': urljoin(url, item['path'])}, timeout=20
      ).text
      links = _decode_player_files(ph)
      if not links:
        return None
      return {
          'id': item['path'],
          'ordinal': item['ordinal'],
          'title': '',
          'links': links,
          'headers': {'Referer': origin + '/', 'Origin': origin},
      }
    except Exception:
      return None

  with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
    results = list(ex.map(resolve, items))
  eps = [r for r in results if r]
  skipped = len(items) - len(eps)
  if skipped:
    log(f'ℹ️ Серій без доступного відео (пропущено): {skipped}')
  eps.sort(key=lambda e: (e['ordinal'] is None, e['ordinal'] or 0))
  meta = _html_meta(html, url)
  if 'Похожие' in html:
    meta['related'] = _rel_clean(_animru_related(html, url), url)
  return _page_title(html), eps, only_one, meta


# ─────────── YummyAnime (серії через плеєр Kodik) ───────────

# Ідентифікатор додатку, який сам сайт передає в API. Можна замінити на свій
# (реєстрація: https://ru.yummyani.me/dev/applications).
YUMMY_APP_TOKEN = '1-bju0-ww9s67ec2'
_kodik_endpoint_cache = {}


def _kodik_decode(src):
  """Kodik ховає посилання: зсув літер + base64. Зсув перебираємо (зараз 18)."""
  if '//' in src:
    return src
  for shift in [18] + [s for s in range(26) if s != 18]:

    def rot(m, shift=shift):
      c = m.group(0)
      limit = 90 if c <= 'Z' else 122
      code = ord(c) + shift
      return chr(code if code <= limit else code - 26)

    s = re.sub(r'[a-zA-Z]', rot, src)
    try:
      dec = base64.b64decode(s + '=' * (-len(s) % 4)).decode('utf8')
    except Exception:
      continue
    if '//' in dec or '.m3u8' in dec:
      return dec
  return None


def _kodik_endpoint(origin, html):
  """Адреса, на яку плеєр надсилає запит за посиланнями (зараз /ftor)."""
  m = re.search(r'src="(/assets/js/app\.player[^"]+\.js)"', html)
  if m:
    path = m.group(1)
    if path not in _kodik_endpoint_cache:
      ep = None
      try:
        js = http_get(origin + path, headers=HEADERS, timeout=30).text
        mm = re.search(r'url:\s*atob\("([^"]+)"\)', js)
        if mm:
          ep = base64.b64decode(mm.group(1)).decode()
      except Exception:
        pass
      _kodik_endpoint_cache[path] = ep
    ep = _kodik_endpoint_cache[path]
    if ep and ep.startswith('/'):
      return ep
  return '/ftor'


KODIK_THUMBS = {}


def kodik_links(iframe_url, referer):
  """Повертає ({'720p': m3u8, ...}, headers) для плеєра Kodik."""
  url = 'https:' + iframe_url if iframe_url.startswith('//') else iframe_url
  u = urlparse(url)
  origin = f'{u.scheme}://{u.netloc}'
  html = http_get(url, headers={**HEADERS, 'Referer': referer}, timeout=20).text

  mp = re.search(r"urlParams\s*=\s*'(\{.*?\})'", html, re.S)
  mt = re.search(r"vInfo\.type\s*=\s*'([^']*)'", html)
  mh = re.search(r"vInfo\.hash\s*=\s*'([^']*)'", html)
  mi = re.search(r"vInfo\.id\s*=\s*'([^']*)'", html)
  if not (mp and mt and mh and mi):
    raise RuntimeError('плеєр Kodik не віддав дані (відео недоступне або змінилась сторінка)')
  p = json.loads(mp.group(1))
  endpoint = _kodik_endpoint(origin, html)

  data = None
  for ref in dict.fromkeys([unquote(p.get('ref', '')), p.get('ref', ''), '']):
    r = http_post(
        origin + endpoint,
        data={
            'd': p.get('d', ''),
            'd_sign': p.get('d_sign', ''),
            'pd': p.get('pd', ''),
            'pd_sign': p.get('pd_sign', ''),
            'ref': ref,
            'ref_sign': p.get('ref_sign', ''),
            'bad_user': 'false',
            'cdn_is_working': 'true',
            'type': mt.group(1),
            'hash': mh.group(1),
            'id': mi.group(1),
            'info': '{}',
        },
        headers={
            **HEADERS,
            'Referer': url,
            'Origin': origin,
            'X-Requested-With': 'XMLHttpRequest',
        },
        timeout=20,
    )
    try:
      j = r.json()
    except Exception:
      continue
    if isinstance(j, dict) and j.get('links'):
      data = j
      break
  if not data:
    raise RuntimeError('Kodik не повернув посилання на відео')

  links = {}
  for q, arr in data['links'].items():
    if not arr:
      continue
    dec = _kodik_decode(arr[0].get('src', ''))
    if not dec:
      continue
    if dec.startswith('//'):
      dec = 'https:' + dec
    links[f'{q}p' if str(q).isdigit() else str(q)] = dec
  if not links:
    raise RuntimeError('не вдалося розшифрувати посилання Kodik')
  skips = skips_anywhere(html, url, {k: v for k, v in data.items() if k != 'links'})
  try:  # кадри-превʼю плеєра (на сайті вони показуються на смузі перемотування)
    mt_ = re.search(r'var\s+thumbnails\s*=\s*\[(.*?)\]', html, re.S)
    th = re.findall(r'"([^"]+thumb\d+\.jpg[^"]*)"', mt_.group(1)) if mt_ else []
    th = ['https:' + x if x.startswith('//') else x for x in th]
    th.sort(key=lambda x: int(re.search(r'thumb(\d+)\.jpg', x).group(1)))
    KODIK_THUMBS[iframe_url] = th
  except Exception:
    pass
  return links, {'Referer': origin + '/', 'Origin': origin}, skips


# ───────────────────────── Завантаження ─────────────────────────

def _join(base, ref):
  """Як urljoin, але безпечно для імен на кшталт '720.mp4:hls:seg-1.ts'."""
  if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', ref):
    return ref
  if ref.startswith('/'):
    return urljoin(base, ref)
  return base.split('?')[0].rsplit('/', 1)[0] + '/' + ref


def safe_name(s):
  s = re.sub(r'[\\/:*?"<>|\r\n]+', ' ', s or '').strip(' .')
  return re.sub(r'\s+', ' ', s) or 'Аніме'


def qnum(q):
  m = re.match(r'\s*(\d+)', str(q))
  return int(m.group(1)) if m else 0


def pick_quality(links, wanted):
  """Бажана якість, а якщо її немає — найближча нижча (або найнижча з вищих). 'best' — найкраща."""
  if not links:
    return None, None
  if wanted in links:
    return wanted, links[wanted]
  w = qnum(wanted)
  if not w:
    q = max(links, key=qnum)
    return q, links[q]
  lower = [q for q in links if qnum(q) <= w]
  q = max(lower, key=qnum) if lower else min(links, key=qnum)
  return q, links[q]


def load_playlist(m3u8_url, session, depth=0, info=None):
  """Повертає (список_сегментів, чи_зашифровано). Майстер-плейлист → найякісніший варіант."""
  _chk()
  text = None
  for attempt in range(3):
    _chk()
    try:
      r = session.get(m3u8_url, timeout=(8, 15))
      if r.status_code == 200:
        text = r.text
        break
      if r.status_code in (403, 404, 410):
        r.raise_for_status()
    except Cancelled:
      raise
    except requests.HTTPError:
      raise
    except Exception:
      pass
    time.sleep(1.5 * (attempt + 1))
  if text is None:
    text = session.get(m3u8_url, timeout=(8, 15)).text
  lines = [l.strip() for l in text.splitlines() if l.strip()]
  if depth < 3 and any(l.startswith('#EXT-X-STREAM-INF') for l in lines):
    best, best_bw, best_res = None, -1, 0
    for i, l in enumerate(lines):
      if l.startswith('#EXT-X-STREAM-INF') and i + 1 < len(lines) and not lines[i + 1].startswith('#'):
        m = re.search(r'BANDWIDTH=(\d+)', l)
        bw = int(m.group(1)) if m else 0
        if bw > best_bw:
          rm = re.search(r'RESOLUTION=\d+x(\d+)', l)
          best, best_bw, best_res = lines[i + 1], bw, (int(rm.group(1)) if rm else 0)
    if best:
      if info is not None:
        info['bw'], info['height'] = best_bw, best_res
      return load_playlist(_join(m3u8_url, best), session, depth + 1, info)
  items = [l for l in lines if not l.startswith('#')]
  if items and items[0].split('?')[0].endswith('.m3u8') and depth < 3:
    return load_playlist(_join(m3u8_url, items[0]), session, depth + 1, info)
  if info is not None:
    info['dur'] = sum(float(x) for x in re.findall(r'#EXTINF:([\d.]+)', text))
  encrypted = any(
      l.startswith('#EXT-X-KEY') and 'METHOD=NONE' not in l for l in lines
  )
  return [_join(m3u8_url, l) for l in items], encrypted



# ---- Вбудований збирач MP4 з MPEG-TS (H.264 + AAC) без ffmpeg ----


class MuxError(Exception):
  pass


def _mb(t, *parts):
  body = b''.join(parts)
  return struct.pack('>I', 8 + len(body)) + t + body


def _mfb(t, body=b'', ver=0, flags=0):
  return _mb(t, bytes([ver]) + flags.to_bytes(3, 'big') + body)


class _Bits:
  def __init__(self, data):
    self.d, self.p = data, 0

  def bit(self):
    v = (self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1
    self.p += 1
    return v

  def bits(self, n):
    v = 0
    for _ in range(n):
      v = (v << 1) | self.bit()
    return v

  def ue(self):
    z = 0
    while self.bit() == 0:
      z += 1
      if z > 32:
        raise MuxError('SPS пошкоджений')
    return (1 << z) - 1 + (self.bits(z) if z else 0)

  def se(self):
    k = self.ue()
    return (k + 1) // 2 if k & 1 else -(k // 2)


def _sps_size(sps):
  """Ширина/висота з SPS H.264."""
  rb = re.sub(b'\x00\x00\x03', b'\x00\x00', sps[1:])
  b = _Bits(rb)
  prof = b.bits(8)
  b.bits(8)
  b.bits(8)
  b.ue()
  chroma = 1
  if prof in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
    chroma = b.ue()
    if chroma == 3:
      b.bit()
    b.ue()
    b.ue()
    b.bit()
    if b.bit():
      for i in range(8 if chroma != 3 else 12):
        if b.bit():
          last = nxt = 8
          for _ in range(16 if i < 6 else 64):
            if nxt:
              nxt = (last + b.se() + 256) % 256
            last = last if nxt == 0 else nxt
  b.ue()
  poc = b.ue()
  if poc == 0:
    b.ue()
  elif poc == 1:
    b.bit()
    b.se()
    b.se()
    for _ in range(b.ue()):
      b.se()
  b.ue()
  b.bit()
  wm = b.ue()
  hm = b.ue()
  fmo = b.bit()
  if not fmo:
    b.bit()
  b.bit()
  cl = cr = ct = cb = 0
  if b.bit():
    cl, cr, ct, cb = b.ue(), b.ue(), b.ue(), b.ue()
  cx = 1 if chroma in (0, 3) else 2
  cy = (1 if chroma in (0, 2, 3) else 2) * (2 - fmo)
  return (wm + 1) * 16 - cx * (cl + cr), (2 - fmo) * (hm + 1) * 16 - cy * (ct + cb)


_AAC_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]


def _pts(b):
  return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


class _Unwrap:
  def __init__(self):
    self.off, self.last = 0, None

  def __call__(self, v):
    if self.last is not None and v + self.off < self.last - (1 << 32):
      self.off += 1 << 33
    v += self.off
    self.last = v
    return v


def _packets(files, check):
  """Пакети TS по черзі з усіх файлів (кожен файл — окремий сегмент)."""
  n = 0
  for fp in files:
    with open(fp, 'rb') as f:
      data = f.read()
    pos = 0
    L = len(data)
    while pos + 188 <= L:
      if data[pos] != 0x47:
        i = data.find(b'\x47', pos)
        while i != -1 and i + 188 < L and data[i + 188] != 0x47:
          i = data.find(b'\x47', i + 1)
        if i == -1:
          break
        pos = i
      yield data[pos:pos + 188]
      pos += 188
      n += 1
      if n % 4000 == 0 and check:
        check()
    if check:
      check()


def ts_to_mp4(files, out_path, check=None, progress=None):
  """files — список TS-файлів (сегментів) по порядку. Піддерживається H.264 + AAC(ADTS)."""
  if isinstance(files, str):
    files = [files]
  tmp_payload = out_path + '.mdat'
  vsz, vdts, vpts, vkey, vchunks = [], [], [], [], []
  asz, achunks = [], []
  sps_l, pps_l = [], []
  st = {'vpid': None, 'apid': None, 'pmt': None, 'vtype': None, 'atype': None, 'aprof': None, 'arate': None,
        'achan': None, 'apts0': None}
  vpend, apend = [], []
  buf = {}
  unv, una = _Unwrap(), _Unwrap()
  out = open(tmp_payload, 'wb')

  def flush():
    if vpend:
      off = out.tell()
      for d in vpend:
        out.write(d)
      vchunks.append((off, len(vpend)))
      vpend.clear()
    if apend:
      off = out.tell()
      for d in apend:
        out.write(d)
      achunks.append((off, len(apend)))
      apend.clear()

  def on_video(pes):
    if len(pes) < 9:
      return
    fl = pes[7] >> 6
    hl = pes[8]
    pts = dts = None
    if fl >= 2 and len(pes) >= 14:
      pts = unv(_pts(pes[9:14]))
    if fl == 3 and len(pes) >= 19:
      dts = unv(_pts(pes[14:19]))
    if dts is None:
      dts = pts
    if pts is None:
      return
    data = pes[9 + hl:]
    starts = [m.start() for m in re.finditer(b'\x00\x00\x01', data)]
    out_nals = []
    key = False
    for i, s in enumerate(starts):
      e = starts[i + 1] if i + 1 < len(starts) else len(data)
      nal = data[s + 3:e].rstrip(b'\x00')
      if not nal:
        continue
      t = nal[0] & 0x1F
      if t == 7:
        if nal not in sps_l and len(sps_l) < 8:
          sps_l.append(nal)
      elif t == 8:
        if nal not in pps_l and len(pps_l) < 8:
          pps_l.append(nal)
      elif t in (9, 12):
        continue
      else:
        if t == 5:
          key = True
        out_nals.append(struct.pack('>I', len(nal)) + nal)
    if not out_nals:
      return
    d = b''.join(out_nals)
    vpend.append(d)
    vsz.append(len(d))
    vdts.append(dts)
    vpts.append(pts)
    vkey.append(key)
    if len(vpend) >= 30:
      flush()

  def on_audio(pes):
    if len(pes) < 9:
      return
    fl = pes[7] >> 6
    hl = pes[8]
    if fl >= 2 and len(pes) >= 14 and st['apts0'] is None:
      st['apts0'] = una(_pts(pes[9:14]))
    elif fl >= 2 and len(pes) >= 14:
      una(_pts(pes[9:14]))
    d = pes[9 + hl:]
    p = 0
    while p + 7 <= len(d):
      if d[p] != 0xFF or (d[p + 1] & 0xF0) != 0xF0:
        n = d.find(b'\xff', p + 1)
        if n == -1:
          break
        p = n
        continue
      hdr = 7 if d[p + 1] & 1 else 9
      flen = ((d[p + 3] & 3) << 11) | (d[p + 4] << 3) | (d[p + 5] >> 5)
      if flen < hdr or p + flen > len(d):
        break
      if st['arate'] is None:
        st['aprof'] = ((d[p + 2] >> 6) & 3) + 1
        st['arate'] = (d[p + 2] >> 2) & 0xF
        st['achan'] = ((d[p + 2] & 1) << 2) | (d[p + 3] >> 6)
      fr = d[p + hdr:p + flen]
      apend.append(fr)
      asz.append(len(fr))
      p += flen

  def on_pes(pid):
    b = bytes(buf.pop(pid, b''))
    if not b or b[:3] != b'\x00\x00\x01':
      return
    if pid == st['vpid']:
      on_video(b)
    elif pid == st['apid']:
      on_audio(b)

  try:
    for k, pk in enumerate(_packets(files, check)):
      if pk[1] & 0x80:
        continue
      pid = ((pk[1] & 0x1F) << 8) | pk[2]
      afc = (pk[3] >> 4) & 3
      if not afc & 1:
        continue
      o = 4
      if afc & 2:
        o = 5 + pk[4]
      if o >= 188:
        continue
      pl = pk[o:]
      pusi = pk[1] & 0x40
      if pid == 0 and st['pmt'] is None:
        if pusi:
          pl = pl[1 + pl[0]:]
        if len(pl) >= 12 and pl[0] == 0:
          end = 3 + (((pl[1] & 0xF) << 8) | pl[2]) - 4
          for q in range(8, min(end, len(pl) - 3), 4):
            prog = (pl[q] << 8) | pl[q + 1]
            if prog != 0:
              st['pmt'] = ((pl[q + 2] & 0x1F) << 8) | pl[q + 3]
              break
      elif pid == st['pmt'] and st['vpid'] is None:
        if pusi:
          pl = pl[1 + pl[0]:]
        if len(pl) >= 12 and pl[0] == 2:
          sl = ((pl[1] & 0xF) << 8) | pl[2]
          pil = ((pl[10] & 0xF) << 8) | pl[11]
          q = 12 + pil
          end = min(3 + sl - 4, len(pl))
          while q + 5 <= end:
            tp = pl[q]
            ep = ((pl[q + 1] & 0x1F) << 8) | pl[q + 2]
            eil = ((pl[q + 3] & 0xF) << 8) | pl[q + 4]
            if tp == 0x1B and st['vpid'] is None:
              st['vpid'], st['vtype'] = ep, tp
            elif tp in (0x24, 0x10, 0x02) and st['vpid'] is None:
              raise MuxError('Відеокодек цього потоку (не H.264) не підтримується без ffmpeg.')
            elif tp == 0x0F and st['apid'] is None:
              st['apid'], st['atype'] = ep, tp
            elif tp in (0x03, 0x04, 0x81, 0x87) and st['apid'] is None:
              st['apid'], st['atype'] = None, 'unsupported'
            q += 5 + eil
      elif pid in (st['vpid'], st['apid']) and pid is not None:
        if pusi:
          on_pes(pid)
          buf[pid] = bytearray()
        if pid in buf:
          buf[pid] += pl
    for pid in list(buf):
      on_pes(pid)
    flush()
  finally:
    out.close()

  try:
    if not vsz:
      raise MuxError('У потоці не знайдено відео H.264.')
    if not sps_l or not pps_l:
      raise MuxError('У потоці немає параметрів відео (SPS/PPS).')
    w, h = _sps_size(sps_l[0])
    return _write_mp4(out_path, tmp_payload, (vsz, vdts, vpts, vkey, vchunks, sps_l, pps_l, w, h),
                      (asz, achunks, st) if asz and st['arate'] is not None else None)
  finally:
    try:
      os.remove(tmp_payload)
    except OSError:
      pass


def _rle(vals):
  out = []
  for v in vals:
    if out and out[-1][1] == v:
      out[-1][0] += 1
    else:
      out.append([1, v])
  return out


def _stsc(chunks):
  ent, prev = [], None
  for i, (_, n) in enumerate(chunks):
    if n != prev:
      ent.append((i + 1, n, 1))
      prev = n
  return _mfb(b'stsc', struct.pack('>I', len(ent)) + b''.join(struct.pack('>III', *e) for e in ent))


def _write_mp4(out_path, payload_path, V, A):
  vsz, vdts, vpts, vkey, vchunks, sps_l, pps_l, w, h = V
  # --- відео: DTS від нуля, монотонні
  d0 = vdts[0]
  dts = []
  prev = -1
  for d in vdts:
    d = d - d0
    if d <= prev:
      d = prev + 1
    dts.append(d)
    prev = d
  cts = [max(0, p - d0 - d) for p, d in zip(vpts, dts)]
  deltas = [dts[i + 1] - dts[i] for i in range(len(dts) - 1)]
  deltas.append(deltas[-1] if deltas else 3003)
  vdur = dts[-1] + deltas[-1]
  v_start = vpts[0]
  vts = 90000
  a_start = None
  if A:
    a_start = A[2]['apts0'] if A[2]['apts0'] is not None else v_start
  t0 = min(v_start, a_start) if a_start is not None else v_start
  MT = 1000

  def edts(lead90, seg_dur_media, media_time, ts):
    ents = []
    if lead90 > 0:
      ents.append((int(lead90 * MT / 90000), 0xFFFFFFFF, 0x00010000))
    ents.append((int(max(0, seg_dur_media - media_time) * MT / ts), media_time, 0x00010000))
    body = struct.pack('>I', len(ents)) + b''.join(struct.pack('>III', a, b, c) for a, b, c in ents)
    return _mb(b'edts', _mfb(b'elst', body)), sum(e[0] for e in ents)

  def build(vbase, abase):
    tracks = []
    # video trak
    stts = _mfb(b'stts', struct.pack('>I', len(_rle(deltas))) + b''.join(struct.pack('>II', n, d) for n, d in _rle(deltas)))
    ctts = b''
    if any(cts):
      rl = _rle(cts)
      ctts = _mfb(b'ctts', struct.pack('>I', len(rl)) + b''.join(struct.pack('>II', n, c) for n, c in rl))
    keys = [i + 1 for i, k in enumerate(vkey) if k]
    stss = _mfb(b'stss', struct.pack('>I', len(keys)) + b''.join(struct.pack('>I', k) for k in keys)) if keys and len(keys) != len(vkey) else b''
    stsz = _mfb(b'stsz', struct.pack('>II', 0, len(vsz)) + b''.join(struct.pack('>I', s) for s in vsz))
    stco = _mfb(b'stco', struct.pack('>I', len(vchunks)) + b''.join(struct.pack('>I', vbase + o) for o, _ in vchunks))
    sps0 = sps_l[0]
    avcc = _mb(b'avcC', bytes([1, sps0[1], sps0[2], sps0[3], 0xFF, 0xE0 | len(sps_l)])
               + b''.join(struct.pack('>H', len(s)) + s for s in sps_l) + bytes([len(pps_l)])
               + b''.join(struct.pack('>H', len(s)) + s for s in pps_l))
    avc1 = _mb(b'avc1', b'\x00' * 6 + struct.pack('>H', 1) + b'\x00' * 16 + struct.pack('>HH', w, h)
               + struct.pack('>II', 0x00480000, 0x00480000) + b'\x00' * 4 + struct.pack('>H', 1) + b'\x00' * 32
               + struct.pack('>Hh', 0x18, -1) + avcc)
    stsd = _mfb(b'stsd', struct.pack('>I', 1) + avc1)
    stbl = _mb(b'stbl', stsd, stts, ctts, stss, _stsc(vchunks), stsz, stco)
    minf = _mb(b'minf', _mfb(b'vmhd', b'\x00' * 8, 0, 1), _mb(b'dinf', _mfb(b'dref', struct.pack('>I', 1) + _mfb(b'url ', b'', 0, 1))), stbl)
    mdhd = _mfb(b'mdhd', struct.pack('>IIII', 0, 0, vts, vdur) + struct.pack('>HH', 0x55C4, 0))
    hdlr = _mfb(b'hdlr', b'\x00' * 4 + b'vide' + b'\x00' * 12 + b'VideoHandler\x00')
    ed, movdur = edts(v_start - t0, vdur, cts[0], vts)
    tkhd = _mfb(b'tkhd', struct.pack('>IIIII', 0, 0, 1, 0, movdur) + b'\x00' * 8 + struct.pack('>hhhh', 0, 0, 0, 0)
                + struct.pack('>9I', 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) + struct.pack('>II', w << 16, h << 16), 0, 3)
    tracks.append((_mb(b'trak', tkhd, ed, _mb(b'mdia', mdhd, hdlr, minf)), movdur))
    if A:
      asz, achunks, st = A
      rate = _AAC_RATES[st['arate']] if st['arate'] < len(_AAC_RATES) else 44100
      n = len(asz)
      adur = n * 1024
      asc = struct.pack('>H', (st['aprof'] << 11) | (st['arate'] << 7) | (st['achan'] << 3))
      dsi = b'\x05' + bytes([len(asc)]) + asc
      dcd = b'\x04' + bytes([13 + len(dsi)]) + bytes([0x40, 0x15]) + b'\x00\x00\x00' + struct.pack('>II', 0, 0) + dsi
      esd = b'\x03' + bytes([3 + len(dcd) + 3]) + struct.pack('>HB', 0, 0) + dcd + b'\x06\x01\x02'
      mp4a = _mb(b'mp4a', b'\x00' * 6 + struct.pack('>H', 1) + b'\x00' * 8 + struct.pack('>HHHH', st['achan'] or 2, 16, 0, 0)
                 + struct.pack('>I', min(rate, 65535) << 16) + _mfb(b'esds', esd))
      stsd_a = _mfb(b'stsd', struct.pack('>I', 1) + mp4a)
      stts_a = _mfb(b'stts', struct.pack('>III', 1, n, 1024))
      stsz_a = _mfb(b'stsz', struct.pack('>II', 0, n) + b''.join(struct.pack('>I', s) for s in asz))
      stco_a = _mfb(b'stco', struct.pack('>I', len(achunks)) + b''.join(struct.pack('>I', abase + o) for o, _ in achunks))
      stbl_a = _mb(b'stbl', stsd_a, stts_a, _stsc(achunks), stsz_a, stco_a)
      minf_a = _mb(b'minf', _mfb(b'smhd', b'\x00' * 4), _mb(b'dinf', _mfb(b'dref', struct.pack('>I', 1) + _mfb(b'url ', b'', 0, 1))), stbl_a)
      mdhd_a = _mfb(b'mdhd', struct.pack('>IIII', 0, 0, rate, adur) + struct.pack('>HH', 0x55C4, 0))
      hdlr_a = _mfb(b'hdlr', b'\x00' * 4 + b'soun' + b'\x00' * 12 + b'SoundHandler\x00')
      ed_a, movdur_a = edts(a_start - t0, adur, 0, rate)
      tkhd_a = _mfb(b'tkhd', struct.pack('>IIIII', 0, 0, 2, 0, movdur_a) + b'\x00' * 8 + struct.pack('>hhhh', 0, 0, 0x0100, 0)
                    + struct.pack('>9I', 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) + struct.pack('>II', 0, 0), 0, 3)
      tracks.append((_mb(b'trak', tkhd_a, ed_a, _mb(b'mdia', mdhd_a, hdlr_a, minf_a)), movdur_a))
    dur = max(t[1] for t in tracks)
    mvhd = _mfb(b'mvhd', struct.pack('>IIII', 0, 0, MT, dur) + struct.pack('>IH', 0x10000, 0x0100) + b'\x00' * 10
                + struct.pack('>9I', 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) + b'\x00' * 24 + struct.pack('>I', len(tracks) + 1))
    return _mb(b'moov', mvhd, *[t[0] for t in tracks])

  ftyp = _mb(b'ftyp', b'isom' + struct.pack('>I', 512) + b'isomiso2avc1mp41')
  psize = os.path.getsize(payload_path)
  if psize > 0xFFFF0000:
    raise MuxError('Файл завеликий (понад 4 ГБ).')
  moov = build(0, 0)
  base = len(ftyp) + len(moov) + 8
  moov = build(base, base)
  tmp = out_path + '.mux'
  with open(tmp, 'wb') as f:
    f.write(ftyp)
    f.write(moov)
    f.write(struct.pack('>I', psize + 8) + b'mdat')
    with open(payload_path, 'rb') as p:
      shutil.copyfileobj(p, f, 1 << 20)
  os.replace(tmp, out_path)
  return {'width': w, 'height': h}


def mp4_height(path):
  """Висота відео з MP4 (tkhd відеодоріжки) без ffprobe."""
  try:
    with open(path, 'rb') as f:
      size = os.path.getsize(path)
      pos = 0
      while pos + 8 <= size:
        f.seek(pos)
        hd = f.read(16)
        sz, t = struct.unpack('>I4s', hd[:8])
        hl = 8
        if sz == 1:
          sz = struct.unpack('>Q', hd[8:16])[0]
          hl = 16
        elif sz == 0:
          sz = size - pos
        if t == b'moov':
          f.seek(pos + hl)
          m = f.read(min(sz - hl, 8 << 20))
          best = 0
          for mm in re.finditer(b'tkhd', m):
            i = mm.start() + 4
            ver = m[i]
            o = i + (88 if ver == 1 else 76)
            if o + 8 <= len(m):
              ww, hh = struct.unpack('>II', m[o:o + 8])
              if ww and hh:
                best = max(best, hh >> 16)
          return best
        if sz < 8:
          break
        pos += sz
  except Exception:
    pass
  return 0


def _have_ffmpeg():
  return bool(shutil.which('ffmpeg'))


def _ffmpeg_remux(src, out_path, cancel, headers=None):
  cmd = ['ffmpeg', '-y', '-nostdin', '-loglevel', 'error']
  if headers:
    cmd += ['-headers', ''.join(f'{k}: {v}\r\n' for k, v in headers.items())]
  code, tail = run_cancellable(cmd + ['-i', src, '-c', 'copy', '-movflags', '+faststart', out_path], cancel)
  if code != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
    raise RuntimeError('ffmpeg не зміг зібрати відео' + (': ' + tail if tail else '.'))


def download_episode(m3u8_url, out_path, log, headers=None, progress=None, cancel=None, stats=None):
  """Качає HLS-серію в MP4. progress(done, total, phase), cancel — threading.Event.
  Завантажені сегменти лежать у out_path+'.parts' — після збою наступний запуск продовжує."""
  blk = threading.Lock()

  def report(done, total, phase='download'):
    if stats is not None and phase == 'download' and total > 1 and done >= 3 and stats.get('bytes'):
      stats['total'] = stats['bytes'] / done * total
    if progress:
      progress(done, total, phase)

  session = requests.Session()
  session.headers.update(HEADERS)
  if headers:
    session.headers.update(headers)
  report(0, 1, 'playlist')
  pinfo = {}
  segments, encrypted = load_playlist(m3u8_url, session, 0, pinfo)
  if stats is not None and pinfo.get('bw') and pinfo.get('dur'):
    stats['total'] = pinfo['bw'] * pinfo['dur'] / 8
  if not segments:
    raise RuntimeError('Порожній плейлист.')

  if encrypted and not _have_ffmpeg():
    raise ValueError('Цей потік зашифрований — без ffmpeg (на iPhone-версії) такі серії поки не підтримуються.')
  if encrypted:
    log('Потік зашифрований — завантажую через ffmpeg напряму')
    report(0, 1, 'convert')
    _ffmpeg_remux(m3u8_url, out_path, cancel, headers)
    return True

  total = len(segments)
  tmp_dir = out_path + '.parts'
  os.makedirs(tmp_dir, exist_ok=True)

  def part(idx, url):
    p = os.path.join(tmp_dir, f'{idx:05d}.ts')
    if cancel is not None and cancel.is_set():
      return False
    if os.path.exists(p) and os.path.getsize(p) > 0:
      return True
    for attempt in range(4):
      if cancel is not None and cancel.is_set():
        return False
      try:
        r = session.get(url, timeout=(6, 25))
        if r.status_code == 200 and r.content:
          with open(p + '.tmp', 'wb') as f:
            f.write(r.content)
          os.replace(p + '.tmp', p)
          if stats is not None:
            with blk:
              stats['bytes'] = stats.get('bytes', 0) + len(r.content)
          return True
        if r.status_code in (429, 503):  # сервер просить зменшити темп
          time.sleep(2 * (attempt + 1))
          continue
        if r.status_code in (403, 404, 410):  # назавжди недоступно — не мучимо повторами
          return None
      except Exception:
        pass
      time.sleep(0.5 * (attempt + 1))
    return False

  # Кілька проходів: кожен наступний — з меншою кількістю потоків
  pending = [i for i in range(total)
             if not (os.path.exists(os.path.join(tmp_dir, f'{i:05d}.ts')) and os.path.getsize(os.path.join(tmp_dir, f'{i:05d}.ts')) > 0)]
  if stats is not None:
    stats['bytes'] = sum(os.path.getsize(os.path.join(tmp_dir, f'{i:05d}.ts')) for i in range(total)
                         if i not in pending and os.path.exists(os.path.join(tmp_dir, f'{i:05d}.ts')))
  report(total - len(pending), total)
  dead = []
  mw = _lim('seg')
  for workers in (mw, mw // 2, 4, 2):
    if not pending:
      break
    ok_count = total - len(pending) - len(dead)
    still = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
      futs = {ex.submit(part, i, segments[i]): i for i in pending}
      for f in concurrent.futures.as_completed(futs):
        res = f.result()
        if res:
          ok_count += 1
        elif res is None:
          dead.append(futs[f])
        else:
          still.append(futs[f])
        report(ok_count, total)
    if cancel is not None and cancel.is_set():
      raise Cancelled()
    pending = still
    if pending:
      log(f'Не вдалося {len(pending)} сегм. — повторюю повільніше')
      for _ in range(6):  # пауза, яку можна перервати
        _chk_event(cancel)
        time.sleep(0.5)

  if pending or dead:
    raise RuntimeError(f'Не вдалося завантажити сегментів: {len(pending) + len(dead)} з {total}. Запустіть ще раз — завантажене збережено.')

  if not _have_ffmpeg():  # iPhone-збірка: збираємо MP4 вбудованим збирачем, без ffmpeg
    report(0, total, 'convert')
    try:
      ts_to_mp4([os.path.join(tmp_dir, f'{i:05d}.ts') for i in range(total)], out_path, check=lambda: _chk_event(cancel))
    except MuxError as ex:
      raise ValueError(str(ex))
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return True

  report(0, total, 'merge')
  ts_path = out_path + '.ts'
  with open(ts_path, 'wb') as out:
    for i in range(total):
      _chk_event(cancel)
      with open(os.path.join(tmp_dir, f'{i:05d}.ts'), 'rb') as inp:
        shutil.copyfileobj(inp, out, 1 << 20)
      if i % 20 == 0:
        report(i, total, 'merge')
  report(total, total, 'convert')
  try:
    _ffmpeg_remux(ts_path, out_path, cancel)
  finally:
    if os.path.exists(ts_path):
      try:
        os.remove(ts_path)
      except OSError:
        pass
  shutil.rmtree(tmp_dir, ignore_errors=True)
  return True


def _chk_event(cancel):
  if cancel is not None and cancel.is_set():
    raise Cancelled()
  if PAUSE['on'] or _kpaused(cancel):
    _pause_wait(cancel)


def _probe_range(url, headers):
  """Чи вміє сервер віддавати частинами; повертає загальний розмір або None."""
  try:
    r = SESS.get(url, headers={**headers, 'Range': 'bytes=0-0'}, timeout=(6, 15), stream=True)
    try:
      if r.status_code == 206:
        cr = r.headers.get('Content-Range', '')
        if '/' in cr and cr.rsplit('/', 1)[1].isdigit():
          return int(cr.rsplit('/', 1)[1])
      elif r.status_code == 200 and r.headers.get('Accept-Ranges', '').lower() == 'bytes' and r.headers.get('Content-Length', '').isdigit():
        return int(r.headers['Content-Length'])
    finally:
      r.close()
  except Exception:
    pass
  return None


def _download_single(url, dl_path, h, progress, cancel):
  """Один потік із докачуванням (запасний варіант)."""
  have = os.path.getsize(dl_path) if os.path.exists(dl_path) else 0
  for attempt in range(6):
    _chk_event(cancel)
    hh = dict(h)
    if have:
      hh['Range'] = f'bytes={have}-'
    try:
      r = SESS.get(url, headers=hh, stream=True, timeout=(6, 20))
    except Exception:
      time.sleep(1 + attempt)
      continue
    if r.status_code == 416:
      return True
    if r.status_code not in (200, 206):
      r.close()
      if r.status_code in (429, 503):
        time.sleep(2 * (attempt + 1))
        continue
      raise RuntimeError(f'Сервер відповів {r.status_code}')
    if r.status_code == 200 and have:
      have = 0
    total = None
    cr = r.headers.get('Content-Range')
    if cr and '/' in cr and cr.rsplit('/', 1)[1].isdigit():
      total = int(cr.rsplit('/', 1)[1])
    elif r.headers.get('Content-Length', '').isdigit():
      total = have + int(r.headers['Content-Length'])
    try:
      with open(dl_path, 'ab' if have else 'wb') as f:
        for chunk in r.iter_content(1 << 20):
          _chk_event(cancel)
          f.write(chunk)
          have += len(chunk)
          if progress:
            progress(have, total or have + 1, 'download')
    except Cancelled:
      raise
    except Exception:
      time.sleep(1 + attempt)
      continue
    finally:
      r.close()
    if total is None or have >= total:
      if progress:
        progress(have, have or 1, 'download')
      return True
  raise RuntimeError('Не вдалося докачати файл. Запустіть ще раз — завантажене збережено.')


CHUNK = 3 * 1024 * 1024


def download_direct(url, dl_path, headers=None, progress=None, cancel=None, workers=8):
  """Пряме завантаження mp4: кілька паралельних з'єднань (кожне — свій шматок файлу), докачування після збою.
  Прогрес збережених шматків лежить у dl_path+'.map'."""
  h = {**HEADERS, **(headers or {})}
  total = _probe_range(url, h)
  if not total or total < 2 * CHUNK:
    return _download_single(url, dl_path, h, progress, cancel)

  map_path = dl_path + '.map'
  n = (total + CHUNK - 1) // CHUNK
  done = set()
  try:
    if os.path.exists(map_path) and os.path.exists(dl_path):
      m = json.load(open(map_path))
      if m.get('total') == total and m.get('chunk') == CHUNK:
        done = set(m.get('done', []))
  except Exception:
    done = set()
  if not os.path.exists(dl_path) or os.path.getsize(dl_path) != total:
    with open(dl_path, 'wb') as f:
      f.truncate(total)
    done = set()
  lock = threading.Lock()
  state = {'bytes': sum(min(CHUNK, total - i * CHUNK) for i in done), 'saved': 0}

  def save_map():
    tmp = map_path + '.tmp'
    with open(tmp, 'w') as f:
      json.dump({'total': total, 'chunk': CHUNK, 'done': sorted(done)}, f)
    os.replace(tmp, map_path)

  def fetch(idx):
    start = idx * CHUNK
    end = min(total, start + CHUNK) - 1
    for attempt in range(5):
      _chk_event(cancel)
      got = 0
      try:
        r = SESS.get(url, headers={**h, 'Range': f'bytes={start}-{end}'}, stream=True, timeout=(6, 20))
        if r.status_code != 206 and not (r.status_code == 200 and start == 0 and end == total - 1):
          code = r.status_code
          r.close()
          if code in (429, 503):
            time.sleep(2 * (attempt + 1))
            continue
          if code in (403, 404, 410):
            raise RuntimeError(f'Сервер відповів {code}')
          time.sleep(1 + attempt)
          continue
        with open(dl_path, 'r+b') as f:
          f.seek(start)
          for chunk in r.iter_content(256 * 1024):
            _chk_event(cancel)
            f.write(chunk)
            got += len(chunk)
            with lock:
              state['bytes'] += len(chunk)
              if progress:
                progress(min(state['bytes'], total), total, 'download')
        r.close()
        if got == end - start + 1:
          with lock:
            done.add(idx)
            if len(done) - state['saved'] >= 3:
              state['saved'] = len(done)
              save_map()
          return True
        with lock:
          state['bytes'] -= got  # неповний шматок — починаємо його заново
      except Cancelled:
        raise
      except RuntimeError:
        raise
      except Exception:
        with lock:
          state['bytes'] -= got
        time.sleep(1 + attempt)
    return False

  pending = [i for i in range(n) if i not in done]
  if progress:
    progress(state['bytes'], total, 'download')
  for w in (workers, max(2, workers // 3), 1):
    if not pending:
      break
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=w) as ex:
      futs = {ex.submit(fetch, i): i for i in pending}
      try:
        for f in concurrent.futures.as_completed(futs):
          if not f.result():
            failed.append(futs[f])
      except BaseException:
        for f in futs:
          f.cancel()
        raise
      finally:
        with lock:
          save_map()
    pending = failed
    _chk_event(cancel)
  if pending:
    raise RuntimeError(f'Не вдалося докачати {len(pending)} з {n} частин. Запустіть ще раз — завантажене збережено.')
  try:
    os.remove(map_path)
  except OSError:
    pass
  if progress:
    progress(total, total, 'download')
  return True


# ───────────────────────── Інформація про сайт ─────────────────────────

SITES = [
    ('yummyani', 'YummyAnime', ('yummyani', 'yummy-anime')),
    ('aniliberty', 'AniLiberty', ('aniliberty', 'anilibria')),
    ('animru', 'Anim-RU', ('anim-ru',)),
    ('animevost', 'AnimeVost', ('vost.pw', 'animevost', 'agorov')),
]


def site_info(url):
  host = (urlparse(url or '').netloc or '').lower()
  for sid, name, keys in SITES:
    if any(k in host for k in keys):
      return {'id': sid, 'name': name}
  return {'id': 'other', 'name': host.split(':')[0].replace('www.', '') or 'Сайт'}


def _is_vost(url):
  return site_info(url)['id'] == 'animevost'


# ───────────────────────── Плеєри ─────────────────────────

def q_label(h):
  h = int(h or 0)
  if h >= 1000:
    return '1080p'
  if h >= 680:
    return '720p'
  if h >= 460:
    return '480p'
  if h >= 300:
    return '360p'
  return f'{h}p' if h else '720p'


def hls_variants(url, hdrs):
  """Майстер-плейлист → {якість: посилання} за реальними RESOLUTION; порожньо, якщо це не майстер."""
  try:
    text = http_get(url, headers=hdrs, timeout=10, retries=0).text
  except Cancelled:
    raise
  except Exception:
    return {}
  lines = [l.strip() for l in text.splitlines() if l.strip()]
  out, bws = {}, {}
  for i, l in enumerate(lines):
    if l.startswith('#EXT-X-STREAM-INF') and i + 1 < len(lines) and not lines[i + 1].startswith('#'):
      rm = re.search(r'RESOLUTION=\d+x(\d+)', l)
      if not rm:
        continue
      bm = re.search(r'BANDWIDTH=(\d+)', l)
      bw = int(bm.group(1)) if bm else 0
      q = q_label(rm.group(1))
      if bw >= bws.get(q, -1):
        bws[q], out[q] = bw, urljoin(url, lines[i + 1])
  return out


def _valid_media(url, hdrs):
  """Швидка перевірка, що за посиланням справжнє відео (а не реклама чи заглушка)."""
  try:
    if '.m3u8' in url.split('?')[0].lower():
      t = http_get(url, headers=hdrs, timeout=8, retries=0).text
      return t.lstrip().startswith('#EXTM3U')
    r = SESS.get(url, headers={**HEADERS, **(hdrs or {}), 'Range': 'bytes=0-0'}, timeout=(6, 8), stream=True)
    try:
      if r.status_code not in (200, 206):
        return False
      cr = r.headers.get('Content-Range', '')
      size = int(cr.rsplit('/', 1)[1]) if '/' in cr and cr.rsplit('/', 1)[1].isdigit() else (
          int(r.headers['Content-Length']) if r.headers.get('Content-Length', '').isdigit() and r.status_code == 200 else None)
      ctype = r.headers.get('Content-Type', '').lower()
      if size is not None:
        return size >= 3 * 1024 * 1024
      return ctype.startswith('video') or 'octet-stream' in ctype
    finally:
      r.close()
  except Cancelled:
    raise
  except Exception:
    return False


def label_quality(label):
  """«FHD (1080p)», «HD (720p)», «SD (480p)», «1080», «Full HD» → '1080p' (або None)."""
  l = str(label or '')
  m = re.search(r'(\d{3,4})\s*p?(?!\d)', l)
  if m and int(m.group(1)) >= 140:
    return m.group(1) + 'p'
  ll = l.lower()
  if re.search(r'fhd|full\s*hd|1080', ll):
    return '1080p'
  if re.search(r'(^|[^a-z])hd([^a-z]|$)', ll):
    return '720p'
  if re.search(r'(^|[^a-z])sd([^a-z]|$)', ll):
    return '480p'
  return None


def extract_media_links(html):
  """Шукає в тексті плеєра прямі m3u8/mp4 і визначає якість за мітками плеєра, а не вгадує зі стандартного списку."""
  html = html.replace('\\/', '/')
  links = {}
  ext = r'(?:m3u8|mp4)'
  url_re = r'(https?://[^\s,"\'\[\]<>]*\.' + ext + r'[^\s,"\'\[\]<>]*)'
  for m in re.finditer(r'\[([^\[\]\n]{1,40})\]\s*' + url_re, html):  # Playerjs: [FHD (1080p)]https://…mp4,[HD (720p)]…
    q = label_quality(m.group(1))
    if q:
      links.setdefault(q, m.group(2))
  for m in re.finditer(r'\{[^{}]*\}', html):  # {"file":"...mp4","label":"1080p"}
    blk = m.group(0)
    f = re.search(r'["\'](?:file|src|url|link)["\']\s*:\s*["\']' + url_re + r'["\']', blk)
    q = re.search(r'["\'](?:label|quality|res|height|size)["\']\s*:\s*["\']?([^"\',}]{1,30})', blk)
    if f and q:
      lq = label_quality(q.group(1))
      if lq:
        links.setdefault(lq, f.group(1))
  if not links:
    for m in re.finditer(r'https?://[^\s"\'<>\\]+\.' + ext + r'[^\s"\'<>\\]*', html):
      link = m.group(0)
      q = re.search(r'[/_.=-](1080|720|480|360)p?(?=[/_.&?-]|$)', link)
      links.setdefault((q.group(1) + 'p') if q else '?', link)
  return links


def generic_links(iframe_url, referer):
  """Невідомий плеєр: шукаємо в його сторінці прямі m3u8/mp4; якість — з міток, URL або з майстер-плейлиста."""
  url = 'https:' + iframe_url if iframe_url.startswith('//') else iframe_url
  u = urlparse(url)
  origin = f'{u.scheme}://{u.netloc}'
  hdrs = {'Referer': origin + '/', 'Origin': origin}
  html = http_get(url, headers={'Referer': referer}, timeout=10, retries=1).text
  found = extract_media_links(html)
  links = {}
  for q, link in found.items():
    if q != '?':
      links[q] = link
      continue
    vs = hls_variants(link, hdrs) if '.m3u8' in link.split('?')[0] else {}
    if vs:
      for vq, vu in vs.items():
        links.setdefault(vq, vu)
    else:
      links.setdefault('720p', link)
  if not links:
    raise RuntimeError('у плеєрі не знайдено прямих посилань')
  return links, hdrs, skips_anywhere(html, url)


def resolve_players(players, referer, base_skips=None):
  """players: [(назва, iframe)]. Опитує всі плеєри паралельно й бере найкращу якість із усіх.
  Повертає {'links': {q: url}, 'hdrs': {q: заголовки}, 'players': {q: назва}}."""
  _stage('Отримую посилання на відео…')

  def one(pl):
    name, iframe = pl
    try:
      if 'kodik' in name.lower() or 'kodik' in iframe.lower():
        links, h, sk = kodik_links(iframe, referer)
        th = KODIK_THUMBS.pop(iframe, None)
      else:
        links, h, sk = generic_links(iframe, referer)
        th = None
      return name, links, (h, sk, th)
    except Cancelled:
      raise
    except Exception as ex:
      return name, None, str(ex)

  # Чекаємо плеєри не довше за ліміт: після першої успішної відповіді — ще кілька секунд решті, повільні відкидаємо.
  ex = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(players)))
  futs = {ex.submit(one, pl): i for i, pl in enumerate(players)}
  got, pending, t0, first_ok = {}, set(futs), time.time(), None
  while pending:
    _chk()
    left = 22 - (time.time() - t0)
    if first_ok:
      left = min(left, first_ok + 5 - time.time())
    if left <= 0:
      break
    dn, pending = concurrent.futures.wait(pending, timeout=min(left, 1.0), return_when=concurrent.futures.FIRST_COMPLETED)
    for f in dn:
      got[futs[f]] = f.result()
      if got[futs[f]][1] and first_ok is None:
        first_ok = time.time()
  ex.shutdown(wait=False)
  results = [got.get(i) or (players[i][0], None, 'не відповів вчасно') for i in range(len(players))]
  out = {'links': {}, 'hdrs': {}, 'players': {}, 'skips': dict(base_skips or {}), 'pinfo': ''}
  errs, info = [], []
  for name, links, hs in results:
    if not links:
      errs.append(f'{name}: {hs}')
      info.append(f'{name}: {str(hs)[:60]}')
      continue
    info.append(f'{name}: ' + ','.join(q.replace("p", "") for q in sorted(links, key=qnum)))
    h, sk, th = hs
    if th and not out.get('thumbs'):
      out['thumbs'] = th
    for k_, v_ in (sk or {}).items():
      out['skips'].setdefault(k_, v_)
    for q, u in links.items():
      if q not in out['links']:
        out['links'][q], out['hdrs'][q], out['players'][q] = u, h, name
  out['pinfo'] = ' | '.join(info)
  if not out['links']:
    raise RuntimeError('Не вдалося отримати відео. ' + '; '.join(errs)[:300])
  return out


def _hms(t):
  parts = [int(x) for x in t.split(':') if x.isdigit()]
  r = 0
  for x in parts:
    r = r * 60 + x
  return r


def parse_skip_button(s):
  """Формат Kodik: «[Опенинг]1:30-2:50,[Эндинг]22:05-23:30» → {'opening': [90, 170], 'ending': [...]}."""
  out, unknown = {}, []
  for part in (s or '').split(','):
    m = re.match(r'^\s*(?:\[(.+?)\])?\s*([\d:]+)-([\d:]+)\s*$', part)
    if not m:
      continue
    a, b = _hms(m.group(2)), _hms(m.group(3))
    if b <= a:
      continue
    label = (m.group(1) or '').lower()
    if re.search(r'end|энд|конц|титр|outro|финал|credit', label):
      out.setdefault('ending', [float(a), float(b)])
    elif re.search(r'open|опен|заставк|intro|начал', label):
      out.setdefault('opening', [float(a), float(b)])
    else:
      unknown.append((a, b))
  for a, b in unknown:  # без підписів: рання частина — інтро, пізня — аутро
    if 'opening' not in out and a < 600:
      out['opening'] = [float(a), float(b)]
    elif 'ending' not in out:
      out['ending'] = [float(a), float(b)]
  return out


def skips_from_obj(o, depth=0):
  """Шукає в довільному JSON-об'єкті таймінги інтро/аутро: {opening|intro: {start,end}}, [s, e] тощо."""
  out = {}
  if depth > 4 or not isinstance(o, (dict, list)):
    return out
  if isinstance(o, list):
    for x in o[:10]:
      if isinstance(x, dict):  # [{"type": "opening", "start": 1, "end": 2}]
        lab = next((str(x[t]).lower() for t in ('type', 'kind', 'name', 'label', 'title') if isinstance(x.get(t), str)), '')
        kind = 'opening' if re.search(r'open|intro|опен|заставк|^op$', lab) else ('ending' if re.search(r'end|outro|энд|эндинг|титр|^ed$', lab) else None)
        st = next((x[t] for t in ('start', 'startAt', 'start_at', 'from', 'begin') if isinstance(x.get(t), (int, float))), None)
        en = next((x[t] for t in ('end', 'endAt', 'end_at', 'stop', 'to') if isinstance(x.get(t), (int, float))), None)
        if kind and st is not None and en is not None and en > st:
          out.setdefault(kind, [float(st), float(en)])
          continue
      for k, v in skips_from_obj(x, depth + 1).items():
        out.setdefault(k, v)
    return out
  for k, v in o.items():
    kl = str(k).lower()
    if 'skip' in kl and isinstance(v, str):
      for k2, v2 in parse_skip_button(v).items():
        out.setdefault(k2, v2)
    kind = 'opening' if re.search(r'(^|_)(opening|intro|op)($|_)', kl) else ('ending' if re.search(r'(^|_)(ending|outro|ed)($|_)', kl) else None)
    if kind and kind not in out:
      st = en = None
      if isinstance(v, dict):
        st = next((v[x] for x in ('start', 'startAt', 'start_at', 'from', 'begin') if isinstance(v.get(x), (int, float))), None)
        en = next((v[x] for x in ('end', 'endAt', 'end_at', 'stop', 'to') if isinstance(v.get(x), (int, float))), None)
        if st is None and isinstance(v.get('time'), (int, float)) and isinstance(v.get('length'), (int, float)):  # YummyAnime
          st, en = v['time'], v['time'] + v['length']
      elif isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v):
        st, en = v
      if st is not None and en is not None and en > st:
        out[kind] = [float(st), float(en)]
        continue
    if isinstance(v, (dict, list)):
      for k2, v2 in skips_from_obj(v, depth + 1).items():
        out.setdefault(k2, v2)
  return out


_SK_PAIR = r'\d{1,2}:\d{2}(?::\d{2})?'


def skips_anywhere(text='', url='', data=None):
  """Шукає таймінги інтро/аутро будь-де: в параметрах посилання, у тексті плеєра (skip_button..., «[Опенинг]1:30-2:50»), у JSON."""
  out = {}

  def merge(d):
    for k, v in (d or {}).items():
      out.setdefault(k, v)
  try:
    for k, vals in parse_qs(urlparse(htmllib.unescape(url or '')).query).items():
      if 'skip' in k.lower():
        for v in vals:
          merge(parse_skip_button(v))
  except Exception:
    pass
  text = (text or '').replace('\\/', '/').replace('&quot;', '"')
  for m in re.finditer(r'skip[\w-]*["\']?\s*[:=]\s*["\']([^"\'\n]{3,300})["\']', text, re.I):
    merge(parse_skip_button(m.group(1)))
  for m in re.finditer(r'(?:\[[^\]\n]{2,30}\]\s*' + _SK_PAIR + r'\s*-\s*' + _SK_PAIR + r'\s*,?\s*)+', text):
    merge(parse_skip_button(m.group(0)))
  for m in re.finditer(r'skip[\w-]*["\']?\s*[:=]\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}|\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\])', text, re.I):
    try:
      merge(skips_from_obj(json.loads(m.group(1))))
    except Exception:
      pass
  if data is not None:
    merge(skips_from_obj(data))
  return out


def ep_choice(ep, wanted):
  """(якість, url, заголовки, плеєр) для серії."""
  q, u = pick_quality(ep.get('links') or {}, wanted)
  if not u:
    return None, None, {}, None
  h = (ep.get('hdrs') or {}).get(q) or ep.get('headers') or {}
  return q, u, h, (ep.get('players') or {}).get(q)


# ───────────────────────── YummyAnime ─────────────────────────

_YHOST_OK = {}


def _yummy_api(host, path, params=None, retries=1):
  r = http_get(
      f'https://{host}/api{path}',
      params=params,
      headers={**HEADERS, 'X-Application': YUMMY_APP_TOKEN, 'Lang': 'ru', 'Accept': 'application/json'},
      timeout=10,
      retries=retries,
  )
  r.raise_for_status()
  j = r.json()
  return j.get('response', j) if isinstance(j, dict) else j


_YGOOD = [None, 0.0]   # (останнє робоче дзеркало, коли)
_YBAD = {}             # дзеркало -> коли востаннє не відповіло


def _yummy_call(host, path, params=None):
  """Відомо робоче дзеркало — пробуємо його (короткий таймаут). Інакше опитуємо всі дзеркала ОДНОЧАСНО,
  перший успішний виграє; ті, що нещодавно мовчали, відсуваємо. Запит лишається скасовуваним."""
  now = time.time()
  err = None
  if _YGOOD[0] and now - _YGOOD[1] < 900:
    g = _YGOOD[0]
    try:
      r = _yummy_api(g, path, params, retries=0)
      _YGOOD[1] = time.time()
      return g, r
    except Cancelled:
      raise
    except Exception as ex:
      err = ex
      _YBAD[g] = time.time()
      _YGOOD[0] = None
  hosts = list(dict.fromkeys([host, 'ru.yummyani.me', 'yummyani.me', 'old.yummyani.me']))
  fresh = [h for h in hosts if now - _YBAD.get(h, 0) > 90]
  cand = fresh or hosts
  _stage('Підключаюсь до YummyAnime (пробую кілька дзеркал)…' if err else 'Підключаюсь до YummyAnime…')
  ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(cand))
  futs = {ex.submit(_yummy_api, h, path, params, 0): h for h in cand}
  pending = set(futs)
  t_end = time.time() + 22
  try:
    while pending and time.time() < t_end:
      _chk()
      done, pending = concurrent.futures.wait(pending, timeout=0.3, return_when=concurrent.futures.FIRST_COMPLETED)
      for f in done:
        try:
          res = f.result()
        except Exception as e2:
          err = e2
          _YBAD[futs[f]] = time.time()
          continue
        _YGOOD[0], _YGOOD[1] = futs[f], time.time()
        _YBAD.pop(futs[f], None)
        return futs[f], res
  finally:
    ex.shutdown(wait=False)
  raise err or RuntimeError('YummyAnime не відповідає')


def _find_next(obj, depth=0):
  """Шукає в відповіді API дату виходу наступної серії (ключі на кшталт next_date / next_episode_date)."""
  now = time.time()
  if depth > 4:
    return None
  if isinstance(obj, dict):
    for k, v in obj.items():
      kl = str(k).lower()
      if 'next' in kl and (any(x in kl for x in ('date', 'air', 'time', 'release', 'episode', 'at')) or kl == 'next'):
        ts = _to_ts(v) if not isinstance(v, dict) else None
        if ts and ts > now - 86400:
          return ts
        if isinstance(v, dict):
          r = _find_next(v, depth + 1)
          if r:
            return r
    for v in obj.values():
      if isinstance(v, (dict, list)):
        r = _find_next(v, depth + 1)
        if r:
          return r
  elif isinstance(obj, list):
    for v in obj[:20]:
      r = _find_next(v, depth + 1)
      if r:
        return r
  return None


def _yummy_next(a):
  ts = _find_next(a)
  if not ts:
    return None
  aired = None
  eps = a.get('episodes') if isinstance(a, dict) else None
  if isinstance(eps, dict):
    aired = eps.get('aired') or eps.get('count_aired')
  return {'ts': ts, 'ep': _num(float(aired) + 1) if isinstance(aired, (int, float)) else None, 'approx': False}


def _yummy_meta(a):
  """Обкладинка, опис, рік, жанри — прямо з відповіді API (без окремого завантаження сторінки)."""
  meta = {'poster': None, 'description': None, 'year': None, 'original': None, 'genres': []}
  if not isinstance(a, dict):
    return meta
  ps = a.get('poster')
  if isinstance(ps, dict):
    for k in ('fullsize', 'huge', 'big', 'medium', 'mega', 'small'):
      if ps.get(k):
        ps = ps[k]
        break
    else:
      ps = None
  if isinstance(ps, str) and ps:
    meta['poster'] = 'https:' + ps if ps.startswith('//') else _abs('https://yummyani.me/', ps)
  d = a.get('description')
  meta['description'] = _strip_tags(d) if isinstance(d, str) and d.strip() else None
  meta['year'] = a.get('year')
  ot = a.get('other_titles')
  if isinstance(ot, list) and ot:
    meta['original'] = str(ot[0])
  g = a.get('genres')
  if isinstance(g, list):
    meta['genres'] = [x.get('title') if isinstance(x, dict) else str(x) for x in g if x][:8]
  return meta


def _fetch_yummy(url):
  u = urlparse(url)
  host = u.netloc
  slug = unquote(u.path.split('/catalog/item/', 1)[1].strip('/').split('/')[0])
  referer = f'https://{host}/'

  _stage('Підключаюсь до YummyAnime…')
  anime_id = name = None
  meta = {}
  a = None
  try:
    h2, a = _yummy_call(host, '/anime/' + quote(slug, safe=''), {'need_videos': 'false'})
    host = h2
    anime_id, name = a.get('anime_id'), a.get('title')
    meta = _yummy_meta(a)
    meta['next'] = _yummy_next(a)
  except Cancelled:
    raise
  except Exception:
    pass
  html = None
  if not anime_id:  # запасний варіант — дані зі сторінки
    _stage('Читаю сторінку аніме…')
    html = http_get(url).text
    m = re.search(r'<main[^>]*>\s*<div data-id="(\d+)"', html)
    anime_id = m.group(1) if m else None
    mn = re.search(r'"@type":\s*"TVSeries",\s*"name":\s*"([^"]+)"', html)
    name = htmllib.unescape(mn.group(1)) if mn else _page_title(html)
    meta = _html_meta(html, url)
  if not anime_id:
    raise RuntimeError('не вдалося визначити ID аніме на YummyAnime')

  _stage('Отримую озвучки та серії…')
  _, videos = _yummy_call(host, f'/anime/{anime_id}/videos')
  if isinstance(videos, dict):
    videos = videos.get('items') or videos.get('videos') or []

  groups, order, players_seen, skip_map = {}, [], set(), {}
  for v in videos or []:
    d = v.get('data') or {}
    player = str(d.get('player') or '')
    players_seen.add(player)
    iframe = v.get('iframe_url')
    if not iframe:
      continue
    try:
      num = _num(float(str(v.get('number')).replace(',', '.')))
    except Exception:
      continue
    dub = str(d.get('dubbing') or 'Без назви')
    k = (dub, num)
    if k not in groups:
      groups[k] = []
      order.append(k)
    groups[k].append((player or 'Плеєр', iframe))
    sk = {**skips_anywhere('', iframe, v)}
    if sk:
      skip_map.setdefault(k, {}).update(sk)

  eps = []
  for (dub, num) in order:
    pls = groups[(dub, num)]
    pls.sort(key=lambda x: 0 if 'kodik' in x[0].lower() else 1)  # Kodik надійніший — першим
    eps.append({
        'id': f'{dub}:{num}', 'ordinal': num, 'title': '', 'links': {}, 'dubbing': dub,
        'players_list': [p for p, _ in pls],
        'skips': skip_map.get((dub, num), {}),
        'resolve': (lambda pls=pls, bs=skip_map.get((dub, num), {}): resolve_players(pls, referer, bs)),
    })
  if not eps:
    raise RuntimeError('немає відео на сторінці (плеєри сайту: ' + (', '.join(sorted(p for p in players_seen if p)) or 'нічого') + ').')
  eps.sort(key=lambda e: (e['dubbing'], e['ordinal']))
  if not meta.get('poster') or not meta.get('description'):  # докомплектовуємо зі сторінки, якщо API нічого не дало
    try:
      _stage('Читаю обкладинку й опис…')
      page_meta = _html_meta(html or http_get(url, timeout=8, retries=0).text, url)
      meta = {**page_meta, **{k: v for k, v in meta.items() if v}}  # (next береться лише з API)
    except Cancelled:
      raise
    except Exception:
      pass
  try:
    box = []
    def _rel():
      try:
        box.append(related_cached('y:' + slug, lambda: _yummy_related(host, slug, anime_id, a)))
      except BaseException:
        pass
    th = threading.Thread(target=_rel, daemon=True)
    th.start()
    t_end = time.time() + 5  # «Пов'язане» не варте довгого очікування: не встигло — наступного разу з кешу
    while th.is_alive() and time.time() < t_end:
      _chk()
      th.join(0.2)
    if box:
      meta['related'] = _rel_clean(box[0], url)
  except Cancelled:
    raise
  except Exception:
    pass
  return name or slug, eps, False, meta


# ───────────────────────── AnimeVost (vost.pw та дзеркала) ─────────────────────────

_RU_MONTHS = {'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6, 'июля': 7,
              'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12}


def _vost_next(html, title_raw, rid):
  """«[18 серия - 29 октября]» у заголовку + час із розкладу (за Москвою)."""
  m = re.search(r'\[\s*(\d+)\s*сери[яи]\s*-\s*(\d{1,2})\s+([а-яё]+)\s*\]', title_raw, re.I)
  if not m or m.group(3).lower() not in _RU_MONTHS:
    return None
  ep, day, mon = int(m.group(1)), int(m.group(2)), _RU_MONTHS[m.group(3).lower()]
  tm = re.search(r'/%s-[^"\']*\.html[^<]*</a>\s*\((\d{1,2}):(\d{2})\)' % rid, html)
  msk = datetime.timezone(datetime.timedelta(hours=3))
  now = datetime.datetime.now(msk)
  for year in (now.year, now.year + 1):
    try:
      dt = datetime.datetime(year, mon, day, int(tm.group(1)) if tm else 0, int(tm.group(2)) if tm else 0, tzinfo=msk)
    except ValueError:
      return None
    if dt > now - datetime.timedelta(days=2):
      return {'ts': int(dt.timestamp()), 'ep': ep, 'approx': False, 'date_only': tm is None}
  return None


def _vost_meta(html, base):
  meta = _html_meta(html, base)
  m = re.search(r'<img[^>]+class="[^"]*imgRadius[^"]*"[^>]*?\ssrc="([^"]+)"', html) or \
      re.search(r'<img[^>]+\ssrc="([^"]+)"[^>]*class="[^"]*imgRadius[^"]*"', html)
  if m:
    meta['poster'] = _abs(base, m.group(1))
  m = re.search(r'Год выхода:</strong>\s*(?:<a[^>]*>)?\s*(\d{4})', html)
  if m:
    meta['year'] = m.group(1)
  m = re.search(r'Жанр:</strong>(.*?)</p>', html, re.S)
  if m:
    meta['genres'] = [x.strip() for x in _strip_tags(m.group(1)).split(',') if x.strip()][:8]
  if not meta.get('description'):
    m = re.search(r'Описание:</strong>(.*?)</p>', html, re.S)
    if m:
      meta['description'] = _strip_tags(m.group(1)) or None
  return meta


def _fetch_vost(url):
  u = urlparse(url)
  origin = f'{u.scheme}://{u.netloc}'
  m = re.search(r'/(\d+)-[^/]*\.html', u.path) or re.search(r'[?&]id=(\d+)', url)
  if not m:
    raise RuntimeError('не вдалося визначити номер релізу в посиланні AnimeVost')
  rid = m.group(1)
  _stage('Читаю сторінку AnimeVost…')
  html = http_get(url).text
  h1 = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
  title_raw = _strip_tags(h1.group(1)) if h1 else _page_title(html)
  title = title_raw
  title = re.sub(r'\s*\[.*$', '', title).strip()
  ru, orig = (title.split(' / ', 1) + [''])[:2] if ' / ' in title else (title, '')
  meta = _vost_meta(html, url)
  meta['related'] = _rel_clean(_vost_related(html, url), url)
  meta['next'] = _vost_next(html, title_raw, rid)
  if orig and not meta.get('original'):
    meta['original'] = orig.strip()

  _stage('Отримую список серій…')
  playlist = None
  for api in dict.fromkeys(['https://api.animevost.org/v1/playlist', f'{origin}/v1/playlist']):
    try:
      r = http_post(api, data={'id': rid}, headers={'Referer': origin + '/'}, timeout=12, retries=1)
      j = r.json()
      if isinstance(j, list) and j:
        playlist = j
        break
    except Cancelled:
      raise
    except Exception:
      continue

  eps = []
  hdr = {'Referer': origin + '/'}
  page = html.replace('\\/', '/')
  # плеєри сторінки: /frame.php (плеєр 1), /frame2.php (плеєр 2), /frame5.php, /getlink.php — усі приймають ID серії
  found_tpl = list(dict.fromkeys(re.findall(r'["\'/]((?:frame\d*|getlink)\.php\?(?:play|id)=)', page)))
  found_tpl.sort(key=lambda t: (t.startswith('getlink'), int(re.search(r'frame(\d*)', t).group(1) or 0) if t.startswith('frame') else 0))
  templates = found_tpl or ['frame.php?play=', 'frame2.php?play=', 'frame5.php?play=']
  page_ids = {}
  md0 = re.search(r'var\s+data\s*=\s*(\{.*?\})\s*;', page, re.S)
  if md0:
    try:
      page_ids = {str(k).strip(): str(v) for k, v in json.loads(re.sub(r',\s*\}', '}', md0.group(1))).items()}
    except Exception:
      page_ids = {}

  def frame_resolver(pid, api_links):
    """Опитуємо всі плеєри серії паралельно. Перевірені посилання з API (480/720) лишаються надійною основою,
    а з плеєрів беремо те, чого в API немає (напр. 1080p) — але лише після швидкої перевірки, що це справжнє відео."""
    pls = [(f'Плеєр {i + 1}', f'{origin}/{tpl}{pid}') for i, tpl in enumerate(templates)]

    def resolve():
      try:
        r = resolve_players(pls, origin + '/')
      except Cancelled:
        raise
      except Exception as ex:
        if not api_links:
          raise
        return {'links': dict(api_links), 'hdrs': {q: hdr for q in api_links}, 'players': {q: 'API' for q in api_links},
                'pinfo': 'плеєри не дали посилань → API', 'fallback': dict(api_links)}
      cand = {q: lk for q, lk in r['links'].items() if q not in api_links}
      ok = {}
      if cand:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(cand)) as ex2:
          res = dict(zip(cand, ex2.map(lambda kv: _valid_media(kv[1], r['hdrs'].get(kv[0]) or hdr), cand.items())))
        ok = {q: cand[q] for q, v in res.items() if v}
        bad = [q for q, v in res.items() if not v]
      else:
        bad = []
      out = {'links': {}, 'hdrs': {}, 'players': {}, 'skips': r.get('skips') or {}}
      for q, lk in api_links.items():
        out['links'][q], out['hdrs'][q], out['players'][q] = lk, hdr, 'API'
      for q, lk in ok.items():
        out['links'][q], out['hdrs'][q], out['players'][q] = lk, r['hdrs'].get(q) or hdr, r['players'].get(q)
      if not out['links']:
        raise RuntimeError('Плеєри AnimeVost не дали робочих посилань. ' + (r.get('pinfo') or ''))
      out['pinfo'] = (r.get('pinfo') or '') + (' | не пройшли перевірку: ' + ','.join(bad) if bad else '')
      out['fallback'] = dict(api_links)
      return out
    return resolve

  if playlist:
    for d in playlist:
      nm = str(d.get('name') or '')
      mm = re.match(r'\s*(\d+(?:\.\d+)?)', nm)
      if not mm:
        continue  # OVA/спеціальні випуски без номера пропускаємо
      links = {}
      if d.get('hd'):
        links['720p'] = d['hd']
      if d.get('std'):
        links['480p'] = d['std']
      if not links:
        continue
      ep = {'id': nm, 'ordinal': _num(float(mm.group(1))), 'title': '', 'links': links,
            'headers': hdr, 'dubbing': None, 'players': {q: 'API' for q in links}}
      pid = page_ids.get(nm.strip()) or d.get('id')
      if pid:
        ep['resolve'] = frame_resolver(pid, dict(links))
        ep['preview'] = f'https://media.aniland.org/img/{pid}.jpg'
      eps.append(ep)
  else:
    # запасний варіант: список із самої сторінки + кадр плеєра
    for nm, pid in page_ids.items():
      mm = re.match(r'\s*(\d+(?:\.\d+)?)', str(nm))
      if not mm:
        continue
      eps.append({'id': str(nm), 'ordinal': _num(float(mm.group(1))), 'title': '', 'links': {},
                  'headers': hdr, 'dubbing': None, 'resolve': frame_resolver(pid, {}),
                  'preview': f'https://media.aniland.org/img/{pid}.jpg'})
  if not eps:
    raise RuntimeError('не вдалося отримати серії AnimeVost (сайт або API недоступні).')
  seen, uniq = set(), []
  for e in eps:
    if e['ordinal'] not in seen:
      seen.add(e['ordinal'])
      uniq.append(e)
  uniq.sort(key=lambda e: e['ordinal'])
  return ru or title, uniq, False, meta


# ───────────────────────── «Пов'язане»: порядок перегляду, сезони, схожі ─────────────────────────
# Кожен сайт дає список [{url, title, note, poster, kind}], kind: 'order' (сезони/порядок перегляду) | 'similar'.

_REL_CACHE = {}  # url -> (ts, items): не смикаємо сайт зайвий раз при кожній перевірці


def _rel_clean(items, self_url=None):
  out, seen = [], set()
  for x in items or []:
    u = (x.get('url') or '').strip()
    t = (x.get('title') or '').strip()
    if not u.startswith('http') or not t or u in seen or u == self_url:
      continue
    seen.add(u)
    out.append({'url': u, 'title': t[:160], 'note': (x.get('note') or '').strip()[:80], 'poster': x.get('poster') or None,
                'kind': x.get('kind') or 'order'})
  return out[:40]


def _vost_related(html, base):
  """AnimeVost: спойлер «Это аниме состоит из:» — список сезонів (без постерів, вони підтягуються пізніше)."""
  m = re.search(r'Это аниме состоит из:.*?<ol>(.*?)</ol>', html or '', re.S)
  items = []
  if m:
    for li in re.findall(r'<li>(.*?)</li>', m.group(1), re.S):
      a = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*)', li, re.S)
      if a:
        note = _strip_tags(a.group(3)).strip(' -–—')
        items.append({'url': _abs(base, a.group(1)), 'title': _strip_tags(a.group(2)), 'note': note, 'kind': 'order'})
  return items


def _animru_related(html, base):
  """Anim-RU: блок «Похожие» знизу сторінки (з постерами); новини без серій відкидаємо."""
  m = re.search(r'<h2[^>]*>\s*Похожие\s*</h2>(.*?)</section>', html or '', re.S)
  items = []
  if m:
    for art in re.findall(r'<article.*?</article>', m.group(1), re.S):
      a = re.search(r'<a[^>]+href="([^"]+)"[^>]*title="([^"]*)"', art)
      im = re.search(r'<img[^>]+src="([^"]+)"', art)
      if not a:
        continue
      raw = htmllib.unescape(a.group(2))
      if not re.search(r'\[[^\]]*сери', raw, re.I) and not re.search(r'\((?:19|20)\d\d\)', raw):
        continue
      y = re.search(r'\(((?:19|20)\d\d)\)', raw)
      items.append({'url': _abs(base, a.group(1)), 'title': _clean_title(raw), 'note': y.group(1) if y else '',
                    'poster': _abs(base, im.group(1)) if im else None, 'kind': 'similar'})
  return items


def _yummy_rel_item(d, host):
  if not isinstance(d, dict):
    return None
  slug = d.get('anime_url') or d.get('url') or d.get('slug')
  title = d.get('title') or d.get('name')
  if not slug or not title:
    return None
  ps = d.get('poster')
  if isinstance(ps, dict):
    ps = next((ps[k] for k in ('big', 'medium', 'huge', 'fullsize', 'small', 'mega') if ps.get(k)), None)
  if isinstance(ps, str) and ps:
    ps = 'https:' + ps if ps.startswith('//') else _abs('https://yummyani.me/', ps)
  else:
    ps = None
  t = d.get('type')
  t = t.get('name') if isinstance(t, dict) else t
  note = ', '.join(str(x) for x in (t, d.get('year')) if x)
  return {'url': f'https://{host}/catalog/item/{slug}', 'title': str(title), 'note': note, 'poster': ps}


def _yummy_related(host, slug, anime_id, a):
  """YummyAnime: «Порядок просмотра» (старий сайт віддає його в HTML, API — якщо є) і «Похожие» (API рекомендацій)."""
  items = []
  if isinstance(a, dict):
    for k in ('view_order', 'viewOrder', 'franchise', 'related'):
      v = a.get(k)
      if isinstance(v, list) and v:
        for d in v:
          it = _yummy_rel_item(d, host)
          if it:
            items.append({**it, 'kind': 'order'})
        break
  if not items:
    try:
      h = http_get(f'https://old.yummyani.me/catalog/item/{quote(slug, safe="")}', timeout=12, retries=0).text
      m = re.search(r'class="view-list[^"]*"[^>]*>\s*<ol>(.*?)</ol>', h, re.S)
      for li in re.findall(r'<li>(.*?)</li>', m.group(1), re.S) if m else []:
        am = re.search(r'<a[^>]+href="(/catalog/item/[^"]+)"[^>]*>(.*?)</a>\s*<span>(.*?)</span>', li, re.S)
        if am:
          items.append({'url': 'https://old.yummyani.me' + am.group(1), 'title': _strip_tags(am.group(2)),
                        'note': _strip_tags(am.group(3)), 'kind': 'order'})
    except Cancelled:
      raise
    except Exception:
      pass
  if anime_id:
    try:
      _, rec = _yummy_call(host, f'/anime/{anime_id}/recommendations')
      if isinstance(rec, dict):
        rec = rec.get('items') or rec.get('recommendations') or []
      for d in (rec or [])[:20]:
        it = _yummy_rel_item(d.get('anime') if isinstance(d, dict) and isinstance(d.get('anime'), dict) else d, host)
        if it:
          items.append({**it, 'kind': 'similar'})
    except Cancelled:
      raise
    except Exception:
      pass
  return items


def _anilib_related(host, rid, alias):
  """AniLiberty: вкладка «Связанное» (франшиза) — через API; реліз знаходимо за alias, якщо id невідомий."""
  base = f'https://{host}/'
  if not rid and alias:
    j = http_get(f'https://{host}/api/v1/anime/releases/{alias}', headers=HEADERS, timeout=15, retries=0).json()
    rid = j.get('id') if isinstance(j, dict) else None
  if not rid:
    return []
  j = http_get(f'https://{host}/api/v1/anime/franchises/release/{rid}', headers=HEADERS, timeout=15, retries=0).json()
  items, seen = [], set()

  def walk(o, depth=0):
    if depth > 6:
      return
    if isinstance(o, dict):
      if o.get('alias') and isinstance(o.get('name'), dict) and (o['name'].get('main') or o['name'].get('english')):
        if o['alias'] not in seen:
          seen.add(o['alias'])
          mt = _anilib_meta(o, base)
          tp = o.get('type')
          tp = tp.get('description') or tp.get('value') if isinstance(tp, dict) else None
          items.append({'url': f'https://{host}/anime/releases/release/{o["alias"]}/episodes',
                        'title': o['name'].get('main') or o['name'].get('english'),
                        'note': ', '.join(str(x) for x in (tp, o.get('year')) if x), 'poster': mt.get('poster'), 'kind': 'order'})
        return
      for v in o.values():
        walk(v, depth + 1)
    elif isinstance(o, list):
      for v in o:
        walk(v, depth + 1)
  walk(j)
  return items


def related_cached(key, fn):
  """Список «пов'язаного» з кешем на 6 год; збій не ламає основне завантаження."""
  c = _REL_CACHE.get(key)
  if c and time.time() - c[0] < 6 * 3600:
    return c[1]
  try:
    items = fn() or []
  except Cancelled:
    raise
  except Exception:
    return []
  _REL_CACHE[key] = (time.time(), items)
  return items


_REL_POSTER = {}


def rel_poster_url(page_url):
  """Постер сторінки аніме (для карток «пов'язаного» без постера). Кеш у пам'яті."""
  if page_url in _REL_POSTER:
    return _REL_POSTER[page_url]
  u = urlparse(page_url)
  poster = None
  try:
    if '/catalog/item/' in u.path:
      slug = unquote(u.path.split('/catalog/item/', 1)[1].strip('/').split('/')[0])
      try:
        _, a = _yummy_call(u.netloc, '/anime/' + quote(slug, safe=''), {'need_videos': 'false'})
        poster = _yummy_meta(a).get('poster')
      except Cancelled:
        raise
      except Exception:
        poster = None
      if not poster:
        poster = _html_meta(http_get(page_url, timeout=12, retries=0).text, page_url).get('poster')
    elif _is_vost(page_url):
      poster = _vost_meta(http_get(page_url, timeout=12, retries=0).text, page_url).get('poster')
    else:
      poster = _html_meta(http_get(page_url, timeout=12, retries=0).text, page_url).get('poster')
  except Cancelled:
    raise
  except Exception:
    poster = None
  if poster:
    _REL_POSTER[page_url] = poster
  return poster


# ═════════════════════════ Пошук по сайтах та об'єднання однакових аніме ═════════════════════════

LOGOS = {'yummyani': {'a': '<svg version="1.1" id="Layer_1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" x="0px" y="0px" viewBox="0 0 200.7 200.7" style="enable-background:new 0 0 200.7 200.7;" xml:space="preserve"> <style type="text/css"> .st0{fill:#333333;} .st1{fill:#FF6666;} .st2{fill:none;} </style> <circle class="st0" cx="100.3" cy="100.3" r="82.8"/> <path class="st1" d="M100.3,0C44.9,0,0,44.9,0,100.4c0,31.7,14.7,59.9,37.6,78.3c0.5,0.4,1.1,0.9,1.6,1.3c1,0.8,2,1.6,3.1,2.3 c2.4,1.7,4.9,3.3,7.5,4.8c14.8,8.6,32.1,13.6,50.5,13.6c26.5,0,50.6-10.3,68.5-27.1c1.7-1.6,3.4-3.3,5-5c0.2-0.2,0.4-0.4,0.6-0.6 c0.5-0.5,1-1.1,1.4-1.6c15.4-17.6,24.7-40.7,24.7-66C200.7,44.9,155.8,0,100.3,0z M176.7,101.9c-0.3,14.3-4.5,27.7-11.6,39 c-0.4,0.7-0.8,1.3-1.3,2c-4.8,7.1-10.7,13.3-17.5,18.5c-0.5,0.4-1.1,0.8-1.6,1.2c-12.1,8.7-26.9,13.9-42.9,14.2 c-11.3,0.2-22-2-31.7-6.2c-0.6-0.3-1.2-0.5-1.8-0.8c-7-3.3-13.5-7.5-19.1-12.7c-0.5-0.5-1-0.9-1.5-1.4 c-14.2-13.6-23.2-32.6-23.6-53.8c-0.2-10.4,1.7-20.4,5.3-29.5c0.4-1.1,0.9-2.2,1.4-3.3c0.4-0.9,0.9-1.9,1.3-2.8l0,0 C35,60.4,38.7,54.9,43,50c0.1-0.2,0.3-0.4,0.5-0.5c0.7-0.8,1.4-1.5,2.1-2.3c13.5-13.9,32.4-22.7,53.3-23.1c1.9,0,3.9,0,5.8,0.1 c3.9,0.2,7.7,0.7,11.4,1.5c18.5,3.9,34.6,14.5,45.5,29.1c0.6,0.9,1.3,1.7,1.9,2.6c4.6,6.8,8.2,14.4,10.4,22.5 c0.3,1.3,0.7,2.5,0.9,3.8c1.1,4.9,1.7,10,1.8,15.2V101.9z"/> <g> <path class="st1" d="M27.6,68.4l14.8-22l48,61.2L73,126.2L27.6,68.4z"/> <path class="st1" d="M164.1,142.9l-1.3-3l-12.4-29.2v0l-11.2-26.4l-24-56.6l-1.4-3.3l-8.8-1.1c-0.4,1-0.8,1.9-1.2,2.9 c-9.2,21.1-18.3,42.2-27.5,63.3c-3.8,8.8-7.6,17.5-11.4,26.3c-5.5,12.8-11.1,25.5-16.6,38.3c-0.5,1.2-1,2.4-1.5,3.6 c-2.8,6.4-5.6,12.9-8.4,19.3c1,0.8,2,1.6,3.1,2.3c2.4,1.7,4.9,3.3,7.5,4.8l13.4-0.4l5.1-11.9l1.8-4.2l7.7-18.2h62.5l4.3,10.2 l1.6,3.8l3.1,7.3l19.4-0.1c1.7-1.6,3.4-3.3,5-5c0.2-0.2,0.4-0.4,0.6-0.6L164.1,142.9z M86.3,127.4c5.1-11.9,10.1-23.9,15.2-35.8 c2.4-5.7,4.8-11.4,7.2-17.1l6.6,16.1l15.1,36.8H86.3z"/> <polygon class="st1" points="179.1,74.1 148.6,113 131.2,94.4 168.2,47.2 \t"/> <line class="st2" x1="179.1" y1="74.1" x2="168.2" y2="47.2"/> </g> </svg>'}, 'aniliberty': {'red': '<svg xmlns="http://www.w3.org/2000/svg" fill="#fe3635" version="1.2" viewBox="0 0 1174 1174"> <path d="M698 245c0-7 8-12 14-9l169 77c4 2 6 6 6 9v395c0 6 4 10 10 10h248c6 0 11 6 9 12-25.3 94.2-73.6 180.7-140.7 251.5-67.1 70.9-150.8 123.9-243.5 154.2-92.7 30.4-191.5 37.2-287.5 19.8-96-17.4-186.2-58.5-262.3-119.5-3-3-4-8-2-12l34-58c3-5 10-7 15-3 51.5 43.1 111.1 75.4 175.4 94.9 64.2 19.6 131.7 25.9 198.5 18.7 66.7-7.2 131.4-27.8 190-60.5 58.6-32.8 110-77 151.1-130.1 5-7 1-16-8-16h-93c-4 0-7-2-9-5L699 574l-1-5z"/> <path d="M422 240c4-7 14-7 18 0l360 624c4 7-1 15-8 15H628c-4 0-7-2-9-5l-43-75c-2-3-5-5-9-5H373c-7 0-12-8-8-15l65-113c1-3 5-5 8-5h41c7 0 12-8 8-15l-47-83c-4-7-14-7-18 0L211 929v1l-23 39-1 1-10 19c-4 6-12 7-16 2-36-38-67-82-92-128-2-3-2-7 0-10l10-19 2-2v-1z"/> <path d="M587 0c83.2.1 165.5 17.8 241.3 52.1 75.8 34.3 143.5 84.2 198.5 146.7 55.1 62.4 96.2 135.8 120.7 215.3s31.8 163.3 21.5 245.9c0 5-4 9-9 9h-67c-6 0-11-6-10-12 20.8-146.3-24-294.3-122.5-404.5C862.1 142.4 720 81.3 572.3 85.6 424.6 90 286.3 159.3 194.5 275 102.7 390.8 66.6 541.2 96 686l-1 7-47 81c-5 7-15 6-18-2C.5 683.8-7.6 589.8 6.4 497.8c14-91.9 49.6-179.2 104.1-254.7 54.4-75.4 126-136.8 208.9-179.1C402.2 21.7 494-.2 587 0z"/> </svg>', 'purple': '<svg xmlns="http://www.w3.org/2000/svg" fill="#a070ff" version="1.2" viewBox="0 0 1174 1174"> <path d="M698 245c0-7 8-12 14-9l169 77c4 2 6 6 6 9v395c0 6 4 10 10 10h248c6 0 11 6 9 12-25.3 94.2-73.6 180.7-140.7 251.5-67.1 70.9-150.8 123.9-243.5 154.2-92.7 30.4-191.5 37.2-287.5 19.8-96-17.4-186.2-58.5-262.3-119.5-3-3-4-8-2-12l34-58c3-5 10-7 15-3 51.5 43.1 111.1 75.4 175.4 94.9 64.2 19.6 131.7 25.9 198.5 18.7 66.7-7.2 131.4-27.8 190-60.5 58.6-32.8 110-77 151.1-130.1 5-7 1-16-8-16h-93c-4 0-7-2-9-5L699 574l-1-5z"/> <path d="M422 240c4-7 14-7 18 0l360 624c4 7-1 15-8 15H628c-4 0-7-2-9-5l-43-75c-2-3-5-5-9-5H373c-7 0-12-8-8-15l65-113c1-3 5-5 8-5h41c7 0 12-8 8-15l-47-83c-4-7-14-7-18 0L211 929v1l-23 39-1 1-10 19c-4 6-12 7-16 2-36-38-67-82-92-128-2-3-2-7 0-10l10-19 2-2v-1z"/> <path d="M587 0c83.2.1 165.5 17.8 241.3 52.1 75.8 34.3 143.5 84.2 198.5 146.7 55.1 62.4 96.2 135.8 120.7 215.3s31.8 163.3 21.5 245.9c0 5-4 9-9 9h-67c-6 0-11-6-10-12 20.8-146.3-24-294.3-122.5-404.5C862.1 142.4 720 81.3 572.3 85.6 424.6 90 286.3 159.3 194.5 275 102.7 390.8 66.6 541.2 96 686l-1 7-47 81c-5 7-15 6-18-2C.5 683.8-7.6 589.8 6.4 497.8c14-91.9 49.6-179.2 104.1-254.7 54.4-75.4 126-136.8 208.9-179.1C402.2 21.7 494-.2 587 0z"/> </svg>'}, 'animru': {'a': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 105 105"><defs><style>.a{fill:#fbad3d}.b{fill:#f48b20}.c{fill:#fdfdfc}.d{fill:none;stroke:#844a15;stroke-linecap:round;stroke-width:4.5px}</style></defs><ellipse class="a" cx="52.53" cy="51.91" rx="41.48" ry="41.39"/><path class="a" d="M105 60.36c0 6.58-23.51 9.9-52.5 9.9S0 66.94 0 60.36c0-15.62 23.51-1.83 52.5-1.83S105 45 105 60.36Z"/><ellipse class="a" cx="45.64" cy="15.91" rx="8.58" ry="15.91"/><ellipse class="a" cx="60.72" cy="12.88" rx="12.61" ry="7.7" transform="rotate(-52.41 60.725 12.876)"/><path class="b" d="m74.07 16.54-.42.4c-3.86 3.62-8.33 5.44-11.35 4.91a19.32 19.32 0 0 0 4.45-4.18 19.86 19.86 0 0 0 2.3-3.74 41.13 41.13 0 0 1 5.02 2.61ZM91.1 67.17c0 .12-.09.23-.14.35a41.53 41.53 0 0 1-76.85 0c-.06-.16-.13-.31-.19-.47.14.16 2.06.35 2.19.47 12.89 12 19 14.65 36.42 14.65S75.31 79.76 89 67.52c.06-.09 2-.23 2.1-.35Z"/><path class="b" d="M50 97.22c0 7.53-.14 7.78-10.12 7.78-9.33 0-9.9-.11-9.9-3.19 0-3.74 4.6.29 4.6-7.62 0-4.78-2.8-5.46-1.7-6.54 2.45-2.41 20.52 5.46 20.52 5.46s-3.4-.29-3.4 4.11Z"/><path class="b" d="M55 97.22c0 7.53.14 7.78 10.12 7.78 9.33 0 9.89-.11 9.89-3.19 0-3.74-4.59.29-4.59-7.62 0-4.78 2.8-5.46 1.7-6.54-2.45-2.41-20.52 5.46-20.52 5.46s3.4-.29 3.4 4.11Z"/><ellipse class="c" cx="28.48" cy="54.54" rx="7.17" ry="6.18"/><ellipse class="c" cx="76.48" cy="54.54" rx="7.17" ry="6.18"/><path class="d" d="M31 42.2a4 4 0 1 1 8.08 0M66 42.2a4 4 0 1 1 8.08 0M62.3 49.35c-2.39 5.65-17.3 5.82-19.68 0"/></svg>'}, 'animevost': {'a': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512"> <path d="M334.32 82.11C337.31 82.49 341.66 85.75 343.51 88.17C343.56 88.38 343.62 88.59 343.68 88.79C343.85 88.96 344.01 89.13 344.18 89.3C344.33 89.44 344.47 89.58 344.62 89.72C344.78 89.9 344.94 90.07 345.1 90.25C345.3 90.23 345.49 90.21 345.69 90.19C352.02 95.76 356.03 104.8 357.46 112.92C357.84 115.08 357.65 117.31 358.09 119.47C362.75 122.66 370.43 125.84 373.6 130.88C372.49 131.47 372.36 131.37 371.49 130.69C369.5 133.89 368.72 137.12 367.06 140.47C366.32 141.97 364.83 143.15 364.15 144.59C363.87 145.2 364.02 145.91 363.78 146.55C363.3 147.84 361.93 148.89 361.34 150.2C358.95 155.52 355.12 160.74 351.39 165.28C347.9 165.71 344.57 165.04 341.1 164.84C337.05 164.61 332.98 164.76 328.93 164.82C326.44 164.85 321.8 164.51 319.9 166.01C319.88 166.1 319.85 166.2 319.82 166.29C316.82 165.89 313.21 167.31 310.24 167.9C298.39 170.28 286.78 174.86 276.75 181.65C280.08 181.81 287.39 177.64 291.18 176.44C301.98 173.02 313.09 172.58 324.28 174.05C327.12 174.43 334.53 175.42 336.61 177.1C337.29 177.84 337.98 178.58 338.67 179.32C334.17 182.37 330.88 187.07 326.48 190.19C324.58 191.54 321.78 194.84 319.58 195.21C317.61 198.54 309.8 201.38 306.38 203.3C305.82 203.62 305.5 204.3 304.94 204.63C302.34 206.14 299.45 207.92 296.65 209.02C294.24 209.96 291.68 210.44 289.34 211.6C288.57 211.98 288.07 212.75 287.23 213.07C282.97 214.68 278.06 215.5 274.24 217.3C271.76 217.12 268.44 216.14 266.01 215.31C264.35 214.75 262.93 213.77 261.23 213.72C257.2 211.52 255.34 207.56 253.19 203.74C252.32 205.09 252.46 205.89 252.17 207.41C251.64 210.24 250.09 213.64 247.95 215.59C246.71 215.91 246.36 216.68 245.32 217.49C242.94 219.37 239.3 221.87 236.33 222.41C232.86 223.03 221.89 222.23 218.46 221.42C214.86 220.57 210.51 221.22 206.94 220.18C206.27 219.99 206 219.27 205.36 219.08C203.28 218.46 201.08 218.43 198.96 218.01C195.96 217.42 193.26 216.14 190.4 215.34C189.57 215.11 188.64 215.32 187.81 215.03C185.56 214.22 183.17 213.57 180.97 212.48C180.37 212.18 180 211.56 179.44 211.25C174.86 208.74 167.39 208.31 164.25 203.63C168.07 200.49 172.2 197.79 176.68 195.67C178.24 194.94 183.39 193.4 184.32 192.5C184.89 191.95 184.92 191.49 185.63 191.01C186.2 190.91 187.15 191.08 187.65 190.91C188.12 190.75 188.26 190.12 188.76 189.93C189.55 189.61 190.55 189.95 191.39 189.75C192.09 189.58 192.63 189.04 193.36 188.92C194.89 188.67 196.12 189.27 197.55 189.39C200.36 189.62 203.66 188.79 206.51 188.64C209.28 188.51 212.09 188.83 214.85 189.01C217.07 189.15 221.08 189.59 223.15 190.04C223.88 190.2 224.33 190.87 225.13 190.87C225.85 190.86 226 190.46 226.51 190.07C225.49 188.69 222.72 188.33 220.93 187.91C216.01 186.76 211.02 185.74 205.97 185.48C203.62 185.36 201.33 184.89 198.96 184.83C189.14 184.58 179.53 185.64 169.98 187.54C166.06 188.33 162.05 189.57 158.35 190.9C154.99 192.11 151.63 194.01 147.99 194.41C146.64 193.51 145.72 191.99 144.37 191.15C138.01 187.17 131.48 180.7 126.37 174.99C124.39 172.78 120.69 167.22 118.1 166.45C119.01 163.21 121.11 160.45 123.91 158.28C124.36 156.09 126.74 154.51 128.35 152.9C128.25 151.31 127.84 149.96 127.57 148.46C126.71 143.67 126.99 138.31 127.6 133.49C127.82 131.77 128.82 130.28 129.22 128.73C129.39 128.03 129.16 127.25 129.43 126.46C129.66 125.81 130.27 125.41 130.53 124.83C131.71 122.25 132.49 119.51 134.22 117.18C134.61 116.66 135.46 116.53 135.79 115.95C136.02 115.53 135.93 114.86 136.15 114.38C137.06 112.38 140.64 108.89 142.61 108.29C146.25 108.17 148.91 102.49 153.13 103.73C153.86 104.65 153.92 105.21 153.9 106.37C156.38 106.16 158.98 105.05 161.55 104.65C168.47 103.56 175.98 104.41 182.17 107.48C184.58 108.68 188.47 110.35 189.4 112.92C186.47 114.73 181.76 114.6 178.41 115.57C172.71 117.21 166.9 119.14 162.11 122.74C164.33 124.49 165.84 126.6 167.43 128.78C170.13 132.48 174.25 135.6 177.48 138.84C178.98 140.34 180.6 142.61 182.41 143.71C184.95 145.25 187.23 147.03 189.82 148.51C190.38 148.82 190.73 149.51 191.29 149.81C191.84 150.1 192.53 150.03 193.03 150.4C193.71 150.9 193.64 151.63 193.81 152.33C195.45 152.32 195.73 152.86 197.08 153.44C197.5 153.25 197.55 152.94 198.09 152.98C198.84 153.04 198.63 153.6 199.16 153.87C199.47 154.04 200.69 153.75 200.78 154.31C200.85 154.7 200.54 154.58 200.47 155C203.55 154.55 202.57 155.31 204.67 156.03C205.37 156.27 206.17 156.04 206.87 156.33C207.67 156.66 208.54 157.54 209.33 157.85C210.28 158.24 211.46 157.84 212.44 158.15C212.88 158.29 213.12 158.71 213.56 158.84C214.56 159.14 215.68 158.73 216.69 159.13C217.12 159.3 217.13 159.64 217.65 159.78C219.11 160.14 220.78 159.64 222.24 160.08C222.82 160.25 223.12 160.66 223.75 160.77C228.29 161.52 236.26 161 241.04 160.81C242.72 160.75 244.93 161.19 246.54 160.71C246.98 160.58 247.21 160.15 247.65 160.02C248.92 159.66 250.46 160.19 251.71 159.73C252.14 159.57 252.16 159.21 252.68 159.07C253.86 158.74 255.21 159.19 256.36 158.84C258.11 158.31 258.62 157.95 260.68 157.97C262.34 155.97 262.03 156.81 264.31 156.03C264.95 155.81 265.22 155.08 265.89 154.88C266.74 154.63 267.67 154.92 268.52 154.61C272.05 153.33 274.97 150.81 278.61 149.86C279.9 147.46 282.43 146.9 284.71 145.6C285.63 145.08 286.13 143.82 287.08 143.34C287.41 143.18 287.78 143.31 288.09 143.13C290.71 141.61 291.32 138.43 294.62 138.17C294.5 137.65 294.23 137.78 294.24 137.28C294.24 136.88 295.55 136.05 295.83 135.74C296.94 134.48 301.39 130.5 301.89 129.65C302.14 129.22 302.04 128.58 302.29 128.1C302.79 127.15 304.03 126.65 304.57 125.73C305.51 124.15 306.52 122.68 307.41 121.09C307.95 120.12 309.13 119.61 309.7 118.72C310.6 117.32 310.57 115.86 311.14 114.39C311.81 112.68 313.19 111.06 313.95 109.38C314.62 107.89 314.7 106.11 315.23 104.57C315.75 103.06 316.8 101.73 317.53 100.3C311 97.37 300.95 98.18 293.87 98.35C291.97 98.39 290.4 99.19 288.49 98.89C288.14 97.63 288.84 96.82 289.92 96.23C289.92 96.81 289.91 97.38 289.9 97.96C303.03 95.95 316.24 97.62 328.89 101.53C332.69 102.7 336.46 104.44 339.93 106.31C341.74 107.29 343.64 109.04 345.68 109.18C346.17 105.42 343.29 100.65 341.12 97.7C332.7 86.19 318.94 81.3 305.13 85.7C301.49 86.86 298.03 89.04 295.15 91.52C293.6 92.87 291.82 95.73 289.92 96.23C290.29 94.17 293.13 92.11 294.72 90.64C299.15 86.58 305.02 84.17 310.75 82.91C314.07 82.18 317.68 83.08 320.99 82.41C321.09 81.51 321.18 80.62 321.27 79.72C324.05 79.13 327.45 79.7 330.15 80.69C331.62 81.23 332.8 82.27 334.32 82.11ZM188.27 112.45C183.77 109.31 178.83 106.41 173.24 105.42C162.35 103.49 150.28 108.52 143.73 117.31C140.39 121.79 138.69 126.21 137.39 131.62C136.82 134.04 136.32 137.48 137.7 139.58C147.8 128.86 156.97 122.68 170.57 117.03C176.53 114.55 182.43 114.16 188.27 112.45ZM308.92 134.37C299.39 135.91 291.01 141.36 285.88 149.55C284.48 151.78 281.65 156.14 283.31 158.77C284.16 160.11 286.12 160.31 287.36 159.47C289 158.36 289.96 156.02 291.36 154.6C294.69 151.22 298.29 148.26 302.52 146.09C308.14 143.2 314.69 141.84 320.99 142C323.26 142.06 327.08 143.17 327.96 140.18C328.8 137.32 325.9 136.21 323.73 135.6C318.93 134.23 313.9 133.57 308.92 134.37ZM178.89 153.09C174.06 153.57 168.63 155.99 164.85 159C163.05 160.44 161.2 163.72 164.45 164.95C167.02 165.92 171.17 163.39 173.72 162.75C178.96 161.42 185.32 161.3 190.57 162.63C194.48 163.62 198.24 164.96 201.72 167.04C203.5 168.11 205.14 169.68 207.01 170.58C208.38 171.24 210.15 170.95 210.95 169.54C212.66 166.52 207.94 162.59 205.96 160.87C198.29 154.23 188.88 152.11 178.89 153.09ZM255.53 188.98C254.05 186.42 250.68 187.97 248.05 187.97C246.06 190.4 242.87 188.75 241.04 191.2C239.57 190.9 238.07 192.66 237.21 193.81C239.2 198.1 248.91 199.81 252.96 200.83C254.07 199.54 256.33 198.79 257.88 197.76C260.81 195.81 263.38 193.21 265.35 190.31C264.67 188.33 261.2 188.54 259.74 187.5C257.44 188.88 257.27 186.36 255.53 188.98ZM379.85 230.05C382.76 232 383.86 237.81 383.93 241.04C383.98 243.29 384.23 245.8 383.78 248.02C382.5 254.32 378.13 260.57 372.65 263.93C354.72 274.9 331.31 278.07 311.42 271.44C308.55 270.49 299.89 268.69 298.11 267.12C302.09 267.06 306.18 269.35 309.89 270.52C314.87 272.09 325.6 274.03 330.58 272.83C328.54 271.25 325.81 270.28 323.69 268.49C316.07 262.05 314.06 256.74 311.27 247.58C313.78 245.92 316.61 244.71 319.31 243.38C320.62 242.74 322.34 242.4 322.93 241.12C329 238.02 335.08 234.91 341.15 231.81C341.91 231.69 342.68 231.57 343.44 231.45C349.8 234.64 355.32 242.53 358.52 248.66C359.6 250.74 361.3 252.76 361.67 255.06C364.06 256.83 366.28 258.34 368.22 260.69C369.65 260.87 370.01 261.43 371.02 262.37C373.23 262.15 374.62 260.56 376.27 259.12C382.08 254.08 384.3 245.32 382.79 237.83C382.25 235.11 380.3 232.6 379.85 230.05ZM433.18 240.41C435.26 241.88 436.37 245.19 437.57 247.34C442.6 256.33 444.59 261.61 446.96 271.66C447.53 274.08 448.15 276.66 447.98 279.15C447.04 279.07 446.99 279.16 446.29 278.44C442.2 282.53 438.11 286.62 434.02 290.71C432.13 292.6 430.09 295.1 427.37 295.28C426.83 297.28 424.56 298.16 422.8 299.15C421.57 299.84 420.94 301.57 419.62 302.13C418.36 302.66 416.99 302.97 415.79 303.83C413.52 305.46 408.95 309.21 406.53 310.08C405.88 310.31 405.19 310.1 404.55 310.37C403.15 310.96 401.82 312.19 400.45 312.88C398.26 313.99 395.91 314.69 393.61 315.4C392.57 315.72 391.65 316.72 390.63 317.09C389 317.68 387.31 317.72 385.66 318.2C384.62 318.51 383.7 319.53 382.68 319.9C381.13 320.45 380.69 320.19 379.44 321.44C377.29 321.44 375.84 322 373.83 322.35C372.82 322.53 371.67 322.13 370.69 322.41C370.1 322.58 369.79 323.11 369.15 323.26C368.01 323.52 366.68 323.02 365.56 323.35C365.1 323.48 364.89 323.95 364.44 324.11C363.3 324.51 362.01 323.96 360.87 324.28C360.28 324.45 359.97 324.98 359.33 325.13C356.94 325.69 353.84 325.18 351.39 325.18C351.09 334.79 358.67 341.69 363.5 349.05C362.85 349.3 358.75 352.12 358.41 352.49C358.09 352.84 358.22 353.44 357.77 353.81C357.37 354.15 356.84 354.08 356.4 354.33C355.28 354.97 354.68 356.45 353.57 357.1C352.56 357.7 351.35 357.96 350.33 358.54C349.64 358.92 349.31 359.83 348.55 360.11C347.9 360.35 347.21 360.13 346.57 360.4C345.9 360.69 345.45 361.57 344.83 361.88C343.56 362.52 342.23 362.96 340.97 363.68C340.29 364.07 339.96 364.97 339.2 365.25C338.64 365.46 338.05 365.26 337.5 365.43C335.8 365.95 334.32 367.45 332.65 368.06C332.09 368.26 331.5 368.07 330.95 368.23C330.24 368.45 329.64 369.16 328.9 369.44C327.1 370.13 325.2 370.65 323.47 371.5C322.49 371.98 321.52 372.84 320.49 373.2C319.49 373.55 318.38 373.09 317.39 373.38C315.79 373.85 314.33 374.88 312.58 375.19C310.7 375.52 308.82 375.76 306.96 376.1C305.96 376.29 304.81 375.9 303.83 376.18C303.24 376.35 302.93 376.88 302.29 377.03C300.85 377.37 299.17 376.71 297.76 377.12C297.16 377.29 296.86 377.84 296.21 377.99C293.17 378.71 289.52 377.6 286.53 378.51C285.75 378.75 285.44 379.3 284.52 379.42C282.2 379.72 278.38 379.93 276.11 379.38C275.46 379.22 275.16 378.68 274.57 378.5C273.39 378.15 269.95 378.06 268.62 378.04C269.11 381.4 273.85 386.82 276.42 389.18C283.82 395.96 292.42 400.96 302.32 402.94C306.18 403.72 310.47 403.27 314.21 404.22C314.41 404.73 314.6 405.25 314.79 405.76C311.98 406.13 309.32 407.08 306.53 407.7C305.48 407.94 303.67 407.81 302.77 408.37C302.21 408.72 302.39 408.91 302.06 409.37C300.61 409.53 299.15 408.91 297.61 409.1C295.37 409.37 293.12 410.28 290.83 410.59C289.38 410.78 287.86 410.49 286.4 410.7C284.07 411.04 281.76 411.49 279.38 411.68C276.06 411.94 272.64 412.31 269.33 412.4C266.62 412.48 263.88 412.14 261.14 412.38C257.07 412.73 248.81 412.66 244.76 412.13C243.2 411.93 241.66 411.87 240.1 411.78C239.9 411.77 239.3 411.86 239.14 411.65C238.97 411.43 239.48 411.23 239.63 411C236.39 411.25 235.94 412.02 232.15 411.12C231.92 410.63 231.69 410.14 231.45 409.65C232.25 409.56 233.05 409.46 233.85 409.37C235.38 407.15 237.22 407.38 239.51 406.55C240.35 406.24 241.19 405.35 241.98 404.97C242.46 404.73 243.03 404.82 243.51 404.57C244.29 404.17 244.74 403.3 245.57 402.89C247.1 402.14 249.02 402.08 250.53 401.3C251.22 400.94 251.55 400.04 252.3 399.73C257.99 397.37 267.71 391.77 271.83 386.92C270.9 384.78 268.93 383.48 267.88 381.53C266.97 379.82 266.9 378.88 265.12 377.73C264.81 377.26 264.5 376.79 264.18 376.32C265.26 373.01 261.36 369.62 262.31 366.35C262.11 366.44 261.9 366.74 261.71 366.63C261.5 366.5 261.52 365.85 261.49 365.65C261.35 364.6 260.8 363.75 260.21 362.84C260.45 361.95 260.13 354.45 259.97 358.4C259.51 355.07 259.32 351.61 259.16 348.11C259.09 346.66 259.31 344.17 258.81 343.5C258.81 345.11 258.81 346.73 258.81 348.35C259.77 345.01 257.23 343.56 259.04 340.16C260.21 340.09 261.38 340.01 262.55 339.93C264.32 341.63 266.42 340.27 268.54 340.95C268.99 341.1 269.2 341.57 269.65 341.71C271.89 342.44 280.68 341.8 283.59 341.8C285.93 341.8 288.76 342.24 291.05 341.79C291.81 341.63 292.19 341.03 292.95 340.88C294.6 340.55 296.42 341.18 298.07 340.85C298.83 340.7 299.21 340.1 299.97 339.95C301.61 339.62 303.43 340.25 305.08 339.92C306.01 339.73 306.7 338.57 307.78 339.08C307.82 339.1 308.25 339.82 308.37 339.93C308.68 339.31 308.99 338.68 309.3 338.06C313.18 338.02 315.42 335.05 318.74 333.94C319.29 333.76 319.89 333.95 320.44 333.76C322.13 333.2 323.62 331.77 325.29 331.13C326.43 330.7 327.6 330.49 328.69 329.86C329.81 329.22 330.41 327.73 331.53 327.09C332.93 326.28 334.31 325.52 335.71 324.72C336.64 324.18 337.14 322.95 338.07 322.41C338.51 322.16 339.01 322.17 339.45 321.91C340.94 321.06 341.73 319.06 343.22 318.2C344.34 317.56 345.83 317.18 346.39 315.76C346.73 314.9 347.38 313.02 347.4 312.09C347.42 311.23 346.86 310.57 346.69 309.76C346.37 308.3 346.78 306.77 346.47 305.33C345.99 303.11 345.44 300.67 344.66 298.55C344.28 297.49 343.32 296.43 343.06 295.27C342.86 294.4 342.98 293.73 342.74 292.94C340.98 291.72 339.64 289.35 339 287.33C343.31 286.47 347.96 287.29 352.31 286.61C354.1 286.33 356.13 286.5 357.92 286.14C359.72 285.78 359.64 285.22 362.14 285.22C363.16 284.21 365.48 283.17 366.83 282.9C367.71 282.73 368.67 283.08 369.53 282.8C370.13 282.61 370.38 282.1 371.04 281.97C371.77 281.82 372.55 282.1 373.27 281.86C374.85 281.35 376.11 279.75 377.66 279.23C378.21 279.05 378.8 279.24 379.35 279.06C379.8 278.91 380.02 278.45 380.46 278.3C381.14 278.07 381.81 278.29 382.47 277.96C382.96 277.71 383.18 277.09 383.74 276.89C384.28 276.71 384.88 276.9 385.43 276.72C386.68 276.3 388.33 275.35 389.48 274.69C390.04 274.37 390.34 273.61 390.91 273.32C391.51 273.01 392.16 273.12 392.75 272.82C394.49 271.93 395.76 270.08 397.46 269.11C402.88 266 407.38 261.96 411.94 257.4C413.97 255.38 417.28 253.08 418.7 250.61C418.96 250.17 418.95 249.67 419.2 249.23C419.84 248.12 421.33 247.52 421.98 246.4C422.78 245 423.54 243.62 424.34 242.22C424.88 241.29 425.98 241.05 426.65 240.32C427.52 239.38 427.77 238.05 428.54 237.06C429.83 237.87 430.72 238.3 430.87 239.87C431.64 240.05 432.41 240.23 433.18 240.41ZM181.42 250.37C181.87 251.14 180.58 251.25 180.02 251.79C182.28 251.84 184.54 251.88 186.8 251.93C187.12 254.29 187.25 255.64 185.16 256.94C183.84 256.23 182.51 255.53 181.19 254.83C179.85 255.17 178.62 254.7 177.43 255.33C173.43 254.38 169.28 253.8 165.3 252.9C164.32 252.67 163.57 251.95 162.72 251.68C167.52 249.85 176.04 251.24 181.42 250.37Z" fill="#C7732F" fill-rule="evenodd" stroke="#C7732F" stroke-width="0.25" stroke-linejoin="round"/> <path d="M334.32 82.11C332.8 82.27 331.62 81.23 330.15 80.69C327.45 79.7 324.05 79.13 321.27 79.72C321.18 80.62 321.09 81.51 320.99 82.41C317.68 83.08 314.07 82.18 310.75 82.91C305.02 84.17 299.15 86.58 294.72 90.64C293.13 92.11 290.29 94.17 289.92 96.23C288.84 96.82 288.14 97.63 288.49 98.89C290.4 99.19 291.97 98.39 293.87 98.35C300.95 98.18 311 97.37 317.53 100.3C316.8 101.73 315.75 103.06 315.23 104.57C314.7 106.11 314.62 107.89 313.95 109.38C313.19 111.06 311.81 112.68 311.14 114.39C310.57 115.86 310.6 117.32 309.7 118.72C309.13 119.61 307.95 120.12 307.41 121.09C306.52 122.68 305.51 124.15 304.57 125.73C304.03 126.65 302.79 127.15 302.29 128.1C302.04 128.58 302.14 129.22 301.89 129.65C301.39 130.5 296.94 134.48 295.83 135.74C295.55 136.05 294.24 136.88 294.24 137.28C294.23 137.78 294.5 137.65 294.62 138.17C291.32 138.43 290.71 141.61 288.09 143.13C287.78 143.31 287.41 143.18 287.08 143.34C286.13 143.82 285.63 145.08 284.71 145.6C282.43 146.9 279.9 147.46 278.61 149.86C274.97 150.81 272.05 153.33 268.52 154.61C267.67 154.92 266.74 154.63 265.89 154.88C265.22 155.08 264.95 155.81 264.31 156.03C262.03 156.81 262.34 155.97 260.68 157.97C258.62 157.95 258.11 158.31 256.36 158.84C255.21 159.19 253.86 158.74 252.68 159.07C252.16 159.21 252.14 159.57 251.71 159.73C250.46 160.19 248.92 159.66 247.65 160.02C247.21 160.15 246.98 160.58 246.54 160.71C244.93 161.19 242.72 160.75 241.04 160.81C236.26 161 228.29 161.52 223.75 160.77C223.12 160.66 222.82 160.25 222.24 160.08C220.78 159.64 219.11 160.14 217.65 159.78C217.13 159.64 217.12 159.3 216.69 159.13C215.68 158.73 214.56 159.14 213.56 158.84C213.12 158.71 212.88 158.29 212.44 158.15C211.46 157.84 210.28 158.24 209.33 157.85C208.54 157.54 207.67 156.66 206.87 156.33C206.17 156.04 205.37 156.27 204.67 156.03C202.57 155.31 203.55 154.55 200.47 155C200.54 154.58 200.85 154.7 200.78 154.31C200.69 153.75 199.47 154.04 199.16 153.87C198.63 153.6 198.84 153.04 198.09 152.98C197.55 152.94 197.5 153.25 197.08 153.44C195.73 152.86 195.45 152.32 193.81 152.33C193.64 151.63 193.71 150.9 193.03 150.4C192.53 150.03 191.84 150.1 191.29 149.81C190.73 149.51 190.38 148.82 189.82 148.51C187.23 147.03 184.95 145.25 182.41 143.71C180.6 142.61 178.98 140.34 177.48 138.84C174.25 135.6 170.13 132.48 167.43 128.78C165.84 126.6 164.33 124.49 162.11 122.74C166.9 119.14 172.71 117.21 178.41 115.57C181.76 114.6 186.47 114.73 189.4 112.92C188.47 110.35 184.58 108.68 182.17 107.48C175.98 104.41 168.47 103.56 161.55 104.65C158.98 105.05 156.38 106.16 153.9 106.37C153.92 105.21 153.86 104.65 153.13 103.73C148.91 102.49 146.25 108.17 142.61 108.29C142.87 105.77 150.75 102 153.13 100.99C162.68 96.98 173.95 96.25 183.76 99.67C186.3 100.56 189.35 101.36 191.44 103.07C190.63 103.53 190.72 103.24 190.94 104.04C195.85 105.94 198.68 110.99 202.75 114.05C203.34 114.5 204.02 114.58 204.64 114.96C206.4 116.03 207.78 117.85 209.59 118.85C211.47 119.89 216.37 121.92 218.45 122.59C233.6 127.52 246.71 125.8 260.38 118.05C264.42 115.76 269.05 112.82 272.01 109.06C279.03 100.17 281.29 91.54 291.07 84.63C291.38 84.08 291.39 84.31 290.72 83.94C293.15 81.76 296.44 80.77 299.4 79.72C300.39 79.37 301.33 78.6 302.37 78.26C308.97 76.15 317.38 76.05 324.02 77.82C326.25 78.41 333.53 79.85 334.32 82.11ZM343.51 88.17C343.57 88.14 343.64 88.11 343.71 88.08C343.7 88.32 343.69 88.56 343.68 88.79C343.62 88.59 343.56 88.38 343.51 88.17ZM343.68 88.79C343.87 88.77 344.06 88.75 344.25 88.73C344.23 88.92 344.21 89.11 344.18 89.3C344.01 89.13 343.85 88.96 343.68 88.79ZM344.18 89.3C344.38 89.26 344.57 89.21 344.77 89.16C344.72 89.34 344.67 89.53 344.62 89.72C344.47 89.58 344.33 89.44 344.18 89.3ZM344.62 89.72C344.8 89.7 344.99 89.68 345.17 89.66C345.15 89.85 345.12 90.05 345.1 90.25C344.94 90.07 344.78 89.9 344.62 89.72ZM345.1 90.25C345.3 90.23 345.49 90.21 345.69 90.19C345.49 90.21 345.3 90.23 345.1 90.25ZM378.27 207.84C382.78 208.61 387.28 208.86 391.6 210.27C395.76 211.63 400.02 213.43 404 215.28C405.49 215.97 406.81 217.28 408.21 217.93C412.07 219.72 416.67 222.17 419.19 225.84C420.63 226.1 420.69 226.46 421.52 227.71C422.16 228.29 422.61 228.3 423.33 228.69C424.5 229.32 426.04 231.13 426.89 232.16C427.53 232.92 427.62 233.75 428.3 234.49C429.78 235.47 433.34 238.59 433.18 240.41C432.41 240.23 431.64 240.05 430.87 239.87C430.72 238.3 429.83 237.87 428.54 237.06C427.77 238.05 427.52 239.38 426.65 240.32C425.98 241.05 424.88 241.29 424.34 242.22C423.54 243.62 422.78 245 421.98 246.4C421.33 247.52 419.84 248.12 419.2 249.23C418.95 249.67 418.96 250.17 418.7 250.61C417.28 253.08 413.97 255.38 411.94 257.4C407.38 261.96 402.88 266 397.46 269.11C395.76 270.08 394.49 271.93 392.75 272.82C392.16 273.12 391.51 273.01 390.91 273.32C390.34 273.61 390.04 274.37 389.48 274.69C388.33 275.35 386.68 276.3 385.43 276.72C384.88 276.9 384.28 276.71 383.74 276.89C383.18 277.09 382.96 277.71 382.47 277.96C381.81 278.29 381.14 278.07 380.46 278.3C380.02 278.45 379.8 278.91 379.35 279.06C378.8 279.24 378.21 279.05 377.66 279.23C376.11 279.75 374.85 281.35 373.27 281.86C372.55 282.1 371.77 281.82 371.04 281.97C370.38 282.1 370.13 282.61 369.53 282.8C368.67 283.08 367.71 282.73 366.83 282.9C365.48 283.17 363.16 284.21 362.14 285.22C359.64 285.22 359.72 285.78 357.92 286.14C356.13 286.5 354.1 286.33 352.31 286.61C347.96 287.29 343.31 286.47 339 287.33C339.64 289.35 340.98 291.72 342.74 292.94C342.98 293.73 342.86 294.4 343.06 295.27C343.32 296.43 344.28 297.49 344.66 298.55C345.44 300.67 345.99 303.11 346.47 305.33C346.78 306.77 346.37 308.3 346.69 309.76C346.86 310.57 347.42 311.23 347.4 312.09C347.38 313.02 346.73 314.9 346.39 315.76C345.83 317.18 344.34 317.56 343.22 318.2C341.73 319.06 340.94 321.06 339.45 321.91C339.01 322.17 338.51 322.16 338.07 322.41C337.14 322.95 336.64 324.18 335.71 324.72C334.31 325.52 332.93 326.28 331.53 327.09C330.41 327.73 329.81 329.22 328.69 329.86C327.6 330.49 326.43 330.7 325.29 331.13C323.62 331.77 322.13 333.2 320.44 333.76C319.89 333.95 319.29 333.76 318.74 333.94C315.42 335.05 313.18 338.02 309.3 338.06C308.99 338.68 308.68 339.31 308.37 339.93C308.25 339.82 307.82 339.1 307.78 339.08C306.7 338.57 306.01 339.73 305.08 339.92C303.43 340.25 301.61 339.62 299.97 339.95C299.21 340.1 298.83 340.7 298.07 340.85C296.42 341.18 294.6 340.55 292.95 340.88C292.19 341.03 291.81 341.63 291.05 341.79C288.76 342.24 285.93 341.8 283.59 341.8C280.68 341.8 271.89 342.44 269.65 341.71C269.2 341.57 268.99 341.1 268.54 340.95C266.42 340.27 264.32 341.63 262.55 339.93C261.38 340.01 260.21 340.09 259.04 340.16C257.23 343.56 259.77 345.01 258.81 348.35C258.81 346.73 258.81 345.11 258.81 343.5C259.31 344.17 259.09 346.66 259.16 348.11C259.32 351.61 259.51 355.07 259.97 358.4C260.13 354.45 260.45 361.95 260.21 362.84C260.8 363.75 261.35 364.6 261.49 365.65C261.52 365.85 261.5 366.5 261.71 366.63C261.9 366.74 262.11 366.44 262.31 366.35C261.36 369.62 265.26 373.01 264.18 376.32C263.58 376.67 262.92 376.78 262.33 377.12C261.58 377.55 261.18 378.53 260.43 378.96C259.41 379.54 258.2 379.81 257.18 380.39C256.25 380.93 255.75 382.16 254.82 382.7C253.2 383.63 251.32 384.14 249.7 385.07C248.79 385.59 248.07 386.72 247.03 387.07C245.77 387.49 244.67 387.56 243.46 388.18C242.75 388.54 242.4 389.33 241.72 389.71C239.33 391.09 236.34 392.77 233.56 392.77C231.79 394.53 228.06 395.34 225.69 396.13C223.55 396.84 221.78 398.27 219.44 398.76C217 399.27 214.33 399.37 212.52 401.18C210.62 401.18 210.27 401.8 208.76 402.1C207.74 402.31 206.6 401.88 205.59 402.21C204.29 402.63 203.78 403.05 202.23 403.05C200.63 403.05 198.47 402.83 197.79 404.4C192.13 403.42 186.23 401.44 180.85 399.56C178.7 398.81 176.64 397.75 174.41 397.19C173.15 396.89 166.29 394.54 165.74 393.78C165.62 393.61 165.74 392.96 165.76 392.77C162.92 393.32 158.21 391.24 156.41 389.03C153.86 389.04 145.39 384.41 143.08 382.68C143.72 381.32 144.55 381.2 146.12 381.17C146.59 381.62 147.05 382.08 147.52 382.53C150.96 382.53 154.02 383.14 157.34 383.46C160.85 383.8 164.57 383.46 168.09 383.46C174.78 383.46 181.23 383.42 187.75 382.54C189.13 382.35 190.54 382.46 191.87 381.96C192.3 381.8 192.53 381.37 192.98 381.23C193.99 380.91 195.12 381.32 196.14 381.11C196.78 380.97 197.06 380.48 197.65 380.3C198.64 379.99 199.8 380.31 200.81 380.15C201.56 380.04 202.1 379.5 202.8 379.33C203.93 379.05 205.22 379.38 206.36 379.04C207.42 378.73 208.41 377.88 209.32 377.51C210.01 377.23 210.8 377.52 211.5 377.28C211.95 377.13 212.17 376.68 212.61 376.52C214.5 375.87 216.65 375.75 218.52 374.9C220.34 374.07 221.96 373.02 223.84 372.35C226.06 371.55 227.76 371.32 229.35 369.43C231.58 369.46 232.77 368.17 234.57 367.22C235.52 366.72 236.72 366.55 237.69 366.06C239.4 365.2 240.97 363.98 242.62 363.03C243.68 362.42 244.29 361.22 245.27 360.53C247.2 359.17 249.67 358.14 251.74 357.09C252.74 356.58 253.34 355.25 254.4 354.7C255.47 354.15 256.77 353.81 257.37 352.56C259.03 349.1 257.21 343.72 256.75 340.16C255.41 329.82 251.48 318.97 246.45 309.72C244.89 306.83 243.28 302.64 240.8 300.7C239.63 299.24 238.47 297.77 237.3 296.31C236.52 297.76 234.58 299.85 232.85 300.42C233.27 298.49 235.26 296.9 236.87 295.74C229.04 284.42 218 274.73 206.21 267.71C202.23 265.34 197.26 261.61 192.64 260.97C190.95 260.11 188.53 259 186.71 258.36C183.96 257.41 179.64 257.05 177.43 255.33C178.62 254.7 179.85 255.17 181.19 254.83C182.51 255.53 183.84 256.23 185.16 256.94C187.25 255.64 187.12 254.29 186.8 251.93C184.54 251.88 182.28 251.84 180.02 251.79C180.58 251.25 181.87 251.14 181.42 250.37C183.72 250.61 186.55 249.79 189.14 250.16C190.12 250.29 191.04 250.75 192.03 250.87C196.56 251.4 201.43 251.2 205.97 251.41C208.82 251.54 211.79 251.2 214.62 251.54C227.48 253.11 240.64 253.93 253.19 256.77C254.94 257.17 256.89 257.07 258.57 257.42C258.75 257.52 258.94 257.61 259.12 257.7C257.41 258.88 255.16 257.62 253.19 257.37C249.93 256.96 246.68 256.21 243.41 255.87C242.2 255.75 240.94 255.97 239.72 255.8C238.28 255.6 237.14 255.29 235.66 255.53C236.17 256.91 235.78 258.32 236.22 259.65C236.36 260.1 236.83 260.32 236.98 260.76C237.16 261.31 236.97 261.91 237.15 262.46C237.3 262.91 237.76 263.12 237.91 263.57C238.09 264.12 237.9 264.72 238.09 265.26C238.43 266.3 239.39 267.22 239.78 268.24C240.22 269.39 240.42 270.55 241.05 271.65C241.48 272.4 242.46 272.8 242.89 273.55C245.05 277.31 248.74 280.3 251.79 283.35C253.42 284.98 254.77 286.24 256.69 287.34C257.36 287.73 257.68 288.59 258.43 288.88C260.67 289.76 262.99 290.16 265.26 290.92C265.71 291.07 265.93 291.53 266.37 291.68C266.92 291.87 267.52 291.67 268.07 291.86C269.41 292.32 270.43 293.8 271.91 294.09C273.94 294.5 285.76 294.5 287.78 294.09C288.44 293.96 288.69 293.45 289.29 293.26C290.22 292.96 291.5 293.17 292.47 293.17C293.66 291.98 294.16 292.18 295.66 291.68C296.68 291.34 297.66 290.48 298.64 289.99C300.88 288.87 303.17 287.89 305.48 287.01C308.31 285.92 310.43 283.16 312.58 281.02C314.27 279.32 316.86 277.62 317.02 275.17C314.73 273.09 303.44 269.58 299.91 268.5C297.68 267.81 294.49 267.53 292.83 265.95C294.59 266.34 296.35 266.73 298.11 267.12C299.89 268.69 308.55 270.49 311.42 271.44C331.31 278.07 354.72 274.9 372.65 263.93C378.13 260.57 382.5 254.32 383.78 248.02C384.23 245.8 383.98 243.29 383.93 241.04C383.86 237.81 382.76 232 379.85 230.05C378.11 225.66 372.64 222.22 368.5 220.47C366.94 219.8 363.29 219.62 362.98 217.92C362.98 217.77 362.98 217.62 362.98 217.47C366.01 215.25 369.04 213.03 372.07 210.81C372.06 211.21 372.28 211.49 372.04 211.9C371.73 212.44 365.37 217.24 364.3 218.13C367.65 219.63 370.98 220.03 374.3 222.1C376.24 218.86 379.45 216.49 381.33 213.23C382.2 211.71 383.56 211.27 382.95 209.24C381.12 208.88 379.49 209.34 378.27 207.84ZM265.12 377.73C266.9 378.88 266.97 379.82 267.88 381.53C268.93 383.48 270.9 384.78 271.83 386.92C267.71 391.77 257.99 397.37 252.3 399.73C251.55 400.04 251.22 400.94 250.53 401.3C249.02 402.08 247.1 402.14 245.57 402.89C244.74 403.3 244.29 404.17 243.51 404.57C243.03 404.82 242.46 404.73 241.98 404.97C241.19 405.35 240.35 406.24 239.51 406.55C237.22 407.38 235.38 407.15 233.85 409.37C233.05 409.46 232.25 409.56 231.45 409.65C231.14 410.03 230.83 410.4 230.52 410.77C227.97 410.38 221.92 410.72 220.46 408.66C219.39 409.21 218.79 409.32 217.63 409C217.4 408.93 217.18 408.86 216.96 408.79C213.39 408.35 209.21 406.42 205.74 407.03C204.96 406.6 204.18 406.18 203.4 405.76C203.63 405.32 203.86 404.89 204.1 404.46C205.5 403.94 206.81 404.24 208.22 403.9C208.92 403.73 209.46 403.19 210.19 403.07C210.92 402.95 211.71 403.2 212.43 402.97C212.88 402.82 213.09 402.35 213.54 402.21C214.26 401.97 215.05 402.25 215.77 402.1C216.43 401.97 216.68 401.46 217.28 401.27C218.22 400.97 219.49 401.18 220.46 401.18C222.68 398.97 226.04 399.15 228.78 397.81C229.11 397.65 229.12 397.24 229.45 397.08C231.32 396.17 233.39 395.67 235.34 395.02C236.63 394.59 237.6 393.45 238.79 392.85C240.62 391.94 242.84 391.53 244.69 390.81C245.43 390.52 245.76 389.66 246.43 389.27C247.95 388.4 249.68 387.85 251.24 387.07C252.81 386.28 254.59 385.36 256 384.35C257.13 383.54 258.02 381.61 259.36 381.17C260.04 380.94 260.91 381.08 261.61 381.08C262.78 379.96 263.95 378.84 265.12 377.73Z" fill="#DF9C5E" fill-rule="evenodd" stroke="#DF9C5E" stroke-width="0.25" stroke-linejoin="round"/> <path d="M373.6 130.88C376.72 134.27 379.19 138.22 382.32 141.61C384.41 143.89 387.58 145.76 389.27 148.14C387.7 149.1 387.83 147.66 385.99 147.51C384.5 148.77 384.27 152.05 383.16 153.85C382.74 154.54 381.69 154.86 381.32 155.62C380.54 157.18 380.03 158.97 379.2 160.54C378.83 161.24 378.09 161.65 377.65 162.29C376.63 163.79 376.1 165.45 375.3 167.03C374.68 168.26 373.54 169.21 372.89 170.49C368.1 170.96 363.41 169.15 358.82 168.06C355.33 167.23 351.64 166.67 348.08 166.16C342.56 165.36 336.39 165.02 330.81 165.26C327.17 165.42 323.55 166.04 319.9 166.01C321.8 164.51 326.44 164.85 328.93 164.82C332.98 164.76 337.05 164.61 341.1 164.84C344.57 165.04 347.9 165.71 351.39 165.28C355.12 160.74 358.95 155.52 361.34 150.2C361.93 148.89 363.3 147.84 363.78 146.55C364.02 145.91 363.87 145.2 364.15 144.59C364.83 143.15 366.32 141.97 367.06 140.47C368.72 137.12 369.5 133.89 371.49 130.69C372.36 131.37 372.49 131.47 373.6 130.88ZM118.1 166.45C120.69 167.22 124.39 172.78 126.37 174.99C131.48 180.7 138.01 187.17 144.37 191.15C145.72 191.99 146.64 193.51 147.99 194.41C151.63 194.01 154.99 192.11 158.35 190.9C162.05 189.57 166.06 188.33 169.98 187.54C179.53 185.64 189.14 184.58 198.96 184.83C201.33 184.89 203.62 185.36 205.97 185.48C211.02 185.74 216.01 186.76 220.93 187.91C222.72 188.33 225.49 188.69 226.51 190.07C226 190.46 225.85 190.86 225.13 190.87C224.33 190.87 223.88 190.2 223.15 190.04C221.08 189.59 217.07 189.15 214.85 189.01C212.09 188.83 209.28 188.51 206.51 188.64C203.66 188.79 200.36 189.62 197.55 189.39C196.12 189.27 194.89 188.67 193.36 188.92C192.63 189.04 192.09 189.58 191.39 189.75C190.55 189.95 189.55 189.61 188.76 189.93C188.26 190.12 188.12 190.75 187.65 190.91C187.15 191.08 186.2 190.91 185.63 191.01C184.92 191.49 184.89 191.95 184.32 192.5C183.39 193.4 178.24 194.94 176.68 195.67C172.2 197.79 168.07 200.49 164.25 203.63C167.39 208.31 174.86 208.74 179.44 211.25C180 211.56 180.37 212.18 180.97 212.48C183.17 213.57 185.56 214.22 187.81 215.03C188.64 215.32 189.57 215.11 190.4 215.34C193.26 216.14 195.96 217.42 198.96 218.01C201.08 218.43 203.28 218.46 205.36 219.08C206 219.27 206.27 219.99 206.94 220.18C210.51 221.22 214.86 220.57 218.46 221.42C221.89 222.23 232.86 223.03 236.33 222.41C239.3 221.87 242.94 219.37 245.32 217.49C246.36 216.68 246.71 215.91 247.95 215.59C246.75 218.17 243.58 219.86 241.22 221.36C234.71 225.52 226.63 228.22 218.94 228.9C215.7 229.19 211.98 228.31 208.94 229.35C211.99 231.2 220 230.68 223.55 230.3C224.9 230.16 226.38 230.35 227.71 230.09C238.04 228.08 246.44 222.78 254.6 216.28C258.86 217.07 262.96 219.67 267.24 220.74C278.93 223.66 293.61 222.11 303.79 215.32C301.26 214.7 295.84 217.48 292.94 218.08C285.42 219.63 277.9 219.29 270.47 217.57C267.53 216.89 263.17 216.12 261.23 213.72C262.93 213.77 264.35 214.75 266.01 215.31C268.44 216.14 271.76 217.12 274.24 217.3C278.06 215.5 282.97 214.68 287.23 213.07C288.07 212.75 288.57 211.98 289.34 211.6C291.68 210.44 294.24 209.96 296.65 209.02C299.45 207.92 302.34 206.14 304.94 204.63C305.5 204.3 305.82 203.62 306.38 203.3C309.8 201.38 317.61 198.54 319.58 195.21C321.78 194.84 324.58 191.54 326.48 190.19C330.88 187.07 334.17 182.37 338.67 179.32C337.98 178.58 337.29 177.84 336.61 177.1C340.98 177.36 348.46 181.84 352.2 184.37C353.53 185.27 354.81 186.46 356.3 186.97C357.62 188.08 358.94 189.19 360.26 190.31C360.94 192.38 363.12 193.75 363.55 196.15C361.52 198.28 359.49 200.41 357.46 202.53C356.61 201.61 355.75 200.68 354.89 199.76C353.67 198.84 352.28 197.43 351.85 195.92C350.54 195.67 347.47 193.82 346.32 192.83C346.07 192.61 345.84 192.08 345.53 191.96C343.98 191.34 344.1 192.56 342.5 190.77C341.42 190.8 340.72 191.14 339.7 191.34C337.98 191.68 336.11 191.31 334.34 191.7C330.11 192.63 325.02 195.61 322.79 199.42C323.2 199.5 323.22 199.69 323.71 199.6C325.08 199.33 326.68 197.85 327.99 197.27C329.76 196.49 331.61 196.13 333.38 195.46C333.71 196.11 333.52 195.99 334.32 196.15C338.28 194.9 344.55 196.46 346.49 200.36C345.83 200.82 345.06 201.06 344.41 201.57C342.88 202.74 341.82 204.72 340.2 205.75C338.59 206.78 336.06 207.9 334.69 209.19C334.09 209.75 333.61 210.61 332.93 211.07C332.08 211.65 331.06 211.96 330.21 212.63C328.89 213.66 327.56 215.21 326.1 215.96C323.88 217.11 321.59 218.34 319.4 219.58C318.84 219.9 318.56 220.6 317.97 220.89C317.23 221.25 316.25 221.07 315.53 221.4C314.26 221.99 313.04 223.39 311.83 224.13C310.33 225.04 308.43 225.31 306.89 226.2C305.32 227.12 303.78 228.13 302.07 228.83C300.4 229.53 298.49 229.67 296.81 230.28C295.83 230.64 295.26 231.83 294.22 232.21C289.95 233.75 285.65 234.55 281.35 235.72C280.18 236.04 279.18 236.87 278 237.12C276.96 237.34 275.84 237.04 274.83 237.29C274.1 237.47 273.47 238.18 272.74 238.44C272.01 238.69 271.29 238.51 270.6 238.69C269.48 238.97 268.45 239.65 267.22 239.91C263.19 240.74 259.1 240.67 255.08 241.41C253.38 241.72 251.74 242.21 249.92 242.39C247.32 242.65 244.6 242.01 242 242.4C238.26 242.96 233.31 243.74 229.42 242.94C228.86 242.83 228.52 242.48 227.93 242.4C225.18 242.04 222.3 242.64 219.53 242.38C217.56 242.19 215.75 241.75 213.9 241.41C209.48 240.59 203.69 240.77 199.49 239.36C197.79 238.79 195.93 237.74 194.15 237.29C187.75 235.67 181.33 234.21 174.92 232.09C173.24 231.54 171.93 230 170.3 229.36C167.38 228.21 164.95 227.7 162.43 225.61C163.82 224.06 165.21 222.52 166.59 220.98C167.42 220.89 168.55 220.78 167.63 219.98C168.28 219.98 169.26 220.31 169.88 220.14C170.97 219.85 169.94 218.93 171.37 218.84C173.34 218.72 172.92 220.04 174.64 218.35C174.99 218.63 175.49 219.22 175.96 219.18C176.41 219.14 176.7 218.42 177.45 218.39C180.11 218.26 182.67 220.06 185.24 219.53C181.63 216.89 177.96 215.53 173.47 215.07C170.94 214.82 168.65 216.35 166.22 215.55C165.44 216.61 164.76 216.78 163.7 217.47C160.73 219.4 157.51 222.11 155.81 225.27C153.82 226.62 153.86 229.93 152.2 231.76C149.7 230.33 147.21 228.9 144.71 227.48C144.63 223.95 147.37 221.5 148.12 218.36C149.26 216.25 152.62 212.84 152.9 211.11C155.24 210.54 157.17 207.59 158.95 205.95C162.21 202.93 165.9 200.32 169.57 197.86C171.5 196.57 174.86 195.41 175.9 193.34C173.06 192.51 170.57 190.76 167.58 190.47C164.94 190.21 162.56 191.3 160.12 192.1C154.35 193.97 148.43 196 143.14 198.99C139.73 198.64 133.35 205.02 129.29 205.63C123.05 199.61 116.82 193.59 110.58 187.57C109.68 187.39 108.77 187.21 107.87 187.03C108.54 185.15 109.97 183.48 110.88 181.66C112.43 178.56 113.52 175.43 114.83 172.23C115.54 170.52 116.36 167.26 118.1 166.45ZM362.98 217.92C363.29 219.62 366.94 219.8 368.5 220.47C372.64 222.22 378.11 225.66 379.85 230.05C380.3 232.6 382.25 235.11 382.79 237.83C384.3 245.32 382.08 254.08 376.27 259.12C374.62 260.56 373.23 262.15 371.02 262.37C370.01 261.43 369.65 260.87 368.22 260.69C366.28 258.34 364.06 256.83 361.67 255.06C361.3 252.76 359.6 250.74 358.52 248.66C355.32 242.53 349.8 234.64 343.44 231.45C342.68 231.57 341.91 231.69 341.15 231.81C342.11 230.64 343.73 230.14 345.31 230.05C346.46 228.96 347.48 228.29 349.05 228.18C349.09 227.79 349.14 227.4 349.18 227C350.67 227.17 350.67 226.99 351.39 225.72C352.5 225.82 352.09 225.99 352.79 224.79C354.09 225.17 354.4 225.15 354.48 223.74C354.79 223.57 355.1 223.4 355.4 223.24C356.95 223.08 359.93 221.03 361.26 219.84C362.01 219.17 362.1 218.44 362.98 217.92ZM322.93 241.12C322.34 242.4 320.62 242.74 319.31 243.38C316.61 244.71 313.78 245.92 311.27 247.58C314.06 256.74 316.07 262.05 323.69 268.49C325.81 270.28 328.54 271.25 330.58 272.83C325.6 274.03 314.87 272.09 309.89 270.52C306.18 269.35 302.09 267.06 298.11 267.12C296.35 266.73 294.59 266.34 292.83 265.95C285.62 263.99 278.41 262.04 271.2 260.08C270.57 259.66 269.95 259.23 269.33 258.81C272.37 257.33 276.53 256.99 279.82 256.16C286.8 254.41 294.05 252.19 300.91 249.75C306.04 247.92 311.07 245.87 316.12 243.83C318.32 242.94 320.69 241.42 322.93 241.12ZM447.98 279.15C449.41 292.09 447.36 305.1 443.02 317.25C441.37 321.86 438.35 326.14 436.95 330.72C435.15 330.79 433.58 334.46 432.43 336.02C431.07 336.89 430.05 338.57 428.94 339.84C426.7 342.4 422.02 345.45 421.52 348.81C420.63 349.19 420.12 348.92 419.61 349.61C416.76 351.17 411.04 356.55 408.3 358.96C407.21 359.92 405.11 362.12 403.76 361.91C402.14 364.22 400.42 364.18 398.72 365.83C397.29 367.21 396.51 367.34 395.12 368.41C393.76 369.45 392.67 370.76 391.12 371.69C388.92 373.01 386.38 374.24 385.05 376.4C384.31 376.14 384.32 376.06 383.58 376.54C383.37 376.66 383.16 376.78 382.95 376.91C381.46 377.85 379.97 378.8 378.47 379.75C378.25 379.79 378.03 379.83 377.8 379.87C374.99 380.49 373.51 383.74 369.62 383.42C369.15 384.07 368.69 384.73 368.22 385.38C367.17 385.09 368.3 385 367.37 384.94C366.73 384.9 366.76 385.94 366.46 386.27C365.95 386.85 364.7 387.25 364.01 387.6C360.82 389.22 355.85 392.69 352.32 391.83C352.17 392.28 352.03 392.73 351.88 393.18C351.6 393.35 351.32 393.52 351.04 393.69C346.31 395.65 341.58 397.62 336.84 399.58C334.72 399.72 329.36 400.87 328.24 402.82C327.41 402.88 326.57 402.95 325.74 403.01C325.64 403.02 325.54 403.04 325.44 403.06C321.89 403.96 318.34 404.86 314.79 405.76C314.6 405.25 314.41 404.73 314.21 404.22C310.47 403.27 306.18 403.72 302.32 402.94C292.42 400.96 283.82 395.96 276.42 389.18C273.85 386.82 269.11 381.4 268.62 378.04C269.95 378.06 273.39 378.15 274.57 378.5C275.16 378.68 275.46 379.22 276.11 379.38C278.38 379.93 282.2 379.72 284.52 379.42C285.44 379.3 285.75 378.75 286.53 378.51C289.52 377.6 293.17 378.71 296.21 377.99C296.86 377.84 297.16 377.29 297.76 377.12C299.17 376.71 300.85 377.37 302.29 377.03C302.93 376.88 303.24 376.35 303.83 376.18C304.81 375.9 305.96 376.29 306.96 376.1C308.82 375.76 310.7 375.52 312.58 375.19C314.33 374.88 315.79 373.85 317.39 373.38C318.38 373.09 319.49 373.55 320.49 373.2C321.52 372.84 322.49 371.98 323.47 371.5C325.2 370.65 327.1 370.13 328.9 369.44C329.64 369.16 330.24 368.45 330.95 368.23C331.5 368.07 332.09 368.26 332.65 368.06C334.32 367.45 335.8 365.95 337.5 365.43C338.05 365.26 338.64 365.46 339.2 365.25C339.96 364.97 340.29 364.07 340.97 363.68C342.23 362.96 343.56 362.52 344.83 361.88C345.45 361.57 345.9 360.69 346.57 360.4C347.21 360.13 347.9 360.35 348.55 360.11C349.31 359.83 349.64 358.92 350.33 358.54C351.35 357.96 352.56 357.7 353.57 357.1C354.68 356.45 355.28 354.97 356.4 354.33C356.84 354.08 357.37 354.15 357.77 353.81C358.22 353.44 358.09 352.84 358.41 352.49C358.75 352.12 362.85 349.3 363.5 349.05C358.67 341.69 351.09 334.79 351.39 325.18C353.84 325.18 356.94 325.69 359.33 325.13C359.97 324.98 360.28 324.45 360.87 324.28C362.01 323.96 363.3 324.51 364.44 324.11C364.89 323.95 365.1 323.48 365.56 323.35C366.68 323.02 368.01 323.52 369.15 323.26C369.79 323.11 370.1 322.58 370.69 322.41C371.67 322.13 372.82 322.53 373.83 322.35C375.84 322 377.29 321.44 379.44 321.44C380.69 320.19 381.13 320.45 382.68 319.9C383.7 319.53 384.62 318.51 385.66 318.2C387.31 317.72 389 317.68 390.63 317.09C391.65 316.72 392.57 315.72 393.61 315.4C395.91 314.69 398.26 313.99 400.45 312.88C401.82 312.19 403.15 310.96 404.55 310.37C405.19 310.1 405.88 310.31 406.53 310.08C408.95 309.21 413.52 305.46 415.79 303.83C416.99 302.97 418.36 302.66 419.62 302.13C420.94 301.57 421.57 299.84 422.8 299.15C424.56 298.16 426.83 297.28 427.37 295.28C430.09 295.1 432.13 292.6 434.02 290.71C438.11 286.62 442.2 282.53 446.29 278.44C446.99 279.16 447.04 279.07 447.98 279.15Z" fill="#B35A1D" fill-rule="evenodd" stroke="#B35A1D" stroke-width="0.25" stroke-linejoin="round"/> <path d="M290.72 83.94C291.39 84.31 291.38 84.08 291.07 84.63C281.29 91.54 279.03 100.17 272.01 109.06C269.05 112.82 264.42 115.76 260.38 118.05C246.71 125.8 233.6 127.52 218.45 122.59C216.37 121.92 211.47 119.89 209.59 118.85C207.78 117.85 206.4 116.03 204.64 114.96C204.02 114.58 203.34 114.5 202.75 114.05C198.68 110.99 195.85 105.94 190.94 104.04C190.72 103.24 190.63 103.53 191.44 103.07C195.12 104.15 201.13 109.21 203.4 112.45C205.97 112.03 207.62 109.99 210.18 109.88C211.53 107.36 220.04 104.89 222.94 103.97C234.64 100.26 246.57 99.31 258.81 99.91C262.2 100.07 270.52 100.59 272.83 102.87C276.19 100.87 276.36 98.97 278.3 95.75C280.37 92.31 287.28 85.33 290.72 83.94ZM343.71 88.08C343.89 88.3 344.07 88.51 344.25 88.73C344.06 88.75 343.87 88.77 343.68 88.79C343.69 88.56 343.7 88.32 343.71 88.08ZM344.25 88.73C344.42 88.87 344.6 89.01 344.77 89.16C344.57 89.21 344.38 89.26 344.18 89.3C344.21 89.11 344.23 88.92 344.25 88.73ZM344.77 89.16C344.9 89.32 345.04 89.49 345.17 89.66C344.99 89.68 344.8 89.7 344.62 89.72C344.67 89.53 344.72 89.34 344.77 89.16ZM345.17 89.66C345.34 89.83 345.51 90.01 345.69 90.19C345.49 90.21 345.3 90.23 345.1 90.25C345.12 90.05 345.15 89.85 345.17 89.66ZM376.52 207.01C376.79 207.29 377.07 207.56 377.34 207.84C377.65 207.84 377.96 207.84 378.27 207.84C379.49 209.34 381.12 208.88 382.95 209.24C383.56 211.27 382.2 211.71 381.33 213.23C379.45 216.49 376.24 218.86 374.3 222.1C370.98 220.03 367.65 219.63 364.3 218.13C365.37 217.24 371.73 212.44 372.04 211.9C372.28 211.49 372.06 211.21 372.07 210.81C373.55 209.54 375.04 208.28 376.52 207.01ZM259.12 257.7C263.14 258.49 267.17 259.29 271.2 260.08C278.41 262.04 285.62 263.99 292.83 265.95C294.49 267.53 297.68 267.81 299.91 268.5C303.44 269.58 314.73 273.09 317.02 275.17C316.86 277.62 314.27 279.32 312.58 281.02C310.43 283.16 308.31 285.92 305.48 287.01C303.17 287.89 300.88 288.87 298.64 289.99C297.66 290.48 296.68 291.34 295.66 291.68C294.16 292.18 293.66 291.98 292.47 293.17C291.5 293.17 290.22 292.96 289.29 293.26C288.69 293.45 288.44 293.96 287.78 294.09C285.76 294.5 273.94 294.5 271.91 294.09C270.43 293.8 269.41 292.32 268.07 291.86C267.52 291.67 266.92 291.87 266.37 291.68C265.93 291.53 265.71 291.07 265.26 290.92C262.99 290.16 260.67 289.76 258.43 288.88C257.68 288.59 257.36 287.73 256.69 287.34C254.77 286.24 253.42 284.98 251.79 283.35C248.74 280.3 245.05 277.31 242.89 273.55C242.46 272.8 241.48 272.4 241.05 271.65C240.42 270.55 240.22 269.39 239.78 268.24C239.39 267.22 238.43 266.3 238.09 265.26C237.9 264.72 238.09 264.12 237.91 263.57C237.76 263.12 237.3 262.91 237.15 262.46C236.97 261.91 237.16 261.31 236.98 260.76C236.83 260.32 236.36 260.1 236.22 259.65C235.78 258.32 236.17 256.91 235.66 255.53C237.14 255.29 238.28 255.6 239.72 255.8C240.94 255.97 242.2 255.75 243.41 255.87C246.68 256.21 249.93 256.96 253.19 257.37C255.16 257.62 257.41 258.88 259.12 257.7ZM240.8 300.7C243.28 302.64 244.89 306.83 246.45 309.72C251.48 318.97 255.41 329.82 256.75 340.16C257.21 343.72 259.03 349.1 257.37 352.56C256.77 353.81 255.47 354.15 254.4 354.7C253.34 355.25 252.74 356.58 251.74 357.09C249.67 358.14 247.2 359.17 245.27 360.53C244.29 361.22 243.68 362.42 242.62 363.03C240.97 363.98 239.4 365.2 237.69 366.06C236.72 366.55 235.52 366.72 234.57 367.22C232.77 368.17 231.58 369.46 229.35 369.43C227.76 371.32 226.06 371.55 223.84 372.35C221.96 373.02 220.34 374.07 218.52 374.9C216.65 375.75 214.5 375.87 212.61 376.52C212.17 376.68 211.95 377.13 211.5 377.28C210.8 377.52 210.01 377.23 209.32 377.51C208.41 377.88 207.42 378.73 206.36 379.04C205.22 379.38 203.93 379.05 202.8 379.33C202.1 379.5 201.56 380.04 200.81 380.15C199.8 380.31 198.64 379.99 197.65 380.3C197.06 380.48 196.78 380.97 196.14 381.11C195.12 381.32 193.99 380.91 192.98 381.23C192.53 381.37 192.3 381.8 191.87 381.96C190.54 382.46 189.13 382.35 187.75 382.54C181.23 383.42 174.78 383.46 168.09 383.46C164.57 383.46 160.85 383.8 157.34 383.46C154.02 383.14 150.96 382.53 147.52 382.53C147.05 382.08 146.59 381.62 146.12 381.17C144.55 381.2 143.72 381.32 143.08 382.68C140.16 381.54 136.53 379.67 134.66 377.1C131.26 376.87 128.16 373.8 125.31 372.03C119.63 368.51 114.35 364.29 108.99 360.23C106.67 358.47 103.94 356.74 102.01 354.57C98.57 351.39 95.12 348.2 91.68 345.01C91.49 344.89 91.3 344.77 91.11 344.64C90.21 343.85 89.99 343.57 89.78 342.5C89.62 342.44 89.46 342.37 89.31 342.3C89.13 342.28 88.94 342.25 88.76 342.23C88.71 342.23 88.66 342.24 88.61 342.25C88.22 341.71 87.83 341.17 87.44 340.63C86.23 340.33 86.36 340.39 86.03 339.23C84.94 338.97 84.85 338.65 84.4 337.59C84.16 337.62 83.93 337.64 83.7 337.67C82.41 336.62 79.97 334.16 79.21 332.67C78.84 331.95 78.54 330.88 77.84 330.13C77.29 329.53 76.38 329.46 75.84 328.85C74.85 327.76 74.06 326.18 73.16 324.98C71.74 323.1 70.19 322.9 69.9 320.29C67.65 319.36 66.85 317.21 65.75 315.15C65.21 314.13 64.09 313.26 63.41 312.29C60.9 308.73 58.11 305.2 56.1 301.36C56.6 298.92 58.57 298.02 59.69 295.97C62.15 291.43 68.86 284.18 73.24 281.17C75.19 281 76.35 278.74 78.09 278.44C78.96 279.51 78.97 280.43 78.94 281.72C81.11 283.54 80.63 284.87 81.85 287.26C83.81 291.08 85.88 294.97 88.68 298.18C90.36 300.09 90.51 302.31 92.81 303.54C93.81 307.5 103.87 317.15 107.49 319.21C108.62 319.84 109.21 321.31 110.33 321.96C112.76 323.36 115.04 324.83 117.47 326.13C119.04 326.97 120.02 328.99 121.63 329.8C122.24 330.11 122.94 330.03 123.57 330.28C125.32 330.99 126.98 332.48 128.83 332.96C130.39 333.37 132.19 333.5 133.68 334.17C134.56 334.58 134.94 335.75 135.9 336.09C137.69 336.73 139.69 337.42 141.51 337.9C142.49 338.16 143.64 337.92 144.61 338.29C145.56 338.65 146.55 339.52 147.59 339.78C150.17 340.42 153.4 340.04 155.94 340.12C156.56 340.13 156.84 340.57 157.41 340.71C158.54 340.98 159.97 340.9 161.08 341.01C161.05 338.24 158.81 338.92 156.39 338.54C152.28 337.9 147.84 337.42 143.87 336.1C141.06 335.17 138.9 333.18 135.83 334.32C135.18 332.12 133.73 331.44 131.62 332.45C130.73 331.6 130.44 331.56 129.29 331.98C128.88 331.46 128.7 330.62 128.25 330.21C127.16 329.23 125.19 329.19 123.92 328.46C123.32 328.11 122.92 327.08 122.2 326.94C121.81 326.87 121.23 327.18 120.87 327.31C120.27 325.28 119.37 324.83 117.36 325.2C116.34 321.87 116.31 324.85 115.27 323.55C114.91 323.09 114.91 322.43 114.56 321.93C113.59 320.57 112.27 320.23 111.05 319.36C109.44 318.21 108.35 315.83 106.59 314.93C105.88 314.56 105.19 314.69 104.5 314.21C102.06 312.5 100.69 309.25 97.72 308.37C97.81 307.92 98.17 307.01 98.09 306.57C97.92 305.64 93.71 302.04 92.81 301.59C92.74 301.04 92.66 300.5 92.58 299.95C91.88 299.56 91.18 299.17 90.48 298.78C90.48 295.88 89.41 296.75 88.15 294.56C87.25 292.98 86.77 290.13 85.33 288.96C84.68 288.44 83.91 288.32 83.23 287.8C82.73 285.3 81.37 283.07 81.83 280.78C79.37 278.79 80.72 280.29 80.32 278.05C80.14 277.03 79.58 277.3 79.96 276.11C81.9 276.31 81.35 278.14 82.07 279.6C83.3 282.04 85.34 282.25 86.03 285.46C86.51 285.41 86.9 284.98 87.22 284.95C88.88 284.79 89.66 288.68 90.02 289.42C90.87 291.12 93.47 292.25 92.58 294.34C94.13 295.15 95.49 296.4 95.85 298.08C94.92 297.77 93.98 297.46 93.05 297.15C92.17 299.23 95.14 303.02 96.79 303.23C96.4 301.43 94.78 300.62 93.98 299.02C95.07 299.38 95.31 299.94 96.09 298.78C97.2 299.9 97.69 300.74 98.44 302.04C99.73 304.29 104.57 308.2 106.61 310.24C107.73 311.36 108.99 313.18 110.36 313.97C113.13 315.55 120.42 319.98 121.8 322.63C122.51 322.24 123.21 321.85 123.91 321.46C123.21 320.6 122.51 319.75 121.8 318.89C123.29 319.9 124.77 320.92 126.25 321.93C125.93 322.55 125.62 323.18 125.31 323.8C128.27 324.68 128.96 326.84 132.56 326.84C133.52 327.8 134.2 328.26 133.96 329.64C135.33 329.13 136.28 328.95 137.47 329.88C134.16 331.61 136.76 332.76 139.11 332.92C139.77 332.26 141.84 329.86 142.89 331.29C142.9 331.31 142.98 332.45 143.08 332.68C145.32 331.15 145.9 332.48 148 332.9C149.35 333.17 150.84 332.66 152.18 332.93C152.94 333.08 153.32 333.69 154.08 333.84C155.39 334.1 156.9 333.52 158.19 333.94C158.78 334.13 159.03 334.64 159.69 334.77C161.97 335.23 163.72 334.15 165.52 335.96C164.23 336.21 162.85 335.95 161.56 336.2C160.9 336.34 160.65 336.84 160.06 337.04C159.03 337.37 157.71 337.14 156.64 337.36C157.9 338.57 160.6 338.06 162.48 338.06C166.85 338.06 171.21 338.06 175.58 338.06C177.48 338.06 179.89 338.55 181.42 337.36C178.76 336.83 179.47 337.25 177.91 335.25C176.07 336.18 174.64 336.38 172.54 335.96C174.68 333.81 177.53 335.31 180.24 334.77C180.9 334.64 181.15 334.13 181.74 333.94C183.17 333.48 184.86 334.13 186.32 333.84C187.08 333.69 187.46 333.08 188.22 332.93C189.24 332.73 190.38 333.16 191.39 332.83C193.98 331.99 196.71 329.74 199.19 332.22C198.25 332.53 197.32 332.84 196.38 333.15C199.19 335.34 199.64 332.41 199.89 330.58C201.44 329.84 202.01 330.36 203.16 331.51C204.1 331.36 205.03 331.2 205.97 331.05C205.99 329.41 206.36 329.45 207.84 328.71C207.79 328.66 206.89 328.08 206.86 327.99C206.76 327.69 208.07 326.54 208.31 326.37C209.89 325.24 211.95 324.21 213.22 326.14C212.16 327.2 210.81 327.68 210.41 328.94C210.85 328.94 210.86 329.13 211.33 328.93C212.1 328.6 212.51 327.5 213.23 327.09C214.46 326.38 215.83 326.08 216.96 325.2C215.99 324.57 215.43 324.12 215.32 323.1C216.46 322 217.95 321.74 219.28 320.98C220.11 320.5 220.55 319.29 221.49 318.98C221.91 318.84 222.37 318.92 222.8 318.89C222.57 319.59 222.33 320.29 222.1 320.99C223.33 321.24 223.89 321.31 224.91 320.53C224.77 318.86 224.23 319.12 223.04 318.19C224.01 317.21 224.7 317.35 225.83 316.77C227.18 316.08 228.5 314.12 229.58 313.04C230 313.04 230.74 313.26 231.15 313.18C232.26 312.96 236.69 307.81 237.76 306.73C238.3 306.2 239.81 304.11 240.57 304.1C240.83 304.1 241.71 304.73 241.97 304.86C241.6 303.23 240.43 302.37 240.8 300.7ZM87.2 277.74C88.35 278.89 90.14 278.99 90.93 280.56C91.26 281.22 91.04 281.89 91.27 282.56C91.52 283.32 92.46 283.62 92.8 284.3C93.38 285.47 92.99 286.3 93.98 287.33C93.59 288.03 93.2 288.73 92.81 289.43C92.36 289 91.05 288.58 90.83 288.18C90.63 287.8 91.15 287.35 91.08 286.93C90.86 285.78 89.39 284.5 90.24 283.59C91.34 284.73 91.32 286 92.58 286.86C92.32 285.66 91.54 283.28 90.24 282.68C89.66 282.41 89.46 282.65 88.84 282.18C89.58 281.44 89.81 281.49 89.54 280.08C88.39 279.86 89.5 280.52 88.59 280.83C87.5 281.2 87.2 278.16 87.2 277.74ZM97.02 287.09C97.38 287.28 97.68 287.2 97.94 287.58C98.42 288.24 98.08 289.16 98.44 289.88C98.92 290.84 100.22 291.33 100.75 292.25C101.06 292.8 101.09 293.35 101.46 293.87C102.02 294.66 103.07 295.15 103.55 295.99C105.83 299.96 109.85 303.19 113.15 306.5C114.06 307.41 115.07 308.97 116.05 309.68C116.54 310.04 116.97 309.88 117.51 310.09C119.13 310.74 120.12 312.7 120.4 314.21C117.85 313.46 119.17 311.91 117.27 310.79C116.8 310.51 116.3 310.61 115.83 310.37C115.28 310.09 111.54 306.6 110.9 305.94C109.51 304.52 108.11 301.49 105.91 302.52C105.21 299.04 102.06 296.22 100.28 293.19C98.97 290.96 96.38 290.5 97.02 287.09ZM105.21 303.23C104.05 302.84 97.92 297.54 97.26 296.21C98.19 296.84 99.13 297.46 100.06 298.08C98.19 296.21 96.32 294.34 94.45 292.47C95.23 292.94 96.01 293.41 96.79 293.87C95.85 292.32 94.92 290.76 93.98 289.2C95.85 289.39 96.27 291.19 97.04 292.69C97.95 294.48 99.76 295.68 100.75 297.4C101.06 297.94 101.09 298.49 101.46 299.02C102.58 300.58 104.34 301.5 105.21 303.23ZM238.47 300.42C236.87 303.62 230.72 308.63 227.95 311.41C227.32 312.03 226.26 312.77 225.93 313.6C225.74 314.07 226.01 314.73 225.51 315.05C225.19 315.24 225.01 315.04 224.67 315.15C224.24 311.97 229.81 310.02 230.75 307.2C228.38 308.38 227.16 311.26 224.89 312.56C223.58 313.32 222.38 313.62 221.87 315.15C222.49 314.84 223.11 314.53 223.74 314.21C222.8 315.15 221.87 316.08 220.93 317.02C220.93 316.4 220.93 315.77 220.93 315.15C218.19 316.52 218.16 319.47 215.54 320.98C214.23 321.73 213.13 321.28 211.81 321.46C211.69 322.04 211.69 322.1 212.05 322.63C210.9 322.62 210.37 322.62 209.48 321.93C210.1 321.44 210.66 321.36 211.33 320.98C212.08 320.55 212.47 319.53 213.23 319.14C213.72 318.89 214.28 319.02 214.75 318.79C215.39 318.47 215.72 317.45 216.36 317.12C216.73 316.93 217.18 317.11 217.56 316.92C218.55 316.41 219.24 315.04 220.24 314.46C220.68 314.21 221.18 314.22 221.62 313.97C222.74 313.32 223.33 311.83 224.45 311.19C227.15 309.64 228.55 307.18 230.53 306.05C231.43 305.53 232.52 305.44 233.32 305.07C235.36 304.13 236.35 301.12 238.47 300.42ZM243.18 306.5C241.2 306.53 240.71 308.54 239.39 309.76C238.74 310.35 237.79 310.58 237.31 311.42C236.89 312.16 236.96 313 236.11 313.5C234.04 314.71 231.94 316.15 230.75 318.42C228.36 317.55 227.89 320.17 225.99 321.14C224.09 322.11 222.55 323.65 220.68 324.72C219.06 325.65 217.19 326.16 215.57 327.09C214.63 327.62 214.14 328.86 213.2 329.4C210.21 331.11 210.03 329.64 207.37 332.92C207.02 332.74 206.25 332.13 205.9 332.08C204.98 331.96 202.68 333.47 202.23 334.32C199.66 333.36 197.55 335.29 195.13 336.1C191.57 337.29 186.16 338.77 182.59 338.06C181.1 339.49 179.11 338.32 178.15 340.16C178.82 340.79 178 340.43 178.63 340.82C179.54 341.38 182.13 340.65 183.16 340.53C186.3 340.16 189.6 340.06 192.52 339.14C195.07 338.34 197.73 337.97 200.29 337.1C202.5 336.34 204.47 334.93 206.54 334.09C207.22 333.82 208.01 334 208.71 333.75C210.11 333.25 211.12 331.72 212.62 331.23C214.57 330.6 216.72 330.03 218.55 329C219.23 328.62 219.54 327.54 220.28 327.13C220.77 326.86 221.35 326.91 221.83 326.66C222.43 326.35 222.79 325.43 223.38 325.14C224.46 324.62 225.7 324.41 226.8 323.78C228.23 322.96 230.8 320.49 232.1 319.3C233.01 318.47 234.28 317.57 234.99 316.56C235.26 316.17 235.23 315.66 235.58 315.33C237.61 313.43 239.51 313.09 241.12 310.52C242.16 308.87 243.82 308.72 243.18 306.5ZM128.82 321.23C129.2 321.73 129.04 322.27 129.32 322.63C129.77 323.23 131.4 323.68 132.09 324.03C131.96 323.64 130.99 322.53 131.27 322.25C132.93 320.59 133.21 323.42 134.52 323.95C135.41 324.3 135.85 324.63 136.4 324.87C137.07 325.15 137.86 324.79 138.54 325.07C138.88 325.21 138.87 325.66 139.21 325.8C139.88 326.08 140.67 325.77 141.36 325.99C141.8 326.14 142.02 326.6 142.47 326.75C144.31 327.36 145.8 327.46 147.29 328.94C145.84 329.23 145.49 329.34 144.72 330.11C143.69 328.75 144.15 327.92 141.91 327.77C142.07 328.24 142.22 328.71 142.38 329.18C140.42 329.16 139.38 328.14 137.7 327.31C138.01 326.99 138.33 326.68 138.64 326.37C136.89 325.54 137.16 326.16 136.3 327.31C135.92 327 135.3 326.14 134.81 325.99C134.16 325.79 133.69 326.42 133.11 326.25C132.25 326.01 131.61 324.78 130.91 324.28C129.65 323.38 127.12 322.93 128.82 321.23ZM209.01 322.4C207.68 323.1 206.36 323.8 205.03 324.5C205.07 324.53 206 325.16 206.01 325.22C206.15 325.68 205.03 325.95 204.81 326.15C203.63 327.22 199.14 329.97 201.06 326.14C199.85 326.54 199.21 326.89 197.79 326.6C198.64 325.94 201.49 324.49 202.58 325.06C202.82 325.18 202.12 325.29 202.7 325.9C204.46 325.52 204.16 325.26 202.93 324.27C205.07 323.81 206.85 321.99 209.01 322.4ZM142.85 324.03C143.68 324.21 144.06 324.64 144.8 324.88C145.65 325.15 146.59 324.78 147.43 325.06C147.88 325.2 148.1 325.67 148.54 325.82C149.39 326.09 150.33 325.72 151.17 325.99C151.62 326.14 151.84 326.61 152.28 326.75C153.71 327.21 155.4 326.56 156.86 326.85C157.52 326.98 157.77 327.49 158.36 327.69C160.08 328.24 161.66 327.29 163.19 328.47C161.41 330.18 158.47 328.84 156.32 328.33C155.63 328.16 154.82 328.41 154.17 328.14C153.69 327.94 153.7 327.48 153.12 327.32C151.9 326.98 150.1 327.58 149.01 327.19C148.33 326.94 146.71 325.83 146.02 325.54C144.57 324.93 143.44 325.82 142.85 324.03ZM197.32 324.27C196.34 325.24 194.2 325.2 194.28 326.37C193.19 326.16 191.72 326.01 190.64 326.47C190.31 326.62 190.32 327.07 189.98 327.19C188.89 327.58 187.1 326.98 185.88 327.32C185.3 327.48 185.31 327.94 184.83 328.14C184.17 328.41 183.37 328.16 182.68 328.33C180.52 328.84 177.58 330.18 175.81 328.47C177.62 327.07 179.93 328.5 181.65 326.84C183.26 327.16 185.07 327.28 186.71 326.75C187.16 326.61 187.37 326.14 187.82 325.99C188.66 325.72 189.61 326.09 190.45 325.82C190.9 325.67 191.11 325.2 191.56 325.06C192.4 324.78 193.35 325.15 194.19 324.88C195.54 324.44 195.73 323.95 197.32 324.27ZM162.95 331.05C167.16 331.05 171.37 331.05 175.58 331.05C176.87 332.34 177.91 331.79 180.02 332.22C179.36 334.42 177.85 333.18 176.04 333.85C173.26 331.18 163.61 333.85 159.91 333.15C160.65 330.82 163.08 333.04 162.95 331.05ZM264.18 376.32C264.5 376.79 264.81 377.26 265.12 377.73C263.95 378.84 262.78 379.96 261.61 381.08C260.91 381.08 260.04 380.94 259.36 381.17C258.02 381.61 257.13 383.54 256 384.35C254.59 385.36 252.81 386.28 251.24 387.07C249.68 387.85 247.95 388.4 246.43 389.27C245.76 389.66 245.43 390.52 244.69 390.81C242.84 391.53 240.62 391.94 238.79 392.85C237.6 393.45 236.63 394.59 235.34 395.02C233.39 395.67 231.32 396.17 229.45 397.08C229.12 397.24 229.11 397.65 228.78 397.81C226.04 399.15 222.68 398.97 220.46 401.18C219.49 401.18 218.22 400.97 217.28 401.27C216.68 401.46 216.43 401.97 215.77 402.1C215.05 402.25 214.26 401.97 213.54 402.21C213.09 402.35 212.88 402.82 212.43 402.97C211.71 403.2 210.92 402.95 210.19 403.07C209.46 403.19 208.92 403.73 208.22 403.9C206.81 404.24 205.5 403.94 204.1 404.46C203.86 404.89 203.63 405.32 203.4 405.76C203.01 405.48 202.62 405.2 202.23 404.92C201.26 405.26 201.37 405.37 200.59 404.69C199.6 404.8 199.4 404.85 198.72 404.22C198.41 404.28 198.1 404.34 197.79 404.4C198.47 402.83 200.63 403.05 202.23 403.05C203.78 403.05 204.29 402.63 205.59 402.21C206.6 401.88 207.74 402.31 208.76 402.1C210.27 401.8 210.62 401.18 212.52 401.18C214.33 399.37 217 399.27 219.44 398.76C221.78 398.27 223.55 396.84 225.69 396.13C228.06 395.34 231.79 394.53 233.56 392.77C236.34 392.77 239.33 391.09 241.72 389.71C242.4 389.33 242.75 388.54 243.46 388.18C244.67 387.56 245.77 387.49 247.03 387.07C248.07 386.72 248.79 385.59 249.7 385.07C251.32 384.14 253.2 383.63 254.82 382.7C255.75 382.16 256.25 380.93 257.18 380.39C258.2 379.81 259.41 379.54 260.43 378.96C261.18 378.53 261.58 377.55 262.33 377.12C262.92 376.78 263.58 376.67 264.18 376.32Z" fill="#E1A67C" fill-rule="evenodd" stroke="#E1A67C" stroke-width="0.25" stroke-linejoin="round"/> <path d="M138.95 252.38C138.11 253.4 138.27 252.98 137 252.73C135.57 254.37 136.69 254.86 137.23 256.7C137.55 255.91 137.86 255.12 138.17 254.34C138.88 255.2 139.6 256.07 140.31 256.94C140.22 257.18 139.88 257.99 139.91 258.26C139.94 258.66 141.75 260.83 141.84 260.78C142.41 260.43 141.86 260.46 142.38 259.94C144.1 262.8 145.91 265.21 148.31 267.61C149.48 268.78 151.42 269.72 151.73 271.2C154.12 271.04 156.23 272.9 158.37 273.78C159.05 274.05 159.84 273.89 160.54 274.12C165.71 275.85 170.2 276.3 175.58 274.9C176.66 275.26 177 275.76 178.15 275.64C180.58 272.75 183.52 272.71 186.53 270.9C187.39 270.38 188.22 268.58 189.2 268.39C190.01 268.23 190.13 268.74 191.01 268.39C191.33 266.43 192.53 266.49 193.68 265.35C194.42 264.62 194.02 264.29 195.28 263.95C194.42 262.42 192.6 262.66 192.64 260.97C197.26 261.61 202.23 265.34 206.21 267.71C218 274.73 229.04 284.42 236.87 295.74C235.26 296.9 233.27 298.49 232.85 300.42C234.58 299.85 236.52 297.76 237.3 296.31C238.47 297.77 239.63 299.24 240.8 300.7C240.43 302.37 241.6 303.23 241.97 304.86C241.71 304.73 240.83 304.1 240.57 304.1C239.81 304.11 238.3 306.2 237.76 306.73C236.69 307.81 232.26 312.96 231.15 313.18C230.74 313.26 230 313.04 229.58 313.04C228.5 314.12 227.18 316.08 225.83 316.77C224.7 317.35 224.01 317.21 223.04 318.19C224.23 319.12 224.77 318.86 224.91 320.53C223.89 321.31 223.33 321.24 222.1 320.99C222.33 320.29 222.57 319.59 222.8 318.89C222.37 318.92 221.91 318.84 221.49 318.98C220.55 319.29 220.11 320.5 219.28 320.98C217.95 321.74 216.46 322 215.32 323.1C215.43 324.12 215.99 324.57 216.96 325.2C215.83 326.08 214.46 326.38 213.23 327.09C212.51 327.5 212.1 328.6 211.33 328.93C210.86 329.13 210.85 328.94 210.41 328.94C210.81 327.68 212.16 327.2 213.22 326.14C211.95 324.21 209.89 325.24 208.31 326.37C208.07 326.54 206.76 327.69 206.86 327.99C206.89 328.08 207.79 328.66 207.84 328.71C206.36 329.45 205.99 329.41 205.97 331.05C205.03 331.2 204.1 331.36 203.16 331.51C202.01 330.36 201.44 329.84 199.89 330.58C199.64 332.41 199.19 335.34 196.38 333.15C197.32 332.84 198.25 332.53 199.19 332.22C196.71 329.74 193.98 331.99 191.39 332.83C190.38 333.16 189.24 332.73 188.22 332.93C187.46 333.08 187.08 333.69 186.32 333.84C184.86 334.13 183.17 333.48 181.74 333.94C181.15 334.13 180.9 334.64 180.24 334.77C177.53 335.31 174.68 333.81 172.54 335.96C174.64 336.38 176.07 336.18 177.91 335.25C179.47 337.25 178.76 336.83 181.42 337.36C179.89 338.55 177.48 338.06 175.58 338.06C171.21 338.06 166.85 338.06 162.48 338.06C160.6 338.06 157.9 338.57 156.64 337.36C157.71 337.14 159.03 337.37 160.06 337.04C160.65 336.84 160.9 336.34 161.56 336.2C162.85 335.95 164.23 336.21 165.52 335.96C163.72 334.15 161.97 335.23 159.69 334.77C159.03 334.64 158.78 334.13 158.19 333.94C156.9 333.52 155.39 334.1 154.08 333.84C153.32 333.69 152.94 333.08 152.18 332.93C150.84 332.66 149.35 333.17 148 332.9C145.9 332.48 145.32 331.15 143.08 332.68C142.98 332.45 142.9 331.31 142.89 331.29C141.84 329.86 139.77 332.26 139.11 332.92C136.76 332.76 134.16 331.61 137.47 329.88C136.28 328.95 135.33 329.13 133.96 329.64C134.2 328.26 133.52 327.8 132.56 326.84C128.96 326.84 128.27 324.68 125.31 323.8C125.62 323.18 125.93 322.55 126.25 321.93C124.77 320.92 123.29 319.9 121.8 318.89C122.51 319.75 123.21 320.6 123.91 321.46C123.21 321.85 122.51 322.24 121.8 322.63C120.42 319.98 113.13 315.55 110.36 313.97C108.99 313.18 107.73 311.36 106.61 310.24C104.57 308.2 99.73 304.29 98.44 302.04C97.69 300.74 97.2 299.9 96.09 298.78C95.31 299.94 95.07 299.38 93.98 299.02C94.78 300.62 96.4 301.43 96.79 303.23C95.14 303.02 92.17 299.23 93.05 297.15C93.98 297.46 94.92 297.77 95.85 298.08C95.49 296.4 94.13 295.15 92.58 294.34C93.47 292.25 90.87 291.12 90.02 289.42C89.66 288.68 88.88 284.79 87.22 284.95C86.9 284.98 86.51 285.41 86.03 285.46C85.34 282.25 83.3 282.04 82.07 279.6C81.35 278.14 81.9 276.31 79.96 276.11C79.58 277.3 80.14 277.03 80.32 278.05C80.72 280.29 79.37 278.79 81.83 280.78C81.37 283.07 82.73 285.3 83.23 287.8C83.91 288.32 84.68 288.44 85.33 288.96C86.77 290.13 87.25 292.98 88.15 294.56C89.41 296.75 90.48 295.88 90.48 298.78C91.18 299.17 91.88 299.56 92.58 299.95C92.66 300.5 92.74 301.04 92.81 301.59C93.71 302.04 97.92 305.64 98.09 306.57C98.17 307.01 97.81 307.92 97.72 308.37C100.69 309.25 102.06 312.5 104.5 314.21C105.19 314.69 105.88 314.56 106.59 314.93C108.35 315.83 109.44 318.21 111.05 319.36C112.27 320.23 113.59 320.57 114.56 321.93C114.91 322.43 114.91 323.09 115.27 323.55C116.31 324.85 116.34 321.87 117.36 325.2C119.37 324.83 120.27 325.28 120.87 327.31C121.23 327.18 121.81 326.87 122.2 326.94C122.92 327.08 123.32 328.11 123.92 328.46C125.19 329.19 127.16 329.23 128.25 330.21C128.7 330.62 128.88 331.46 129.29 331.98C130.44 331.56 130.73 331.6 131.62 332.45C133.73 331.44 135.18 332.12 135.83 334.32C138.9 333.18 141.06 335.17 143.87 336.1C147.84 337.42 152.28 337.9 156.39 338.54C158.81 338.92 161.05 338.24 161.08 341.01C159.97 340.9 158.54 340.98 157.41 340.71C156.84 340.57 156.56 340.13 155.94 340.12C153.4 340.04 150.17 340.42 147.59 339.78C146.55 339.52 145.56 338.65 144.61 338.29C143.64 337.92 142.49 338.16 141.51 337.9C139.69 337.42 137.69 336.73 135.9 336.09C134.94 335.75 134.56 334.58 133.68 334.17C132.19 333.5 130.39 333.37 128.83 332.96C126.98 332.48 125.32 330.99 123.57 330.28C122.94 330.03 122.24 330.11 121.63 329.8C120.02 328.99 119.04 326.97 117.47 326.13C115.04 324.83 112.76 323.36 110.33 321.96C109.21 321.31 108.62 319.84 107.49 319.21C103.87 317.15 93.81 307.5 92.81 303.54C90.51 302.31 90.36 300.09 88.68 298.18C85.88 294.97 83.81 291.08 81.85 287.26C80.63 284.87 81.11 283.54 78.94 281.72C78.97 280.43 78.96 279.51 78.09 278.44C76.35 278.74 75.19 281 73.24 281.17C74.16 278.52 81.97 273.49 84.62 271.85C85.77 271.13 86.94 269.98 88.06 269.17C92.4 266.04 97.87 263.29 102.67 261.07C106.46 259.33 111.04 258.25 115.06 256.94C118.92 255.69 123.32 254.27 127.42 254.36C127.15 253.4 127.66 253.17 128.38 252.49C130.94 254 135.9 251.06 138.95 252.38ZM87.2 277.74C87.2 278.16 87.5 281.2 88.59 280.83C89.5 280.52 88.39 279.86 89.54 280.08C89.81 281.49 89.58 281.44 88.84 282.18C89.46 282.65 89.66 282.41 90.24 282.68C91.54 283.28 92.32 285.66 92.58 286.86C91.32 286 91.34 284.73 90.24 283.59C89.39 284.5 90.86 285.78 91.08 286.93C91.15 287.35 90.63 287.8 90.83 288.18C91.05 288.58 92.36 289 92.81 289.43C93.2 288.73 93.59 288.03 93.98 287.33C92.99 286.3 93.38 285.47 92.8 284.3C92.46 283.62 91.52 283.32 91.27 282.56C91.04 281.89 91.26 281.22 90.93 280.56C90.14 278.99 88.35 278.89 87.2 277.74ZM97.02 287.09C96.38 290.5 98.97 290.96 100.28 293.19C102.06 296.22 105.21 299.04 105.91 302.52C108.11 301.49 109.51 304.52 110.9 305.94C111.54 306.6 115.28 310.09 115.83 310.37C116.3 310.61 116.8 310.51 117.27 310.79C119.17 311.91 117.85 313.46 120.4 314.21C120.12 312.7 119.13 310.74 117.51 310.09C116.97 309.88 116.54 310.04 116.05 309.68C115.07 308.97 114.06 307.41 113.15 306.5C109.85 303.19 105.83 299.96 103.55 295.99C103.07 295.15 102.02 294.66 101.46 293.87C101.09 293.35 101.06 292.8 100.75 292.25C100.22 291.33 98.92 290.84 98.44 289.88C98.08 289.16 98.42 288.24 97.94 287.58C97.68 287.2 97.38 287.28 97.02 287.09ZM105.21 303.23C104.34 301.5 102.58 300.58 101.46 299.02C101.09 298.49 101.06 297.94 100.75 297.4C99.76 295.68 97.95 294.48 97.04 292.69C96.27 291.19 95.85 289.39 93.98 289.2C94.92 290.76 95.85 292.32 96.79 293.87C96.01 293.41 95.23 292.94 94.45 292.47C96.32 294.34 98.19 296.21 100.06 298.08C99.13 297.46 98.19 296.84 97.26 296.21C97.92 297.54 104.05 302.84 105.21 303.23ZM238.47 300.42C236.35 301.12 235.36 304.13 233.32 305.07C232.52 305.44 231.43 305.53 230.53 306.05C228.55 307.18 227.15 309.64 224.45 311.19C223.33 311.83 222.74 313.32 221.62 313.97C221.18 314.22 220.68 314.21 220.24 314.46C219.24 315.04 218.55 316.41 217.56 316.92C217.18 317.11 216.73 316.93 216.36 317.12C215.72 317.45 215.39 318.47 214.75 318.79C214.28 319.02 213.72 318.89 213.23 319.14C212.47 319.53 212.08 320.55 211.33 320.98C210.66 321.36 210.1 321.44 209.48 321.93C210.37 322.62 210.9 322.62 212.05 322.63C211.69 322.1 211.69 322.04 211.81 321.46C213.13 321.28 214.23 321.73 215.54 320.98C218.16 319.47 218.19 316.52 220.93 315.15C220.93 315.77 220.93 316.4 220.93 317.02C221.87 316.08 222.8 315.15 223.74 314.21C223.11 314.53 222.49 314.84 221.87 315.15C222.38 313.62 223.58 313.32 224.89 312.56C227.16 311.26 228.38 308.38 230.75 307.2C229.81 310.02 224.24 311.97 224.67 315.15C225.01 315.04 225.19 315.24 225.51 315.05C226.01 314.73 225.74 314.07 225.93 313.6C226.26 312.77 227.32 312.03 227.95 311.41C230.72 308.63 236.87 303.62 238.47 300.42ZM243.18 306.5C243.82 308.72 242.16 308.87 241.12 310.52C239.51 313.09 237.61 313.43 235.58 315.33C235.23 315.66 235.26 316.17 234.99 316.56C234.28 317.57 233.01 318.47 232.1 319.3C230.8 320.49 228.23 322.96 226.8 323.78C225.7 324.41 224.46 324.62 223.38 325.14C222.79 325.43 222.43 326.35 221.83 326.66C221.35 326.91 220.77 326.86 220.28 327.13C219.54 327.54 219.23 328.62 218.55 329C216.72 330.03 214.57 330.6 212.62 331.23C211.12 331.72 210.11 333.25 208.71 333.75C208.01 334 207.22 333.82 206.54 334.09C204.47 334.93 202.5 336.34 200.29 337.1C197.73 337.97 195.07 338.34 192.52 339.14C189.6 340.06 186.3 340.16 183.16 340.53C182.13 340.65 179.54 341.38 178.63 340.82C178 340.43 178.82 340.79 178.15 340.16C179.11 338.32 181.1 339.49 182.59 338.06C186.16 338.77 191.57 337.29 195.13 336.1C197.55 335.29 199.66 333.36 202.23 334.32C202.68 333.47 204.98 331.96 205.9 332.08C206.25 332.13 207.02 332.74 207.37 332.92C210.03 329.64 210.21 331.11 213.2 329.4C214.14 328.86 214.63 327.62 215.57 327.09C217.19 326.16 219.06 325.65 220.68 324.72C222.55 323.65 224.09 322.11 225.99 321.14C227.89 320.17 228.36 317.55 230.75 318.42C231.94 316.15 234.04 314.71 236.11 313.5C236.96 313 236.89 312.16 237.31 311.42C237.79 310.58 238.74 310.35 239.39 309.76C240.71 308.54 241.2 306.53 243.18 306.5ZM128.82 321.23C127.12 322.93 129.65 323.38 130.91 324.28C131.61 324.78 132.25 326.01 133.11 326.25C133.69 326.42 134.16 325.79 134.81 325.99C135.3 326.14 135.92 327 136.3 327.31C137.16 326.16 136.89 325.54 138.64 326.37C138.33 326.68 138.01 326.99 137.7 327.31C139.38 328.14 140.42 329.16 142.38 329.18C142.22 328.71 142.07 328.24 141.91 327.77C144.15 327.92 143.69 328.75 144.72 330.11C145.49 329.34 145.84 329.23 147.29 328.94C145.8 327.46 144.31 327.36 142.47 326.75C142.02 326.6 141.8 326.14 141.36 325.99C140.67 325.77 139.88 326.08 139.21 325.8C138.87 325.66 138.88 325.21 138.54 325.07C137.86 324.79 137.07 325.15 136.4 324.87C135.85 324.63 135.41 324.3 134.52 323.95C133.21 323.42 132.93 320.59 131.27 322.25C130.99 322.53 131.96 323.64 132.09 324.03C131.4 323.68 129.77 323.23 129.32 322.63C129.04 322.27 129.2 321.73 128.82 321.23ZM209.01 322.4C206.85 321.99 205.07 323.81 202.93 324.27C204.16 325.26 204.46 325.52 202.7 325.9C202.12 325.29 202.82 325.18 202.58 325.06C201.49 324.49 198.64 325.94 197.79 326.6C199.21 326.89 199.85 326.54 201.06 326.14C199.14 329.97 203.63 327.22 204.81 326.15C205.03 325.95 206.15 325.68 206.01 325.22C206 325.16 205.07 324.53 205.03 324.5C206.36 323.8 207.68 323.1 209.01 322.4ZM142.85 324.03C143.44 325.82 144.57 324.93 146.02 325.54C146.71 325.83 148.33 326.94 149.01 327.19C150.1 327.58 151.9 326.98 153.12 327.32C153.7 327.48 153.69 327.94 154.17 328.14C154.82 328.41 155.63 328.16 156.32 328.33C158.47 328.84 161.41 330.18 163.19 328.47C161.66 327.29 160.08 328.24 158.36 327.69C157.77 327.49 157.52 326.98 156.86 326.85C155.4 326.56 153.71 327.21 152.28 326.75C151.84 326.61 151.62 326.14 151.17 325.99C150.33 325.72 149.39 326.09 148.54 325.82C148.1 325.67 147.88 325.2 147.43 325.06C146.59 324.78 145.65 325.15 144.8 324.88C144.06 324.64 143.68 324.21 142.85 324.03ZM197.32 324.27C195.73 323.95 195.54 324.44 194.19 324.88C193.35 325.15 192.4 324.78 191.56 325.06C191.11 325.2 190.9 325.67 190.45 325.82C189.61 326.09 188.66 325.72 187.82 325.99C187.37 326.14 187.16 326.61 186.71 326.75C185.07 327.28 183.26 327.16 181.65 326.84C179.93 328.5 177.62 327.07 175.81 328.47C177.58 330.18 180.52 328.84 182.68 328.33C183.37 328.16 184.17 328.41 184.83 328.14C185.31 327.94 185.3 327.48 185.88 327.32C187.1 326.98 188.89 327.58 189.98 327.19C190.32 327.07 190.31 326.62 190.64 326.47C191.72 326.01 193.19 326.16 194.28 326.37C194.2 325.2 196.34 325.24 197.32 324.27ZM162.95 331.05C163.08 333.04 160.65 330.82 159.91 333.15C163.61 333.85 173.26 331.18 176.04 333.85C177.85 333.18 179.36 334.42 180.02 332.22C177.91 331.79 176.87 332.34 175.58 331.05C171.37 331.05 167.16 331.05 162.95 331.05Z" fill="#EECDB4" fill-rule="evenodd" stroke="#EECDB4" stroke-width="0.25" stroke-linejoin="round"/> <path d="M432.43 336.02C432.42 337.22 432.21 337.76 431.58 338.76C434.72 339.7 436.8 342.16 439.29 344.14C444.93 348.62 450.07 355.33 448.86 363.06C448.04 368.31 444.08 372.55 440.46 376.17C435.69 380.94 427.96 385.43 421.76 387.86C418 389.33 414.45 391.24 410.68 392.68C404.59 395 397.99 397.16 391.69 398.47C387.19 399.4 382.93 401.16 378.42 402.03C369.25 403.8 360.12 405.8 350.9 407.25C341.13 408.78 331.22 409.46 321.45 410.99C320.08 411.2 318.63 410.8 317.27 411.02C310.21 412.12 302.87 412.23 295.74 412.87C291.31 413.28 286.6 412.2 282.2 412.89C275.45 413.94 268.45 412.72 261.61 413.34C256.59 413.8 251.39 413.34 246.18 413.34C243.26 413.34 239.71 413.9 236.84 413.33C236.08 413.17 235.7 412.57 234.94 412.42C233.74 412.18 232.79 412.68 231.67 412.86C229.78 413.15 227.52 412.93 225.61 412.87C224.72 412.84 224.09 412.55 223.25 412.42C221.28 412.11 219.18 412.59 217.19 412.41C214.73 412.18 212.14 411.87 209.69 411.48C208.18 411.25 206.57 411.69 205.05 411.46C195.3 409.93 185.35 409.71 175.59 408.18C163.9 406.35 152.14 404.32 140.6 401.56C123.1 397.39 106.32 391.86 90.26 383.63C87.07 382.01 84.53 379.31 81.67 377.26C77 373.93 71.54 366.12 71.54 360.27C71.54 358.11 72.86 355.49 73.64 353.49C74.1 352.33 74.68 350.73 75.53 349.76C78.59 346.3 81.95 343.57 85.57 340.63C84.94 339.64 84.32 338.66 83.7 337.67C83.93 337.64 84.16 337.62 84.4 337.59C84.85 338.65 84.94 338.97 86.03 339.23C86.36 340.39 86.23 340.33 87.44 340.63C87.83 341.17 88.22 341.71 88.61 342.25C88.66 342.24 88.71 342.23 88.76 342.23C88.94 342.25 89.13 342.28 89.31 342.3C89.46 342.37 89.62 342.44 89.78 342.5C89.99 343.57 90.21 343.85 91.11 344.64C91.08 344.74 91.05 344.83 91.02 344.92C91.71 345.5 90.82 345.42 91.68 345.01C95.12 348.2 98.57 351.39 102.01 354.57C101.52 354.6 101.02 354.63 100.53 354.66C100.84 355.28 101.7 355.98 101.88 356.62C102.37 358.37 101.56 361.43 101.91 363.54C102.33 366.01 104.06 368.69 105.48 370.75C105.94 371.42 106.67 371.73 107.25 372.25C108.07 372.98 108.54 374.1 109.43 374.75C111.7 376.41 114.1 377.87 116.48 379.39C126.48 385.79 142.99 391.59 154.61 394.03C159.12 394.98 163.61 396.04 168.1 397.07C170.08 397.52 171.87 397.46 173.71 398.38C173.94 397.98 174.17 397.59 174.41 397.19C176.64 397.75 178.7 398.81 180.85 399.56C186.23 401.44 192.13 403.42 197.79 404.4C198.1 404.34 198.41 404.28 198.72 404.22C199.4 404.85 199.6 404.8 200.59 404.69C201.37 405.37 201.26 405.26 202.23 404.92C202.62 405.2 203.01 405.48 203.4 405.76C204.18 406.18 204.96 406.6 205.74 407.03C209.21 406.42 213.39 408.35 216.96 408.79C217.18 408.86 217.4 408.93 217.63 409C218.79 409.32 219.39 409.21 220.46 408.66C221.92 410.72 227.97 410.38 230.52 410.77C230.83 410.4 231.14 410.03 231.45 409.65C231.69 410.14 231.92 410.63 232.15 411.12C235.94 412.02 236.39 411.25 239.63 411C239.48 411.23 238.97 411.43 239.14 411.65C239.3 411.86 239.9 411.77 240.1 411.78C241.66 411.87 243.2 411.93 244.76 412.13C248.81 412.66 257.07 412.73 261.14 412.38C263.88 412.14 266.62 412.48 269.33 412.4C272.64 412.31 276.06 411.94 279.38 411.68C281.76 411.49 284.07 411.04 286.4 410.7C287.86 410.49 289.38 410.78 290.83 410.59C293.12 410.28 295.37 409.37 297.61 409.1C299.15 408.91 300.61 409.53 302.06 409.37C302.39 408.91 302.21 408.72 302.77 408.37C303.67 407.81 305.48 407.94 306.53 407.7C309.32 407.08 311.98 406.13 314.79 405.76C318.34 404.86 321.89 403.96 325.44 403.06C325.54 403.04 325.64 403.02 325.74 403.01C326.57 402.95 327.41 402.88 328.24 402.82C329.36 400.87 334.72 399.72 336.84 399.58C337.78 399.75 338.5 399.56 338.93 399.08C345.23 399.57 353.48 396.81 359.68 395.38C374.1 392.07 394.09 387.05 406.05 378.25C409.02 376.07 412.66 374.25 414.89 371.19C415.53 370.32 415.77 369.25 416.35 368.41C416.61 368.03 417.09 367.98 417.36 367.63C420.5 363.46 419.49 357.47 417.16 353.26C418.67 352.28 419.68 351.42 419.61 349.61C420.12 348.92 420.63 349.19 421.52 348.81C422.02 345.45 426.7 342.4 428.94 339.84C430.05 338.57 431.07 336.89 432.43 336.02Z" fill="#606364" fill-rule="evenodd" stroke="#606364" stroke-width="0.25" stroke-linejoin="round"/> <path d="M289.92 96.23C291.82 95.73 293.6 92.87 295.15 91.52C298.03 89.04 301.49 86.86 305.13 85.7C318.94 81.3 332.7 86.19 341.12 97.7C343.29 100.65 346.17 105.42 345.68 109.18C343.64 109.04 341.74 107.29 339.93 106.31C336.46 104.44 332.69 102.7 328.89 101.53C316.24 97.62 303.03 95.95 289.9 97.96C289.91 97.38 289.92 96.81 289.92 96.23ZM188.27 112.45C182.43 114.16 176.53 114.55 170.57 117.03C156.97 122.68 147.8 128.86 137.7 139.58C136.32 137.48 136.82 134.04 137.39 131.62C138.69 126.21 140.39 121.79 143.73 117.31C150.28 108.52 162.35 103.49 173.24 105.42C178.83 106.41 183.77 109.31 188.27 112.45ZM308.92 134.37C313.9 133.57 318.93 134.23 323.73 135.6C325.9 136.21 328.8 137.32 327.96 140.18C327.08 143.17 323.26 142.06 320.99 142C314.69 141.84 308.14 143.2 302.52 146.09C298.29 148.26 294.69 151.22 291.36 154.6C289.96 156.02 289 158.36 287.36 159.47C286.12 160.31 284.16 160.11 283.31 158.77C281.65 156.14 284.48 151.78 285.88 149.55C291.01 141.36 299.39 135.91 308.92 134.37ZM178.89 153.09C188.88 152.11 198.29 154.23 205.96 160.87C207.94 162.59 212.66 166.52 210.95 169.54C210.15 170.95 208.38 171.24 207.01 170.58C205.14 169.68 203.5 168.11 201.72 167.04C198.24 164.96 194.48 163.62 190.57 162.63C185.32 161.3 178.96 161.42 173.72 162.75C171.17 163.39 167.02 165.92 164.45 164.95C161.2 163.72 163.05 160.44 164.85 159C168.63 155.99 174.06 153.57 178.89 153.09ZM399.31 154.23C403.7 154.28 408.27 156.9 412.87 157.2C414.68 157.32 417.48 156.8 418.48 158.43C417.66 159.07 417.34 160.01 416.6 160.64C416.15 161.02 415.67 160.97 415.23 161.29C414 162.16 412.3 164.29 410.73 165.44C403.68 170.62 393.24 174.43 384.58 174.72C383.01 174.77 378.96 174.88 377.57 174.49C376.94 174.31 376.66 173.77 376.08 173.56C375.51 173.35 368.63 172.54 367.75 172.54C365.08 175.2 360.92 174.69 358.4 177.21C357 177.21 348.33 179.07 347.41 179.78C349.71 182.08 355.45 184.03 356.3 186.97C354.81 186.46 353.53 185.27 352.2 184.37C348.46 181.84 340.98 177.36 336.61 177.1C334.53 175.42 327.12 174.43 324.28 174.05C313.09 172.58 301.98 173.02 291.18 176.44C287.39 177.64 280.08 181.81 276.75 181.65C286.78 174.86 298.39 170.28 310.24 167.9C313.21 167.31 316.82 165.89 319.82 166.29C323.39 167.11 327.61 166.17 331.28 166.02C336.26 165.82 341.76 166.22 346.71 166.77C359.26 168.18 371.3 173.62 384.12 173.42C385.67 173.4 387.17 172.89 388.56 172.77C389.63 171.34 390.59 169.76 391.55 168.31C392.22 167.31 392.41 165.89 393.14 164.89C394.03 163.71 394.81 162.85 395.5 161.48C396.7 159.07 397.45 156.16 399.31 154.23ZM360.26 190.31C360.58 190.14 360.89 189.97 361.21 189.81C362.76 191.55 365.02 193.89 364.99 196.23C365.21 198.39 365.43 200.54 365.65 202.7C363.39 204.64 361.13 206.59 358.87 208.54C358.05 207.49 358.29 206.44 357.12 205.74C356.67 204.78 356.49 204.38 356.53 203.4C356.37 203.09 356.22 202.77 356.06 202.46C355.12 201.94 354.87 201.37 354.3 200.36C354.5 200.16 354.69 199.96 354.89 199.76C355.75 200.68 356.61 201.61 357.46 202.53C359.49 200.41 361.52 198.28 363.55 196.15C363.12 193.75 360.94 192.38 360.26 190.31ZM99.13 197.14C104.04 202.03 108.95 206.92 113.86 211.81C115.18 211.85 116.51 211.9 117.83 211.94C117.08 213.74 110.6 213.7 108.71 213.78C101.3 214.09 94.42 213.16 87.72 210.03C85.76 209.11 83.14 208.38 81.75 206.67C82.87 205.09 85.46 204.7 87.23 203.9C89.14 203.04 91.25 202.12 93.01 201.02C94.97 199.79 97.01 197.53 99.13 197.14ZM350.21 201.79C351.06 202.17 351.14 202.28 351.76 203.16C352.14 204.28 352.83 205.69 353 206.9C353.09 207.51 352.78 208.15 352.85 208.69C352.93 209.26 353.44 209.53 353.43 210.18C353.43 212.11 352.21 213.3 352.45 215.32C351.04 217.47 349.45 219.7 348.43 222.1C347.68 223.86 347.23 225.84 346.41 227.48C347.33 227.32 348.26 227.16 349.18 227C349.14 227.4 349.09 227.79 349.05 228.18C347.48 228.29 346.46 228.96 345.31 230.05C343.73 230.14 342.11 230.64 341.15 231.81C335.08 234.91 329 238.02 322.93 241.12C320.69 241.42 318.32 242.94 316.12 243.83C311.07 245.87 306.04 247.92 300.91 249.75C294.05 252.19 286.8 254.41 279.82 256.16C276.53 256.99 272.37 257.33 269.33 258.81C269.95 259.23 270.57 259.66 271.2 260.08C267.17 259.29 263.14 258.49 259.12 257.7C258.94 257.61 258.75 257.52 258.57 257.42C258.65 256.35 258.79 256.1 259.27 255.25C261.45 256.05 262.82 254.75 264.9 254.47C265.37 254.4 265.75 254.79 266.2 254.7C266.78 254.57 267.01 253.97 267.69 253.89C268.16 253.83 268.52 254.3 269 254.21C269.93 254.04 270.93 253.22 271.91 253.03C273.12 252.79 274.4 253.09 275.55 252.82C275.96 252.73 276.18 252.23 276.59 252.07C277.42 251.76 278.5 252.12 279.29 251.88C279.71 251.75 279.89 251.31 280.33 251.16C280.73 251.02 280.78 251.36 281.16 251.28C282.66 250.99 284.03 250.11 285.47 249.73C286.15 249.55 286.99 249.83 287.71 249.54C289.28 248.91 290.74 248.38 292.38 248.01C295.59 247.3 300.75 244 303.69 244.08C305.31 242.79 308.29 241.45 310.24 241.27C318.58 236.67 326.92 232.08 335.25 227.48C340.61 221.9 351.54 219.32 351.95 210.18C352.11 206.7 350.61 204.83 350.21 201.79ZM261.23 213.72C263.17 216.12 267.53 216.89 270.47 217.57C277.9 219.29 285.42 219.63 292.94 218.08C295.84 217.48 301.26 214.7 303.79 215.32C293.61 222.11 278.93 223.66 267.24 220.74C262.96 219.67 258.86 217.07 254.6 216.28C246.44 222.78 238.04 228.08 227.71 230.09C226.38 230.35 224.9 230.16 223.55 230.3C220 230.68 211.99 231.2 208.94 229.35C211.98 228.31 215.7 229.19 218.94 228.9C226.63 228.22 234.71 225.52 241.22 221.36C243.58 219.86 246.75 218.17 247.95 215.59C250.09 213.64 251.64 210.24 252.17 207.41C252.46 205.89 252.32 205.09 253.19 203.74C255.34 207.56 257.2 211.52 261.23 213.72ZM148.12 218.36C147.37 221.5 144.63 223.95 144.71 227.48C147.21 228.9 149.7 230.33 152.2 231.76C153.86 229.93 153.82 226.62 155.81 225.27C155.93 225.54 156.05 225.81 156.17 226.07C155.47 227.71 154.77 229.35 154.07 230.98C154.35 232.14 154.27 232.64 153.83 233.79C152.47 234.75 153.1 236.37 152.43 237.76C149.78 236.36 147.13 234.96 144.48 233.56C144.24 231.38 143.99 229.21 143.75 227.04C143.09 224.01 146.39 221.74 146.77 219.06C147.22 218.83 147.67 218.59 148.12 218.36ZM377.8 379.87C378.03 379.83 378.25 379.79 378.47 379.75C378.25 379.79 378.03 379.83 377.8 379.87ZM351.88 393.18C351.6 393.35 351.32 393.52 351.04 393.69C351.32 393.52 351.6 393.35 351.88 393.18Z" fill="#702E07" fill-rule="evenodd" stroke="#702E07" stroke-width="0.25" stroke-linejoin="round"/> <path d="M389.27 148.14C391.85 149.09 394.16 151.26 396.71 152.47C397.82 152.99 398.8 153.08 399.31 154.23C397.45 156.16 396.7 159.07 395.5 161.48C394.81 162.85 394.03 163.71 393.14 164.89C392.41 165.89 392.22 167.31 391.55 168.31C390.59 169.76 389.63 171.34 388.56 172.77C387.17 172.89 385.67 173.4 384.12 173.42C371.3 173.62 359.26 168.18 346.71 166.77C341.76 166.22 336.26 165.82 331.28 166.02C327.61 166.17 323.39 167.11 319.82 166.29C319.85 166.2 319.88 166.1 319.9 166.01C323.55 166.04 327.17 165.42 330.81 165.26C336.39 165.02 342.56 165.36 348.08 166.16C351.64 166.67 355.33 167.23 358.82 168.06C363.41 169.15 368.1 170.96 372.89 170.49C373.54 169.21 374.68 168.26 375.3 167.03C376.1 165.45 376.63 163.79 377.65 162.29C378.09 161.65 378.83 161.24 379.2 160.54C380.03 158.97 380.54 157.18 381.32 155.62C381.69 154.86 382.74 154.54 383.16 153.85C384.27 152.05 384.5 148.77 385.99 147.51C387.83 147.66 387.7 149.1 389.27 148.14ZM107.87 187.03C108.77 187.21 109.68 187.39 110.58 187.57C116.82 193.59 123.05 199.61 129.29 205.63C133.35 205.02 139.73 198.64 143.14 198.99C142.03 200.74 139.23 201.56 137.43 202.67C132.55 205.68 123.42 211.56 117.83 211.94C116.51 211.9 115.18 211.85 113.86 211.81C108.95 206.92 104.04 202.03 99.13 197.14C102.04 193.77 104.95 190.4 107.87 187.03ZM350.21 201.79C350.61 204.83 352.11 206.7 351.95 210.18C351.54 219.32 340.61 221.9 335.25 227.48C326.92 232.08 318.58 236.67 310.24 241.27C308.29 241.45 305.31 242.79 303.69 244.08C300.75 244 295.59 247.3 292.38 248.01C290.74 248.38 289.28 248.91 287.71 249.54C286.99 249.83 286.15 249.55 285.47 249.73C284.03 250.11 282.66 250.99 281.16 251.28C280.78 251.36 280.73 251.02 280.33 251.16C279.89 251.31 279.71 251.75 279.29 251.88C278.5 252.12 277.42 251.76 276.59 252.07C276.18 252.23 275.96 252.73 275.55 252.82C274.4 253.09 273.12 252.79 271.91 253.03C270.93 253.22 269.93 254.04 269 254.21C268.52 254.3 268.16 253.83 267.69 253.89C267.01 253.97 266.78 254.57 266.2 254.7C265.75 254.79 265.37 254.4 264.9 254.47C262.82 254.75 261.45 256.05 259.27 255.25C258.79 256.1 258.65 256.35 258.57 257.42C256.89 257.07 254.94 257.17 253.19 256.77C240.64 253.93 227.48 253.11 214.62 251.54C211.79 251.2 208.82 251.54 205.97 251.41C201.43 251.2 196.56 251.4 192.03 250.87C191.04 250.75 190.12 250.29 189.14 250.16C186.55 249.79 183.72 250.61 181.42 250.37C176.04 251.24 167.52 249.85 162.72 251.68C160.05 252.92 156.21 251.74 153.35 251.51C148.88 251.17 143.22 251.1 138.95 252.38C135.9 251.06 130.94 254 128.38 252.49C131.79 249.5 135.19 246.51 138.6 243.52C139.92 243.08 141.56 244.29 142.97 244.89C146.09 246.21 151.03 249.02 154.33 248.52C153.05 246.46 150.26 245.34 148.6 243.45C144.52 238.79 143.14 233.11 143.75 227.04C143.99 229.21 144.24 231.38 144.48 233.56C147.13 234.96 149.78 236.36 152.43 237.76C153.1 236.37 152.47 234.75 153.83 233.79C153.5 237.68 154 241.67 155.9 245.1C157.02 247.13 157.77 247.69 158.52 249.92C160.64 251.78 166.29 250.92 168.51 249.45C162.43 243.56 155.68 238.51 159.33 228.81C160.2 226.51 163.75 220.27 166.59 220.98C165.21 222.52 163.82 224.06 162.43 225.61C164.95 227.7 167.38 228.21 170.3 229.36C171.93 230 173.24 231.54 174.92 232.09C181.33 234.21 187.75 235.67 194.15 237.29C195.93 237.74 197.79 238.79 199.49 239.36C203.69 240.77 209.48 240.59 213.9 241.41C215.75 241.75 217.56 242.19 219.53 242.38C222.3 242.64 225.18 242.04 227.93 242.4C228.52 242.48 228.86 242.83 229.42 242.94C233.31 243.74 238.26 242.96 242 242.4C244.6 242.01 247.32 242.65 249.92 242.39C251.74 242.21 253.38 241.72 255.08 241.41C259.1 240.67 263.19 240.74 267.22 239.91C268.45 239.65 269.48 238.97 270.6 238.69C271.29 238.51 272.01 238.69 272.74 238.44C273.47 238.18 274.1 237.47 274.83 237.29C275.84 237.04 276.96 237.34 278 237.12C279.18 236.87 280.18 236.04 281.35 235.72C285.65 234.55 289.95 233.75 294.22 232.21C295.26 231.83 295.83 230.64 296.81 230.28C298.49 229.67 300.4 229.53 302.07 228.83C303.78 228.13 305.32 227.12 306.89 226.2C308.43 225.31 310.33 225.04 311.83 224.13C313.04 223.39 314.26 221.99 315.53 221.4C316.25 221.07 317.23 221.25 317.97 220.89C318.56 220.6 318.84 219.9 319.4 219.58C321.59 218.34 323.88 217.11 326.1 215.96C327.56 215.21 328.89 213.66 330.21 212.63C331.06 211.96 332.08 211.65 332.93 211.07C333.61 210.61 334.09 209.75 334.69 209.19C336.06 207.9 338.59 206.78 340.2 205.75C341.82 204.72 342.88 202.74 344.41 201.57C345.06 201.06 345.83 200.82 346.49 200.36C344.55 196.46 338.28 194.9 334.32 196.15C333.52 195.99 333.71 196.11 333.38 195.46C339.07 192.83 347.11 196.68 350.21 201.79ZM364.99 196.23C367.76 201.59 366.28 209.09 363.47 214.09C362.5 215.81 360.84 217.03 359.74 218.59C361.04 218.71 361.72 217.81 362.98 217.47C362.98 217.62 362.98 217.77 362.98 217.92C362.1 218.44 362.01 219.17 361.26 219.84C359.93 221.03 356.95 223.08 355.4 223.24C355.9 222.63 356.4 222.01 356.9 221.4C355.91 219.21 357.65 217.48 358.11 215.31C358.72 212.49 358.26 208.37 357.12 205.74C358.29 206.44 358.05 207.49 358.87 208.54C361.13 206.59 363.39 204.64 365.65 202.7C365.43 200.54 365.21 198.39 364.99 196.23ZM383.58 376.54C383.37 376.66 383.16 376.78 382.95 376.91C383.16 376.78 383.37 376.66 383.58 376.54ZM325.44 403.06C325.54 403.04 325.64 403.02 325.74 403.01C325.64 403.02 325.54 403.04 325.44 403.06Z" fill="#963D08" fill-rule="evenodd" stroke="#963D08" stroke-width="0.25" stroke-linejoin="round"/> <path d="M418.48 158.43C419 159.51 417.71 160.85 416.99 161.92C414.81 165.13 412.57 168.44 410.11 171.43C405.35 177.24 400.82 183.25 395.62 188.71C389.51 195.11 382.95 200.97 376.52 207.01C375.04 208.28 373.55 209.54 372.07 210.81C369.04 213.03 366.01 215.25 362.98 217.47C361.72 217.81 361.04 218.71 359.74 218.59C360.84 217.03 362.5 215.81 363.47 214.09C366.28 209.09 367.76 201.59 364.99 196.23C365.02 193.89 362.76 191.55 361.21 189.81C360.89 189.97 360.58 190.14 360.26 190.31C358.94 189.19 357.62 188.08 356.3 186.97C355.45 184.03 349.71 182.08 347.41 179.78C348.33 179.07 357 177.21 358.4 177.21C360.92 174.69 365.08 175.2 367.75 172.54C368.63 172.54 375.51 173.35 376.08 173.56C376.66 173.77 376.94 174.31 377.57 174.49C378.96 174.88 383.01 174.77 384.58 174.72C393.24 174.43 403.68 170.62 410.73 165.44C412.3 164.29 414 162.16 415.23 161.29C415.67 160.97 416.15 161.02 416.6 160.64C417.34 160.01 417.66 159.07 418.48 158.43ZM255.53 188.98C257.27 186.36 257.44 188.88 259.74 187.5C261.2 188.54 264.67 188.33 265.35 190.31C263.38 193.21 260.81 195.81 257.88 197.76C256.33 198.79 254.07 199.54 252.96 200.83C248.91 199.81 239.2 198.1 237.21 193.81C238.07 192.66 239.57 190.9 241.04 191.2C242.87 188.75 246.06 190.4 248.05 187.97C250.68 187.97 254.05 186.42 255.53 188.98ZM148.12 218.36C147.67 218.59 147.22 218.83 146.77 219.06C146.39 221.74 143.09 224.01 143.75 227.04C143.14 233.11 144.52 238.79 148.6 243.45C150.26 245.34 153.05 246.46 154.33 248.52C151.03 249.02 146.09 246.21 142.97 244.89C141.56 244.29 139.92 243.08 138.6 243.52C137.37 241.98 134.53 241.4 132.73 240.59C127.61 238.29 122.52 235.61 117.64 232.82C104.93 225.54 92.79 217.38 81.59 207.9C81.18 206.97 81.17 207.38 81.75 206.67C83.14 208.38 85.76 209.11 87.72 210.03C94.42 213.16 101.3 214.09 108.71 213.78C110.6 213.7 117.08 213.74 117.83 211.94C123.42 211.56 132.55 205.68 137.43 202.67C139.23 201.56 142.03 200.74 143.14 198.99C148.43 196 154.35 193.97 160.12 192.1C162.56 191.3 164.94 190.21 167.58 190.47C170.57 190.76 173.06 192.51 175.9 193.34C174.86 195.41 171.5 196.57 169.57 197.86C165.9 200.32 162.21 202.93 158.95 205.95C157.17 207.59 155.24 210.54 152.9 211.11C152.62 212.84 149.26 216.25 148.12 218.36ZM354.89 199.76C354.69 199.96 354.5 200.16 354.3 200.36C354.87 201.37 355.12 201.94 356.06 202.46C356.22 202.77 356.37 203.09 356.53 203.4C356.49 204.38 356.67 204.78 357.12 205.74C358.26 208.37 358.72 212.49 358.11 215.31C357.65 217.48 355.91 219.21 356.9 221.4C356.4 222.01 355.9 222.63 355.4 223.24C355.1 223.4 354.79 223.57 354.48 223.74C354.4 225.15 354.09 225.17 352.79 224.79C352.09 225.99 352.5 225.82 351.39 225.72C350.67 226.99 350.67 227.17 349.18 227C348.26 227.16 347.33 227.32 346.41 227.48C347.23 225.84 347.68 223.86 348.43 222.1C349.45 219.7 351.04 217.47 352.45 215.32C352.21 213.3 353.43 212.11 353.43 210.18C353.44 209.53 352.93 209.26 352.85 208.69C352.78 208.15 353.09 207.51 353 206.9C352.83 205.69 352.14 204.28 351.76 203.16C351.14 202.28 351.06 202.17 350.21 201.79C347.11 196.68 339.07 192.83 333.38 195.46C331.61 196.13 329.76 196.49 327.99 197.27C326.68 197.85 325.08 199.33 323.71 199.6C323.22 199.69 323.2 199.5 322.79 199.42C325.02 195.61 330.11 192.63 334.34 191.7C336.11 191.31 337.98 191.68 339.7 191.34C340.72 191.14 341.42 190.8 342.5 190.77C344.1 192.56 343.98 191.34 345.53 191.96C345.84 192.08 346.07 192.61 346.32 192.83C347.47 193.82 350.54 195.67 351.85 195.92C352.28 197.43 353.67 198.84 354.89 199.76ZM166.59 220.98C163.75 220.27 160.2 226.51 159.33 228.81C155.68 238.51 162.43 243.56 168.51 249.45C166.29 250.92 160.64 251.78 158.52 249.92C157.77 247.69 157.02 247.13 155.9 245.1C154 241.67 153.5 237.68 153.83 233.79C154.27 232.64 154.35 232.14 154.07 230.98C154.77 229.35 155.47 227.71 156.17 226.07C156.05 225.81 155.93 225.54 155.81 225.27C157.51 222.11 160.73 219.4 163.7 217.47C164.76 216.78 165.44 216.61 166.22 215.55C168.65 216.35 170.94 214.82 173.47 215.07C177.96 215.53 181.63 216.89 185.24 219.53C182.67 220.06 180.11 218.26 177.45 218.39C176.7 218.42 176.41 219.14 175.96 219.18C175.49 219.22 174.99 218.63 174.64 218.35C172.92 220.04 173.34 218.72 171.37 218.84C169.94 218.93 170.97 219.85 169.88 220.14C169.26 220.31 168.28 219.98 167.63 219.98C168.55 220.78 167.42 220.89 166.59 220.98Z" fill="#3C0C01" fill-rule="evenodd" stroke="#3C0C01" stroke-width="0.25" stroke-linejoin="round"/> <path d="M419.61 349.61C419.68 351.42 418.67 352.28 417.16 353.26C419.49 357.47 420.5 363.46 417.36 367.63C417.09 367.98 416.61 368.03 416.35 368.41C415.77 369.25 415.53 370.32 414.89 371.19C412.66 374.25 409.02 376.07 406.05 378.25C394.09 387.05 374.1 392.07 359.68 395.38C353.48 396.81 345.23 399.57 338.93 399.08C338.5 399.56 337.78 399.75 336.84 399.58C341.58 397.62 346.31 395.65 351.04 393.69C351.32 393.52 351.6 393.35 351.88 393.18C352.03 392.73 352.17 392.28 352.32 391.83C355.85 392.69 360.82 389.22 364.01 387.6C364.7 387.25 365.95 386.85 366.46 386.27C366.76 385.94 366.73 384.9 367.37 384.94C368.3 385 367.17 385.09 368.22 385.38C368.69 384.73 369.15 384.07 369.62 383.42C373.51 383.74 374.99 380.49 377.8 379.87C378.03 379.83 378.25 379.79 378.47 379.75C379.97 378.8 381.46 377.85 382.95 376.91C383.16 376.78 383.37 376.66 383.58 376.54C384.32 376.06 384.31 376.14 385.05 376.4C386.38 374.24 388.92 373.01 391.12 371.69C392.67 370.76 393.76 369.45 395.12 368.41C396.51 367.34 397.29 367.21 398.72 365.83C400.42 364.18 402.14 364.22 403.76 361.91C405.11 362.12 407.21 359.92 408.3 358.96C411.04 356.55 416.76 351.17 419.61 349.61ZM102.01 354.57C103.94 356.74 106.67 358.47 108.99 360.23C114.35 364.29 119.63 368.51 125.31 372.03C128.16 373.8 131.26 376.87 134.66 377.1C136.53 379.67 140.16 381.54 143.08 382.68C145.39 384.41 153.86 389.04 156.41 389.03C158.21 391.24 162.92 393.32 165.76 392.77C165.74 392.96 165.62 393.61 165.74 393.78C166.29 394.54 173.15 396.89 174.41 397.19C174.17 397.59 173.94 397.98 173.71 398.38C171.87 397.46 170.08 397.52 168.1 397.07C163.61 396.04 159.12 394.98 154.61 394.03C142.99 391.59 126.48 385.79 116.48 379.39C114.1 377.87 111.7 376.41 109.43 374.75C108.54 374.1 108.07 372.98 107.25 372.25C106.67 371.73 105.94 371.42 105.48 370.75C104.06 368.69 102.33 366.01 101.91 363.54C101.56 361.43 102.37 358.37 101.88 356.62C101.7 355.98 100.84 355.28 100.53 354.66C101.02 354.63 101.52 354.6 102.01 354.57Z" fill="#363535" fill-rule="evenodd" stroke="#363535" stroke-width="0.25" stroke-linejoin="round"/> <path d="M162.72 251.68C163.57 251.95 164.32 252.67 165.3 252.9C169.28 253.8 173.43 254.38 177.43 255.33C179.64 257.05 183.96 257.41 186.71 258.36C188.53 259 190.95 260.11 192.64 260.97C192.6 262.66 194.42 262.42 195.28 263.95C194.02 264.29 194.42 264.62 193.68 265.35C192.53 266.49 191.33 266.43 191.01 268.39C190.13 268.74 190.01 268.23 189.2 268.39C188.22 268.58 187.39 270.38 186.53 270.9C183.52 272.71 180.58 272.75 178.15 275.64C177 275.76 176.66 275.26 175.58 274.9C170.2 276.3 165.71 275.85 160.54 274.12C159.84 273.89 159.05 274.05 158.37 273.78C156.23 272.9 154.12 271.04 151.73 271.2C151.42 269.72 149.48 268.78 148.31 267.61C145.91 265.21 144.1 262.8 142.38 259.94C141.86 260.46 142.41 260.43 141.84 260.78C141.75 260.83 139.94 258.66 139.91 258.26C139.88 257.99 140.22 257.18 140.31 256.94C139.6 256.07 138.88 255.2 138.17 254.34C137.86 255.12 137.55 255.91 137.23 256.7C136.69 254.86 135.57 254.37 137 252.73C138.27 252.98 138.11 253.4 138.95 252.38C143.22 251.1 148.88 251.17 153.35 251.51C156.21 251.74 160.05 252.92 162.72 251.68Z" fill="#EECDB4" fill-rule="evenodd" stroke="#EECDB4" stroke-width="0.25" stroke-linejoin="round"/> <path d="M88.76 342.23C88.94 342.25 89.13 342.28 89.31 342.3C89.13 342.28 88.94 342.25 88.76 342.23ZM91.02 344.92C91.24 344.95 91.46 344.98 91.68 345.01C90.82 345.42 91.71 345.5 91.02 344.92ZM216.96 408.79C217.18 408.86 217.4 408.93 217.63 409C217.4 408.93 217.18 408.86 216.96 408.79Z" fill="#878A8C" fill-rule="evenodd" stroke="#878A8C" stroke-width="0.25" stroke-linejoin="round"/> <path d="M91.11 344.64C91.3 344.77 91.49 344.89 91.68 345.01C91.46 344.98 91.24 344.95 91.02 344.92C91.05 344.83 91.08 344.74 91.11 344.64Z" fill="#9D9FA1" fill-rule="evenodd" stroke="#9D9FA1" stroke-width="0.25" stroke-linejoin="round"/> </svg>', 'b': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500" width="500" height="500"> <path d="M302.74 379.14C300.77 378.62 299.8 377.4 298.69 375.79C281.68 343.71 266.57 310.45 249.03 278.65C243.08 267.86 237.73 256.61 232.22 245.61C228.18 237.57 224.89 229.32 218.89 222.45C200.68 201.62 170.08 193.38 144.2 204.29C136.37 207.58 128.89 212.49 122.88 218.54C117.36 224.1 112.54 230.75 109.42 237.96C91.63 279.09 117.52 323.33 161.19 329.83C182.69 333.04 204.47 323.28 218.7 307.5C221.61 304.27 224.33 300.3 226.37 296.46C227.79 293.79 228.72 290.01 231.05 288.02C233.48 287.08 234.58 287.28 236.91 288.18C243.24 298.05 248.07 309.47 253.34 319.95C255.2 323.65 259.65 329.3 259.52 333.56C259.39 337.84 248.21 347.64 244.88 350.58C226.37 366.94 206.16 374.34 182.19 378.6C180.21 378.74 178.23 378.89 176.26 379.03C127.84 381.08 80.52 353.77 62.78 307.38C42.32 253.89 63.23 190.13 115.51 164.04C123.11 160.25 130.65 156.41 139.06 154.36C151.4 151.35 164.43 149.44 177.17 150.8C207.1 154 232.36 165.04 253.22 187.26C266.27 201.18 274.22 219.12 282.4 236.14C292.59 257.32 304.22 277.83 314.45 299.03C323.6 318 333.39 336.72 343.07 355.34C345.29 359.61 353.5 372.08 351.91 376.4C350.41 380.48 343.23 378.95 339.95 379.04C327.65 379.39 315.03 379.71 302.74 379.14Z" fill="#EE675D" fill-rule="evenodd" stroke="#EE675D" stroke-width="0.25" stroke-linejoin="round"/> <path d="M337.92 65.8C337.92 74.15 337.91 82.49 337.91 90.84C337.91 82.49 337.92 74.15 337.92 65.8ZM436.38 150.41C439.43 150.17 444.55 150.05 445.15 154.12C445.66 157.51 442.91 161.64 441.69 164.73C438.42 173.02 434.87 181.25 431.77 189.59C426.43 203.99 420.3 218.2 414.33 232.33C403.45 258.12 393.09 284.4 382.88 310.5C378.31 322.19 373.51 333.72 368.79 345.3C367.32 348.91 365.75 356.47 360.29 354.55C356.45 353.21 354.42 345.84 352.71 342.48C347.38 331.99 341.89 321.59 336.69 311.04C334.63 306.88 330.47 301.94 332.1 297.12C333.91 291.78 336.45 286.55 338.65 281.37C344.62 267.29 350.21 252.94 355.84 238.69C359.75 228.78 363.7 218.9 367.76 209.09C370.61 202.21 372.98 195.06 376.23 188.38C383.35 173.73 396.89 161.1 412.11 155.24C420.01 152.19 427.98 151.06 436.38 150.41Z" transform="translate(0 0.29203)" fill="#5B5B59" fill-rule="evenodd" stroke="#5B5B59" stroke-width="0.25" stroke-linejoin="round"/> <path d="M60.5 121.3C66.13 121.3 71.01 124.02 76.2 126.28C84.18 129.75 92.47 132.29 100.3 136.03C104.98 138.26 110.47 138.54 112.82 143.61C112.3 144.76 112.06 146.48 111.12 147.4C110.07 148.43 102.57 151.98 100.6 153.31C97.41 155.46 94.51 158.02 91.42 160.08C89.91 161.08 88.12 161.45 86.72 162.77C85.27 164.13 84.12 165.91 82.57 167.26C80.18 169.33 76.45 171.13 75.57 174.12C74.2 174.12 72.83 174.12 71.46 174.12C67.73 172.66 68.83 169.56 67.62 166.25C65.93 161.65 66.43 159.99 65.97 155.49C65.87 154.42 64.72 153.33 64.41 152.28C63.4 148.9 61.22 139.77 61.42 136.53C60.41 135.66 58.62 125.59 59.11 123.88C59.37 122.97 60.02 122.11 60.5 121.3ZM279.16 123.52C279.27 128.8 277.33 133.54 276.17 138.57C275.8 140.2 276.25 141.97 275.84 143.59C275.5 144.9 274.37 145.96 274.06 147.3C273.25 150.7 272.95 154.52 272.09 158.23C271.69 160 272.13 161.93 271.61 163.65C271.24 164.87 270.08 165.89 269.76 167.12C268.95 170.17 269.83 171.51 267.35 173.91C261.29 173.91 259.46 169.32 255.43 165.79C254.2 164.71 252.3 164.41 250.97 163.44C248.44 161.61 246.41 159.13 243.91 157.26C241.25 155.28 238.05 153.6 235.27 151.67C231.8 149.27 227.26 148.95 225.16 144.98C225.58 143.46 226.01 141.93 226.43 140.41C232.63 138.08 238.46 134.88 244.61 132.54C250.33 130.36 256.65 128.68 262.21 125.9C264.06 124.97 265.84 124.75 267.64 123.98C272.49 121.91 274 118.9 279.16 123.52ZM191.59 240.41C192.61 243.63 194.93 244.75 196.75 247.34C200.32 252.43 202.26 260.77 201.52 266.89C200.81 272.8 198.9 278.54 195.9 283.73C195.21 284.92 193.55 285.84 192.63 286.9C182.04 299.12 162.89 300.7 149.95 291.36C130.11 277.05 133.12 244.69 155.68 235.7C156.91 235.22 157.96 233.84 159.22 233.44C160.49 233.04 161.92 233.44 163.22 233.25C164.37 233.08 165.35 232.29 166.5 232.14C171.49 231.47 178.76 233.51 183.33 235.43C186.46 236.75 188.43 239.42 191.59 240.41ZM236.91 288.18C234.58 287.28 233.48 287.08 231.05 288.02C230.86 287.83 230.66 287.64 230.47 287.44C231.5 286.98 232.53 286.53 233.56 286.07C234.62 286.37 235.68 286.68 236.73 286.99C236.79 287.38 236.85 287.78 236.91 288.18ZM298.69 375.79C299.8 377.4 300.77 378.62 302.74 379.14C302.36 379.38 301.98 379.62 301.6 379.86C300.31 378.58 299.03 377.31 297.74 376.03C298.06 375.95 298.38 375.87 298.69 375.79ZM182.19 378.6C182.8 378.81 183.41 379.01 184.02 379.22C181.28 379.38 178.54 379.53 175.8 379.68C175.95 379.47 176.1 379.25 176.26 379.03C178.23 378.89 180.21 378.74 182.19 378.6Z" transform="translate(0.29206 -0.47382)" fill="#EEE8E4" fill-rule="evenodd" stroke="#EEE8E4" stroke-width="0.25" stroke-linejoin="round"/> <ellipse cx="176.998" cy="278.452" rx="44.241" ry="44.241" transform="matrix(0.73941 0 0 0.73941 38.98598 59.01274)" fill="#EEE8E4" stroke="#EEE8E4" stroke-width="2"/> </svg>'}}

SITE_IDS = ['aniliberty', 'yummyani', 'animevost', 'animru']
SITE_NAMES = {'yummyani': 'YummyAnime', 'aniliberty': 'AniLiberty', 'animru': 'Anim-RU', 'animevost': 'AnimeVost'}


def _text(h):
  return re.sub(r'\s+', ' ', htmllib.unescape(re.sub(r'<[^>]+>', ' ', h or ''))).strip()


def _utf(r):
  try:
    r.encoding = 'utf-8'
  except Exception:
    pass
  return r.text


def _search_aniliberty(q):
  last = None
  for host in ('aniliberty.top', 'anilibria.top'):
    try:
      r = http_get(f'https://{host}/api/v1/app/search/releases', params={'query': q}, headers=HEADERS, timeout=12, retries=0)
      if r.status_code != 200:
        last = RuntimeError(f'HTTP {r.status_code}')
        continue
      j = r.json()
      if isinstance(j, dict):
        j = j.get('data') or j.get('items') or j.get('list') or []
      out = []
      for d in j or []:
        if not isinstance(d, dict):
          continue
        alias = d.get('alias')
        nm = d.get('name') if isinstance(d.get('name'), dict) else {}
        title = nm.get('main') or nm.get('english') or alias
        if not alias or not title:
          continue
        meta = _anilib_meta(d, f'https://{host}/')
        out.append({'title': title, 'orig': nm.get('english') or '', 'url': f'https://{host}/anime/releases/release/{alias}/episodes',
                    'poster': meta.get('poster'), 'year': d.get('year')})
      return out
    except Cancelled:
      raise
    except Exception as ex:
      last = ex
  raise last or RuntimeError('AniLiberty не відповідає')


def _search_yummy_api(q):
  """Пошук через API сайту (HTML-сторінка пошуку збирається скриптом і часто порожня)."""
  last = None
  for path, params in (('/search', {'q': q, 'limit': 20}), ('/anime', {'q': q, 'limit': 20}), ('/search', {'q': q})):
    try:
      _h, res = _yummy_call('ru.yummyani.me', path, params)
    except Cancelled:
      raise
    except Exception as ex:
      last = ex
      continue
    if isinstance(res, dict):
      res = res.get('list') or res.get('data') or res.get('items') or []
    out = []
    for a in res if isinstance(res, list) else []:
      if not isinstance(a, dict) or not a.get('anime_url') or not a.get('title'):
        continue
      meta = _yummy_meta(a)
      ot = a.get('other_titles')
      out.append({'title': str(a['title']).strip(), 'orig': str(ot[0]) if isinstance(ot, list) and ot else '',
                  'url': 'https://ru.yummyani.me/catalog/item/' + str(a['anime_url']).strip('/'),
                  'poster': meta.get('poster'), 'year': a.get('year') or None})
    if out:
      return out
  if last:
    raise last
  return []


def _search_yummy(q):
  try:
    res = _search_yummy_api(q)
    if res:
      return res
  except Cancelled:
    raise
  except Exception:
    pass
  return _search_yummy_html(q)


def _search_yummy_html(q):
  last = None
  for host in ('old.yummyani.me', 'ru.yummyani.me'):
    try:
      r = http_get(f'https://{host}/search', params={'word': q}, headers=HEADERS, timeout=12, retries=0)
      if r.status_code != 200:
        last = RuntimeError(f'HTTP {r.status_code}')
        continue
      html = _utf(r)
      out, seen = [], set()
      for blk in re.split(r'<div class="anime-column"', html)[1:]:
        blk = blk[:4000]
        m = re.search(r'class="anime-title"[^>]*href="(/catalog/item/[^"]+)"[^>]*>(.*?)</a>', blk, re.S) or \
            re.search(r'href="(/catalog/item/[^"]+)"[^>]*class="anime-title"[^>]*>(.*?)</a>', blk, re.S)
        if not m:
          continue
        slug = m.group(1)
        if slug in seen:
          continue
        seen.add(slug)
        im = re.search(r'<img[^>]+src="([^"]+)"', blk)
        out.append({'title': _text(m.group(2)), 'orig': '', 'url': 'https://ru.yummyani.me' + slug,
                    'poster': _abs(f'https://{host}/', im.group(1)) if im else None, 'year': None})
      return out
    except Cancelled:
      raise
    except Exception as ex:
      last = ex
  raise last or RuntimeError('YummyAnime не відповідає')


_DLE_BAD = re.compile(r'/(user|page|tags?|xfsearch|index\.php|engine|templates|uploads|lastnews|rules|favorites|newposts|statistics)(/|$)|#|\?|/page-\d+-|/\d+-[^/]*-[^/]*/page/', re.I)


def _best_img(html, base):
  """Адреса картинки з першого <img>: lazy-атрибути мають перевагу над заглушками в src."""
  for m in re.finditer(r'<img\b([^>]*)>', html or '', re.I):
    attrs = dict((k.lower(), v) for k, _, v in re.findall(r'([\w:-]+)\s*=\s*(["\'])(.*?)\2', m.group(1), re.S))
    cands = [attrs.get(k) for k in ('data-src', 'data-original', 'data-lazy-src', 'data-lazy')]
    ss = attrs.get('data-srcset') or attrs.get('srcset')
    if ss:
      cands.append(ss.split(',')[0].strip().split(' ')[0])
    cands.append(attrs.get('src'))
    for c in cands:
      if c and not c.startswith('data:') and not re.search(r'(blank|spacer|pixel|loader|placeholder|1x1)', c, re.I):
        return _abs(base, c)
  return None


def _dle_results(html, base, link_re, strict=False):
  """Результати пошуку DLE-сайту: заголовок і постер для кожного посилання на сторінку аніме."""
  items = {}
  order = []
  if 'class="shortstory' in html:  # лише самі результати — без бічних списків «останні оновлення» тощо
    html = '<div class="shortstory"' + '<div class="shortstory"'.join(html.split('class="shortstory"')[1:])
  elif strict:  # немає блоків-результатів — це «нічого не знайдено» (інакше підхопимо бічні списки сайту)
    return []
  elif 'id="dle-content"' in html:
    html = html.split('id="dle-content"', 1)[1]
  for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S | re.I):
    href = _abs(base, m.group(1)).split('#')[0]
    if not re.search(link_re, href):
      continue
    path = urlparse(href).path
    if re.search(r'/page-\d+-', path):
      href = re.sub(r'/page-\d+-', '/', href)
    inner = m.group(2)
    h3 = re.search(r'<h[1-4][^>]*>(.*?)</h[1-4]>', inner, re.S)
    t = _text(h3.group(1)) if h3 else _text(inner)
    it = items.get(href)
    if it is None:
      it = items[href] = {'title': '', 'poster': None}
      order.append(href)
    if t and not re.fullmatch(r'[\d\s.,]+', t) and len(t) > len(it['title']) and not re.search(r'читать далее|подробнее|комментар', t, re.I):
      it['title'] = t
    if not it['poster']:
      it['poster'] = _best_img(inner, base)
    if not it['poster']:  # зображення поруч після посилання (різні шаблони)
      it['poster'] = _best_img(html[m.end():m.end() + 500], base) if re.search(r'/uploads/', html[m.end():m.end() + 500]) else None
  out = []
  for href in order:
    it = items[href]
    if it['title']:
      out.append({'title': it['title'], 'orig': '', 'url': href, 'poster': it['poster'], 'year': None})
  return out


def _search_animru(q):
  last = None
  for host in ('anim-ru.net',):
    try:
      r = http_post(f'https://{host}/index.php?do=search', data={'do': 'search', 'subaction': 'search', 'story': q, 'search_start': 0, 'full_search': 0, 'result_from': 1},
                    headers={**HEADERS, 'Referer': f'https://{host}/'}, timeout=12, retries=0)
      if r.status_code != 200:
        last = RuntimeError(f'HTTP {r.status_code}')
        continue
      res = _dle_results(_utf(r), f'https://{host}/', r'^https?://[^/]*anim-ru[^/]*/(?:page-\d+-)?\d+-[^/]+\.html$')
      for it in res:
        it['title'] = re.split(r'\s*\[', it['title'])[0].strip() or it['title']
      return res
    except Cancelled:
      raise
    except Exception as ex:
      last = ex
  raise last or RuntimeError('Anim-RU не відповідає')


def _search_vost_api(q):
  last = None
  for api in ('https://api.animevost.org/v1/search', 'https://animevost.org/v1/search'):
    try:
      r = http_post(api, data={'name': q}, headers=HEADERS, timeout=12, retries=0)
      j = r.json()
      out = []
      for d in (j.get('data') or []) if isinstance(j, dict) else []:
        if not isinstance(d, dict) or not d.get('id') or not d.get('title'):
          continue
        t = re.sub(r'\s*\[[^\]]*\]', '', htmllib.unescape(str(d['title']))).strip()
        ru, _, orig = t.partition(' / ')
        out.append({'title': ru.strip() or t, 'orig': orig.strip(), 'url': f"https://animevost.org/tip/tv/{d['id']}-anime.html",
                    'poster': _abs('https://animevost.org/', d.get('urlImagePreview')) or f"https://media.aniland.org/img/{d['id']}.jpg", 'year': d.get('year') or None})
      return out
    except Cancelled:
      raise
    except Exception as ex:
      last = ex
  raise last or RuntimeError('API AnimeVost не відповідає')


def _search_vost(q):
  last, diag = None, ''
  for host in ('animevost.org', 'vost.pw'):
    try:
      r = http_post(f'https://{host}/index.php?do=search', data={'do': 'search', 'subaction': 'search', 'story': q, 'search_start': 0, 'full_search': 0, 'result_from': 1},
                    headers={**HEADERS, 'Referer': f'https://{host}/'}, timeout=12, retries=0)
      if r.status_code != 200:
        last = RuntimeError(f'HTTP {r.status_code}')
        continue
      html = _utf(r)
      raw = _dle_results(html, f'https://{host}/', r'^https?://[^/]+/tip/[^/]+/\d+-[^/]+\.html$', strict=True)
      out = []
      for it in raw:
        t = re.sub(r'\s*\[[^\]]*\]', '', it['title']).strip()
        ru, _, orig = t.partition(' / ')
        it.update(title=ru.strip() or t, orig=orig.strip())
        out.append(it)
      if out:
        return out
      if not re.search(r'не дал|ничего не найден|найдено 0|найдено\s+0', html, re.I):
        m = re.search(r'<title>(.*?)</title>', html, re.S)
        diag = f'сторінка без результатів ({len(html)} байт, «{_text(m.group(1))[:50] if m else "?"}»)'
      break
    except Cancelled:
      raise
    except Exception as ex:
      last = ex
  try:  # запасний шлях — офіційне API сайту
    res = _search_vost_api(q)
    if res:
      return res
  except Cancelled:
    raise
  except Exception as ex:
    last = last or ex
  if diag:
    raise RuntimeError(diag + (f'; API: {last}' if last else ''))
  if last and not diag:
    raise last
  return []


SEARCHERS = {'aniliberty': _search_aniliberty, 'yummyani': _search_yummy, 'animru': _search_animru, 'animevost': _search_vost}

# ─── порівняння назв ───

_ORD = {'первый': 1, 'второй': 2, 'третий': 3, 'четвертый': 4, 'пятый': 5, 'шестой': 6, 'седьмой': 7,
        'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5}
_ROM = {'ii': 2, 'iii': 3, 'iv': 4, 'v': 5}


def _season_base(name):
  s = (name or '').lower().replace('ё', 'е').replace('’', "'")
  season = 1
  mtv = re.search(r'(?<![a-zа-я0-9])(?:тв|tv)\s*[-–]?\s*([2-9])(?!\d)', s)  # «ТВ-4», «TV-4», «[ТВ-4]»
  s = re.sub(r'\[[^\]]*\]', ' ', s)
  s = re.sub(r'(?<![a-zа-я0-9])(?:тв|tv)\s*[-–]?\s*[2-9](?!\d)', ' ', s)
  s = re.sub(r'\b(?:(?:часть|part|cour)\s*\d+|\d+\s*(?:-?я\s*)?(?:часть|part|cour))\b', ' ', s)  # «2 часть» — той самий сезон
  pats = [
      (r'\b(первый|второй|третий|четвертый|пятый|шестой|седьмой)\s+сезон\b', lambda m: _ORD.get(m.group(1), 1)),
      (r'\bсезон\s+(первый|второй|третий|четвертый|пятый)\b', lambda m: _ORD.get(m.group(1), 1)),
      (r'\b(\d+)\s*-?\s*(?:й|ой|ый)?\s*сезон\b', lambda m: int(m.group(1))),
      (r'\bсезон\s*(\d+)\b', lambda m: int(m.group(1))),
      (r'\b(\d+)(?:st|nd|rd|th)\s+season\b', lambda m: int(m.group(1))),
      (r'\b(first|second|third|fourth|fifth)\s+season\b', lambda m: _ORD.get(m.group(1), 1)),
      (r'\bseason\s*(\d+)\b', lambda m: int(m.group(1))),
      (r'\bs(\d)\b', lambda m: int(m.group(1))),
  ]
  for pat, fn in pats:
    m = re.search(pat, s)
    if m:
      try:
        season = fn(m)
      except Exception:
        pass
      s = s[:m.start()] + ' ' + s[m.end():]
      break
  s = re.sub(r'[()«»"\'.,:;!?~\-–—_/]+', ' ', s)
  s = re.sub(r'\s+', ' ', s).strip()
  if season == 1 and mtv:
    season = int(mtv.group(1))
  if season == 1:
    m = re.search(r'\s(ii|iii|iv|v)$', s)
    if m and len(s) > 6:
      season = _ROM[m.group(1)]
      s = s[:m.start()]
    else:
      m = re.search(r'\s([2-9])$', s)
      if m and len(s) > 4:
        season = int(m.group(1))
        s = s[:m.start()]
  s = re.sub(r'\b(tv|тв|сериал|season|сезон)\b', ' ', s)
  base = re.sub(r'[^0-9a-zа-я]+', '', s)
  return base, season


class _Keys(list):
  weak = ()
  plain = ()


def _fuzzy(ba, bb):
  import difflib
  la, lb = len(ba), len(bb)
  return min(la, lb) >= 8 and abs(la - lb) <= max(la, lb) * 0.15 and ba[:3] == bb[:3] and difflib.SequenceMatcher(None, ba, bb).ratio() >= 0.9


def title_keys(*names):
  keys = _Keys()
  weak, plain = [], []
  for n in names:
    raw = [p for p in re.split(r'\s+/\s+|\s+\|\s+', n or '')]
    parts = [_season_base(p) for p in raw]
    mx = max([sn for _, sn in parts] or [1])  # «Назва / Original (TV-4)»: сезон з однієї частини діє на всю назву
    for p, (b, sn) in zip(raw, parts):
      sn = sn if sn > 1 else mx
      if len(b) >= 2 and (b, sn) not in keys:
        keys.append((b, sn))
      if ':' in p:  # «Назва: довгий підзаголовок» — основа до двокрапки може бути єдиною назвою на іншому сайті
        hb = _season_base(p.split(':', 1)[0])[0]
        if len(hb) >= 10 and (hb, sn) not in weak:
          weak.append((hb, sn))
      elif len(b) >= 2 and (b, sn) not in plain:
        plain.append((b, sn))
  keys.weak, keys.plain = weak, plain
  return keys


def _keys_match(a, b):
  for ba, sa in a:
    for bb, sb in b:
      if sa != sb:
        continue
      if ba == bb or _fuzzy(ba, bb):
        return True
  # слабкі ключі (основа до двокрапки) порівнюються лише з назвами без підзаголовка — щоб «Fate: A» не склеювалось з «Fate: B»
  for x, y in ((a, b), (b, a)):
    for wb, ws in getattr(x, 'weak', ()):
      for pb, ps in getattr(y, 'plain', ()):
        if ws == ps and (wb == pb or _fuzzy(wb, pb)):
          return True
  return False


def group_results(items, order):
  """items: [{site,title,orig,url,poster,year,rank}] → групи однакових аніме (по одному запису на сайт)."""
  n = len(items)
  keys = [title_keys(x['title'], x.get('orig')) for x in items]
  par = list(range(n))

  def find(i):
    while par[i] != i:
      par[i] = par[par[i]]
      i = par[i]
    return i
  for i in range(n):
    for j in range(i + 1, n):
      if items[i]['site'] != items[j]['site'] and find(i) != find(j) and _keys_match(keys[i], keys[j]):
        par[find(j)] = find(i)
  groups = {}
  for i in range(n):
    groups.setdefault(find(i), []).append(items[i])
  rank = {s: i for i, s in enumerate(order)}
  res = []
  for g in groups.values():
    g.sort(key=lambda x: (rank.get(x['site'], 99), x.get('rank', 0)))
    best = {}
    for x in g:  # один запис на сайт
      best.setdefault(x['site'], x)
    lst = list(best.values())
    head = next((x for x in lst if x.get('poster')), lst[0])
    res.append({
        'id': hashlib.sha1(lst[0]['url'].encode()).hexdigest()[:10],
        'title': lst[0]['title'], 'orig': next((x['orig'] for x in lst if x.get('orig')), ''),
        'poster': head.get('poster'), 'year': next((x['year'] for x in lst if x.get('year')), None),
        'items': [{'site': {'id': x['site'], 'name': SITE_NAMES.get(x['site'], x['site'])}, 'url': x['url'], 'title': x['title'],
                   'poster': x.get('poster')} for x in lst],
        '_score': (min(x.get('rank', 0) for x in g) - (len(lst) - 1) * 1.5),
    })
  res.sort(key=lambda g: g['_score'])
  for g in res:
    g.pop('_score', None)
  return res


SEARCHES = {}
SEARCH_CACHE = {}


def _site_order():
  try:
    o = [s for s in SETTINGS.get('site_order', []) if s in SITE_IDS]
  except Exception:
    o = []
  return o + [s for s in SITE_IDS if s not in o]


def _safe_info(u):
  try:
    fetch_info(u, cache=True, bg=True)
  except Exception:
    pass


def api_search_start(q):
  _cancel_browse()
  q = (q or '').strip()
  if len(q) < 2:
    raise ValueError('Введіть хоча б 2 символи.')
  if net_state()['state'] == 'offline':
    raise ValueError('Немає інтернету.')
  now = time.time()
  for t in [t for t, v in SEARCHES.items() if now - v['t'] > 900]:
    del SEARCHES[t]
  tid = hashlib.sha1(f'{q}{now}'.encode()).hexdigest()[:10]
  for old in SEARCHES.values():  # попередні пошуки більше нікому не потрібні — зупиняємо їхні запити
    ev = old.get('cancel')
    if ev is not None:
      ev.set()
  S_ = {'q': q, 't': now, 'cancel': threading.Event(), 'sites': {s: {'state': 'run', 'items': [], 'err': ''} for s in SITE_IDS}}
  SEARCHES[tid] = S_

  def run(sid):
    _tl.cancel = S_['cancel']
    try:
      ck = (sid, q.lower())
      c = SEARCH_CACHE.get(ck)
      if c and time.time() - c[0] < 600:
        lst = c[1]
      else:
        lst = SEARCHERS[sid](q)
        if lst:  # порожню відповідь не кешуємо: сайт міг тимчасово відмовити
          SEARCH_CACHE[ck] = (time.time(), lst)
        if len(SEARCH_CACHE) > 60:
          for k in sorted(SEARCH_CACHE, key=lambda k: SEARCH_CACHE[k][0])[:20]:
            SEARCH_CACHE.pop(k, None)
      for x in lst[:14]:  # обкладинки підтягуємо наперед у фоні — до моменту, коли їх запросить сторінка
        if x.get('poster') and net_state()['state'] != 'cell':
          _IMG_POOL.submit(img_get, x['poster'])
      S_['sites'][sid].update(items=[{**x, 'site': sid, 'rank': i} for i, x in enumerate(lst)], state='ok')
    except Exception as ex:
      S_['sites'][sid].update(state='err', err=(str(ex) or type(ex).__name__)[:160])
  for sid in SITE_IDS:
    threading.Thread(target=run, args=(sid,), daemon=True).start()
  return {'task': tid}


def api_search_status(tid):
  S_ = SEARCHES.get(tid)
  if not S_:
    raise KeyError('Пошук не знайдено')
  allitems = []
  for sid, v in S_['sites'].items():
    allitems += v['items']
  done = all(v['state'] != 'run' for v in S_['sites'].values())
  if not done and time.time() - S_['t'] > 25:  # занадто довго — віддаємо, що є
    for v in S_['sites'].values():
      if v['state'] == 'run':
        v['state'], v['err'] = 'err', 'Сайт відповідає занадто довго'
    done = True
  gsig = (len(allitems), tuple(_site_order()))
  if S_.get('gsig') != gsig:  # групування — дорога операція; рахуємо лише коли додались результати
    S_['gsig'], S_['groups'] = gsig, group_results(allitems, _site_order())
  groups = S_['groups']
  if done and not S_.get('pre') and net_state()['state'] == 'wifi':  # перші два результати відкриються миттєво
    S_['pre'] = True
    for g in groups[:2]:
      threading.Thread(target=lambda u=g['items'][0]['url']: _safe_info(u), daemon=True).start()
  return {'done': done, 'q': S_['q'],
          'sites': {sid: {'name': SITE_NAMES.get(sid, sid), 'state': v['state'], 'n': len(v['items']), 'err': v['err']} for sid, v in S_['sites'].items()},
          'groups': groups}


def _query_variants(*names):
  """Варіанти пошукового запиту: повна назва, без слова «сезон»/номера, коротка основа."""
  out = []

  def add(x):
    x = re.sub(r'\s+', ' ', x or '').strip(' -–—:,.')
    if len(x) >= 2 and x.lower() not in [o.lower() for o in out]:
      out.append(x)
  for n in names:
    if not n:
      continue
    add(n)
    t = re.sub(r'\[[^\]]*\]', ' ', n)
    t = re.sub(r'\(\s*(?:первый|второй|третий|четвертый|пятый|шестой|седьмой|\d+[-\s]?(?:й|ой)?)\s+сезон\s*\)', ' ', t, flags=re.I)
    t = re.sub(r'\b(?:первый|второй|третий|четвертый|пятый|шестой|седьмой)\s+сезон\b|\b\d+\s*-?\s*(?:й|ой)?\s*сезон\b|\bсезон\s*\d+\b', ' ', t, flags=re.I)
    t = re.sub(r'\b(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth)\s+season\b|\bseason\s*\d+\b', ' ', t, flags=re.I)
    t = re.sub(r'\s+(?:[2-9]|ii|iii|iv)\s*$', ' ', t, flags=re.I)
    add(t)
    if ':' in t:
      add(t.split(':', 1)[0])
    ws = re.sub(r'[^\w\s]', ' ', t.split(':', 1)[0]).split()
    if len(ws) > 3:
      add(' '.join(ws[:3]))
    if len(ws) > 2:
      add(' '.join(ws[:2]))
    sn = _season_base(n)[1]
    if sn > 1 and t.strip():
      add(f'{t.strip()} {sn}')
  return out


def _note(msg):
  try:
    RECENT.append(time.strftime('%H:%M:%S ') + str(msg)[:140])
  except Exception:
    pass


_ALTS_GATE = threading.Lock()  # одночасно лише один пошук «на інших сайтах» (фоновий після додавання + ручний не накладаються)


def find_alternatives(e):
  with _ALTS_GATE:
    return _find_alternatives(e)


def _find_alternatives(e):
  """Шукає це ж аніме на інших сайтах. Повертає [{url,title,site}] (без поточного сайту)."""
  _note('alts: старт ' + str(e.get('title'))[:40])
  cur_site = site_info(e['url'])['id']
  meta = e.get('meta') or {}
  names = [e.get('title') or '', meta.get('original') or '']
  for sx in e.get('sources') or []:  # назви з інших сайтів теж можуть краще підійти для пошуку
    if sx.get('title'):
      names.append(sx['title'])
  mine = title_keys(*names)
  found = []
  lock = threading.Lock()
  qs = _query_variants(*names)[:8]
  have = {sx.get('site') for sx in e.get('sources') or []}

  def run(sid):
    for q in qs:
      try:
        _note(f'alts: {sid} «{q[:30]}»')
        lst = SEARCHERS[sid](q)
      except Exception:
        continue
      hit = [x for x in lst if _keys_match(mine, title_keys(x['title'], x.get('orig')))]
      if hit:
        with lock:
          found.append({'url': hit[0]['url'], 'title': hit[0]['title'], 'site': sid})
        return
  ths = [threading.Thread(target=run, args=(s,), daemon=True) for s in SITE_IDS if s != cur_site and s not in have]
  for t in ths:
    t.start()
  for t in ths:
    t.join(45)
  _note(f'alts: кінець, знайдено {len(found)}')
  return found


# ═════════════════════════ Новинки: каталоги онгоїнгів на сайтах ═════════════════════════
# Кожен парсер повертає (items, has_more); елемент: title, orig, url, poster, year, type, genres, info, rating.

BROWSE_CACHE = {}
BROWSE_TTL = 300


def _bi(title, url, poster=None, year=None, typ='', genres=None, info='', orig='', rating=None):
  return {'title': title, 'orig': orig or '', 'url': url, 'poster': poster, 'year': year, 'type': typ or '',
          'genres': genres or [], 'info': info or '', 'rating': rating}


def _browse_aniliberty(page, hosts=('aniliberty.top', 'anilibria.top')):
  last = None
  t_end = time.time() + 25  # загальний ліміт: ланцюжок варіантів не має висіти хвилинами
  for host in hosts:
    # 1) каталог з фільтром «онгоїнг» та сортуванням за свіжістю; 2) той самий каталог без фільтра; 3) «останні релізи»
    attempts = (
        ('/api/v1/anime/catalog/releases', {'page': page, 'limit': 24, 'f[publish_statuses][]': 'IS_ONGOING', 'f[sorting]': 'FRESH_AT_DESC'}),
        ('/api/v1/anime/catalog/releases', {'page': page, 'limit': 24, 'f[publish_statuses]': 'IS_ONGOING', 'f[sorting]': 'FRESH_AT_DESC'}),
        ('/api/v1/anime/catalog/releases', {'page': page, 'limit': 24, 'f[sorting]': 'FRESH_AT_DESC'}),
        ('/api/v1/anime/releases/latest', {'limit': 24}),
    )
    for path, params in attempts:
      if time.time() > t_end:
        break
      try:
        r = http_get(f'https://{host}{path}', params=params, headers=HEADERS, timeout=9, retries=0)
        if r.status_code != 200:
          last = RuntimeError(f'HTTP {r.status_code}')
          continue
        j = r.json()
        meta = j.get('meta') if isinstance(j, dict) else None
        lst = (j.get('data') or j.get('items') or j.get('list') or []) if isinstance(j, dict) else j
        out = []
        for d in lst or []:
          if not isinstance(d, dict):
            continue
          alias = d.get('alias')
          nm = d.get('name') if isinstance(d.get('name'), dict) else {}
          title = nm.get('main') or nm.get('english') or alias
          if not alias or not title:
            continue
          tp = d.get('type') if isinstance(d.get('type'), dict) else {}
          gs = [g.get('name') for g in (d.get('genres') or []) if isinstance(g, dict) and g.get('name')]
          le = d.get('latest_episode') if isinstance(d.get('latest_episode'), dict) else {}
          tot = d.get('episodes_total')
          info = ''
          if le.get('ordinal') is not None:
            info = f"серія {le.get('ordinal')}" + (f' з {tot}' if tot else '')
          elif tot:
            info = f'{tot} серій'
          sn = d.get('season') if isinstance(d.get('season'), dict) else {}
          out.append(_bi(title, f'https://{host}/anime/releases/release/{alias}/episodes', _anilib_meta(d, f'https://{host}/').get('poster'),
                         d.get('year'), tp.get('description') or tp.get('value') or '', gs, info, nm.get('english') or ''))
        if not out:
          last = RuntimeError('порожня відповідь')
          continue
        pg = ((meta or {}).get('pagination') or {}) if isinstance(meta, dict) else {}
        total = pg.get('total_pages') or pg.get('last_page')
        more = (page < int(total)) if total else (len(out) >= 24 and 'latest' not in path)
        return out, more
      except Cancelled:
        raise
      except Exception as ex:
        last = ex
  raise last or RuntimeError('AniLiberty не відповідає')


def _browse_yummy(page, hosts=('old.yummyani.me', 'ru.yummyani.me')):
  last = None
  for host in hosts:
    try:
      r = http_get(f'https://{host}/catalog/ongoing', params={'page': page} if page > 1 else None, headers=HEADERS, timeout=14, retries=0)
      if r.status_code != 200:
        last = RuntimeError(f'HTTP {r.status_code}')
        continue
      html = _utf(r)
      out, seen = [], set()
      for blk in re.split(r'<div class="anime-column"', html)[1:]:
        blk = blk[:4500]
        m = re.search(r'class="anime-title"[^>]*href="(/catalog/item/[^"]+)"[^>]*>(.*?)</a>', blk, re.S) or \
            re.search(r'href="(/catalog/item/[^"]+)"[^>]*class="anime-title"[^>]*>(.*?)</a>', blk, re.S)
        if not m or m.group(1) in seen:
          continue
        seen.add(m.group(1))
        im = re.search(r'<img[^>]+src="([^"]+)"', blk)
        tp = re.search(r'class="anime-title"[^>]*>.*?</a>\s*<div>(.*?)</div>', blk, re.S)
        rt = re.search(r'class="main-rating[^"]*"[^>]*>\s*([\d.]+)', blk)
        poster = im.group(1) if im else None
        if poster and poster.startswith('//'):
          poster = 'https:' + poster
        out.append(_bi(_text(m.group(2)), 'https://ru.yummyani.me' + m.group(1), _abs(f'https://{host}/', poster) if poster else None,
                       None, _text(tp.group(1)) if tp else '', None, '', '', float(rt.group(1)) if rt else None))
      if not out:
        last = RuntimeError('сторінка без карток')
        continue
      return out, bool(re.search(r'rel="next"', html))
    except Cancelled:
      raise
    except Exception as ex:
      last = ex
  raise last or RuntimeError('YummyAnime не відповідає')


MOBILE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'


def _vost_cards(html, host):
  out = []
  for blk in re.findall(r'<article class="post"[^>]*>.*?</article>', html, re.S):  # мобільна верстка
    a = re.search(r'<a href="(https?://[^"]+/tip/[^"]+\.html)"', blk)
    h = re.search(r'<h2>(.*?)</h2>', blk, re.S)
    if not a or not h:
      continue
    bg = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", blk)
    t = re.sub(r'\s*\[[^\]]*\]', '', _text(h.group(1))).strip()
    ru, _, orig = t.partition(' / ')
    cats = re.search(r'class="short-categori">(.*?)</div>', blk, re.S)
    links = re.findall(r'<a href="[^"]*/(tip|zhanr|god)/([^/"]+)/?"[^>]*>(.*?)</a>', cats.group(1), re.S) if cats else []
    typ = next((_text(x[2]) for x in links if x[0] == 'tip'), '')
    gen = [_text(x[2]) for x in links if x[0] == 'zhanr']
    yr = next((int(x[1]) for x in links if x[0] == 'god' and x[1].isdigit()), None)
    out.append(_bi(ru.strip() or t, a.group(1), _abs(f'https://{host}/', bg.group(1)) if bg else None, yr, typ, gen, '', orig.strip()))
  if out:
    return out
  seen = set()  # запасний розбір: будь-яка верстка DLE — посилання на /tip/…/N-….html з назвою та картинкою поруч
  for m in re.finditer(r'<a\b[^>]*href="((?:https?://[^"/]+)?/tip/[^"/]+/\d+-[^"]+\.html)"[^>]*>(.*?)</a>', html, re.S):
    u = m.group(1)
    u = u if u.startswith('http') else f'https://{host}' + u
    if u in seen:
      continue
    ttl = _text(m.group(2))
    ctx = html[max(0, m.start() - 700): m.end() + 700]
    if not ttl:
      al = re.search(r'alt="([^"]{3,})"', ctx)
      ttl = _text(al.group(1)) if al else ''
    if not ttl:
      continue
    seen.add(u)
    t = re.sub(r'\s*\[[^\]]*\]', '', ttl).strip()
    ru, _, orig = t.partition(' / ')
    out.append(_bi(ru.strip() or t, u, _best_img(ctx, f'https://{host}/'), None, '', None, '', orig.strip()))
  return out


def _browse_vost(page, hosts=('animevost.org', 'v14.vost.pw', 'vost.pw'), uas=(MOBILE_UA, DESKTOP_UA)):
  last = None
  for host in hosts:
    for ua in uas:  # сайт віддає різну верстку мобільним і десктопним браузерам
      try:
        r = http_get(f'https://{host}/ongoing/' + (f'page/{page}/' if page > 1 else ''), headers={**HEADERS, 'User-Agent': ua, 'Referer': f'https://{host}/'}, timeout=9, retries=0)
        if r.status_code != 200:
          last = RuntimeError(f'{host}: HTTP {r.status_code}')
          continue
        html = _utf(r)
        out = _vost_cards(html, host)
        if not out:
          m = re.search(r'<title>(.*?)</title>', html, re.S)
          last = RuntimeError(f'{host}: сторінка без карток ({len(html)} байт, «{_text(m.group(1))[:40] if m else "?"}»)')
          continue
        return out, bool(re.search(r'/ongoing/page/%d/' % (page + 1), html))
      except Cancelled:
        raise
      except Exception as ex:
        last = ex
  raise last or RuntimeError('AnimeVost не відповідає')


def _browse_animru(page):
  last = None
  for host in ('anim-ru.net',):
    try:
      path = '/novinki-anime-sdg3j34b6jd/' + (f'page/{page}/' if page > 1 else '')
      r = http_get(f'https://{host}{path}', headers=HEADERS, timeout=14, retries=0)
      if r.status_code != 200:
        last = RuntimeError(f'HTTP {r.status_code}')
        continue
      html = _utf(r)
      out = []
      for blk in re.findall(r'<article class="dddewtwettwe">.*?</article>', html, re.S):
        a = re.search(r'<a href="(/[^"]+\.html)"', blk)
        h = re.search(r'<h3[^>]*>(.*?)</h3>', blk, re.S)
        if not a or not h:
          continue
        raw = _text(h.group(1))
        inf = re.search(r'\[([^\]]*)\]', raw)
        yr = re.search(r'\((\d{4})\)', raw)
        t = re.sub(r'\s*\[[^\]]*\]|\s*\(\d{4}\)', '', raw).strip()
        out.append(_bi(t or raw, f'https://{host}' + a.group(1), _best_img(blk, f'https://{host}/'), int(yr.group(1)) if yr else None, '', None, inf.group(1).strip() if inf else ''))
      if not out:
        last = RuntimeError('сторінка без карток')
        continue
      return out, bool(re.search(r'/page/%d/' % (page + 1), html))
    except Cancelled:
      raise
    except Exception as ex:
      last = ex
  raise last or RuntimeError('Anim-RU не відповідає')


def _par(fns):
  """Запускає варіанти (дзеркала) паралельно, повертає перший успішний результат — повільне дзеркало не гальмує решту."""
  cancel = getattr(_tl, 'cancel', None)

  def wrap(f):
    def run():
      _tl.cancel = cancel
      return f()
    return run
  ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(fns))
  last = None
  try:
    for fu in concurrent.futures.as_completed([ex.submit(wrap(f)) for f in fns], timeout=35):
      try:
        return fu.result()
      except Cancelled:
        raise
      except Exception as e:
        last = e
  except concurrent.futures.TimeoutError:
    last = RuntimeError('сайт не відповів вчасно')
  finally:
    ex.shutdown(wait=False)
  raise last or RuntimeError('сайт не відповідає')


def _browse_vost_all(page):
  mob = ('animevost.org', 'v14.vost.pw', 'vost.pw')
  try:
    return _par([lambda h=h: _browse_vost(page, (h,), (MOBILE_UA,)) for h in mob])
  except Cancelled:
    raise
  except Exception as ex1:
    try:
      return _par([lambda h=h: _browse_vost(page, (h,), (DESKTOP_UA,)) for h in mob])
    except Cancelled:
      raise
    except Exception:
      raise ex1


BROWSERS = {
    'aniliberty': lambda page: _par([lambda h=h: _browse_aniliberty(page, (h,)) for h in ('aniliberty.top', 'anilibria.top')]),
    'yummyani': lambda page: _par([lambda h=h: _browse_yummy(page, (h,)) for h in ('old.yummyani.me', 'ru.yummyani.me')]),
    'animevost': _browse_vost_all,
    'animru': _browse_animru,
}
_BROWSE_SEM = _ASem(2)  # не більше двох сайтів-каталогів одночасно (Mix запитує всі чотири)
_BROWSE_CANCELS = set()


def _cancel_browse():
  """Користувач відкриває аніме/шукає — фонові запити каталогу більше не потрібні."""
  for ev in list(_BROWSE_CANCELS):
    ev.set()


def api_browse(site, page=1, force=False):
  if site not in BROWSERS:
    raise ValueError('Невідомий сайт.')
  try:
    page = max(1, min(60, int(page or 1)))
  except Exception:
    page = 1
  ck = (site, page)
  c = BROWSE_CACHE.get(ck)
  if c and not force and time.time() - c[0] < BROWSE_TTL:
    items, more = c[1], c[2]
  else:
    if net_state()['state'] == 'offline':
      raise ValueError('Немає інтернету.')
    ev = threading.Event()
    _BROWSE_CANCELS.add(ev)
    _tl.cancel = ev
    try:
      while not _BROWSE_SEM.acquire(timeout=0.4):
        if ev.is_set():
          raise Cancelled()
      try:
        items, more = BROWSERS[site](page)
      finally:
        _BROWSE_SEM.release()
    except Cancelled:
      return {'site': site, 'page': page, 'has_more': True, 'items': [], 'cancelled': True}
    finally:
      _BROWSE_CANCELS.discard(ev)
      _tl.cancel = None
    BROWSE_CACHE[ck] = (time.time(), items, more)
    if len(BROWSE_CACHE) > 40:
      for k in sorted(BROWSE_CACHE, key=lambda k: BROWSE_CACHE[k][0])[:15]:
        BROWSE_CACHE.pop(k, None)
  with LOCK:
    have = set()
    for e in library.values():
      if e.get('temp'):  # тимчасовий перегляд — це ще не «У бібліотеці»
        continue
      for u in [e.get('url')] + [s.get('url') for s in (e.get('sources') or [])]:
        if u:
          have.add(_slug_of(u))
  return {'site': site, 'page': page, 'has_more': bool(more),
          'items': [{**x, 'lib': _slug_of(x['url']) in have} for x in items]}


# ═════════════════════════ Бібліотека ═════════════════════════

LOCK = threading.RLock()
library = {}
LOOKUPS = {}
INFO_CACHE = {}  # id аніме → (час, {ordinal: серія}) — щоб не перечитувати сайт для кожної серії
POSTER_TYPES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.webp': 'image/webp', '.avif': 'image/avif', '.gif': 'image/gif',
}


def _lib_path():
  return os.path.join(BASE_DIR, 'library.json')


def _cache_dir():
  d = os.path.join(BASE_DIR, '.cache')
  os.makedirs(d, exist_ok=True)
  return d


def lib_load():
  global library
  try:
    with open(_lib_path(), encoding='utf8') as f:
      d = json.load(f)
    library = d if isinstance(d, dict) else {}
  except Exception:
    library = {}
  for i, e in enumerate(library.values()):  # міграція: список озвучок, базова озвучка, порядок
    if e.get('dubbing') and not e.get('all_dubs'):
      e['all_dubs'] = [e['dubbing']]
    e.setdefault('base_dub', e.get('dubbing'))
    e.setdefault('order', i)
    e.setdefault('added_ts', time.time())
    try:
      _dedupe_sources(e)
    except Exception:
      pass
  return library


_SAVE_LOCK = threading.Lock()
_SAVE_SEQ = [0, 0]  # [виданий номер, записаний номер]


def lib_save():
  # знімок робимо під LOCK (швидко), а запис на диск — поза ним, щоб не блокувати інші запити
  with LOCK:
    _SAVE_SEQ[0] += 1
    seq = _SAVE_SEQ[0]
    data = json.dumps(library, ensure_ascii=False, separators=(',', ':'))
  with _SAVE_LOCK:
    if seq <= _SAVE_SEQ[1]:
      return  # новіший знімок уже записано
    os.makedirs(BASE_DIR, exist_ok=True)
    tmp = _lib_path() + '.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
      f.write(data)
    os.replace(tmp, _lib_path())
    _SAVE_SEQ[1] = seq


def _now():
  return datetime.datetime.now().strftime('%d.%m %H:%M')


def _kid(key):
  return hashlib.sha1(key.encode('utf8')).hexdigest()[:10]


def _find(kid):
  with LOCK:
    for k, e in library.items():
      if _kid(k) == kid:
        return k, e
  raise KeyError('Аніме не знайдено в бібліотеці')


def _norm_url(url):
  """/page-5-2573-slug.html → /2573-slug.html (сторінка всього аніме)."""
  return re.sub(r'^(https?://[^/]+)/page-\d+-', r'\1/', url)


def _num_arg(x):
  try:
    return _num(float(x))
  except Exception:
    raise ValueError('Невірний номер серії')


def _folder(e, dub=None):
  """Папка з файлами озвучки. Базова озвучка лежить у e['folder'], решта — «Назва (Озвучка)»."""
  dub = e.get('dubbing') if dub is None else dub
  if not dub or dub == e.get('base_dub', e.get('dubbing')):
    return e.get('folder') or ''
  return safe_name(f"{e['title']} ({dub})")


def _ep_path(e, o, dub=None):
  return os.path.join(BASE_DIR, _folder(e, dub), f'Серія {o}.mp4')


def _is_downloaded(e, o, dub=None):
  p = _ep_path(e, o, dub)
  return os.path.exists(p) and os.path.getsize(p) > 0


_EP_FILE_RE = re.compile(r'^Серія (\d+(?:\.\d+)?)(\.mp4|\.part\.mp4(?:\.parts|\.ts|\.dl|\.dl\.map|\.dl\.map\.tmp)?)$')


def _downloaded_set(e, dub=None):
  d = os.path.join(BASE_DIR, _folder(e, dub))
  res = set()
  try:
    for fn in os.listdir(d):
      m = _EP_FILE_RE.match(fn)
      if m and m.group(2) == '.mp4' and os.path.getsize(os.path.join(d, fn)) > 0:
        res.add(_num(float(m.group(1))))
  except OSError:
    pass
  return res


def _rm(path):
  try:
    if os.path.isdir(path):
      shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
      os.remove(path)
  except OSError:
    pass


def _purge_folder(folder, only_tmp=False, ordinal=None):
  """Видаляє файли серій (і тимчасові) у папці. only_tmp — лише недокачане; ordinal — лише одна серія."""
  d = os.path.join(BASE_DIR, folder)
  try:
    names = os.listdir(d)
  except OSError:
    return
  for fn in names:
    m = _EP_FILE_RE.match(fn)
    if not m:
      continue
    if ordinal is not None and _num(float(m.group(1))) != ordinal:
      continue
    if only_tmp and m.group(2) == '.mp4':
      continue
    _rm(os.path.join(d, fn))
  try:
    os.rmdir(d)  # лише якщо порожня
  except OSError:
    pass


def _sweep_tmp():
  """При запуску прибираємо уламки збірки (докачування .parts/.dl лишаємо)."""
  try:
    for fn in os.listdir(BASE_DIR):
      d = os.path.join(BASE_DIR, fn)
      if os.path.isdir(d) and not fn.startswith('.'):
        for f2 in os.listdir(d):
          if f2.endswith('.part.mp4') or f2.endswith('.part.mp4.ts'):
            _rm(os.path.join(d, f2))
  except OSError:
    pass


def _apply_current(e, current, titles=None):
  known = set(e.get('known', []))
  new = sorted(o for o in current if o not in known)
  e['known'] = sorted(known | set(current))
  e['new'] = sorted(set(e.get('new', [])) | set(new))
  e['checked'] = _now()
  if titles:
    e.setdefault('ep_titles', {}).update({str(k): v for k, v in titles.items() if v})
  return new


def _poster_file(kid):
  d = os.path.join(BASE_DIR, '.cache')
  if not os.path.isdir(d):
    return None
  for fn in os.listdir(d):
    if fn.startswith(f'poster_{kid}.'):
      return os.path.join(d, fn)
  return None


def cache_poster(kid, url):
  """Зберігає обкладинку на диск, щоб вона була доступна офлайн."""
  if not url:
    return False
  try:
    pu = urlparse(url)
    h = dict(HEADERS)
    h['Referer'] = f'{pu.scheme}://{pu.netloc}/'
    r = SESS.get(url, headers=h, timeout=(6, 25))
    if r.status_code != 200 or not r.content:
      return False
    ct = (r.headers.get('content-type') or '').split(';')[0].strip().lower()
    if ct and not ct.startswith('image/'):
      return False
    ext = {v: k for k, v in POSTER_TYPES.items()}.get(ct)
    if not ext:
      ext = os.path.splitext(urlparse(url).path)[1].lower()
      if ext not in POSTER_TYPES:
        ext = '.jpg'
    old = _poster_file(kid)
    path = os.path.join(_cache_dir(), f'poster_{kid}{ext}')
    with open(path, 'wb') as f:
      f.write(r.content)
    if old and old != path and os.path.exists(old):
      os.remove(old)
    try:
      _find(kid)[1]['poster_src'] = url  # з якої адреси взято файл — щоб помітити, що обкладинка змінилась
    except Exception:
      pass
    return True
  except Exception:
    return False


_FI_CACHE = {}
_FI_LOCKS = {}


_HEAVY = _ASem(2)
_HEAVY_BG = _ASem(1)


class _acq:
  """Захоплення семафора/замка, яке можна скасувати (інакше «завислий» запит назавжди блокує всіх)."""
  def __init__(self, lk):
    self.lk = lk

  def __enter__(self):
    while not self.lk.acquire(timeout=0.4):
      _chk()
    return self

  def __exit__(self, *a):
    self.lk.release()


_KID_OP = {}


class _kid_op:
  """Одна «довга» операція (зміна сайту/озвучки) на аніме одночасно."""
  def __init__(self, kid):
    self.kid = kid

  def __enter__(self):
    with LOCK:
      if self.kid in _KID_OP and time.time() - _KID_OP[self.kid] < 120:
        raise ValueError('Зачекайте — для цього аніме вже йде перемикання.')
      _KID_OP[self.kid] = time.time()

  def __exit__(self, *a):
    with LOCK:
      _KID_OP.pop(self.kid, None)


def fetch_info(url, cache=False, bg=False):
  """cache=True: результат зберігається 10 хв (відкриття з пошуку, перемикання сайтів) — повторні відкриття миттєві."""
  url = (url or '').strip()
  sem = _HEAVY_BG if bg else _HEAVY
  if not cache:
    with _acq(sem):
      return _fetch_info(url)
  with LOCK:
    lk = _FI_LOCKS.setdefault(url, threading.Lock())
  with _acq(lk):
    c = _FI_CACHE.get(url)
    if c and time.time() - c[0] < 600:
      return c[1]
    with _acq(sem):
      info = _fetch_info(url)
    _FI_CACHE[url] = (time.time(), info)
    for u in [u for u, v in _FI_CACHE.items() if time.time() - v[0] > 1200]:
      _FI_CACHE.pop(u, None)
    return info


def _fetch_info(url):
  url = (url or '').strip()
  if not re.match(r'^https?://', url):
    raise ValueError('Вставте повне посилання, що починається з http')
  name, eps, only_one, meta = fetch_episodes(url)
  if not eps:
    raise ValueError('Серій не знайдено. Перевірте посилання.')
  dubs = list(dict.fromkeys(e['dubbing'] for e in eps if e.get('dubbing')))
  return {'title': name or 'Аніме', 'eps': eps, 'only_one': only_one, 'meta': meta or {}, 'dubbings': dubs}


def _eps_for(info, dub):
  return [e for e in info['eps'] if not dub or e.get('dubbing') == dub]


def _dk(dub):
  return dub or ''


def _note_quals(e, dub, n, links):
  qs = sorted((q for q in (links or {}) if qnum(q)), key=qnum, reverse=True)
  if not qs:
    return False
  d = e.setdefault('quals', {}).setdefault(_dk(dub), {})
  if d.get(str(n)) != qs:
    d[str(n)] = qs
    return True
  return False


def _note_eps(e, dub, eps):
  ch = False
  pv = e.setdefault('previews', {}).setdefault(_dk(dub), {})
  for x in eps:
    if x.get('links'):
      ch = _note_quals(e, dub, x['ordinal'], x['links']) or ch
    if x.get('preview') and pv.get(str(x['ordinal'])) != x['preview']:
      pv[str(x['ordinal'])] = x['preview']
      ch = True
  return ch


def _probe_height(path):
  """Висота відео: спершу читаємо MP4 самі, потім ffprobe, а якщо його немає (напр. у Carnets) — розбір виводу `ffmpeg -i`."""
  h0 = mp4_height(path)
  if h0:
    return h0
  try:
    if shutil.which('ffprobe'):
      r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=height',
                          '-of', 'csv=p=0', path], capture_output=True, text=True, timeout=20)
      h = int((r.stdout or '0').strip().split()[0])
      if h:
        return h
  except Exception:
    pass
  try:
    if shutil.which('ffmpeg'):
      r = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-i', path], capture_output=True, text=True, timeout=20)
      m = re.search(r'Video:.*?[\s,](\d{2,5})x(\d{2,5})[\s,\[]', r.stderr or '')
      if m:
        return int(m.group(2))
  except Exception:
    pass
  return 0


_PROBING = set()


def _probe_missing(kid, dub, items):
  """Для файлів, скачаних до оновлення, визначаємо якість у фоні (ffprobe) і запам'ятовуємо."""
  def run():
    changed = False
    try:
      for n, path in items:
        h = _probe_height(path)
        if h:
          try:
            key, e = _find(kid)
          except Exception:
            return
          e.setdefault('dlinfo', {})[f'{_dk(dub)}|{n}'] = {'q': q_label(h)}
          changed = True
      if changed:
        lib_save()
    finally:
      for n, _ in items:
        _PROBING.discard((kid, dub, n))
  threading.Thread(target=run, daemon=True).start()


def _sources_view(e):
  cur = _norm_url(e['url'])
  ck = _src_key(e['url'])
  res, seen = [], {}
  for sx in e.get('sources') or []:
    k = _src_key(sx['url'])
    act = _norm_url(sx['url']) == cur or k == ck
    if k in seen:  # той самий сайт і сторінка вдруге — не показуємо другу кнопку
      if act:
        res[seen[k]]['active'] = True
      continue
    seen[k] = len(res)
    res.append({'site': site_info(sx['url']), 'url': sx['url'], 'title': sx.get('title') or '', 'active': act})
  if not any(x['active'] for x in res):
    res.insert(0, {'site': site_info(e['url']), 'url': e['url'], 'title': e.get('title') or '', 'active': True})
  try:  # порядок — як у налаштуваннях («основний сайт» першим)
    rank = {sid: i for i, sid in enumerate(_site_order())}
    res.sort(key=lambda x: rank.get(x['site']['id'], 99))
  except Exception:
    pass
  return res


def _slug_of(url):
  """Ідентифікатор сторінки без домену/дати/префікса: для порівняння однієї й тієї ж сторінки на дзеркалах."""
  p = urlparse(url or '').path.rstrip('/').split('/')[-1]
  if p == 'episodes':
    p = urlparse(url).path.rstrip('/').split('/')[-2]
  p = re.sub(r'\.html$', '', p)
  p = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', p)
  return p.lower()


def _related_view(e, rel):
  if not rel:
    return []
  me = {_slug_of(e['url'])} | {_slug_of(x.get('url')) for x in e.get('sources') or []}
  mine = title_keys(e.get('title') or '', (e.get('meta') or {}).get('original') or '')
  mybase = {b for b, _ in mine}
  lib = {}
  for k2, e2 in library.items():
    if e2.get('temp'):  # тимчасовий перегляд — ще не «в бібліотеці»
      continue
    for u in [e2['url']] + [x.get('url') for x in e2.get('sources') or []]:
      lib[_slug_of(u)] = _kid(k2)
  has_order = any((x.get('kind') or 'order') == 'order' for x in rel)
  seen, rows = set(), []
  for x in rel:  # спершу порядок/сезони, потім схожі; дублікати (той самий запис у обох списках) прибираємо
    sl = _slug_of(x['url'])
    kind = x.get('kind') or 'order'
    if kind == 'order' and sl not in seen:
      seen.add(sl)
      rows.append((x, 'order', sl))
  for x in rel:
    sl = _slug_of(x['url'])
    if (x.get('kind') or 'order') == 'order' or sl in seen or sl in me:
      continue
    seen.add(sl)
    kind = 'similar'
    if not has_order and any(b in mybase for b, _ in title_keys(x.get('title'))):
      kind = 'order'  # сайт не віддав порядок (Anim-RU): сезони цього ж аніме серед «Похожие»
    rows.append((x, kind, sl))

  def year(x):
    m = re.search(r'(?:19|20)\d\d', x.get('note') or '')
    return int(m.group(0)) if m else 9999
  order = [r for r in rows if r[1] == 'order']
  order = [order[i] for i in sorted(range(len(order)), key=lambda i: (year(order[i][0]), i))]  # порядок виходу: за роком, при рівних — як на сайті
  sim = [r for r in rows if r[1] != 'order']
  out = []
  for x, kind, sl in order[:40] + sim[:24]:
    out.append({'url': x['url'], 'title': x['title'], 'note': x.get('note') or '', 'kind': kind,
                'cur': sl in me, 'lib': lib.get(sl),
                'poster': ('/img?u=' + quote(x['poster'], safe='')) if x.get('poster') else ('/relposter?u=' + quote(x['url'], safe='')),
                'rp': not x.get('poster')})
  return out


def entry_view(key, e):
  kid = _kid(key)
  meta = dict(e.get('meta') or {})
  meta['related'] = _related_view(e, meta.get('related'))
  meta['poster'] = f'/poster/{kid}?v={int(os.path.getmtime(_poster_file(kid)))}' if _poster_file(kid) else (
      '/img?u=' + quote(meta['poster'], safe='') if meta.get('poster') else None)
  watched = set(e.get('watched', []))
  newset = set(e.get('new', []))
  titles = e.get('ep_titles', {})
  prog = e.get('progress', {})
  cur = e.get('dubbing')
  eps = []
  qd = (e.get('quals') or {}).get(_dk(cur)) or {}
  pvd = (e.get('previews') or {}).get(_dk(cur)) or {}
  di = e.get('dlinfo') or {}
  total_size, allq, miss, dlq = 0, set(), [], set()
  for o in sorted(set(e.get('known', []))):
    dl = _is_downloaded(e, o)
    size, dq = 0, None
    if dl:
      pth = _ep_path(e, o)
      try:
        size = os.path.getsize(pth)
      except OSError:
        size = 0
      total_size += size
      dq = (di.get(f'{_dk(cur)}|{o}') or {}).get('q')
      if dq:
        dlq.add(dq)
      if not dq and (kid, cur, o) not in _PROBING:
        _PROBING.add((kid, cur, o))
        miss.append((o, pth))
    qs = qd.get(str(o)) or []
    allq.update(qs)
    eps.append({
        'n': o,
        'size': size,
        'q': dq,
        'qs': qs,
        'th': True,
        'title': titles.get(str(o)) or '',
        'downloaded': dl,
        'watched': o in watched,
        'new': o in newset and not dl and o not in watched,
        'pos': prog.get(str(o), 0),
    })
  if miss:
    _probe_missing(kid, cur, miss)
  names = list(e.get('all_dubs') or ([cur] if cur else []))
  if cur and cur not in names:
    names.insert(0, cur)
  dubs = [{'name': d, 'active': d == cur, 'downloaded': len(_downloaded_set(e, d)), 'count': (e.get('dub_counts') or {}).get(d)} for d in names]
  return {
      'id': kid,
      'title': e['title'],
      'dubbing': cur,
      'dubs': dubs,
      'site': site_info(e['url']),
      'url': e['url'],
      'temp': bool(e.get('temp')),
      'preview_of': e.get('preview_of') if e.get('temp') else None,
      'archived': bool(e.get('archived')),
      'last_watch': int(e.get('last_watch') or 0),
      'alias': e.get('alias') or '',
      'group': e.get('group') or '', 'gname': e.get('gname') or '',
      'sources': _sources_view(e),
      'meta': meta,
      'episodes': eps,
      'quals': sorted(allq, key=qnum, reverse=True),
      'dlq': sorted(dlq, key=qnum, reverse=True),
      'checked': e.get('checked'),
      'stats': {
          'total': len(eps),
          'watched': sum(1 for x in eps if x['watched']),
          'downloaded': sum(1 for x in eps if x['downloaded']),
          'size': total_size,
          'new': sum(1 for x in eps if x['new']),
      },
  }


def _fill_meta(key):
  """Для записів зі старого скрипта: дотягує обкладинку й опис."""
  with LOCK:
    e = library.get(key)
  if not e or e.get('meta') is not None:
    return
  info = fetch_info(e['url'])
  with LOCK:
    e['meta'] = info['meta']
    titles = {x['ordinal']: x['title'] for x in _eps_for(info, e.get('dubbing'))}
    e.setdefault('ep_titles', {}).update({str(k): v for k, v in titles.items() if v})
  cache_poster(_kid(key), info['meta'].get('poster'))
  lib_save()


POSTER_VER = 2  # версія логіки вибору обкладинки; при зміні записи оновляться самі


def _poster_repair():
  """Дотягує обкладинки, яких немає на диску (щоб вони були доступні офлайн), і виправляє застарілі."""
  for key in list(library):
    e = library.get(key)
    if not e:
      continue
    kid = _kid(key)
    try:
      if e.get('poster_v') != POSTER_VER:
        info = fetch_info(e['url'])
        p = (info['meta'] or {}).get('poster')
        with LOCK:
          if p:
            e['meta'] = {**(e.get('meta') or {}), 'poster': p}
          e['poster_v'] = POSTER_VER
        if p:
          cache_poster(kid, p)
        lib_save()
      elif not _poster_file(kid):
        p = (e.get('meta') or {}).get('poster')
        if p:
          cache_poster(kid, p)
    except Exception:
      pass


def api_reorder(ids):
  with LOCK:
    pos = {kid: i for i, kid in enumerate(ids or [])}
    rest = sorted(((k, e) for k, e in library.items() if _kid(k) not in pos), key=lambda kv: kv[1].get('order', 0))
    for k, e in library.items():
      if _kid(k) in pos:
        e['order'] = pos[_kid(k)]
    base = len(pos)
    for i, (k, e) in enumerate(rest):
      e['order'] = base + i
  lib_save()
  return {'ok': True}


# ─── скріншоти серій ───

try:
  requests.packages.urllib3.disable_warnings()
except Exception:
  pass
_IMG_SEMS = {}
_IMG_SEMS_LOCK = threading.Lock()


def _img_sem(url):
  h = urlparse(url).netloc.lower().split(':')[0]
  h = '.'.join(h.split('.')[-2:])
  with _IMG_SEMS_LOCK:
    if h not in _IMG_SEMS:
      _IMG_SEMS[h] = _ASem(_lim('img'))
    return _IMG_SEMS[h]


def _img_urls(url):
  # запасні адреси тієї ж картинки: CDN-піддомен img. у Anim-RU буває нестабільним, а основний домен віддає той самий файл
  out = [url]
  pu = urlparse(url)
  if pu.netloc.lower() == 'img.anim-ru.net':
    out.append(url.replace('//img.anim-ru.net/', '//anim-ru.net/', 1))
  elif pu.netloc.lower() in ('anim-ru.net', 'www.anim-ru.net'):
    out.append(url.replace('//' + pu.netloc + '/', '//img.anim-ru.net/', 1))
  return out
_IMG_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)
_IMG_BAD = {}


def _img_referers(url):
  pu = urlparse(url)
  host = pu.netloc.lower()
  own = f'{pu.scheme}://{pu.netloc}/'
  if any(k in host for k in ('aniland', 'animevost', 'vost')):
    return ['https://animevost.org/', own, None]
  if 'yani' in host or 'yummy' in host:
    return ['https://ru.yummyani.me/', own, None]
  if 'anim-ru' in host:
    return [own, None]
  return [own, None]


def _img_fail(url, ref, why):
  if not SETTINGS.get('logging'):
    return
  try:
    pth = os.path.join(_crash_dir(), 'images.log')
    with open(pth, 'a', encoding='utf8') as f:
      f.write(f"{time.strftime('%H:%M:%S')} {why} | ref={ref} | {url}\n")
    if os.path.getsize(pth) > 200000:
      _rm(pth)
  except Exception:
    pass


_SHRINK = {}


def _shrink_img(url, data, ct):
  """Великі постери (напр. YummyAnime «full») зменшуємо до ~420 px: розпакована картинка в WebView інакше їсть пам'ять. Без Pillow — віддаємо як є."""
  if len(data) < 60000:
    return data, ct
  hit = _SHRINK.get(url)
  if hit:
    return hit
  try:
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(data))
    if im.width <= 480:
      return data, ct
    h = max(1, int(im.height * 420 / im.width))
    im = im.convert('RGB').resize((420, h))
    out = io.BytesIO()
    im.save(out, 'JPEG', quality=80)
    res = (out.getvalue(), 'image/jpeg')
    if len(_SHRINK) > 80:
      _SHRINK.pop(next(iter(_SHRINK)), None)
    _SHRINK[url] = res
    return res
  except Exception:
    return data, ct


def img_get(url, force=False):
  """(bytes, content-type) з диска або з сайту; None, якщо недоступно."""
  d = os.path.join(BASE_DIR, '.cache', 'img')
  fp = os.path.join(d, hashlib.sha1(url.encode()).hexdigest()[:20])
  try:
    if os.path.exists(fp) and os.path.getsize(fp) > 64:
      raw = open(fp, 'rb').read()
      return raw[1:], {b'J': 'image/jpeg', b'P': 'image/png', b'W': 'image/webp', b'A': 'image/avif', b'G': 'image/gif'}.get(raw[:1], 'image/jpeg')
  except OSError:
    pass
  if not force and time.time() - _IMG_BAD.get(url, 0) < 20:
    return None
  t0 = time.time()
  with _img_sem(url):
    h0 = dict(HEADERS)
    h0['Accept'] = 'image/avif,image/webp,image/*,*/*;q=0.8'
    for u2 in _img_urls(url):
      ssl_bad = False
      for ref in _img_referers(u2):
        for verify in ((True, False) if not ssl_bad else (False,)):
          try:
            h = dict(h0)
            if ref:
              h['Referer'] = ref
            r = SESS.get(u2, headers=h, timeout=(4, 10), verify=verify)
            ct = (r.headers.get('content-type') or '').split(';')[0].strip().lower()
            if r.status_code == 200 and r.content and (not ct or ct.startswith('image/')):
              ct = ct or 'image/jpeg'
              try:
                os.makedirs(d, exist_ok=True)
                tag = {'image/jpeg': b'J', 'image/png': b'P', 'image/webp': b'W', 'image/avif': b'A', 'image/gif': b'G'}.get(ct, b'J')
                with open(fp, 'wb') as f:
                  f.write(tag + r.content)
              except OSError:
                pass
              if time.time() - t0 > 6:
                _img_fail(url, ref, f'повільно {time.time() - t0:.1f}с (OK)')
              return r.content, ct
            _img_fail(u2, ref, f'HTTP {r.status_code} {ct} {len(r.content)}Б')
            break
          except requests.exceptions.SSLError as ex:
            if verify:  # сертифікат CDN не проходить перевірку в Python — для публічних картинок пробуємо без перевірки
              ssl_bad = True
              continue
            _img_fail(u2, ref, 'SSL: ' + str(ex)[:60])
            break
          except Exception as ex:
            _img_fail(u2, ref, type(ex).__name__ + ': ' + str(ex)[:80])
            break
  _IMG_BAD[url] = time.time()
  return None


_IMG_REQ_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8)
_IMG_FLIGHT = {}
_IMG_FLIGHT_LOCK = threading.Lock()


_REL_FLIGHT = {}
_REL_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)  # визначення постера за сторінкою — окремо від завантаження картинок


def rel_async(page_url, force=False):
  """Постер для картки «пов'язаного»: спершу визначаємо адресу постера зі сторінки, потім звичайний img_get."""
  with _IMG_FLIGHT_LOCK:
    f = _REL_FLIGHT.get(page_url)
    if f is None:
      def work():
        pu = rel_poster_url(page_url)
        return img_async(pu, force).result(timeout=40) if pu else None
      f = _REL_POOL.submit(work)
      _REL_FLIGHT[page_url] = f
      f.add_done_callback(lambda _f, u=page_url: (_IMG_FLIGHT_LOCK.acquire(), _REL_FLIGHT.pop(u, None), _IMG_FLIGHT_LOCK.release()))
    return f


def img_async(url, force=False):
  """Одне завантаження на адресу: повторні запити чіпляються до вже запущеного (і не дублюють мережу)."""
  with _IMG_FLIGHT_LOCK:
    f = _IMG_FLIGHT.get(url)
    if f is None:
      f = _IMG_REQ_POOL.submit(img_get, url, force)
      _IMG_FLIGHT[url] = f
      f.add_done_callback(lambda _f, u=url: (_IMG_FLIGHT_LOCK.acquire(), _IMG_FLIGHT.pop(u, None), _IMG_FLIGHT_LOCK.release()))
    return f


_THUMB_SEM = _ASem(2)
_THUMB_FAIL = {}


def _thumb_path(kid, dub, n):
  d = os.path.join(BASE_DIR, '.cache', 'thumbs')
  os.makedirs(d, exist_ok=True)
  h = hashlib.sha1((dub or '').encode('utf8')).hexdigest()[:6]
  return os.path.join(d, f'{kid}_{h}_{n}.jpg')


def _thumb_from_stream(url, hdrs, path):
  """Кадр зі стріму без ffmpeg-мережі (у Carnets він часто не вміє https): качаємо один сегмент (або початок mp4) через requests, кадр знімає ffmpeg з локального файлу."""
  tmp = path + '.tmp.ts'
  try:
    sess = requests.Session()
    sess.headers.update(HEADERS)
    if hdrs:
      sess.headers.update(hdrs)
    if _is_hls(url):
      segs, enc = load_playlist(url, sess)
      if not segs or enc:
        return False
      seg = segs[min(len(segs) - 1, 6)]
      r = sess.get(seg, timeout=(8, 25))
      if r.status_code != 200 or not r.content:
        return False
      data = r.content
    else:
      r = sess.get(url, headers={'Range': 'bytes=0-4194303'}, timeout=(8, 25))
      if r.status_code not in (200, 206) or not r.content:
        return False
      data = r.content[:4194304]
    with open(tmp, 'wb') as f:
      f.write(data)
    for ss in ('2', '0'):
      try:
        subprocess.run(['ffmpeg', '-y', '-nostdin', '-loglevel', 'error', '-ss', ss, '-i', tmp, '-frames:v', '1',
                        '-vf', 'scale=480:-2', '-q:v', '4', path], capture_output=True, timeout=30)
      except Exception:
        pass
      if os.path.exists(path) and os.path.getsize(path) > 0:
        return True
      _rm(path)
    return False
  except Exception:
    return False
  finally:
    _rm(tmp)


def api_thumb(kid, n):
  """Шлях до скріншота серії: кеш → картинка з сайту → кадр із завантаженого файлу (ffmpeg). Інакше None."""
  key, e = _find(kid)
  n = _num_arg(n)
  dub = e.get('dubbing')
  path = _thumb_path(kid, dub, n)
  if os.path.exists(path) and os.path.getsize(path) > 0:
    return path
  prev = ((e.get('previews') or {}).get(_dk(dub)) or {}).get(str(n))
  with _THUMB_SEM:
    if os.path.exists(path) and os.path.getsize(path) > 0:
      return path
    if prev and net_state()['state'] != 'offline':
      try:
        pu = urlparse(prev)
        r = SESS.get(prev, headers={**HEADERS, 'Referer': f'{pu.scheme}://{pu.netloc}/'}, timeout=(6, 15))
        ct = (r.headers.get('content-type') or '').lower()
        if r.status_code == 200 and r.content and (not ct or ct.startswith('image/')):
          if shutil.which('ffmpeg') and 'jpeg' not in ct and 'jpg' not in ct:  # webp/png → jpg для всіх браузерів
            tmp = path + '.src'
            with open(tmp, 'wb') as f:
              f.write(r.content)
            subprocess.run(['ffmpeg', '-y', '-nostdin', '-loglevel', 'error', '-i', tmp, '-vf', 'scale=480:-2', '-q:v', '4', path],
                           capture_output=True, timeout=20)
            _rm(tmp)
          else:
            with open(path, 'wb') as f:
              f.write(r.content)
          if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
      except Exception:
        pass
    if _is_downloaded(e, n, dub) and shutil.which('ffmpeg'):
      src = _ep_path(e, n, dub)
      for ss in ('60', '10', '0'):
        try:
          subprocess.run(['ffmpeg', '-y', '-nostdin', '-loglevel', 'error', '-ss', ss, '-i', src, '-frames:v', '1',
                          '-vf', 'scale=480:-2', '-q:v', '4', path], capture_output=True, timeout=30)
        except Exception:
          pass
        if os.path.exists(path) and os.path.getsize(path) > 0:
          return path
        _rm(path)
    elif not prev and time.time() - _THUMB_FAIL.get((kid, dub, n), 0) > 900:
      # немає ні файлу, ні картинки сайту (YummyAnime, Kodik…) — кадр прямо зі стріму найнижчої якості
      st_ = net_state()['state']
      if st_ != 'offline' and not (st_ == 'cell' and not SETTINGS['mobile_download']):
        try:
          ep = _get_ep(kid, e, n)
          th = ep.get('thumbs') or []
          if th:  # готовий кадр із плеєра (Kodik) — найдешевший варіант
            try:
              tu = th[len(th) // 2]
              pu = urlparse(tu)
              r = SESS.get(tu, headers={**HEADERS, 'Referer': 'https://kodikplayer.com/'}, timeout=(6, 15))
              if r.status_code == 200 and r.content[:2] == b'\xff\xd8':
                with open(path, 'wb') as f:
                  f.write(r.content)
                return path
            except Exception:
              pass
          q, url, hdrs, player = ep_choice(ep, '360p')
          if url and shutil.which('ffmpeg') and _thumb_from_stream(url, hdrs, path):
            return path
        except Exception:
          pass
      _THUMB_FAIL[(kid, dub, n)] = time.time()
  return None


# ─── автоперевірка нових серій ───

AC_WAKE = threading.Event()


def _ac_path():
  return os.path.join(BASE_DIR, 'autocheck.json')


def _ac_load():
  try:
    with open(_ac_path(), encoding='utf8') as f:
      AC['last_run'] = int(json.load(f).get('last_run') or 0)
  except Exception:
    pass


def _ac_save():
  try:
    tmp = _ac_path() + '.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
      json.dump({'last_run': AC['last_run']}, f)
    os.replace(tmp, _ac_path())
  except Exception:
    pass


AC = {'gen': 0, 'last_run': 0, 'next_run': 0, 'running': False, 'seq': 0, 'found': []}


def _ac_due_at(e, hours):
  """Коли аніме треба перевірити наступного разу (розумно: за таймером, а давно завершені — рідко)."""
  now = time.time()
  iv = hours * 3600
  last = e.get('chk_ts') or 0
  nx = (e.get('meta') or {}).get('next')
  nts = nx.get('ts') if isinstance(nx, dict) else None
  if nts:
    if nts > now:  # наступна серія ще не вийшла — чекаємо її часу (з запасом 10 хв), але не довше тижня
      return min(nts + 600, last + 7 * 86400)
    if now - nts < 2 * 86400:  # час минув, а серії ще немає — пробуємо частіше, не рідше за годину
      return max(nts + 600, last + min(iv, 3600))
  ref = e.get('last_new_ts') or e.get('added_ts') or now
  if now - ref > 45 * 86400:  # давно нічого нового — схоже на завершене; раз на тиждень на випадок «12/12 → 13»
    return last + 7 * 86400
  return last + iv


def _ac_pick(hours, limit):
  now = time.time()
  due, soonest = [], None
  with LOCK:
    for k, e in library.items():
      if e.get('temp') or e.get('archived'):
        continue
      if not e.get('chk_ts'):  # після оновлення розкидаємо перевірки у часі, щоб не було «шторму»
        e['chk_ts'] = now - random.random() * hours * 3600
      t = _ac_due_at(e, hours)
      if t <= now:
        due.append((t, k))
      elif soonest is None or t < soonest:
        soonest = t
  due.sort()
  return [k for _, k in due[:limit]], soonest


def _user_busy():
  """Користувач зараз щось шукає/перемикає — автоперевірка почекає."""
  now = time.time()
  try:
    if any(now - v['t'] < 40 and not v.get('done') for v in list(TASKS.values())):
      return True
    if any(now - v['t'] < 30 for v in list(SEARCHES.values())):
      return True
  except Exception:
    pass
  return False


def _autocheck_loop(gen):
  _ac_load()
  AC_WAKE.wait(8)
  AC_WAKE.clear()
  while AC['gen'] == gen:
    try:
      if SETTINGS.get('auto_check') and net_state()['state'] != 'offline' and not _user_busy() and not any(
          j['status'] == 'running' for j in JOBS):
        keys, soonest = _ac_pick(SETTINGS.get('auto_check_hours', 3), 5)
        AC['next_run'] = int(soonest or 0) if not keys else int(time.time())
        if keys:
          AC['running'] = True
          for k in keys:
            if AC['gen'] != gen:
              return
            with LOCK:
              e = library.get(k)
            if not e:
              continue
            try:
              res = api_check(_kid(k), bg=True)
              if res.get('new'):
                AC['found'] = (AC['found'] + [{'kid': _kid(k), 'title': e['title'], 'new': res['new']}])[-10:]
                AC['seq'] += 1
            except Exception:
              e['chk_ts'] = time.time() - SETTINGS.get('auto_check_hours', 3) * 3600 + 1800  # збій — повтор за пів години
            try:
              lib_save()  # час останньої перевірки кожного аніме одразу на диск
            except Exception:
              pass
            time.sleep(2 + random.random() * 2)
          AC['last_run'] = int(time.time())
          _ac_save()
          lib_save()
          AC['running'] = False
          AC_WAKE.wait(10)
          AC_WAKE.clear()
          continue
      else:
        AC['next_run'] = 0
    except Exception:
      AC['running'] = False
    AC_WAKE.wait(60)
    AC_WAKE.clear()


def api_autocheck(kick=False):
  if kick:
    AC_WAKE.set()  # застосунок відкрито — одразу дивимось, чи не настав час перевірки
  return {'enabled': bool(SETTINGS.get('auto_check')), 'hours': SETTINGS.get('auto_check_hours', 3), 'last_run': AC['last_run'],
          'next_run': AC['next_run'], 'running': AC['running'], 'seq': AC['seq'], 'found': AC['found'][-5:]}


def _meta_refresh_loop():
  for key in list(library):
    try:
      _fill_meta(key)
    except Exception:
      with LOCK:
        if key in library and library[key].get('meta') is None:
          library[key]['meta'] = {}  # не пробуємо знову кожного запуску
      lib_save()
  while True:
    if net_state()['state'] != 'offline':
      _poster_repair()
    tmp_sweep()
    time.sleep(600)


# ─── дії з бібліотекою ───

def api_lookup(url):
  info = fetch_info(url, cache=True)
  token = hashlib.sha1(f'{url}{time.time()}'.encode()).hexdigest()[:12]
  now = time.time()
  for t in [t for t, v in LOOKUPS.items() if now - v['time'] > 1800]:
    del LOOKUPS[t]
  LOOKUPS[token] = {'url': url.strip(), 'info': info, 'time': now}
  norm = _norm_url(url.strip())
  can_add = not info['only_one'] or norm != url.strip()
  return {
      'token': token,
      'title': info['title'],
      'meta': info['meta'],
      'site': site_info(url),
      'dubbings': info['dubbings'],
      'count': len(info['eps']) if not info['dubbings'] else max(
          sum(1 for e in info['eps'] if e.get('dubbing') == d) for d in info['dubbings']),
      'only_one': info['only_one'],
      'can_add': can_add,
      'hint': None if can_add else 'Це посилання на одну серію. Для бібліотеки потрібне посилання на сторінку всього аніме.',
  }


# Пошук виконується у фоні: сторінка бачить етапи й може скасувати.
TASKS = {}
TASK_LIMIT = 75  # секунд


def api_lookup_start(url):
  if net_state()['state'] == 'offline':
    raise ValueError('Немає інтернету.')
  now = time.time()
  for t in [t for t, v in TASKS.items() if now - v['t'] > 900]:
    del TASKS[t]
  tid = hashlib.sha1(f'{url}{now}'.encode()).hexdigest()[:10]
  task = {'stage': 'Підключаюсь до сайту…', 'done': False, 'result': None, 'error': None,
          'cancel': threading.Event(), 't': now}
  TASKS[tid] = task

  def run():
    _tl.cancel = task['cancel']
    _tl.stage = lambda m: task.__setitem__('stage', m)
    try:
      res = api_lookup(url)
      if not task['cancel'].is_set():
        task['result'] = res
    except Cancelled:
      task['error'] = 'Скасовано'
    except Exception as ex:
      if not task['cancel'].is_set():
        task['error'] = str(ex)[:300] or type(ex).__name__
    finally:
      task['done'] = True

  threading.Thread(target=run, daemon=True).start()
  return {'task': tid}


def api_lookup_status(tid):
  t = TASKS.get(tid)
  if not t:
    raise KeyError('Пошук не знайдено')
  el = time.time() - t['t']
  if not t['done'] and el > TASK_LIMIT:
    t['cancel'].set()
    t['error'], t['done'] = 'Сайт відповідає занадто довго. Спробуйте ще раз або пізніше.', True
  return {'stage': t['stage'], 'done': t['done'], 'error': t['error'], 'result': t['result'], 'elapsed': round(el, 1)}


def api_lookup_cancel(tid):
  t = TASKS.get(tid)
  if t:
    t['cancel'].set()
    t['done'], t['error'] = True, 'Скасовано'
  return {'ok': True}


def _switch_dub(e, dub, info=None):
  """Перемикає озвучку; епізоди й «нові» кожної озвучки зберігаються окремо, файли не чіпаємо."""
  cur = e.get('dubbing')
  if dub == cur:
    return
  with LOCK:
    e.setdefault('base_dub', cur)
    e.setdefault('dubs', {})[cur or ''] = {'known': list(e.get('known', [])), 'new': list(e.get('new', []))}
    snap = e['dubs'].get(dub or '')
    e['dubbing'] = dub
    files = _downloaded_set(e, dub)
    if snap:
      known, newl = sorted(set(snap['known']) | files), list(snap['new'])
    elif info:
      eps = _eps_for(info, dub)
      known, newl = sorted({x['ordinal'] for x in eps} | files), []
      e.setdefault('ep_titles', {}).update({str(x['ordinal']): x['title'] for x in eps if x['title']})
    else:
      known, newl = sorted(files), []
    e['known'], e['new'] = known, newl


def api_add(token, dub, temp=False, sources=None, preview_of=None):
  L = LOOKUPS.get(token)
  if not L:
    raise ValueError('Пошук застарів — натисніть «Знайти» ще раз.')
  url, info = L['url'], L['info']
  if info['only_one']:
    norm = _norm_url(url)
    if norm == url:
      raise ValueError('Це посилання на одну серію. Вставте посилання на сторінку всього аніме.')
    url, info = norm, fetch_info(norm)
    if info['only_one']:
      raise ValueError('Не вдалося знайти повний список серій за цим посиланням.')
  dubs = info['dubbings']
  if dubs and dub not in dubs:
    dub = max(dubs, key=lambda d: sum(1 for e in info['eps'] if e.get('dubbing') == d))
  if not dubs:
    dub = None
  eps = _eps_for(info, dub)
  cur = sorted({e['ordinal'] for e in eps})
  titles = {e['ordinal']: e['title'] for e in eps}
  folder = safe_name(f"{info['title']} ({dub})" if dub else info['title'])
  with LOCK:
    if preview_of:  # перегляд іншого сайту: не склеюємо з записом бібліотеки, лише повторно використовуємо власний тимчасовий
      existing = next((k for k, x in library.items() if x.get('temp') and _norm_url(x['url']) == url), None)
    else:
      existing = next((k for k, x in library.items() if _norm_url(x['url']) == url
                       or any(_norm_url(sx['url']) == url for sx in x.get('sources') or [])), None)
    if existing and temp:
      return {'id': _kid(existing)}
    if existing:  # те саме аніме вже є — лише додаємо/перемикаємо озвучку
      key = existing
      e = library[key]
      if e.get('temp'):
        _promote(e)
      e['all_dubs'] = list(dict.fromkeys((e.get('all_dubs') or []) + dubs))
      _set_counts(e, info)
      _switch_dub(e, dub, info)
      _apply_current(e, cur, titles)
      e['meta'] = {**(e.get('meta') or {}), **{k: v for k, v in (info['meta'] or {}).items() if v}}
    else:
      key = f'{url}||{dub or ""}'
      library[key] = {
          'url': url, 'title': info['title'], 'dubbing': dub, 'base_dub': dub, 'all_dubs': list(dubs),
          'folder': folder, 'known': cur, 'watched': [], 'new': [], 'checked': _now(),
          'meta': info['meta'] or {}, 'ep_titles': {str(k): v for k, v in titles.items() if v},
          'progress': {}, 'poster_v': POSTER_VER,
          'order': 1 + max([x.get('order', 0) for x in library.values()] or [-1]), 'added_ts': time.time(),
      }
      if temp:
        library[key]['temp'] = True
        library[key]['tmp_ts'] = time.time()
        if preview_of:
          library[key]['preview_of'] = preview_of
    _merge_sources(library[key], [{'url': url, 'title': info['title']}] + list(sources or []))
  _note_eps(library[key], dub, eps)
  lib_save()
  if not temp and len(library[key].get('sources') or []) < 2 and net_state()['state'] != 'offline':
    threading.Thread(target=_alts_bg, args=(_kid(key),), daemon=True).start()  # у фоні шукаємо те саме аніме на інших сайтах
  t = threading.Thread(target=cache_poster, args=(_kid(key), (info['meta'] or {}).get('poster')), daemon=True)
  t.start()
  t.join(1.2 if temp else 4)  # даємо обкладинці шанс з'явитись одразу, але не чекаємо довго
  return {'id': _kid(key)}


def _ensure_cur_source(e):
  cur = e.setdefault('sources', [])
  u = _norm_url(e.get('url') or '')
  if u.startswith('http') and u not in {_norm_url(x['url']) for x in cur} and _src_key(u) not in {_src_key(x['url']) for x in cur}:
    cur.append({'url': u, 'title': e.get('title') or '', 'site': site_info(u)['id']})


def _src_key(u):
  """Одна й та сама сторінка на дзеркалах/з датою в адресі (old.yummyani.me/…-2026-10-28 і ru.yummyani.me/…) — один ключ."""
  sl = _slug_of(u)
  m = re.match(r'^(\d+)-', sl)
  if m and site_info(u)['id'] == 'animru':  # DLE: номер новини однозначний, а «хвіст» адреси може відрізнятись (перейменування, транслітерація)
    sl = m.group(1)
  return (site_info(u)['id'], sl)


def _dedupe_sources(e):
  cur = e.get('sources') or []
  if len(cur) < 2:
    return
  main = _src_key(e.get('url') or '') if e.get('url') else None
  out, seen = [], {}
  for x in cur:
    k = _src_key(x.get('url') or '')
    if k in seen:
      if k == main and _norm_url(x['url']) == _norm_url(e.get('url') or ''):  # лишаємо запис, що збігається з поточною адресою
        out[seen[k]] = x
      continue
    seen[k] = len(out)
    out.append(x)
  e['sources'] = out


def _merge_sources(e, items):
  _ensure_cur_source(e)
  cur = e.setdefault('sources', [])
  have = {_src_key(x['url']) for x in cur}
  for x in items or []:
    u = _norm_url((x.get('url') or '').strip())
    if not u.startswith('http') or _src_key(u) in have:
      continue
    have.add(_src_key(u))
    cur.append({'url': u, 'title': x.get('title') or '', 'site': site_info(u)['id']})
  return cur


def _set_counts(e, info):
  """YummyAnime: скільки серій озвучено кожною озвучкою (береться з відповіді сайту)."""
  try:
    if site_info(e.get('url') or '')['id'] != 'yummyani':
      return
    c = {}
    for x in info.get('eps') or []:
      c.setdefault(x.get('dubbing') or '', set()).add(x.get('ordinal'))
    if c:
      e['dub_counts'] = {d: len(v) for d, v in c.items()}
  except Exception:
    pass


def _promote(e):
  e.pop('temp', None)
  e.pop('tmp_ts', None)
  e['order'] = 1 + max([x.get('order', 0) for x in library.values()] or [-1])
  e['added_ts'] = time.time()
  e['chk_ts'] = time.time()


def _mark_all_seen(e):
  """Додали в «переглянуте» — усі відомі серії вважаються переглянутими (під LOCK)."""
  w = set(e.get('watched', [])) | set(e.get('known', []))
  e['watched'] = sorted(w)
  e['new'] = []


def api_keep(kid, archived=False):
  key, e = _find(kid)
  if e.get('temp') and e.get('preview_of'):
    raise ValueError('Це аніме вже є в бібліотеці (на іншому сайті) — відкрийте його звідти.')
  with LOCK:
    if e.get('temp'):
      _promote(e)
    if archived:
      e['archived'] = True
      _mark_all_seen(e)
  lib_save()
  if archived:
    return {'ok': True}
  if net_state()['state'] != 'offline' and len(e.get('sources') or []) < 2:
    threading.Thread(target=_alts_bg, args=(kid,), daemon=True).start()
  return {'ok': True}


def _alts_bg(kid):
  try:
    time.sleep(6)  # не накладаємось на завантаження сторінки й обкладинки одразу після додавання
    key, e = _find(kid)
    found = find_alternatives(e)
    with LOCK:
      _merge_sources(e, found)
      e['alts_ts'] = time.time()
    lib_save()
    return len(found)
  except Exception:
    return 0


def _task_run(fn, stage):
  now = time.time()
  for t in [t for t, v in TASKS.items() if now - v['t'] > 900]:
    del TASKS[t]
  tid = hashlib.sha1(f'{stage}{now}'.encode()).hexdigest()[:10]
  task = {'stage': stage, 'done': False, 'result': None, 'error': None, 'cancel': threading.Event(), 't': now}
  TASKS[tid] = task

  def run():
    _tl.cancel = task['cancel']
    _tl.stage = lambda m: task.__setitem__('stage', m)
    try:
      res = fn()
      if not task['cancel'].is_set():
        task['result'] = res
    except Cancelled:
      task['error'] = 'Скасовано'
    except Exception as ex:
      if not task['cancel'].is_set():
        task['error'] = str(ex)[:300] or type(ex).__name__
    finally:
      task['done'] = True
  threading.Thread(target=run, daemon=True).start()
  return {'task': tid}


def _open_existing(k, x, url, srcs, explicit):
  """Аніме вже є: додаємо джерела; якщо сайт обрано явно (фільтр пошуку) і він інший — перемикаємо запис на нього."""
  with LOCK:
    if x.get('temp'):
      x['tmp_ts'] = time.time()
    _merge_sources(x, srcs + [{'url': url, 'title': (srcs[0].get('title') if srcs else '') or x.get('title') or ''}])
  lib_save()
  kid = _kid(k)
  return {'task': None, 'id': kid}


def _open_preview(url, srcs, of_kid):
  """Тимчасовий перегляд сторінки аніме на обраному сайті; запис у бібліотеці не чіпаємо."""
  if net_state()['state'] == 'offline':
    raise ValueError('Немає інтернету.')

  def go():
    res = api_lookup(url)
    if not res['can_add']:
      raise ValueError(res['hint'])
    r = api_add(res['token'], None, temp=True, sources=srcs, preview_of=of_kid)
    return {'id': r['id']}
  return _task_run(go, 'Підключаюсь до сайту…')


def api_open_start(url, sources, explicit=False):
  """Відкриває аніме для перегляду без додавання в бібліотеку (тимчасовий запис)."""
  _cancel_browse()
  url = _norm_url((url or '').strip())
  if not re.match(r'^https?://', url):
    raise ValueError('Невірне посилання.')
  srcs = [x for x in (sources or []) if isinstance(x, dict)]
  with LOCK:
    for k, x in library.items():
      us = [_norm_url(x['url'])] + [_norm_url(sx['url']) for sx in x.get('sources') or []]
      if url in us:
        hit = (k, x)
        break
    else:
      hit = None
  if hit:
    if explicit and not hit[1].get('temp') and _norm_url(hit[1]['url']) != url:  # обрано інший сайт, ніж у бібліотеці — лише дивимось
      return _open_preview(url, srcs, _kid(hit[0]))
    return _open_existing(hit[0], hit[1], url, srcs, explicit)
  names = [x.get('title') or '' for x in srcs]
  if names:  # те саме аніме вже є в бібліотеці (інший сайт) — відкриваємо його й додаємо сайт як джерело
    theirs = title_keys(*names)
    with LOCK:
      hit = next(((k, x) for k, x in library.items() if not x.get('temp') and _keys_match(title_keys(x.get('title'), (x.get('meta') or {}).get('original')), theirs)), None)
    if hit:
      if explicit and _norm_url(hit[1]['url']) != url:
        return _open_preview(url, srcs, _kid(hit[0]))
      return _open_existing(hit[0], hit[1], url, srcs, explicit)
  if net_state()['state'] == 'offline':
    raise ValueError('Немає інтернету.')

  def go():
    res = api_lookup(url)
    if not res['can_add']:
      raise ValueError(res['hint'])
    r = api_add(res['token'], None, temp=True, sources=srcs)
    return {'id': r['id']}
  return _task_run(go, 'Підключаюсь до сайту…')


def api_find_alts_start(kid):
  key, e = _find(kid)
  if net_state()['state'] == 'offline':
    raise ValueError('Немає інтернету.')

  def go():
    _tl.stage('Шукаю на інших сайтах…')
    n0 = len(e.get('sources') or [])
    found = find_alternatives(e)
    with LOCK:
      _merge_sources(e, found)
      e['alts_ts'] = time.time()
    lib_save()
    return {'found': len(e.get('sources') or []) - n0}
  return _task_run(go, 'Шукаю на інших сайтах…')


def api_switch_site(kid, url):
  key, e = _find(kid)
  url = _norm_url((url or '').strip())
  if not any(_norm_url(sx['url']) == url for sx in e.get('sources') or []) and _norm_url(e['url']) != url:
    raise ValueError('Цього сайту немає серед джерел.')
  if net_state()['state'] == 'offline':
    raise ValueError('Немає інтернету.')
  def go():
    with _kid_op(kid):
      _do_switch(kid, url)
    return {'ok': True}
  return _task_run(go, 'Переключаю сайт…')


def _do_switch(kid, url):
  key, e = _find(kid)
  url = _norm_url((url or '').strip())
  if not any(_norm_url(sx['url']) == url for sx in e.get('sources') or []):
    raise ValueError('Цього сайту немає серед джерел.')
  if _norm_url(e['url']) == url:
    return {'ok': True}
  if net_state()['state'] == 'offline':
    raise ValueError('Немає інтернету.')
  with JOB_LOCK:
    if any(j['kid'] == kid and j['status'] in ('queued', 'running') for j in JOBS):
      raise ValueError('Спершу дочекайтесь завершення завантажень цього аніме (або скасуйте їх).')
  _note('switch: завантажую ' + url[-60:])
  info = fetch_info(url, cache=True)
  _note('switch: сторінку отримано')
  with LOCK:
    _ensure_cur_source(e)  # попередній сайт лишається в списку джерел
  dubs = info['dubbings']
  cur = e.get('dubbing')
  dub = cur if cur in dubs else (max(dubs, key=lambda d: sum(1 for x in info['eps'] if x.get('dubbing') == d)) if dubs else None)
  eps = _eps_for(info, dub)
  with LOCK:
    e['url'] = url
    e['all_dubs'] = list(dubs)
    _set_counts(e, info)
    e['dubs'] = {}
    e['dubbing'] = dub
    files = _downloaded_set(e, dub)
    e['known'] = sorted({x['ordinal'] for x in eps} | files)
    e['new'] = []
    e['quals'], e['previews'], e['skips'] = {}, {}, {}
    e.setdefault('ep_titles', {}).update({str(x['ordinal']): x['title'] for x in eps if x['title']})
    e['meta'] = {**(e.get('meta') or {}), **{k: v for k, v in (info['meta'] or {}).items() if v}}
    e['checked'] = _now()
    e['chk_ts'] = time.time()
  _note_eps(e, dub, eps)
  for x in eps:
    if x.get('skips'):
      e.setdefault('skips', {})[_skip_key(e, None, x['ordinal'])] = x['skips']
  lib_save()
  np_ = (e.get('meta') or {}).get('poster')
  if np_ and (e.get('poster_src') != np_ or not _poster_file(kid)):  # обкладинка нового сайту
    if cache_poster(kid, np_):
      lib_save()
  return {'ok': True}


def api_delete_files(kid, ordinals):
  key, e = _find(kid)
  n = 0
  for o in ordinals or []:
    o = _num_arg(o)
    if _is_downloaded(e, o):
      _rm(_ep_path(e, o))
      _purge_folder(_folder(e), only_tmp=True, ordinal=o)
      n += 1
  return {'ok': True, 'deleted': n}


def api_watched_many(kid, ordinals, value):
  key, e = _find(kid)
  with LOCK:
    w = set(e.get('watched', []))
    for o in ordinals or []:
      o = _num_arg(o)
      (w.add if value else w.discard)(o)
    e['watched'] = sorted(w)
    if value:
      e['new'] = [x for x in e.get('new', []) if x not in w]
  lib_save()
  return {'ok': True}


def tmp_sweep(max_age=6 * 3600):
  """Прибирає тимчасові (непідтверджені) записи, які давно не відкривали."""
  now = time.time()
  with LOCK:
    old = [_kid(k) for k, e in library.items() if e.get('temp') and now - e.get('tmp_ts', 0) > max_age]
  for kid in old:
    try:
      api_remove(kid)
    except Exception:
      pass


def api_set_dub(kid, dub):
  key, e = _find(kid)
  if dub not in (e.get('all_dubs') or []):
    raise ValueError('Такої озвучки немає у цього аніме.')
  if dub == e.get('dubbing'):
    return {'ok': True}
  need = dub not in (e.get('dubs') or {}) and net_state()['state'] != 'offline'

  def go():
    with _kid_op(_kid(key)):
      info = None
      if need:
        try:
          info = fetch_info(e['url'], cache=True)
        except Cancelled:
          raise
        except Exception:
          info = None
      _switch_dub(e, dub, info)
      lib_save()
      return {'ok': True, 'online': info is not None or dub in (e.get('dubs') or {})}
  if not need:
    return go()
  return _task_run(go, 'Міняю озвучку…')


def _purge_foreign(e, top):
  """Чужі серії, що колись потрапили до запису (номер більший за останню реальну), прибираємо з усіх списків — якщо файлу в них немає."""
  def num(k):
    try:
      return float(str(k).split('|')[-1])
    except Exception:
      return None

  def bad(k):
    n = num(k)
    return n is not None and n > top and not os.path.exists(_ep_path(e, _num(n)))
  e['known'] = [x for x in e.get('known', []) if not bad(x)]
  e['new'] = [x for x in e.get('new', []) if not bad(x)]
  for fld in ('ep_titles', 'progress', 'dlinfo', 'skips'):
    d = e.get(fld)
    if isinstance(d, dict):
      for k in [k for k in d if bad(k)]:
        d.pop(k, None)
  for fld in ('previews', 'quals'):
    d = e.get(fld)
    if isinstance(d, dict):
      for sub in d.values():
        if isinstance(sub, dict):
          for k in [k for k in sub if bad(k)]:
            sub.pop(k, None)


def api_check(kid, bg=False):
  if net_state()['state'] == 'offline':
    raise ValueError('Немає інтернету.')
  key, e = _find(kid)
  info = fetch_info(e['url'], bg=bg)
  eps = _eps_for(info, e.get('dubbing'))
  cur = sorted({x['ordinal'] for x in eps})
  if not cur:
    raise ValueError('Серій не знайдено (можливо, змінилась озвучка або сторінка).')
  with LOCK:
    old_poster = (e.get('meta') or {}).get('poster')
    try:
      _purge_foreign(e, max(cur))
    except Exception:
      pass
    new = _apply_current(e, cur, {x['ordinal']: x['title'] for x in eps})
    e['chk_ts'] = time.time()
    if new:
      e['last_new_ts'] = time.time()
    if info['dubbings']:
      e['all_dubs'] = list(dict.fromkeys((e.get('all_dubs') or []) + info['dubbings']))
      _set_counts(e, info)
    for d, snap in (e.get('dubs') or {}).items():  # інші озвучки: оновлюємо тихо
      if d and d != e.get('dubbing'):
        oc = sorted({x['ordinal'] for x in _eps_for(info, d)})
        if oc:
          fresh = [o for o in oc if o not in snap['known']]
          snap['known'] = sorted(set(snap['known']) | set(oc))
          snap['new'] = sorted(set(snap['new']) | set(fresh))
    if info['meta'].get('poster') or info['meta'].get('description'):
      e['meta'] = {**(e.get('meta') or {}), **{k: v for k, v in info['meta'].items() if v}}
    for x in eps:
      if x.get('skips'):
        d_ = e.setdefault('skips', {})
        d_[_skip_key(e, None, x['ordinal'])] = x['skips']
    _note_eps(e, e.get('dubbing'), eps)
    e['poster_v'] = POSTER_VER
    if isinstance(e.get('meta'), dict):
      e['meta']['next'] = info['meta'].get('next')  # прибираємо застарілий таймер, якщо сайт його більше не дає
  lib_save()
  new_poster = (e.get('meta') or {}).get('poster')
  if not _poster_file(kid) or new_poster != old_poster or e.get('poster_src') != new_poster:
    if cache_poster(kid, new_poster):
      lib_save()
  if new and not e.get('temp') and not e.get('archived'):
    notif_add('new', kid=kid, title=e['title'], eps=list(new))
  return {'new': new}


def api_watched(kid, n=None, value=True, upto=None):
  key, e = _find(kid)
  with LOCK:
    w = set(e.get('watched', []))
    if upto is not None:
      upto = _num_arg(upto)
      w |= {o for o in e.get('known', []) if o <= upto}
    else:
      n = _num_arg(n)
      (w.add if value else w.discard)(n)
    e['watched'] = sorted(w)
    if value:
      e['last_watch'] = int(time.time())
  lib_save()
  return {'ok': True}


def _all_folders(e):
  names = list(e.get('all_dubs') or [])
  for d in list((e.get('dubs') or {}).keys()) + [e.get('dubbing'), e.get('base_dub')]:
    if d and d not in names:
      names.append(d)
  return list(dict.fromkeys([_folder(e, d) for d in (names or [None])] + [e.get('folder') or '']))


def api_archive(kid, value=True):
  """Архів: перегляднуте аніме не засмічує бібліотеку (і не перевіряється на нові серії), але нічого не видаляється."""
  key, e = _find(kid)
  with LOCK:
    if e.get('temp'):
      raise ValueError('Спочатку додайте аніме в бібліотеку.')
    if value:
      e['archived'] = True
      _mark_all_seen(e)
    else:
      e.pop('archived', None)
  lib_save()
  return {'ok': True}


def api_rename(kid, name):
  """Власна назва — лише для бібліотеки (картка аніме показує оригінальну). Порожня — повертає оригінальну."""
  key, e = _find(kid)
  name = re.sub(r'\s+', ' ', str(name or '')).strip()[:120]
  with LOCK:
    if name and name != e.get('title'):
      e['alias'] = name
    else:
      e.pop('alias', None)
  lib_save()
  return {'ok': True, 'alias': e.get('alias')}


def _clean_name(name):
  return re.sub(r'\s+', ' ', str(name or '')).strip()[:120]


def api_group(ids, name=None, gid=None):
  """Об'єднує вибрані аніме в групу (або додає до наявної gid): вони стають поруч у порядку бібліотеки."""
  ids = {str(i) for i in (ids or [])}
  with LOCK:
    ordered = [(k, e) for k, e in sorted(library.items(), key=lambda kv: kv[1].get('order', 0)) if not e.get('temp')]
    if gid and not any(e.get('group') == gid for k, e in ordered):
      gid = None
    mem = ids | {_kid(k) for k, e in ordered if gid and e.get('group') == gid}
    sel = [e for k, e in ordered if _kid(k) in mem]
    if len(sel) < 1:
      raise ValueError('Виберіть аніме.')
    if not gid:
      gid = 'g' + hashlib.sha1(f'{time.time()}{len(sel)}{sel[0].get("url")}'.encode()).hexdigest()[:8]
    nm = _clean_name(name)
    keep = next((e.get('gname') for e in sel if e.get('group') == gid and e.get('gname')), '')
    new, done = [], False
    for k, e in ordered:
      if _kid(k) in mem:
        if not done:
          new.extend(sel)
          done = True
      else:
        new.append(e)
    for i, e in enumerate(new):
      e['order'] = i
    for e in sel:
      e['group'] = gid
      n2 = nm or keep
      if n2:
        e['gname'] = n2
      else:
        e.pop('gname', None)
  lib_save()
  return {'ok': True, 'group': gid}


def api_ungroup(ids):
  ids = {str(i) for i in (ids or [])}
  with LOCK:
    for k, e in library.items():
      if _kid(k) in ids:
        e.pop('group', None)
        e.pop('gname', None)
  lib_save()
  return {'ok': True}


def api_group_rename(gid, name):
  nm = _clean_name(name)
  with LOCK:
    for e in library.values():
      if gid and e.get('group') == gid:
        if nm:
          e['gname'] = nm
        else:
          e.pop('gname', None)
  lib_save()
  return {'ok': True}


def api_remove(kid):
  """Видаляє аніме з бібліотеки разом з усіма завантаженими серіями (усіх озвучок) і тимчасовими файлами."""
  key, e = _find(kid)
  with JOB_LOCK:
    for j in JOBS:
      if j['kid'] == kid and j['status'] in ('queued', 'running'):
        j['cancel'].set()
        if j['status'] == 'queued':
          j['status'], j['message'] = 'cancelled', 'Скасовано'
  with LOCK:
    folders = [] if e.get('temp') else _all_folders(e)  # тимчасовий запис не має власних файлів — чужі папки не чіпаємо
    del library[key]
  lib_save()
  for f in folders:
    if f:
      _purge_folder(f)
  pf = _poster_file(kid)
  if pf:
    _rm(pf)
  td = os.path.join(BASE_DIR, '.cache', 'thumbs')
  if os.path.isdir(td):
    for fn in os.listdir(td):
      if fn.startswith(kid + '_'):
        _rm(os.path.join(td, fn))
  return {'ok': True}


def api_delete_file(kid, n):
  key, e = _find(kid)
  n = _num_arg(n)
  _rm(_ep_path(e, n))
  _purge_folder(_folder(e), only_tmp=True, ordinal=n)
  return {'ok': True}


BG_LAST = {'play': 0.0, 'open': 0.0}  # коли востаннє плеєр слав позицію — тоді програму треба тримати активною навіть із вимкненим екраном


def api_beat(closed=False):
  """Плеєр відкритий (навіть на паузі чи в PiP) — тримаємо програму активною; «closed» — плеєр закрито, лишаємо коротку паузу."""
  now = time.time()
  if closed:
    BG_LAST['open'] = 0.0
    BG_LAST['play'] = now - 60  # ще ~30 с, щоб дописати позицію
  else:
    BG_LAST['open'] = now
    BG_LAST['play'] = now
  return {'ok': True}


def api_progress(kid, n, t):
  BG_LAST['play'] = time.time()
  key, e = _find(kid)
  with LOCK:
    e.setdefault('progress', {})[str(_num_arg(n))] = round(float(t), 1)
    e['last_watch'] = int(time.time())
  lib_save()
  return {'ok': True}


# ═════════════════════════ Повідомлення ═════════════════════════

NOTIFS = []
_NOTIF_LOCK = threading.RLock()


def _notif_path():
  return os.path.join(BASE_DIR, 'notifs.json')


def notifs_load():
  try:
    with open(_notif_path(), encoding='utf8') as f:
      d = json.load(f)
    NOTIFS[:] = [x for x in d if isinstance(x, dict) and x.get('id')] if isinstance(d, list) else []
  except Exception:
    NOTIFS[:] = []


def _notif_save():
  try:
    tmp = _notif_path() + '.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
      json.dump(NOTIFS[-80:], f, ensure_ascii=False)
    os.replace(tmp, _notif_path())
  except Exception:
    pass


def notif_add(typ, **kw):
  """Повідомлення горить, поки його не прочитано. Нові серії одного аніме зливаються в одне непрочитане."""
  with _NOTIF_LOCK:
    if typ == 'new':
      for x in reversed(NOTIFS):
        if x['type'] == 'new' and x.get('kid') == kw.get('kid') and not x.get('read'):
          x['eps'] = sorted(set(x.get('eps') or []) | set(kw.get('eps') or []))
          x['ts'] = time.time()
          _notif_save()
          return
    NOTIFS.append({'id': hashlib.sha1(f'{typ}{time.time()}{random.random()}'.encode()).hexdigest()[:10], 'type': typ, 'ts': time.time(), 'read': False, **kw})
    del NOTIFS[:-80]
    _notif_save()


def api_notifs():
  with _NOTIF_LOCK:
    items = list(reversed(NOTIFS))[:60]
    return {'items': items, 'unread': sum(1 for x in NOTIFS if not x.get('read'))}


def api_notifs_read(nid=None):
  with _NOTIF_LOCK:
    for x in NOTIFS:
      if nid is None or x['id'] == nid:
        x['read'] = True
    _notif_save()
  return api_notifs()


def api_notifs_clear():
  with _NOTIF_LOCK:
    NOTIFS[:] = [x for x in NOTIFS if not x.get('read')]
    _notif_save()
  return api_notifs()


# ═════════════════════════ Завантаження ═════════════════════════

JOBS = []
JOB_LOCK = threading.RLock()
_worker = {'thread': None, 'threads': []}
_BATCH = {'t0': None, 'done': 0, 'bytes': 0, 'err': 0}
_job_seq = [0]


SPEED = {'v': 0.0}  # останнє відоме середнє швидкість завантаження, байт/с


def _job_public(j):
  d = {k: j[k] for k in ('id', 'kid', 'title', 'n', 'dub', 'status', 'done', 'total', 'phase', 'message')}
  for k in ('q', 'player', 'bytes', 'bytes_total', 'speed', 'secs', 'pk', 'pk_total', 'rounds'):
    d[k] = j.get(k)
  d['retry_in'] = max(0, int(j.get('not_before', 0) - time.time())) if j['status'] == 'queued' else 0
  bt, b = j.get('bytes_total') or 0, j.get('bytes') or 0
  spd = j.get('speed') or SPEED['v']
  d['eta'] = None
  if j['status'] == 'running' and bt and spd:
    d['eta'] = max(0, (bt - b) / spd)
  elif j['status'] == 'queued' and bt and spd:
    d['eta'] = bt / spd
  d['stage'] = j.get('stage', '')
  d['cancelling'] = j['cancel'].is_set() and j['status'] == 'running'
  d['kpaused'] = j['kid'] in PAUSED_KIDS
  return d


def _jobs_path():
  return os.path.join(BASE_DIR, 'jobs_pending.json')


INTERRUPTED = []  # завантаження, перервані виходом із додатка: чекають на рішення користувача


def _jobs_persist():
  """Черга пишеться на диск: якщо додаток закрився посеред завантаження, після запуску можна запропонувати продовжити."""
  try:
    with JOB_LOCK:
      items = [{'kid': j['kid'], 'title': j['title'], 'n': j['n'], 'dub': j.get('dub'), 'quality': j.get('quality') or '720p'}
               for j in JOBS if j['status'] in ('queued', 'running')]
    have = {(x['kid'], x['n'], x.get('dub')) for x in items}
    items += [x for x in INTERRUPTED if (x['kid'], x['n'], x.get('dub')) not in have]  # перервані/призупинені не губимо
    if not items and not os.path.exists(_jobs_path()):
      return
    tmp = _jobs_path() + '.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
      json.dump(items, f, ensure_ascii=False)
    os.replace(tmp, _jobs_path())
  except Exception:
    pass


def _pause_path():
  return os.path.join(BASE_DIR, 'dl_pause.json')


def _pause_save():
  try:
    with open(_pause_path(), 'w', encoding='utf8') as f:
      json.dump({'on': bool(PAUSE['on'])}, f)
  except Exception:
    pass


def api_dl_pause(on):
  on = bool(on)
  PAUSE['on'] = on
  if not on:
    PAUSED_KIDS.clear()  # «Продовжити» повертає все, включно з окремо призупиненими аніме
  _pause_save()
  if not on:
    if INTERRUPTED:
      api_jobs_resume()
    _worker_watch()
  return {'ok': True, 'paused': PAUSE['on']}


def _jobs_load():
  INTERRUPTED.clear()
  try:
    with open(_pause_path(), encoding='utf8') as f:
      PAUSE['on'] = bool(json.load(f).get('on'))
  except Exception:
    PAUSE['on'] = False
  try:
    with open(_jobs_path(), encoding='utf8') as f:
      items = json.load(f)
  except Exception:
    return
  for it in items if isinstance(items, list) else []:
    try:
      key, e = _find(it['kid'])
      if not _is_downloaded(e, it['n'], it.get('dub')):
        INTERRUPTED.append(it)
    except Exception:
      continue
  _jobs_persist() if not INTERRUPTED else None


def api_jobs_resume():
  if PAUSE['on']:
    PAUSE['on'] = False
    _pause_save()
  items = list(INTERRUPTED)
  groups = {}
  for it in items:
    groups.setdefault((it['kid'], it.get('dub'), it.get('quality') or '720p'), []).append(it)
  n, errs, left = 0, [], []
  for (kid, dub, q), its in groups.items():
    try:
      n += api_download(kid, [x['n'] for x in its], q, dub)['queued']
    except Exception as ex:
      errs.append(str(ex)[:160] or type(ex).__name__)
      left += its
  INTERRUPTED.clear()
  INTERRUPTED.extend(left)  # те, що не вдалось поставити в чергу, лишається — можна спробувати ще
  _jobs_persist()
  if left:  # _jobs_persist пише лише чергу — докидаємо збережене
    try:
      with open(_jobs_path(), 'w', encoding='utf8') as f:
        json.dump([{'kid': x['kid'], 'title': x['title'], 'n': x['n'], 'dub': x.get('dub'), 'quality': x.get('quality') or '720p'} for x in left], f, ensure_ascii=False)
    except Exception:
      pass
  _worker_watch()
  return {'queued': n, 'errors': list(dict.fromkeys(errs))}


def _spawn_workers():
  """(під JOB_LOCK) Доганяє кількість робочих потоків черги: 1 у звичайному режимі, 3 — у «Багатозадачності»."""
  _worker['threads'] = [t for t in _worker['threads'] if t.is_alive()]
  queued = sum(1 for j in JOBS if j['status'] == 'queued')
  need = min(_lim('jobs') - len(_worker['threads']), queued)
  for _ in range(max(0, need)):
    t = threading.Thread(target=_worker_loop, daemon=True)
    _worker['threads'].append(t)
    _worker['thread'] = t
    t.start()


def _worker_watch():
  """Якщо в черзі є завдання, а робочий потік помер — запускаємо знову."""
  with JOB_LOCK:
    _spawn_workers()


def api_jobs_discard():
  INTERRUPTED.clear()
  _jobs_persist()
  return {'ok': True}


_UNSET = object()


def api_download(kid, ordinals, quality, dub=_UNSET):
  key, e = _find(kid)
  net_guard()
  if e.get('temp') and e.get('preview_of'):
    raise ValueError('Це перегляд іншого сайту: аніме вже є в бібліотеці — завантажуйте звідти.')
  if e.get('temp'):
    with LOCK:
      _promote(e)
    lib_save()
  dub = e.get('dubbing') if dub is _UNSET else dub
  added = 0
  PAUSED_KIDS.discard(kid)  # нове завантаження цього аніме знімає його окрему паузу
  _pause_autoclear()  # а якщо нічого іншого не качається — і глобальну
  with JOB_LOCK:
    busy = {(j['kid'], j['n'], j.get('dub')) for j in JOBS if j['status'] in ('queued', 'running')}
    for o in sorted(_num_arg(x) for x in ordinals):
      if _is_downloaded(e, o, dub) or (kid, o, dub) in busy:
        continue
      _job_seq[0] += 1
      _ce = threading.Event()
      _ce.kid = kid
      JOBS.append({
          'id': _job_seq[0], 'kid': kid, 'title': e['title'], 'n': o, 'dub': dub, 'quality': quality,
          'status': 'queued', 'done': 0, 'total': 0, 'phase': '', 'message': '', 'stage': '',
          'cancel': _ce,
      })
      added += 1
    if added and not (_est['thread'] and _est['thread'].is_alive()):
      te = threading.Thread(target=_estimate_loop, daemon=True)
      _est['thread'] = te
      te.start()
    if added:
      _spawn_workers()
  _jobs_persist()
  return {'queued': added}


_KEYLOCKS = {}
_KL = threading.Lock()


def _klock(k):
  with _KL:
    return _KEYLOCKS.setdefault(k, threading.Lock())


def _skip_key(e, dub, n):
  return f"{(e.get('dubbing') if dub is None else dub) or ''}|{n}"


def _save_skips(e, dub, n, skips):
  """Таймінги інтро/аутро зберігаємо в бібліотеці — щоб кнопки працювали й офлайн."""
  if not skips:
    return
  with LOCK:
    d = e.setdefault('skips', {})
    k = _skip_key(e, dub, n)
    if d.get(k) != skips:
      d[k] = skips
      changed = True
    else:
      changed = False
  if changed:
    lib_save()


def _get_skips(e, n, dub=None):
  return (e.get('skips') or {}).get(_skip_key(e, dub, n)) or {}


def _get_ep(kid, e, n, dub=None):
  """Серія з сайту разом з посиланнями. Сторінка кешується на 15 хв, посилання плеєрів — на 20 хв."""
  dub = e.get('dubbing') if dub is None else dub
  ck = (kid, dub)
  now = time.time()
  cached = INFO_CACHE.get(ck)
  if not cached or now - cached[0] > 900 or n not in cached[1]:
    with _klock(ck):
      cached = INFO_CACHE.get(ck)
      if not cached or time.time() - cached[0] > 900 or n not in cached[1]:
        _stage('Читаю сторінку…')
        info = fetch_info(e['url'])
        m = {x['ordinal']: x for x in _eps_for(info, dub)}
        INFO_CACHE[ck] = (time.time(), m)
  ep = INFO_CACHE[ck][1].get(n)
  if not ep:
    raise RuntimeError('Цієї серії вже немає на сайті.')
  if ep.get('resolve') and (not ep.get('links') or time.time() - ep.get('_res_t', 0) > 1200):
    with _klock((kid, dub, n)):
      if not ep.get('links') or time.time() - ep.get('_res_t', 0) > 1200:
        ep.update(ep['resolve']())
        ep['_res_t'] = time.time()
  _save_skips(e, dub, n, ep.get('skips'))
  if _note_quals(e, dub, n, ep.get('links')):
    lib_save()
  return ep


def warm_ep(kid, n):
  """Заздалегідь готує посилання (сторінка + плеєр), щоб відтворення стартувало миттєво.
  Чекає до 14 с, щоб одразу повернути список реально доступних якостей."""
  def run():
    try:
      key, e = _find(kid)
      _get_ep(kid, e, _num_arg(n))
    except Exception:
      pass
  if net_state()['state'] != 'offline':
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(14)
  try:
    key, e = _find(kid)
    return {'ok': True, 'view': entry_view(key, e)}
  except Exception:
    return {'ok': True}


def _is_hls(url):
  return '.m3u8' in url.split('?')[0].lower()


def _run_job(j):
  key, e = _find(j['kid'])
  n, dub = j['n'], j.get('dub')
  final = _ep_path(e, n, dub)
  if _is_downloaded(e, n, dub):
    j['status'] = 'done'
    return
  tmp = final[:-4] + '.part.mp4'
  _tl.cancel = j['cancel']
  _tl.job = True
  _tl.stage = lambda m: j.__setitem__('stage', m)
  try:
    j['phase'], j['stage'] = 'resolve', 'Отримую посилання…'
    tm = threading.Timer(90, lambda: None if PAUSE['on'] else (j.__setitem__('timeout', True), j['cancel'].set()))  # сайт «мовчить» — не висимо вічно
    tm.daemon = True
    tm.start()
    try:
      ep = _get_ep(j['kid'], e, n, dub)
    finally:
      tm.cancel()
    q, url, hdrs, player = ep_choice(ep, j['quality'])
    if j.get('use_fb') and ep.get('fallback'):  # попередня спроба не вдалась — беремо перевірене посилання з API
      q, url = pick_quality(ep['fallback'], j['quality'])
      hdrs, player = ep.get('headers') or hdrs, 'API'
    if not url:
      raise RuntimeError('Немає посилань на відео.')
    if ep.get('fallback') and url not in ep['fallback'].values():
      j['fb_avail'] = True
    j['q'], j['player'] = q, player
    os.makedirs(os.path.dirname(final), exist_ok=True)
    stats = {}
    hist = collections.deque()
    j['t0'] = time.time()
    hls = _is_hls(url)

    def prog(done, total, phase):
      j['done'], j['total'], j['phase'] = done, total, phase
      if phase != 'download':
        return
      b, tt = (stats.get('bytes', 0), stats.get('total', 0)) if hls else (done, total)
      if hls:
        j['pk'], j['pk_total'] = done, total
      else:
        j['pk'], j['pk_total'] = -(-int(done) // CHUNK), -(-int(total) // CHUNK)
      j['bytes'] = int(b)
      if tt:
        j['bytes_total'] = int(tt)
      now = time.time()
      hist.append((now, b))
      while len(hist) > 2 and now - hist[0][0] > 8:
        hist.popleft()
      if len(hist) >= 2 and now - hist[0][0] >= 1.5 and b > hist[0][1]:
        j['speed'] = (b - hist[0][1]) / (now - hist[0][0])
        SPEED['v'] = j['speed'] if not SPEED['v'] else SPEED['v'] * 0.5 + j['speed'] * 0.5

    j['stage'] = ''
    if _is_hls(url):
      download_episode(url, tmp, log, hdrs, progress=prog, cancel=j['cancel'], stats=stats)
      os.replace(tmp, final)
    else:
      dl = tmp + '.dl'
      download_direct(url, dl, hdrs, progress=prog, cancel=j['cancel'])
      os.replace(dl, final)
    _save_skips(e, dub, n, ep.get('skips'))
    try:
      sz = os.path.getsize(final)
    except OSError:
      sz = 0
    ph = _probe_height(final)
    real = q_label(ph) if ph else q
    e.setdefault('dlinfo', {})[f'{_dk(dub)}|{n}'] = {'q': real, 'p': player}
    lib_save()
    j['q'], j['bytes'], j['bytes_total'] = real, sz, sz
    j['secs'] = round(time.time() - j['t0'])
    j['status'] = 'done'
    j['message'] = (ep.get('pinfo') or (f'{player}' if player else ''))[:200]
  except Cancelled:
    _purge_folder(_folder(e, dub), only_tmp=True, ordinal=n)  # скасовано — прибираємо все недокачане
    raise
  except Exception:
    for x in (tmp, tmp + '.ts'):  # збій — лишаємо .parts/.dl для докачування, прибираємо лише збірку
      _rm(x)
    raise
  finally:
    _tl.cancel = None
    _tl.job = False
    _tl.stage = None


def _estimate_size(url, hdrs):
  try:
    if _is_hls(url):
      info = {}
      sess = requests.Session()
      sess.headers.update(HEADERS)
      sess.headers.update(hdrs or {})
      segs, _enc = load_playlist(url, sess, 0, info)
      if info.get('bw') and info.get('dur'):
        return int(info['bw'] * info['dur'] / 8), len(segs)
      return None, len(segs)
    sz = _probe_range(url, hdrs or {})
    return sz, (-(-sz // CHUNK) if sz else None)
  except Exception:
    return None, None


_est = {'thread': None}


def _estimate_loop():
  """Для завдань у черзі заздалегідь визначає якість і приблизний розмір."""
  while True:
    with JOB_LOCK:
      j = next((x for x in JOBS if x['status'] == 'queued' and not x.get('_est')), None)
      if not j:
        _est['thread'] = None
        return
      j['_est'] = True
    try:
      key, e = _find(j['kid'])
      ep = _get_ep(j['kid'], e, j['n'], j.get('dub'))
      q, url, hdrs, player = ep_choice(ep, j['quality'])
      if url and j['status'] == 'queued':
        j['q'], j['player'] = q, player
        sz, pk = _estimate_size(url, hdrs)
        if j['status'] == 'queued':
          if sz:
            j['bytes_total'] = sz
          if pk:
            j['pk_total'] = pk
    except Exception:
      pass


_RETRY_DELAYS = (45, 120, 300)  # пауза перед повторними заходами, с


def _permanent(ex):
  m = str(ex)
  return isinstance(ex, ValueError) or any(x in m for x in ('ffmpeg не знайдено', 'вже немає на сайті', 'Немає посилань на відео'))


def _sleep_cancellable(j, sec):
  end = time.time() + sec
  while time.time() < end:
    if j['cancel'].is_set():
      raise Cancelled()
    time.sleep(0.3)


def _worker_loop():
  me = threading.current_thread()
  while True:
    with JOB_LOCK:
      now = time.time()
      alive = [t for t in _worker['threads'] if t.is_alive()]
      if me in alive and alive.index(me) >= _lim('jobs'):  # режим вимкнули — зайві потоки йдуть, коли звільняться
        _worker['threads'] = [t for t in _worker['threads'] if t is not me]
        return
      queued = [x for x in JOBS if x['status'] == 'queued']
      j = None if PAUSE['on'] else next((x for x in queued if x.get('not_before', 0) <= now and x['kid'] not in PAUSED_KIDS), None)
      if not queued:
        _worker['threads'] = [t for t in _worker['threads'] if t is not me]
        if not _worker['threads']:
          _worker['thread'] = None
        if any(x['status'] == 'running' for x in JOBS):  # інші потоки ще качають — підсумок за останнім
          return
        b = dict(_BATCH)
        _BATCH.update(t0=None, done=0, bytes=0, err=0)
        if b['done'] or b['err']:  # черга спорожніла — підсумок
          notif_add('dl', count=b['done'], err=b['err'], bytes=b['bytes'], secs=int(time.time() - (b['t0'] or time.time())))
        return
      if j:
        j['status'] = 'running'
        j['retry_in'] = 0
        if _BATCH['t0'] is None:
          _BATCH['t0'] = time.time()
    if not j:  # лишились лише відкладені повтори — чекаємо їхнього часу
      time.sleep(1)
      continue
    try:
      net_guard()
      for attempt in (1, 2):  # одразу пробуємо ще раз; серед завантажених частин докачування продовжується
        try:
          _run_job(j)
          break
        except Cancelled:
          raise
        except Exception as ex:
          if _permanent(ex) or attempt == 2:
            raise
          if j.get('fb_avail') and not j.get('use_fb'):
            j['use_fb'] = True
          j['phase'], j['stage'], j['message'] = 'resolve', 'Помилка мережі — пробую ще раз…', str(ex)[:200]
          _sleep_cancellable(j, 4)
    except (Cancelled, Exception) as ex:
      if isinstance(ex, Cancelled):
        if not j.pop('timeout', None):
          j['status'], j['message'] = 'cancelled', 'Скасовано'
          ex = None
        else:
          j['cancel'] = threading.Event()
          ex = RuntimeError('Сайт не відповів за 90 с')
      if ex is None:
        pass
      else:
       rounds = j.get('rounds', 0)
       if not _permanent(ex) and rounds < len(_RETRY_DELAYS) and not j['cancel'].is_set():
        j['rounds'] = rounds + 1
        j['not_before'] = time.time() + _RETRY_DELAYS[rounds]
        j['status'], j['phase'], j['stage'] = 'queued', '', ''
        j['message'] = str(ex)[:200]
        j['_est'] = True
       else:
        j['status'], j['message'] = 'error', str(ex)[:300]
    if j['status'] == 'done':
      _BATCH['done'] += 1
      _BATCH['bytes'] += int(j.get('bytes_total') or j.get('bytes') or 0)
    elif j['status'] == 'error':
      _BATCH['err'] += 1
    _jobs_persist()
    with JOB_LOCK:  # лишаємо в списку лише останні 60
      while len(JOBS) > 60:
        old = next((x for x in JOBS if x['status'] not in ('queued', 'running')), None)
        if not old:
          break
        JOBS.remove(old)


def api_jobs():
  _worker_watch()
  with JOB_LOCK:
    jobs = [_job_public(j) for j in JOBS]
  live = [j for j in jobs if j['status'] in ('queued', 'running')]
  left, unknown, pk_total, pk_left = 0, 0, 0, 0
  for j in live:
    pt = j.get('pk_total') or 0
    pk_total += pt
    pk_left += max(0, pt - (j.get('pk') or 0)) if j['status'] == 'running' else pt
    bt = j.get('bytes_total') or 0
    if not bt:
      unknown += 1
    elif j['status'] == 'running':
      left += max(0, bt - (j.get('bytes') or 0))
    else:
      left += bt
  run = next((j for j in live if j['status'] == 'running'), None)
  spd = (run or {}).get('speed') or SPEED['v']
  total = sum((j.get('bytes_total') or 0) for j in live)
  summ = {'count': len(live), 'pk_total': pk_total, 'pk_left': pk_left, 'bytes_total': total, 'bytes_left': left, 'unknown': unknown,
          'speed': spd, 'eta': (left / spd) if spd and left else None}
  intr = {}
  for it in list(INTERRUPTED):
    intr.setdefault(it['title'], []).append(it['n'])
  return {'jobs': jobs, 'active': bool(live), 'summary': summ, 'paused': PAUSE['on'],
          'interrupted': [{'title': t, 'n': ns} for t, ns in intr.items()]}


def api_kid_pause(kid, on):
  if on:
    PAUSED_KIDS.add(kid)
  else:
    PAUSED_KIDS.discard(kid)
    _worker_watch()
  return {'ok': True, 'paused': kid in PAUSED_KIDS}


def _pause_autoclear():
  """Глобальна пауза не має «залипати», коли качати вже нічого: інакше нове аніме не стартує."""
  with JOB_LOCK:
    live = any(j['status'] in ('queued', 'running') and not j['cancel'].is_set() for j in JOBS)
  if not live and (PAUSE['on'] or PAUSED_KIDS):
    PAUSE['on'] = False
    PAUSED_KIDS.clear()
    _pause_save()


def api_cancel(job_id=None, kid=None):
  if kid is not None:
    PAUSED_KIDS.discard(kid)
  if job_id is None and kid is None:  # «скасувати все» — знімаємо і паузу, і перервані
    PAUSE['on'] = False
    PAUSED_KIDS.clear()
    try:
      INTERRUPTED.clear()
    except Exception:
      pass
    _pause_save()
  with JOB_LOCK:
    for j in JOBS:
      if (job_id is None or j['id'] == job_id) and (kid is None or j['kid'] == kid) and j['status'] in ('queued', 'running'):
        j['cancel'].set()
        if j['status'] == 'queued':
          j['status'], j['message'] = 'cancelled', 'Скасовано'
          if j.get('rounds'):  # відкладений повтор мав недокачані частини — прибираємо
            try:
              _purge_folder(_folder(_find(j['kid'])[1], j.get('dub')), only_tmp=True, ordinal=j['n'])
            except Exception:
              pass
        else:
          j['stage'] = 'Скасовую…'
  _jobs_persist()
  _pause_autoclear()
  return {'ok': True}


def api_jobs_retry(kid):
  """Продовжити скасовані серії аніме: ставимо їх у чергу знову, а скасовані записи прибираємо зі списку."""
  with JOB_LOCK:
    old = [j for j in JOBS if j['kid'] == kid and j['status'] == 'cancelled']
  groups = {}
  for j in old:
    groups.setdefault((j.get('quality') or '720p', j.get('dub')), []).append(j['n'])
  queued = 0
  for (q, dub), ns in groups.items():
    r = api_download(kid, ns, q, dub)
    queued += int((r or {}).get('queued') or 0)
  with JOB_LOCK:
    JOBS[:] = [j for j in JOBS if not (j['kid'] == kid and j['status'] == 'cancelled')]
  _jobs_persist()
  return {'queued': queued}


def api_jobs_clear():
  with JOB_LOCK:
    JOBS[:] = [j for j in JOBS if j['status'] in ('queued', 'running')]
  return {'ok': True}



# ═════════════════════════ Мережа та налаштування ═════════════════════════

def _ifaces():
  """[(ім'я, сім'я 4|6, ip)] через getifaddrs (iOS: en0 = Wi-Fi, pdp_ip* = мобільна мережа)."""
  import ctypes
  import ctypes.util
  import socket

  class IFA(ctypes.Structure):
    pass
  IFA._fields_ = [('next', ctypes.POINTER(IFA)), ('name', ctypes.c_char_p),
                  ('flags', ctypes.c_uint), ('addr', ctypes.c_void_p)]
  libc = ctypes.CDLL(ctypes.util.find_library('c') or None)
  head = ctypes.POINTER(IFA)()
  if libc.getifaddrs(ctypes.byref(head)) != 0:
    return []
  res = []
  mac = sys.platform == 'darwin'
  try:
    cur = head
    while cur:
      ifa = cur.contents
      if ifa.addr and (ifa.flags & 1):  # IFF_UP
        b = ctypes.string_at(ifa.addr, 28)
        fam = b[1] if mac else b[0]
        try:
          if fam == 2:
            res.append((ifa.name.decode(), 4, socket.inet_ntoa(b[4:8])))
          elif fam == (30 if mac else 10):
            res.append((ifa.name.decode(), 6, socket.inet_ntop(socket.AF_INET6, b[8:24])))
        except Exception:
          pass
      cur = ifa.next
  finally:
    libc.freeifaddrs(head)
  return res


def _iface_kind(name):
  n = name or ''
  if n.startswith(('pdp_ip', 'rmnet', 'ccmni', 'wwan', 'ppp')):
    return 'cell'
  if n.startswith(('en', 'wlan', 'eth', 'wl', 'bridge')):
    return 'wifi'
  return None


def _usable(ip, fam):
  if fam == 4:
    return not (ip.startswith('127.') or ip.startswith('169.254.') or ip == '0.0.0.0')
  low = ip.lower()
  return not (low == '::1' or low.startswith('fe80') or low == '::')


_NET = {'t': 0, 'v': None}
ISLAND_OK = {'v': False}  # iOS: пристрій має Dynamic Island і збірка містить Swift-міст
NET_PROVIDER = {'fn': None}  # iOS: SCNetworkReachability → 'wifi' | 'cell' | 'offline' | None (не знаю)


def _route_ip(fam):
  import socket
  try:
    sk = socket.socket(socket.AF_INET if fam == 4 else socket.AF_INET6, socket.SOCK_DGRAM)
    sk.settimeout(2)
    sk.connect(('1.1.1.1', 53) if fam == 4 else ('2606:4700:4700::1111', 53, 0, 0))
    ip = sk.getsockname()[0]
    sk.close()
    return ip
  except Exception:
    return None


def net_state():
  """{'state': wifi | cell | offline | online, 'iface': ім'я}. Кеш на 3 с.
  Спершу інтерфейс реального маршруту (IPv4, потім IPv6 — мобільна мережа часто лише IPv6);
  якщо це VPN/невідоме ім'я — дивимось, які інтерфейси живі: en* з адресою → Wi-Fi, інакше pdp_ip* → мобільна."""
  now = time.time()
  if _NET['v'] and now - _NET['t'] < 3:
    return _NET['v']
  if NET_PROVIDER.get('fn'):
    try:
      k = NET_PROVIDER['fn']()
    except Exception:
      k = None
    if k in ('wifi', 'cell', 'offline'):
      v = {'state': k, 'iface': 'native'}
      _NET.update(t=now, v=v)
      return v
  v = {'state': 'offline', 'iface': None}
  try:
    r4, r6 = _route_ip(4), _route_ip(6)
    if r4 or r6:
      v = {'state': 'online', 'iface': None}
      try:
        ifs = _ifaces()
      except Exception:
        ifs = []
      name = None
      for ip, fam in ((r4, 4), (r6, 6)):
        if ip:
          for n, f, a in ifs:
            if f == fam and a == ip:
              name = n
              break
        if name and _iface_kind(name):
          break
      kind = _iface_kind(name)
      if not kind:
        live = [(n, f, a) for n, f, a in ifs if _usable(a, f)]
        if any(_iface_kind(n) == 'wifi' for n, f, a in live):
          kind = 'wifi'
        elif any(_iface_kind(n) == 'cell' for n, f, a in live):
          kind = 'cell'
      v['iface'] = name
      if kind:
        v['state'] = kind
  except Exception:
    pass
  _NET.update(t=now, v=v)
  return v


SETTINGS = {'mobile_download': False, 'auto_skip': False, 'auto_skip_opening': True, 'auto_skip_ending': True, 'auto_next': True,
            'auto_check': True, 'auto_check_hours': 3, 'blur_thumbs': False, 'logging': False, 'multitask': True, 'ios_player': True, 'np_ep_cover': True,
            'bg_keep': True, 'bg_check': False, 'island': True, 'island_dl': True, 'island_check': False,
            'skip_mode': 'instant', 'vol_max': 100, 'client': {},
            'site_order': ['aniliberty', 'yummyani', 'animevost', 'animru'], 'view': {}}


def _settings_path():
  return os.path.join(BASE_DIR, 'settings.json')


def settings_load():
  try:
    with open(_settings_path(), encoding='utf8') as f:
      d = json.load(f)
      SETTINGS.update({k: v for k, v in d.items() if k in SETTINGS})
      if 'auto_skip' not in d and (d.get('auto_skip_opening') or d.get('auto_skip_ending')):
        SETTINGS['auto_skip'] = True
  except Exception:
    pass


def _apply_multitask():
  """Застосовує ліміти режиму «Багатозадачність» на льоту й запускає додаткові робочі потоки черги."""
  _NET_SEM.set_limit(_lim('net'))
  _BROWSE_SEM.set_limit(_lim('browse'))
  _HEAVY.set_limit(_lim('heavy'))
  _HEAVY_BG.set_limit(_lim('heavy_bg'))
  _THUMB_SEM.set_limit(_lim('thumb'))
  with _IMG_SEMS_LOCK:
    for sm in _IMG_SEMS.values():
      sm.set_limit(_lim('img'))
  _worker_watch()


def api_settings(data=None):
  if data:
    with LOCK:
      for k in SETTINGS:
        if k in data:
          if isinstance(SETTINGS[k], bool):
            SETTINGS[k] = bool(data[k])
          elif isinstance(SETTINGS[k], dict):
            if isinstance(data[k], dict) and len(json.dumps(data[k])) < 4000:
              SETTINGS[k] = data[k]
          elif k == 'skip_mode':
            SETTINGS[k] = 'soft' if data[k] == 'soft' else 'instant'
          elif k == 'vol_max':
            SETTINGS[k] = 200 if int(float(data[k] or 0)) >= 200 else 100
          elif isinstance(SETTINGS[k], list):
            v = [x for x in (data[k] or []) if x in SITE_IDS]
            v = list(dict.fromkeys(v))
            SETTINGS[k] = v + [x for x in SITE_IDS if x not in v]
          else:
            try:
              SETTINGS[k] = max(1, min(24 * 14, int(float(data[k]))))
            except Exception:
              pass
    with open(_settings_path(), 'w', encoding='utf8') as f:
      json.dump(SETTINGS, f)
    if data and 'logging' in data:
      _log_on() if SETTINGS.get('logging') else _log_off()
    if data and 'multitask' in data:
      _apply_multitask()
  d = dict(SETTINGS)
  d['island_ok'] = bool(ISLAND_OK['v'])
  return d


def net_guard():
  """Піднімає помилку, якщо зараз не можна качати (офлайн / мобільна мережа заборонена)."""
  st = net_state()['state']
  if st == 'offline':
    raise ValueError('Немає інтернету.')
  if st == 'cell' and not SETTINGS['mobile_download']:
    raise ValueError('Завантаження через мобільний інтернет вимкнено в налаштуваннях.')


# ═════════════════════════ Відтворення ═════════════════════════

STREAMS = {}  # sid → {'headers': {...}, 'time': ...}


def _b64e(s):
  return base64.urlsafe_b64encode(s.encode()).decode().rstrip('=')


def _b64d(s):
  return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4)).decode()


SEG_CACHE = collections.OrderedDict()  # url → (bytes, content-type); випереджальне читання сегментів
SEG_BYTES = [0]
SEG_MAX = 80 * 1024 * 1024
SEG_INFLIGHT = set()
SEG_LOCK = threading.Lock()
PREFETCH = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def _seg_type(url, ctype=''):
  ext = url.split('?')[0].rsplit('.', 1)[-1].lower()
  return {'ts': 'video/mp2t', 'm4s': 'video/iso.segment', 'mp4': 'video/mp4', 'aac': 'audio/aac'}.get(ext) or ctype.split(';')[0] or 'video/mp2t'


def _seg_fetch(url, hdrs):
  with SEG_LOCK:
    if url in SEG_CACHE or url in SEG_INFLIGHT:
      return
    SEG_INFLIGHT.add(url)
  try:
    r = SESS.get(url, headers={**HEADERS, **hdrs}, timeout=(6, 25))
    if r.status_code == 200 and r.content:
      with SEG_LOCK:
        SEG_CACHE[url] = (r.content, _seg_type(url, r.headers.get('content-type') or ''))
        SEG_BYTES[0] += len(r.content)
        while SEG_BYTES[0] > SEG_MAX and SEG_CACHE:
          _, (b, _) = SEG_CACHE.popitem(last=False)
          SEG_BYTES[0] -= len(b)
  except Exception:
    pass
  finally:
    with SEG_LOCK:
      SEG_INFLIGHT.discard(url)


def _prefetch(urls, hdrs):
  for u in urls:
    PREFETCH.submit(_seg_fetch, u, hdrs)


_SK_TRY = {}


def _bg_skips(kid, n):
  """Для скачаної серії без збережених таймінгів інтро/аутро — довантажуємо їх із сайту у фоні."""
  now = time.time()
  if now - _SK_TRY.get((kid, n), 0) < 600 or net_state()['state'] == 'offline':
    return
  _SK_TRY[(kid, n)] = now

  def go():
    try:
      key, e = _find(kid)
      _get_ep(kid, e, n)  # зберігає skips у бібліотеці
    except Exception:
      pass
  threading.Thread(target=go, daemon=True).start()


# ───────────────────────── Коментарі (лише читання) ─────────────────────────

_CM_YID = {}


def _cm_text(h):
  h = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', h or '', flags=re.S | re.I)
  h = re.sub(r'<br\s*/?>', '\n', h, flags=re.I)
  h = re.sub(r'</(p|div|li|blockquote)>', '\n', h, flags=re.I)
  h = re.sub(r'<[^>]+>', '', h)
  h = htmllib.unescape(h)
  h = re.sub(r'\[/?[a-zа-я_]+(?:=[^\]]*)?\]', '', h, flags=re.I)
  h = re.sub(r'[ \t\xa0]+', ' ', h)
  return re.sub(r'\n\s*\n\s*\n+', '\n\n', h).strip()


def _cm_div(h, start):
  """Вміст div, що починається на позиції start (враховує вкладені div)."""
  m = re.compile(r'<div\b[^>]*>', re.I).match(h, start)
  if not m:
    return ''
  depth, pos = 1, m.end()
  tag = re.compile(r'<(/?)div\b[^>]*>', re.I)
  while depth:
    t = tag.search(h, pos)
    if not t:
      return h[m.end():]
    depth += -1 if t.group(1) else 1
    pos = t.end()
    if not depth:
      return h[m.end():t.start()]
  return ''


def _cm_dle(html, base):
  out = []
  parts = re.split(r'<(?:div|li|article|section)\b[^>]*\bid=["\']comment-id-(\d+)["\'][^>]*>', html or '')
  for i in range(1, len(parts) - 1, 2):
    cid, ch = parts[i], parts[i + 1]
    tm = re.search(r'<div\b[^>]*\bid=["\']comm-id-%s["\']' % cid, ch)
    text = _cm_text(_cm_div(ch, tm.start())) if tm else ''
    am = (re.search(r'<h3[^>]*>(.*?)</h3>', ch, re.S) or re.search(r'<strong>\s*<a[^>]*>(.*?)</a>', ch, re.S)
          or re.search(r'class=["\'][^"\']*(?:author|autor|name|nick|user)[^"\']*["\'][^>]*>(.*?)</', ch, re.S))
    author = _cm_text(am.group(1)) if am else ''
    dm = re.search(r'\d{1,2}\.\d{2}\.\d{4}[ ,]+\d{1,2}:\d{2}|\d{1,2}\s+[а-яё]+\s+\d{4}[ ,]+\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}', ch, re.I)
    avatar = ''
    am2 = re.search(r"background-image:\s*url\(([^)]+)\)", ch) or re.search(r'<img[^>]+src=["\']([^"\']+)["\']', ch)
    if am2 and 'noavatar' not in am2.group(1):
      avatar = urljoin(base, am2.group(1).strip('\'" '))
    lm = re.search(r'ratingtype\w*[^>]*>\s*([+-]?\d+)', ch)
    if text:
      out.append({'id': cid, 'author': author or 'Гість', 'avatar': avatar, 'date': dm.group(0).strip() if dm else '',
                  'text': text, 'likes': int(lm.group(1)) if lm else None})
  return out


def _cm_dle_page(url, page, site):
  """page — з 1. Спершу легкий ajax-запит (лише коментарі), потім запасний варіант — розбір самої сторінки."""
  u = urlparse(url)
  base = f'{u.scheme}://{u.netloc}/'
  skin = 'AnimeVostNext5' if site == 'animevost' else 'animeru'
  m = re.search(r'/(\d+)-[^/]*', u.path)
  nid = m.group(1) if m else None
  err = None

  def ajax(n):
    r = http_get(f'{base}engine/ajax/comments.php', params={'cstart': page, 'news_id': n, 'skin': skin, 'massact': 'disable'},
                 headers={**HEADERS, 'X-Requested-With': 'XMLHttpRequest', 'Referer': url}, timeout=25, retries=1)
    if r.status_code != 200:
      raise RuntimeError('HTTP %s' % r.status_code)
    try:
      j = r.json()
      h = j.get('comments') if isinstance(j, dict) else ''
    except Exception:
      h = r.text
    return _cm_dle(h or '', base)
  if nid:
    try:
      items = ajax(nid)
      if items:
        return {'items': items, 'more': len(items) >= 10}
    except Cancelled:
      raise
    except Exception as ex:
      err = ex
  if page > 1:
    return {'items': [], 'more': False}
  html = http_get(url, timeout=25, retries=1).text
  items = _cm_dle(html, base)
  if not items:
    cm = re.search(r'rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', html)
    if cm:
      cu = urljoin(url, cm.group(1))
      if cu.split('#')[0] != url.split('#')[0]:
        try:
          html = http_get(cu, timeout=25, retries=1).text
          items = _cm_dle(html, base)
        except Cancelled:
          raise
        except Exception as ex:
          err = err or ex
  if not items and not nid:
    nm = re.search(r'name=["\']post_id["\'][^>]*value=["\'](\d+)', html) or re.search(r"dle_news_id\s*=\s*['\"](\d+)", html)
    if nm:
      try:
        items = ajax(nm.group(1))
      except Cancelled:
        raise
      except Exception as ex:
        err = err or ex
  if not items and err:
    raise RuntimeError('Сайт не віддав коментарі (%s)' % err)
  tp = re.search(r"total_comments_pages\s*=\s*['\"](\d+)", html)
  return {'items': items, 'more': (int(tp.group(1)) > 1) if tp else len(items) >= 10}


def _cm_yummy_item(c, host):
  u = c.get('user') or {}
  av = ''
  a = c.get('avatars') or u.get('avatars') or u.get('avatar') or c.get('avatar')
  if isinstance(a, dict):
    av = a.get('small') or a.get('medium') or a.get('big') or next((v for v in a.values() if isinstance(v, str) and v), '')
  elif isinstance(a, str):
    av = a
  if av.startswith('//'):
    av = 'https:' + av
  elif av.startswith('/'):
    av = f'https://{host}{av}'
  ts = c.get('time') or c.get('created_at') or c.get('date') or 0
  try:
    date = datetime.datetime.fromtimestamp(float(ts)).strftime('%d.%m.%Y %H:%M') if ts else ''
  except Exception:
    date = str(ts)
  txt = c.get('text') or c.get('message') or c.get('content') or c.get('body') or ''
  txt = re.sub(r'\[ник\](.*?)\[/ник\]', r'@\1', str(txt), flags=re.S)
  likes = c.get('likes')
  if likes is None:
    likes = c.get('rating')
  try:
    likes = int(likes) - int(c.get('dislikes') or 0) if c.get('dislikes') is not None else int(likes)
  except Exception:
    likes = None
  deleted = bool(c.get('deleted_at'))
  return {'id': str(c.get('id') or c.get('comment_id') or ''), 'author': c.get('nickname') or c.get('name') or u.get('nickname') or u.get('name') or u.get('login') or 'Гість',
          'avatar': av, 'date': date, 'text': 'Коментар видалено' if deleted else _cm_text(txt), 'likes': likes,
          'children': int(c.get('children_count') or 0), 'deleted': deleted}


def _cm_yummy(url, page, parent):
  u = urlparse(url)
  host = u.netloc
  slug = unquote(u.path.split('/catalog/item/', 1)[1].strip('/').split('/')[0]) if '/catalog/item/' in u.path else ''
  if parent:
    h, d = _yummy_call(host, f'/comments/{parent}/children', {'skip': page * 20})
    lst = d.get('comments') if isinstance(d, dict) else d
    lst = list(reversed(lst or []))
    return {'items': [_cm_yummy_item(c, h) for c in lst], 'more': len(lst) >= 20}
  aid = _CM_YID.get(slug)
  if not aid:
    h, a = _yummy_call(host, '/anime/' + quote(slug, safe=''), {'need_videos': 'false'})
    aid = a.get('anime_id')
    if not aid:
      raise RuntimeError('не вдалося визначити ID аніме')
    _CM_YID[slug] = aid
  h, d = _yummy_call(host, f'/comments/anime/{aid}', {'skip': page * 20, 'sort': 'new'})
  lst = (d.get('comments') if isinstance(d, dict) else d) or []
  total = d.get('total') if isinstance(d, dict) else None
  items = [_cm_yummy_item(c, h) for c in lst if not c.get('parent_id')]
  more = (page * 20 + len(lst) < int(total)) if total else len(lst) >= 20
  return {'items': items, 'more': more}


def _cm_liberty(url, page):
  host = urlparse(url).netloc
  m = re.search(r'/release/([^/?#]+)', urlparse(url).path)
  alias = m.group(1) if m else ''
  if not alias:
    raise RuntimeError('не розпізнав адресу релізу')
  last = None
  for path in (f'/api/v1/anime/releases/{alias}/comments', f'/api/v1/comments/releases/{alias}', f'/api/v1/anime/comments/{alias}'):
    try:
      r = http_get(f'https://{host}{path}', params={'page': page + 1, 'limit': 20}, headers={**HEADERS, 'Accept': 'application/json'}, timeout=12, retries=0)
      if r.status_code != 200:
        last = r.status_code
        continue
      j = r.json()
      lst = j.get('data') if isinstance(j, dict) else j
      if isinstance(lst, dict):
        lst = lst.get('comments') or lst.get('items') or []
      if not isinstance(lst, list):
        continue
      out = []
      for c in lst:
        u = c.get('user') or {}
        out.append({'id': str(c.get('id') or ''), 'author': u.get('login') or u.get('nickname') or c.get('author') or 'Гість',
                    'avatar': '', 'date': str(c.get('created_at') or '')[:16].replace('T', ' '),
                    'text': _cm_text(c.get('message') or c.get('text') or ''), 'likes': c.get('votes') if isinstance(c.get('votes'), int) else None})
      return {'items': [x for x in out if x['text']], 'more': len(lst) >= 20}
    except Cancelled:
      raise
    except Exception as ex:
      last = ex
  raise RuntimeError('AniLiberty не віддає коментарі через відомі мені адреси (останнє: %s)' % (last,))


def api_comments(kid, page, parent=None):
  key, e = _find(kid)
  if net_state()['state'] == 'offline':
    raise ValueError('Коментарі доступні лише онлайн.')
  url = e.get('url') or ''
  site = site_info(url)
  try:
    page = max(0, int(page or 0))
  except Exception:
    page = 0
  sid = site['id']
  if sid == 'yummyani':
    res = _cm_yummy(url, page, parent)
  elif sid in ('animevost', 'animru'):
    res = _cm_dle_page(url, page + 1, sid)
  elif sid == 'aniliberty':
    res = _cm_liberty(url, page)
  else:
    raise ValueError('Для цього сайту коментарі не підтримуються.')
  res['site'] = site
  res['page'] = page
  return res


def api_skips(kid, n):
  key, e = _find(kid)
  return {'skips': _get_skips(e, _num_arg(n))}


def api_stream(kid, n, quality):
  key, e = _find(kid)
  n = _num_arg(n)
  if _is_downloaded(e, n):
    info = (e.get('dlinfo') or {}).get(f'{_dk(e.get("dubbing"))}|{n}') or {}
    try:
      size = os.path.getsize(_ep_path(e, n))
    except OSError:
      size = 0
    sk = _get_skips(e, n)
    if not sk:
      _bg_skips(kid, n)
    return {'type': 'file', 'url': f'/media/{kid}/{n}', 'skips': sk, 'quality': info.get('q'), 'size': size, 'skips_pending': not sk}
  if net_state()['state'] == 'offline':
    raise ValueError('Серія не завантажена, а інтернету немає.')
  ep = _get_ep(kid, e, n)
  q, url, hdrs, player = ep_choice(ep, quality)
  if not url:
    raise RuntimeError('Немає посилань на відео.')
  sid = hashlib.sha1(f'{kid}{n}{time.time()}'.encode()).hexdigest()[:10]
  now = time.time()
  for s_ in [s_ for s_, v in STREAMS.items() if now - v['time'] > 6 * 3600]:
    del STREAMS[s_]
  STREAMS[sid] = {'headers': hdrs or {}, 'time': now, 'next': {}}
  hls = _is_hls(url)
  return {'type': 'hls' if hls else 'mp4', 'url': f'/hls/{sid}/{_b64e(url)}' + ('.m3u8' if hls else ''),
          'quality': q, 'player': player, 'skips': ep.get('skips') or _get_skips(e, n)}


def _rewrite_playlist(text, base, sid):
  segs = []

  def prox(u):
    absu = _join(base, u.strip())
    is_pl = absu.split('?')[0].endswith('.m3u8')
    if not is_pl:
      segs.append(absu)
    return f'/hls/{sid}/{_b64e(absu)}' + ('.m3u8' if is_pl else '')

  out = []
  for line in text.splitlines():
    s_ = line.strip()
    if not s_:
      out.append(line)
    elif s_.startswith('#'):
      out.append(re.sub(r'URI="([^"]+)"', lambda m: f'URI="{prox(m.group(1))}"', line))
    else:
      out.append(prox(s_))
  st = STREAMS.get(sid)
  if st is not None and segs:  # запам'ятовуємо порядок, щоб читати наперед
    for i, u in enumerate(segs):
      st['next'][u] = segs[i + 1:i + 5]
    _prefetch(segs[:3], st['headers'])
  return '\n'.join(out) + '\n'


# ═════════════════════════ HTTP ═════════════════════════

class Handler(BaseHTTPRequestHandler):
  protocol_version = 'HTTP/1.1'

  def log_message(self, fmt, *a):
    try:
      RECENT.append(time.strftime('%H:%M:%S ') + str((a[0] if a else fmt) or '')[:140])
    except Exception:
      pass

  # ---- утиліти ----
  def _json(self, obj, code=200):
    data = json.dumps(obj, ensure_ascii=False).encode('utf8')
    self.send_response(code)
    self.send_header('Content-Type', 'application/json; charset=utf-8')
    self.send_header('Content-Length', str(len(data)))
    self.send_header('Cache-Control', 'no-store')
    self.end_headers()
    self.wfile.write(data)

  def _bytes(self, data, ctype, code=200, extra=None):
    self.send_response(code)
    self.send_header('Content-Type', ctype)
    self.send_header('Content-Length', str(len(data)))
    for k, v in (extra or {}).items():
      self.send_header(k, v)
    self.end_headers()
    self.wfile.write(data)

  def _body(self):
    n = int(self.headers.get('Content-Length') or 0)
    if not n:
      return {}
    try:
      return json.loads(self.rfile.read(n).decode('utf8'))
    except Exception:
      return {}

  # ---- маршрути ----
  def do_GET(self):
    _inflight_run(self, self._do_GET)

  def _do_GET(self):
    try:
      u = urlparse(self.path)
      q = {k: v[0] for k, v in parse_qs(u.query).items()}
      p = u.path
      if p in ('/', '/index.html'):
        html = INDEX_HTML.replace('<html ', '<html data-ios="1" ', 1) if IOS_APP else INDEX_HTML
        return self._bytes(html.encode('utf8'), 'text/html; charset=utf-8', extra={'Cache-Control': 'no-store'})
      if p == '/api/library':
        with LOCK:
          srt = sorted(library.items(), key=lambda kv: kv[1].get('order', 0))
          items = [entry_view(k, e) for k, e in srt if not e.get('temp')]
          tmp = [entry_view(k, e) for k, e in srt if e.get('temp')]
        return self._json({'items': items, 'tmp': tmp, 'ffmpeg': True})
      if p == '/api/jobs':
        return self._json(api_jobs())
      if p == '/api/notifs':
        return self._json(api_notifs())
      if p == '/api/net':
        return self._json({**net_state(), 'mobile_ok': SETTINGS['mobile_download']})
      if p == '/api/storage':
        return self._json(api_storage())
      if p == '/api/settings':
        return self._json(api_settings())
      if p == '/api/search_status':
        return self._json(api_search_status(q.get('task')))
      if p == '/api/lookup_status':
        return self._json(api_lookup_status(q.get('task')))
      if p == '/api/comments':
        return self._json(api_comments(q.get('id'), q.get('page'), q.get('parent')))
      if p == '/api/skips':
        return self._json(api_skips(q.get('id'), q.get('n')))
      if p == '/api/stream':
        return self._json(api_stream(q.get('id'), q.get('n'), q.get('q') or '720p'))
      if p == '/api/autocheck':
        return self._json(api_autocheck(bool(q.get('kick'))))
      if p.startswith('/thumb/'):
        return self._thumb(p[len('/thumb/'):])
      if p.startswith('/poster/'):
        return self._poster(p[len('/poster/'):])
      if p.startswith('/media/'):
        return self._media(p)
      if p.startswith('/hls/'):
        return self._hls(p)
      if p.startswith('/logo/'):
        _, _, sid, var = (p.split('/') + ['', ''])[:4]
        svg = (LOGOS.get(sid) or {}).get(var.replace('.svg', ''))
        if not svg:
          return self._json({'error': 'not found'}, 404)
        return self._bytes(svg.encode('utf8'), 'image/svg+xml', extra={'Cache-Control': 'public, max-age=86400'})
      if p == '/img':
        return self._img(q.get('u'), q.get('r'))
      if p == '/relposter':
        return self._img(q.get('u'), q.get('r'), rel=True)
      return self._json({'error': 'not found'}, 404)
    except (BrokenPipeError, ConnectionResetError):
      pass
    except KeyError as ex:
      self._safe_error(str(ex.args[0]) if ex.args else 'Не знайдено', 404)
    except Exception as ex:
      import traceback
      crash_report('request_error', self.path + '\n' + ''.join(traceback.format_exception(type(ex), ex, ex.__traceback__)))
      self._safe_error(str(ex), 500)

  def do_POST(self):
    _inflight_run(self, self._do_POST)

  def _do_POST(self):
    try:
      p = urlparse(self.path).path
      if p == '/api/backup_upload':  # архів з «Файлів» передається сирим тілом — пишемо потоком на диск, без завантаження в пам'ять
        r = api_backup_upload(self)
        return self._json(r)
      b = self._body()
      routes = {
          '/api/lookup': lambda: api_lookup_start(b.get('url')),
          '/api/lookup_cancel': lambda: api_lookup_cancel(b.get('task')),
          '/api/set_dub': lambda: api_set_dub(b.get('id'), b.get('dubbing')),
          '/api/warm': lambda: warm_ep(b.get('id'), b.get('n')),
          '/api/add': lambda: api_add(b.get('token'), b.get('dubbing')),
          '/api/search': lambda: api_search_start(b.get('q')),
          '/api/browse': lambda: api_browse(b.get('site'), b.get('page'), bool(b.get('force'))),
          '/api/clientlog': lambda: api_clientlog(b.get('msg')),
          '/api/open': lambda: api_open_start(b.get('url'), b.get('sources'), bool(b.get('explicit'))),
          '/api/keep': lambda: api_keep(b.get('id'), bool(b.get('archived'))),
          '/api/archive': lambda: api_archive(b.get('id'), bool(b.get('value', True))),
          '/api/rename': lambda: api_rename(b.get('id'), b.get('name')),
          '/api/group': lambda: api_group(b.get('ids'), b.get('name'), b.get('gid')),
          '/api/ungroup': lambda: api_ungroup(b.get('ids')),
          '/api/group_rename': lambda: api_group_rename(b.get('gid'), b.get('name')),
          '/api/find_alts': lambda: api_find_alts_start(b.get('id')),
          '/api/switch_site': lambda: api_switch_site(b.get('id'), b.get('url')),
          '/api/delete_files': lambda: api_delete_files(b.get('id'), b.get('episodes')),
          '/api/watched_many': lambda: api_watched_many(b.get('id'), b.get('episodes'), bool(b.get('value', True))),
          '/api/check': lambda: api_check(b.get('id')),
          '/api/watched': lambda: api_watched(b.get('id'), b.get('n'), bool(b.get('value', True)), b.get('upto')),
          '/api/remove': lambda: api_remove(b.get('id')),
          '/api/delete_file': lambda: api_delete_file(b.get('id'), b.get('n')),
          '/api/beat': lambda: api_beat(b.get('closed')),
          '/api/progress': lambda: api_progress(b.get('id'), b.get('n'), b.get('t', 0)),
          '/api/download': lambda: api_download(b.get('id'), b.get('episodes') or [], b.get('quality') or '720p'),
          '/api/cancel': lambda: api_cancel(b.get('job'), b.get('kid')),
          '/api/export': lambda: api_export(b.get('mode')),
          '/api/backup_status': lambda: api_backup_status(),
          '/api/backup_cancel': lambda: api_backup_cancel(),
          '/api/backup_share': lambda: api_backup_share(b.get('rel')),
          '/api/backup_list': lambda: api_backup_list(),
          '/api/import': lambda: api_import(b.get('rel')),
          '/api/import_dl': lambda: api_import_dl(),
          '/api/backup_delete': lambda: api_backup_delete(b.get('rel')),
          '/api/dl_pause_kid': lambda: api_kid_pause(b.get('kid'), b.get('on')),
          '/api/jobs_retry': lambda: api_jobs_retry(b.get('kid')),
          '/api/jobs_clear': lambda: api_jobs_clear(),
          '/api/jobs_resume': lambda: api_jobs_resume(),
          '/api/notifs_clear': lambda: api_notifs_clear(),
          '/api/notifs_read': lambda: api_notifs_read(b.get('id')),
          '/api/jobs_discard': lambda: api_jobs_discard(),
          '/api/dl_pause': lambda: api_dl_pause(b.get('on')),
          '/api/settings': lambda: api_settings(b),
          '/api/storage_clear': lambda: api_storage_clear(b.get('what'), b.get('id'), b.get('name')),
          '/api/reorder': lambda: api_reorder(b.get('ids')),
      }
      fn = routes.get(p)
      if not fn:
        return self._json({'error': 'not found'}, 404)
      return self._json(fn())
    except (BrokenPipeError, ConnectionResetError):
      pass
    except KeyError as ex:
      self._safe_error(str(ex.args[0]) if ex.args else 'Не знайдено', 404)
    except (ValueError, RuntimeError) as ex:
      self._safe_error(str(ex), 400)
    except Exception as ex:
      self._safe_error(f'{type(ex).__name__}: {ex}', 500)

  def _safe_error(self, msg, code):
    try:
      self._json({'error': msg}, code)
    except Exception:
      pass

  # ---- файли ----
  def _thumb(self, rest):
    parts = rest.split('?')[0].split('/')
    pf = api_thumb(parts[0], unquote(parts[1])) if len(parts) > 1 else None
    if not pf:
      return self._json({'error': 'no thumb'}, 404)
    with open(pf, 'rb') as f:
      data = f.read()
    self._bytes(data, 'image/jpeg', extra={'Cache-Control': 'max-age=86400'})

  def _poster(self, kid):
    pf = _poster_file(kid.split('?')[0])
    if not pf:
      return self._json({'error': 'no poster'}, 404)
    with open(pf, 'rb') as f:
      data = f.read()
    ctype = POSTER_TYPES.get(os.path.splitext(pf)[1].lower(), 'image/jpeg')
    self._bytes(data, ctype, extra={'Cache-Control': 'max-age=86400'})

  def _img(self, url, retry=None, rel=False):
    """Проксі зображень. Чекаємо не довше ~6 с: повільний сайт не повинен тримати з'єднання браузера (їх лише ~6 на хост — інакше зависає й пошук); завантаження триває у фоні, а повтор забере файл з кешу."""
    if not url or not re.match(r'^https?://', url):
      return self._json({'error': 'bad url'}, 400)
    fut = rel_async(url, bool(retry)) if rel else img_async(url, bool(retry))
    try:
      got = fut.result(timeout=6)
    except concurrent.futures.TimeoutError:
      return self._json({'error': 'image pending'}, 504)
    if not got:
      return self._json({'error': 'image unavailable'}, 404)
    data, ct = got
    if not rel:
      data, ct = _shrink_img(url, data, ct)
    self._bytes(data, ct, extra={'Cache-Control': 'public, max-age=604800'})

  def _media(self, path):
    parts = path.strip('/').split('/')  # media / kid / n
    key, e = _find(parts[1])
    fp = _ep_path(e, _num_arg(unquote(parts[2])))
    if not os.path.exists(fp):
      return self._json({'error': 'Файл не знайдено'}, 404)
    size = os.path.getsize(fp)
    start, end = 0, size - 1
    rng = self.headers.get('Range')
    code = 200
    if rng:
      m = re.match(r'bytes=(\d*)-(\d*)', rng)
      if m:
        if m.group(1) == '' and m.group(2):
          start = max(0, size - int(m.group(2)))
        else:
          start = int(m.group(1) or 0)
          if m.group(2):
            end = min(int(m.group(2)), size - 1)
        if start >= size:
          self.send_response(416)
          self.send_header('Content-Range', f'bytes */{size}')
          self.send_header('Content-Length', '0')
          self.end_headers()
          return
        code = 206
    length = end - start + 1
    self.send_response(code)
    self.send_header('Content-Type', 'video/mp4')
    self.send_header('Accept-Ranges', 'bytes')
    self.send_header('Content-Length', str(length))
    if code == 206:
      self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
    self.end_headers()
    with open(fp, 'rb') as f:
      f.seek(start)
      left = length
      while left > 0:
        chunk = f.read(min(262144, left))
        if not chunk:
          break
        self.wfile.write(chunk)
        left -= len(chunk)

  def _hls(self, path):
    """Проксі для онлайн-потоку: додає потрібні заголовки, переписує плейлисти, читає сегменти наперед."""
    parts = path.split('/')  # '', hls, sid, b64[.m3u8]
    st = STREAMS.get(parts[2])
    if not st:
      return self._json({'error': 'Потік застарів'}, 410)
    url = _b64d(parts[3].rsplit('.m3u8', 1)[0] if parts[3].endswith('.m3u8') else parts[3])
    hdrs = {**HEADERS, **st['headers']}
    rng = self.headers.get('Range')
    if not rng:
      with SEG_LOCK:
        hit = SEG_CACHE.get(url)
      if hit:
        self._bytes(hit[0], hit[1], extra={'Cache-Control': 'max-age=600'})
        _prefetch(st['next'].get(url, []), st['headers'])
        return
    if rng:
      hdrs['Range'] = rng
    r = SESS.get(url, headers=hdrs, timeout=(6, 25), stream=True)
    ctype = (r.headers.get('content-type') or '').lower()
    is_pl = 'mpegurl' in ctype or url.split('?')[0].endswith('.m3u8')
    if is_pl:
      text = r.content.decode('utf8', 'replace')
      data = _rewrite_playlist(text, url, parts[2]).encode('utf8')
      return self._bytes(data, 'application/vnd.apple.mpegurl', extra={'Cache-Control': 'no-store'})
    if not rng and r.status_code == 200 and url in st['next']:
      data = r.content
      self._bytes(data, _seg_type(url, ctype), extra={'Cache-Control': 'max-age=600'})
      _prefetch(st['next'].get(url, []), st['headers'])
      return
    self.send_response(r.status_code if r.status_code in (200, 206) else 502)
    self.send_header('Content-Type', _seg_type(url, ctype))
    self.send_header('Accept-Ranges', 'bytes')
    if r.headers.get('Content-Length'):
      self.send_header('Content-Length', r.headers['Content-Length'])
    else:
      self.close_connection = True
    if r.headers.get('Content-Range'):
      self.send_header('Content-Range', r.headers['Content-Range'])
    self.end_headers()
    for chunk in r.iter_content(65536):
      self.wfile.write(chunk)


# ═════════════════════════ Пам'ять: облік місця й очищення ═════════════════════════

def _fsize(path):
  try:
    return os.path.getsize(path)
  except OSError:
    return 0


def _dir_size(path):
  """(байти, кількість файлів) усередині папки, рекурсивно."""
  tot = n = 0
  stack = [path]
  while stack:
    d = stack.pop()
    try:
      with os.scandir(d) as it:
        for x in it:
          try:
            if x.is_dir(follow_symlinks=False):
              stack.append(x.path)
            elif x.is_file(follow_symlinks=False):
              tot += x.stat().st_size
              n += 1
          except OSError:
            pass
    except OSError:
      pass
  return tot, n


def _folder_eps(path):
  """(розмір готових серій, кількість серій, розмір недокачаного, кількість недокачаних файлів) у папці аніме."""
  ok = nok = part = npart = 0
  try:
    for fn in os.listdir(path):
      m = _EP_FILE_RE.match(fn)
      if not m:
        continue
      sz = _fsize(os.path.join(path, fn))
      if m.group(2) == '.mp4':
        ok += sz
        nok += 1 if sz else 0
      else:
        part += sz
        npart += 1
  except OSError:
    pass
  return ok, nok, part, npart


def _app_file():
  try:
    f = sys.modules['__main__'].__dict__.get('__file__')
    if f and os.path.isfile(f):
      return f
  except Exception:
    pass
  p = os.path.join(BASE_DIR, 'anime_app.py')
  return p if os.path.isfile(p) else None


APP_BUNDLE = {'path': None}
DISK_PROVIDER = {'fn': None}


def _did(path):
  """Ідентичність каталогу (пристрій, inode). На iOS (APFS без урахування регістру й Unicode-форми) «Назва» і «назва» — це ОДНА папка;
  рахувати їх за іменем означало б удвічі (а то й втричі) завищувати розмір і кількість серій."""
  try:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)
  except OSError:
    return None


def _nk(s):
  """Ключ для порівняння імен папок: Unicode-нормалізація (iOS/перенесення часто дають «й» як и+знак), регістр, пробіли."""
  return unicodedata.normalize('NFC', s or '').casefold().strip()


def _entry_extra_dirs(e, used, seen_ids=None):
  """Папки в BASE_DIR, що належать цьому аніме, але не збігаються з ним буквально: інша нормалізація імені або озвучка, якої вже нема в списку."""
  tracked = {_nk(f.replace('\\', '/').split('/')[0]) for f in _all_folders(e) if f}
  bases = {_nk(x) for x in (e.get('folder'), safe_name(e.get('title') or '')) if x}
  out = []
  try:
    for fn in os.listdir(BASE_DIR):
      fp = os.path.join(BASE_DIR, fn)
      if fn in used or fn.startswith('.') or fn.startswith('__') or fn in _OWN_DIRS or not os.path.isdir(fp):
        continue
      if seen_ids is not None and _did(fp) in seen_ids:
        continue
      k = _nk(fn)
      if k in tracked or any(k.startswith(b + ' (') and k.endswith(')') for b in bases):
        names = os.listdir(fp)
        if names and all(_EP_FILE_RE.match(x) for x in names):
          out.append(fn)
  except OSError:
    pass
  return out


_OWN_DIRS = ('crash_reports', 'anime_videos', 'app_data', 'Збереження')  # папки самого додатка / його завантажувача — не «сторонні»


# ═════════════════════════ Збереження: експорт / імпорт ═════════════════════════

BK = {'state': 'idle', 'kind': '', 'done': 0, 'total': 0, 'msg': '', 'path': '', 'err': '', 'cancel': threading.Event(), 'result': None}
SHARE_PROVIDER = {'fn': None}  # iOS: показати системне вікно «Поділитись» (зберегти у Файли тощо)
_BK_DIRNAME = 'Збереження'
_BK_DATA = ('library.json', 'settings.json', 'notifs.json')


def _bk_dir():
  d = os.path.join(BASE_DIR, _BK_DIRNAME)
  os.makedirs(d, exist_ok=True)
  return d


def _bk_manifest():
  """Список скачаних серій (аніме → озвучка → серії + якість): за ним імпорт у режимі «лише посилання» ставить усе в чергу."""
  out = []
  with LOCK:
    ents = [(k, dict(e)) for k, e in library.items() if not e.get('temp')]
  for k, e in ents:
    names = list(e.get('all_dubs') or [])
    for d in list((e.get('dubs') or {}).keys()) + [e.get('dubbing'), e.get('base_dub')]:
      if d and d not in names:
        names.append(d)
    seen = set()
    for d in (names or [None]):
      f = _folder(e, d)
      if not f or f in seen:
        continue
      seen.add(f)
      fp = os.path.join(BASE_DIR, f)
      eps = []
      try:
        for fn in os.listdir(fp):
          m = _EP_FILE_RE.match(fn)
          if m and m.group(2) == '.mp4' and _fsize(os.path.join(fp, fn)) > 0:
            eps.append(m.group(1))
      except OSError:
        continue
      if eps:
        q = '720p'
        try:
          h = mp4_height(os.path.join(fp, 'Серія %s.mp4' % eps[0]))
          if h:
            q = '%dp' % h
        except Exception:
          pass
        out.append({'key': k, 'title': e.get('title'), 'dub': d, 'quality': q, 'eps': sorted(eps, key=lambda x: float(x))})
  return out


def _bk_collect(mode):
  """[(шлях у архіві, шлях на диску)]: дані, постери, а для режиму «all» ще й папки з відео."""
  files = []
  for fn in (('settings.json',) if mode == 'settings' else _BK_DATA):
    p = os.path.join(BASE_DIR, fn)
    if os.path.isfile(p):
      files.append((fn, p))
  cache = os.path.join(BASE_DIR, '.cache')
  try:
    if mode == 'settings':
      raise OSError()
    for fn in os.listdir(cache):
      if fn.startswith('poster_') and os.path.isfile(os.path.join(cache, fn)):
        files.append(('.cache/' + fn, os.path.join(cache, fn)))
  except OSError:
    pass
  if mode == 'all':
    with LOCK:
      ents = [dict(e) for e in library.values() if not e.get('temp')]
    folders, seen = [], set()
    for e in ents:
      for f in _all_folders(e):
        if f and f not in seen:
          seen.add(f)
          folders.append(f)
    ids = set()
    for f in folders:
      fp = os.path.join(BASE_DIR, f)
      did = _did(fp)
      if did is None or did in ids:
        continue
      ids.add(did)
      try:
        for fn in os.listdir(fp):
          m = _EP_FILE_RE.match(fn)
          if m and m.group(2) == '.mp4' and _fsize(os.path.join(fp, fn)) > 0:  # недокачані частини не беремо
            files.append((f + '/' + fn, os.path.join(fp, fn)))
      except OSError:
        pass
  return files


def _bk_run_export(mode):
  import zipfile
  BK.update(state='run', kind='export', done=0, total=0, msg='Рахую файли…', path='', err='', result=None)
  BK['cancel'].clear()
  tmp = out = None
  try:
    files = _bk_collect(mode)
    total = sum(_fsize(p) for _, p in files)
    man = {'format': 1, 'mode': mode, 'created': datetime.datetime.now().isoformat(timespec='seconds'), 'downloaded': _bk_manifest() if mode in ('all', 'links') else []}
    try:
      free = shutil.disk_usage(BASE_DIR).free
      if DISK_PROVIDER.get('fn'):
        free = DISK_PROVIDER['fn']()[1]
      if total + (50 << 20) > free:
        raise RuntimeError('Не вистачає місця для архіву: потрібно ≈%s, вільно %s. Оберіть варіант без відео або звільніть місце.' % (_fmt_b(total), _fmt_b(free)))
    except RuntimeError:
      raise
    except Exception:
      pass
    BK['total'] = total
    name = 'Anime-save-%s-%s.zip' % (datetime.datetime.now().strftime('%Y%m%d-%H%M'), {'all': 'video', 'links': 'links', 'lib': 'library', 'settings': 'settings'}.get(mode, 'links'))
    out = os.path.join(_bk_dir(), name)
    tmp = out + '.part'
    BK['msg'] = 'Пакую…'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_STORED, allowZip64=True) as z:
      z.writestr('manifest.json', json.dumps(man, ensure_ascii=False))
      for arc, p in files:
        if BK['cancel'].is_set():
          raise Cancelled()
        BK['msg'] = arc if len(arc) < 60 else '…' + arc[-58:]
        zi = zipfile.ZipInfo(arc, time.localtime(os.path.getmtime(p))[:6])
        zi.compress_type = zipfile.ZIP_STORED
        with open(p, 'rb') as src, z.open(zi, 'w', force_zip64=True) as dst:
          while True:
            if BK['cancel'].is_set():
              raise Cancelled()
            chunk = src.read(1 << 20)
            if not chunk:
              break
            dst.write(chunk)
            BK['done'] += len(chunk)
    os.replace(tmp, out)
    BK.update(state='done', path=out, msg='Готово', result={'name': name, 'size': _fsize(out), 'eps': sum(len(x['eps']) for x in man['downloaded'])})
  except Cancelled:
    BK.update(state='idle', msg='Скасовано')
    for p in (tmp,):
      if p:
        _rm(p)
  except Exception as ex:
    BK.update(state='error', err=str(ex) or ex.__class__.__name__)
    if tmp:
      _rm(tmp)


def _fmt_b(n):
  for u in ('Б', 'КБ', 'МБ', 'ГБ'):
    if n < 1024 or u == 'ГБ':
      return ('%.1f %s' % (n, u)) if u != 'Б' else '%d Б' % n
    n /= 1024.0


def api_export(mode):
  if BK['state'] == 'run':
    raise ValueError('Вже виконується.')
  mode = mode if mode in ('all', 'links', 'lib', 'settings') else 'links'
  threading.Thread(target=_bk_run_export, args=(mode,), daemon=True).start()
  return {'ok': True}


def api_backup_status():
  return {k: BK[k] for k in ('state', 'kind', 'done', 'total', 'msg', 'path', 'err', 'result')}


def api_backup_cancel():
  BK['cancel'].set()
  return {'ok': True}


def api_backup_share(rel=None):
  p = os.path.normpath(os.path.join(BASE_DIR, rel or ''))
  if not p.startswith(os.path.normpath(BASE_DIR)) or not p.lower().endswith('.zip') or not os.path.isfile(p):
    raise ValueError('Файл не знайдено.')
  if SHARE_PROVIDER.get('fn'):
    SHARE_PROVIDER['fn'](p)
    return {'ok': True, 'shared': True}
  return {'ok': True, 'shared': False, 'path': p}


def api_backup_list():
  """Архіви збереження, які лежать у папці програми (Файли → На iPhone → Anime) або в підпапці «Збереження»."""
  import zipfile
  out = []
  for d in (BASE_DIR, os.path.join(BASE_DIR, _BK_DIRNAME)):
    try:
      for fn in sorted(os.listdir(d)):
        p = os.path.join(d, fn)
        if fn.lower().endswith('.zip') and os.path.isfile(p) and fn != '_imported.zip':
          info = {'name': fn, 'rel': os.path.relpath(p, BASE_DIR), 'size': _fsize(p), 'mode': None, 'eps': 0, 'created': None}
          try:
            with zipfile.ZipFile(p) as z:
              if 'manifest.json' not in z.namelist():
                continue
              m = json.loads(z.read('manifest.json').decode('utf8'))
              info.update(mode=m.get('mode'), eps=sum(len(x.get('eps') or []) for x in m.get('downloaded') or []), created=m.get('created'))
          except Exception:
            continue
          out.append(info)
    except OSError:
      pass
  out.sort(key=lambda x: x.get('created') or '', reverse=True)
  return {'files': out, 'dir': BASE_DIR}


def _bk_run_import(rel, cleanup=False):
  import zipfile
  BK.update(state='run', kind='import', done=0, total=0, msg='Читаю архів…', path='', err='', result=None)
  BK['cancel'].clear()
  try:
    p = os.path.normpath(os.path.join(BASE_DIR, rel))
    if not p.startswith(os.path.normpath(BASE_DIR)) or not os.path.isfile(p):
      raise RuntimeError('Файл не знайдено.')
    with zipfile.ZipFile(p) as z:
      man = json.loads(z.read('manifest.json').decode('utf8'))
      infos = [i for i in z.infolist() if not i.filename.endswith('/') and i.filename != 'manifest.json']
      BK['total'] = sum(i.file_size for i in infos)
      lib_new, sett_new = {}, None
      for i in infos:
        if BK['cancel'].is_set():
          raise Cancelled()
        name = i.filename
        parts = name.replace('\\', '/').split('/')
        if any(x in ('..', '') for x in parts) or os.path.isabs(name):
          continue
        BK['msg'] = name if len(name) < 60 else '…' + name[-58:]
        if name == 'library.json':
          lib_new = json.loads(z.read(i).decode('utf8'))
          BK['done'] += i.file_size
          continue
        if name == 'settings.json':
          sett_new = json.loads(z.read(i).decode('utf8'))
          BK['done'] += i.file_size
          continue
        if name == 'notifs.json':
          dst = os.path.join(BASE_DIR, name)
          if os.path.exists(dst):
            BK['done'] += i.file_size
            continue
        dst = os.path.join(BASE_DIR, *parts)
        if os.path.exists(dst) and _fsize(dst) == i.file_size:
          BK['done'] += i.file_size
          continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + '.imp'
        with z.open(i) as src, open(tmp, 'wb') as out:
          while True:
            if BK['cancel'].is_set():
              out.close()
              _rm(tmp)
              raise Cancelled()
            chunk = src.read(1 << 20)
            if not chunk:
              break
            out.write(chunk)
            BK['done'] += len(chunk)
        os.replace(tmp, dst)
    added = 0
    with LOCK:
      for k, e in (lib_new or {}).items():
        if k not in library:
          library[k] = e
          added += 1
    lib_save()
    if isinstance(sett_new, dict):
      try:
        api_settings({k: v for k, v in sett_new.items() if k in SETTINGS})
      except Exception:
        pass
    _heal_folders()
    pend = man.get('downloaded') or [] if man.get('mode') == 'links' else []
    BK['pending'] = pend
    queued = 0
    BK.update(state='done', msg='Готово', result={'added': added, 'queued': queued, 'mode': man.get('mode'), 'pending': sum(len(g.get('eps') or []) for g in pend), 'sett': isinstance(sett_new, dict)})
  except Cancelled:
    BK.update(state='idle', msg='Скасовано')
  except Exception as ex:
    BK.update(state='error', err=str(ex) or ex.__class__.__name__)
  if cleanup:
    _rm(os.path.join(BASE_DIR, rel))


def api_backup_upload(h):
  """Користувач обрав .zip у «Файлах»: приймаємо файл і одразу запускаємо імпорт (режим — відео чи посилання — читається з архіву)."""
  if BK['state'] == 'run':
    raise ValueError('Вже виконується.')
  n = int(h.headers.get('Content-Length') or 0)
  d = _bk_dir()
  os.makedirs(d, exist_ok=True)
  dst = os.path.join(d, '_imported.zip')
  left = n
  with open(dst, 'wb') as out:
    while left > 0:
      chunk = h.rfile.read(min(1 << 20, left))
      if not chunk:
        break
      out.write(chunk)
      left -= len(chunk)
  if left > 0:
    _rm(dst)
    raise ValueError('Файл передано не повністю.')
  import zipfile
  try:
    with zipfile.ZipFile(dst) as z:
      if 'manifest.json' not in z.namelist():
        raise ValueError('x')
  except Exception:
    _rm(dst)
    raise ValueError('Це не архів збереження цієї програми.')
  threading.Thread(target=_bk_run_import, args=(os.path.relpath(dst, BASE_DIR), True), daemon=True).start()
  return {'ok': True}


def api_import_dl():
  """Користувач підтвердив: ставимо в чергу серії, що були скачані на момент збереження."""
  pend = BK.get('pending') or []
  BK['pending'] = []
  if BK.get('result'):
    BK['result']['pending'] = 0
  queued = 0
  for g in pend:
    try:
      with LOCK:
        kid = _kid(g['key']) if g['key'] in library else None
      if kid:
        r = api_download(kid, g.get('eps') or [], g.get('quality') or '720p', g.get('dub'))
        queued += int((r or {}).get('queued') or 0) if isinstance(r, dict) else 0
    except Exception:
      pass
  return {'ok': True, 'queued': queued}


def api_import(rel):
  if BK['state'] == 'run':
    raise ValueError('Вже виконується.')
  threading.Thread(target=_bk_run_import, args=(rel,), daemon=True).start()
  return {'ok': True}


def api_backup_delete(rel):
  p = os.path.normpath(os.path.join(BASE_DIR, rel))
  if not p.startswith(os.path.normpath(BASE_DIR)) or not p.lower().endswith('.zip') or not os.path.isfile(p):
    raise ValueError('Файл не знайдено.')
  os.remove(p)
  return {'ok': True}



def api_storage():
  with LOCK:
    ents = [(k, dict(e)) for k, e in library.items() if not e.get('temp')]
  anime, used_folders, work_used = [], set(), 0
  a_tot = p_tot = 0
  claimed = set()
  seen_ids = set()
  rows = []
  for k, e in ents:
    sz = n = ps = 0
    for f in _all_folders(e):
      if not f or f in used_folders:
        continue
      used_folders.add(f)
      did = _did(os.path.join(BASE_DIR, f))
      if did is not None:
        if did in seen_ids:
          continue  # та сама папка під іншим написанням імені
        seen_ids.add(did)
      ok, nok, part, npart = _folder_eps(os.path.join(BASE_DIR, f))
      if f.replace('\\', '/').split('/')[0] == 'anime_videos':
        work_used += ok + part
      sz += ok
      n += nok
      ps += part
    rows.append((k, e, sz, n, ps))
  top_used = {u.replace('\\', '/').split('/')[0] for u in used_folders}
  for k, e, sz, n, ps in rows:
    for fn in _entry_extra_dirs(e, top_used | claimed, seen_ids):
      claimed.add(fn)
      d2 = _did(os.path.join(BASE_DIR, fn))
      if d2 is not None:
        seen_ids.add(d2)
      ok, nok, part, npart = _folder_eps(os.path.join(BASE_DIR, fn))
      sz += ok
      n += nok
      ps += part
    a_tot += sz
    p_tot += ps
    anime.append({'id': _kid(k), 'title': e.get('alias') or e.get('title') or '?', 'size': sz, 'eps': n, 'partial': ps, 'archived': bool(e.get('archived')),
                  'watched_size': sum(_fsize(_ep_path(e, o)) for o in (e.get('watched') or []) if _is_downloaded(e, o))})
  anime.sort(key=lambda x: -x['size'])
  cache = os.path.join(BASE_DIR, '.cache')
  img, nimg = _dir_size(os.path.join(cache, 'img'))
  th, nth = _dir_size(os.path.join(cache, 'thumbs'))
  posters = other_c = 0
  try:
    for fn in os.listdir(cache):
      fp = os.path.join(cache, fn)
      if os.path.isfile(fp):
        if fn.startswith('poster_'):
          posters += _fsize(fp)
        else:
          other_c += _fsize(fp)
  except OSError:
    pass
  logs, nlogs = _dir_size(os.path.join(BASE_DIR, 'crash_reports'))
  data = sum(_fsize(os.path.join(BASE_DIR, f)) for f in ('library.json', 'settings.json', 'notifs.json', 'jobs_pending.json'))
  try:  # папки не з бібліотеки: показуємо; видаляти дозволяємо лише ті, де самі серії нашого формату
    orphans = []
    for fn in sorted(os.listdir(BASE_DIR)):
      fp = os.path.join(BASE_DIR, fn)
      if not os.path.isdir(fp) or fn.startswith('.') or fn.startswith('__') or fn in _OWN_DIRS or fn in claimed or fn in {u.replace('\\', '/').split('/')[0] for u in used_folders} or _did(fp) in seen_ids:
        continue
      sz, cnt = _dir_size(fp)
      names = []
      try:
        names = os.listdir(fp)
      except OSError:
        pass
      safe = bool(names) and all(_EP_FILE_RE.match(x) for x in names)
      orphans.append({'name': fn, 'size': sz, 'files': cnt, 'safe': safe})
    orphans.sort(key=lambda x: -x['size'])
  except OSError:
    orphans = []
  orph_tot = sum(o['size'] for o in orphans)
  work = max(0, _dir_size(os.path.join(BASE_DIR, 'anime_videos'))[0] - work_used)
  app_sz = 0
  try:
    if APP_BUNDLE.get('path') and os.path.isdir(APP_BUNDLE['path']):
      app_sz = _dir_size(APP_BUNDLE['path'])[0]
  except Exception:
    app_sz = 0
  if not app_sz:
    af = _app_file()
    app_sz = _fsize(af) if af else 0
  cats = [
      {'id': 'anime', 'name': 'Аніме (серії)', 'size': a_tot, 'count': sum(a['eps'] for a in anime), 'unit': 'серій'},
      {'id': 'partial', 'name': 'Недокачане', 'size': p_tot, 'count': 0, 'unit': ''},
      {'id': 'img', 'name': 'Кеш картинок', 'size': img, 'count': nimg, 'unit': 'файлів'},
      {'id': 'thumbs', 'name': 'Мініатюри серій', 'size': th, 'count': nth, 'unit': 'файлів'},
      {'id': 'posters', 'name': 'Обкладинки бібліотеки', 'size': posters + other_c, 'count': 0, 'unit': ''},
      {'id': 'logs', 'name': 'Логи й звіти', 'size': logs, 'count': nlogs, 'unit': 'файлів'},
      {'id': 'data', 'name': 'Дані бібліотеки', 'size': data, 'count': 0, 'unit': ''},
      {'id': 'work', 'name': 'Папка завантажень (anime_videos)', 'size': work, 'count': 0, 'unit': ''},
      {'id': 'backups', 'name': 'Архіви збереження', 'size': _dir_size(os.path.join(BASE_DIR, 'Збереження'))[0], 'count': 0, 'unit': ''},
      {'id': 'orphans', 'name': 'Інші папки', 'size': orph_tot, 'count': len(orphans), 'unit': 'папок'},
      {'id': 'app', 'name': 'Програма', 'size': app_sz, 'count': 0, 'unit': ''},
  ]
  total = sum(c['size'] for c in cats)
  try:
    du = shutil.disk_usage(BASE_DIR)
    disk = {'total': du.total, 'free': du.free}
    if DISK_PROVIDER.get('fn'):  # iOS: «вільно» як у Налаштуваннях (разом із тим, що система звільнить сама)
      try:
        t2, f2 = DISK_PROVIDER['fn']()
        if t2 and f2 is not None:
          disk = {'total': int(t2), 'free': int(f2)}
      except Exception:
        pass
  except Exception:
    disk = None
  av = _mem_avail_mb()
  return {'total': total, 'cats': cats, 'anime': anime, 'orphans': orphans, 'disk': disk, 'logging': bool(SETTINGS.get('logging')),
          'ram': {'peak': _mem_mb(), 'avail': av, 'avail_min': _MEM_MIN[0], 'threads': threading.active_count()},
          'watched_total': sum(a['watched_size'] for a in anime)}


def _busy_kids():
  with JOB_LOCK:
    return {j['kid'] for j in JOBS if j['status'] in ('queued', 'running')}


def api_storage_clear(what, kid=None, name=None):
  freed = 0
  cache = os.path.join(BASE_DIR, '.cache')
  if what == 'img':
    freed = _dir_size(os.path.join(cache, 'img'))[0]
    _rm(os.path.join(cache, 'img'))
  elif what == 'thumbs':
    freed = _dir_size(os.path.join(cache, 'thumbs'))[0]
    _rm(os.path.join(cache, 'thumbs'))
  elif what == 'logs':
    d = os.path.join(BASE_DIR, 'crash_reports')
    freed = _dir_size(d)[0]
    try:
      for fn in os.listdir(d):
        fp = os.path.join(d, fn)
        if fn == 'fatal.log' and _CR.get('fh') is not None:
          try:
            _CR['fh'].truncate(0)
            _CR['fh'].seek(0)
          except Exception:
            pass
        else:
          _rm(fp)
    except OSError:
      pass
  elif what in ('partial', 'watched'):
    busy = _busy_kids()
    with LOCK:
      ents = [(k, dict(e)) for k, e in library.items() if not e.get('temp')]
    for k, e in ents:
      if _kid(k) in busy:
        continue
      if what == 'partial':
        for f in _all_folders(e):
          if not f:
            continue
          d = os.path.join(BASE_DIR, f)
          try:
            for fn in os.listdir(d):
              m = _EP_FILE_RE.match(fn)
              if m and m.group(2) != '.mp4':
                freed += _fsize(os.path.join(d, fn))
                _rm(os.path.join(d, fn))
          except OSError:
            pass
      else:
        for o in e.get('watched') or []:
          if _is_downloaded(e, o):
            freed += _fsize(_ep_path(e, o))
            _rm(_ep_path(e, o))
  elif what == 'anime':
    key, e = _find(kid)
    if _kid(key) in _busy_kids():
      raise ValueError('Для цього аніме йде завантаження — спочатку скасуйте його.')
    fl = [f for f in _all_folders(e) if f]
    fl += _entry_extra_dirs(e, {f.replace('\\', '/').split('/')[0] for f in fl})
    for f in fl:
      d = os.path.join(BASE_DIR, f)
      try:
        for fn in os.listdir(d):
          if _EP_FILE_RE.match(fn):
            freed += _fsize(os.path.join(d, fn))
            _rm(os.path.join(d, fn))
      except OSError:
        pass
  elif what == 'orphan':
    fn = os.path.basename(name or '')
    fp = os.path.join(BASE_DIR, fn)
    if not fn or fn.startswith('.') or not os.path.isdir(fp):
      raise ValueError('Папку не знайдено.')
    with LOCK:
      used = {f for e in library.values() for f in _all_folders(e) if f}
    names = os.listdir(fp)
    if fn in used or not names or not all(_EP_FILE_RE.match(x) for x in names):
      raise ValueError('Цю папку видаляти небезпечно — у ній не лише серії з додатка.')
    freed = _dir_size(fp)[0]
    _rm(fp)
  else:
    raise ValueError('Невідома дія.')
  return {'freed': freed, 'storage': api_storage()}


# ═════════════════════════ Звіти про збої ═════════════════════════

RECENT = collections.deque(maxlen=25)  # останні запити — що відбувалось перед збоєм
_CR = {'n': 0, 'fh': None}


def _crash_dir():
  d = os.path.join(BASE_DIR, 'crash_reports')
  os.makedirs(d, exist_ok=True)
  return d


def _mem_mb():
  try:
    import resource
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(v / (1024 * 1024 if sys.platform in ('darwin', 'ios') else 1024), 1)  # макс. пам'ять процесу, МБ
  except Exception:
    return None


def _mem_avail_mb():
  """Скільки пам'яті ще можна взяти до того, як iOS примусово закриє додаток (os_proc_available_memory); None, якщо недоступно."""
  try:
    import ctypes
    lib = ctypes.CDLL(None)
    fn = lib.os_proc_available_memory
    fn.restype = ctypes.c_size_t
    v = int(fn())
    return round(v / 1048576, 1) if v > 0 else None
  except Exception:
    return None


_MEM_MIN = [None]


def _mem_trim():
  # мало пам'яті: скидаємо кеші, які можна відновити
  try:
    SEARCH_CACHE.clear()
    _FI_CACHE.clear()
    SEG_CACHE.clear()
    SEG_BYTES[0] = 0
    _IMG_BAD.clear()
  except Exception:
    pass
  try:
    import gc
    gc.collect()
  except Exception:
    pass


def _state_text():
  try:
    with JOB_LOCK:
      run = [f"{j['title']} #{j['n']} ({j['status']})" for j in JOBS if j['status'] in ('queued', 'running')][:6]
  except Exception:
    run = []
  return (f"час: {time.strftime('%Y-%m-%d %H:%M:%S')}\nпам'ять (пік): {_mem_mb()} МБ\nпотоків: {threading.active_count()}\n"
          f"мережа: {_NET.get('v')}\nзавантаження: {run}\nостанні запити:\n  " + '\n  '.join(list(RECENT)[-20:]))


def crash_report(kind, text):
  # пише файл звіту в папку crash_reports (зберігається не більше 40 файлів); лише коли логування увімкнено
  if not SETTINGS.get('logging'):
    return
  try:
    _CR['n'] += 1
    d = _crash_dir()
    fn = os.path.join(d, f"{time.strftime('%Y%m%d_%H%M%S')}_{_CR['n']}_{kind}.txt")
    with open(fn, 'w', encoding='utf8') as f:
      f.write(f'{kind}\n\n{text}\n\n--- стан ---\n{_state_text()}\n')
    files = sorted(x for x in os.listdir(d) if x.endswith('.txt'))
    for old in files[:-40]:
      _rm(os.path.join(d, old))
  except Exception:
    pass


INFLIGHT = {}


def _inflight_run(h, fn):
  # які запити виконуються просто зараз: якщо процес помре, у alive.json лишиться, на чому саме
  k = threading.get_ident()
  INFLIGHT[k] = (time.time(), h.command + ' ' + (h.path or '')[:90])
  try:
    fn()
  finally:
    INFLIGHT.pop(k, None)


def _inflight_list():
  now = time.time()
  try:
    return [f'{now - t:.1f}с {p}' for t, p in sorted(INFLIGHT.values())][:8]
  except Exception:
    return []


def _heartbeat_loop():
  p = os.path.join(_crash_dir(), 'alive.json')
  n = 0
  while True:
    n += 1
    av = _mem_avail_mb()
    if av is not None:
      _MEM_MIN[0] = av if _MEM_MIN[0] is None else min(_MEM_MIN[0], av)
      if av < 700:
        _mem_trim()
    elif n % 12 == 0:
      try:
        import gc
        gc.collect()
      except Exception:
        pass
    try:
      if not SETTINGS.get('logging'):
        raise OSError  # логування вимкнено — «пульс» на диск не пишемо
      with open(p, 'w', encoding='utf8') as f:
        json.dump({'t': time.time(), 'mem': _mem_mb(), 'avail_mb': av, 'avail_min_mb': _MEM_MIN[0], 'threads': threading.active_count(), 'inflight': _inflight_list(), 'recent': list(RECENT)[-20:]}, f, ensure_ascii=False)
    except Exception:
      pass
    time.sleep(5)


def _log_on():
  """Вмикає запис фатальних збоїв (faulthandler) у crash_reports/fatal.log."""
  try:
    import faulthandler
    if _CR['fh'] is None:
      _CR['fh'] = open(os.path.join(_crash_dir(), 'fatal.log'), 'a')
      _CR['fh'].write(f"\n=== запуск {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
      _CR['fh'].flush()
      faulthandler.enable(_CR['fh'], all_threads=True)
  except Exception:
    pass


def _log_off():
  try:
    import faulthandler
    faulthandler.disable()
    if _CR['fh'] is not None:
      _CR['fh'].close()
      _CR['fh'] = None
  except Exception:
    pass


def crash_init():
  # виявляє, що минулий запуск обірвався, і ставить перехоплювачі винятків та фатальних збоїв
  try:
    if SETTINGS.get('logging'):
      _log_on()
    d = os.path.join(BASE_DIR, 'crash_reports')
    p = os.path.join(d, 'alive.json')
    if SETTINGS.get('logging') and os.path.exists(p):
      try:
        j = json.load(open(p, encoding='utf8'))
        age = time.time() - j.get('t', 0)
        txt = ("Попередній запуск обірвався (наприклад, iOS закрив Carnets через нестачу пам'яті, або клітинку зупинили).\n"
               f"Останній подих: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(j.get('t', 0)))} ({int(age // 60)} хв тому)\n"
               f"пам'ять (пік): {j.get('mem')} МБ, вільно до ліміту iOS: {j.get('avail_mb')} МБ (мінімум {j.get('avail_min_mb')}), потоків: {j.get('threads')}\nвиконувалось у момент обриву: {j.get('inflight')}\nостанні запити:\n  " + '\n  '.join(j.get('recent') or []))
        crash_report('previous_run_ended', txt)
      except Exception:
        pass
    import traceback
    old_hook, old_th = sys.excepthook, threading.excepthook

    def hook(tp, v, tb):
      crash_report('exception', ''.join(traceback.format_exception(tp, v, tb)))
      old_hook(tp, v, tb)

    def thook(a):
      if not isinstance(a.exc_value, (ConnectionError, TimeoutError)):
        crash_report('thread_exception', f'потік {getattr(a.thread, "name", "?")}\n' + ''.join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
      old_th(a)
    if not getattr(sys.excepthook, '_cr', False):
      hook._cr = True
      sys.excepthook, threading.excepthook = hook, thook
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
  except Exception:
    pass


def api_clientlog(msg):
  if not msg or not SETTINGS.get('logging'):  # обірваний запит (сторінку закрили під час відправки)
    return {'ok': True}
  crash_report('browser_error', str(msg)[:4000])
  return {'ok': True}


class _Server(ThreadingHTTPServer):
  daemon_threads = True

  def handle_error(self, request, client_address):
    ex = sys.exc_info()[1]
    if isinstance(ex, (ConnectionError, TimeoutError)):  # браузер закрив з'єднання — це не помилка
      return
    import traceback
    crash_report('server_error', ''.join(traceback.format_exception(type(ex), ex, ex.__traceback__)))
    super().handle_error(request, client_address)


def _heal_folders():
  """Після перенесення файлів ім'я папки може відрізнятись лише Unicode-формою/регістром — тоді серії «зникають». Перейменовуємо на ім'я з бібліотеки."""
  try:
    with LOCK:
      want = {f for e in library.values() if not e.get('temp') for f in _all_folders(e) if f and '/' not in f.replace('\\', '/')}
    have = [fn for fn in os.listdir(BASE_DIR) if os.path.isdir(os.path.join(BASE_DIR, fn)) and not fn.startswith('.')]
    for f in want:
      if os.path.isdir(os.path.join(BASE_DIR, f)):
        continue
      cand = [fn for fn in have if _nk(fn) == _nk(f)]
      if len(cand) == 1:
        try:
          os.rename(os.path.join(BASE_DIR, cand[0]), os.path.join(BASE_DIR, f))
        except OSError:
          pass
  except Exception:
    pass


_SRV = {'srv': None, 'th': None}


def bg_needed():
  """Чи потрібно зараз не давати iOS приспати програму: йдуть завантаження, дивимось відео (або щойно дивились), або ввімкнено фонову перевірку."""
  if not SETTINGS.get('bg_keep', True):
    return False
  now = time.time()
  if now - BG_LAST['open'] < 45 or now - BG_LAST['play'] < 90:
    return True
  try:
    with JOB_LOCK:
      if any(j['status'] in ('queued', 'running') for j in JOBS) and not PAUSE['on']:
        return True
  except Exception:
    pass
  return bool(SETTINGS.get('bg_check') and SETTINGS.get('auto_check'))


def video_active():
  return time.time() - BG_LAST['open'] < 45 or time.time() - BG_LAST['play'] < 25


def island_state():
  """Що показувати в Dynamic Island: завантаження (прогрес) або таймер до наступної перевірки; None — нічого."""
  try:
    if not SETTINGS.get('island', True):
      return None
    if SETTINGS.get('island_dl', True):
      with JOB_LOCK:
        jobs = [_job_public(j) for j in JOBS]
      live = [j for j in jobs if j['status'] in ('queued', 'running')]
      if live:
        run = next((j for j in live if j['status'] == 'running'), live[0])
        pt = sum((j.get('pk_total') or 0) for j in live)
        pl = sum(max(0, (j.get('pk_total') or 0) - (j.get('pk') or 0)) if j['status'] == 'running' else (j.get('pk_total') or 0) for j in live)
        bt = sum((j.get('bytes_total') or 0) for j in live)
        bl = sum(max(0, (j.get('bytes_total') or 0) - (j.get('bytes') or 0)) if j['status'] == 'running' else (j.get('bytes_total') or 0) for j in live)
        if pt:
          pr = 1 - pl / pt
        elif bt:
          pr = 1 - bl / bt
        else:
          pr = 0.0
        return {'kind': 'download', 'title': str(run.get('title') or 'Завантаження'), 'subtitle': 'Серія %s' % run.get('n'),
                'progress': max(0.0, min(1.0, pr)), 'count': len(live), 'paused': bool(PAUSE['on']), 'until': 0}
    if SETTINGS.get('island_check') and SETTINGS.get('auto_check'):
      nx = AC.get('next_run') or 0
      if nx > time.time():
        return {'kind': 'check', 'title': 'Перевірка нових серій', 'subtitle': '', 'progress': 0.0, 'count': 0, 'paused': False, 'until': nx}
  except Exception:
    pass
  return None


def server_alive():
  """Чи відповідає наш HTTP-сервер (iOS у фоні забирає сокет і зупиняє потік)."""
  th = _SRV.get('th')
  if th is None or not th.is_alive():
    return False
  try:
    import socket
    c = socket.create_connection(('127.0.0.1', PORT), timeout=0.4)
    c.close()
    return True
  except OSError:
    return False


def restart_server():
  """Піднімає HTTP-сервер заново на тому ж порту (бібліотека, завантаження й налаштування в пам'яті лишаються)."""
  old = _SRV.get('srv')
  if old is not None:
    try:
      old.server_close()
    except Exception:
      pass
  server = _Server((HOST, PORT), Handler)
  th = threading.Thread(target=server.serve_forever, daemon=True)
  th.start()
  _SRV['srv'], _SRV['th'] = server, th
  return server


def start():
  """Запускає сервер у фоні. Повторний запуск у тому ж блокноті перезапускає його."""
  g = sys.modules['__main__'].__dict__
  old = g.get('_app_server')
  if old is not None:
    try:
      old.shutdown()
      old.server_close()
    except Exception:
      pass
  os.makedirs(BASE_DIR, exist_ok=True)
  lib_load()
  _heal_folders()
  settings_load()
  try:
    _apply_multitask()
  except Exception:
    pass
  notifs_load()
  _jobs_load()
  crash_init()
  _sweep_tmp()
  tmp_sweep(0)
  server = _Server((HOST, PORT), Handler)
  g['_app_server'] = server
  _SRV['srv'] = server
  _SRV['th'] = threading.Thread(target=server.serve_forever, daemon=True)
  _SRV['th'].start()
  threading.Thread(target=_meta_refresh_loop, daemon=True).start()
  AC['gen'] += 1
  threading.Thread(target=_autocheck_loop, args=(AC['gen'],), daemon=True).start()
  print(f'✅ Додаток запущено: http://{HOST}:{PORT}/')
  print('Відкрийте цю адресу в Carnets. Поки Carnets відкритий, додаток працює.')
  print(f'Папка з відео та бібліотекою: {os.path.abspath(BASE_DIR)}')
  return server


INDEX_HTML = r"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Мої аніме</title>
<style>
:root{
  --bg:#0d0e13; --s1:#161822; --s2:#1f2230; --s3:#2a2e41;
  --tx:#eef0f7; --mu:#8d92a8; --ac:#8b7bff; --ac2:#5ee0c1;
  --ok:#3ddc97; --warn:#ffb84d; --err:#ff6b7a; --r:16px;
  --top:env(safe-area-inset-top,0px); --bot:env(safe-area-inset-bottom,0px); --lft:env(safe-area-inset-left,0px); --rgt:env(safe-area-inset-right,0px);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--tx);
  font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-text-size-adjust:100%}
body{min-height:100vh;padding-bottom:calc(24px + var(--bot))}
a{color:inherit;text-decoration:none}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer}
.hide{display:none!important}

/* ---------- вкладки й мережа ---------- */
body{padding-bottom:calc(84px + var(--bot))}
#tabs{position:fixed;left:0;right:0;bottom:0;z-index:25;display:flex;padding:6px 6px calc(6px + var(--bot));
  background:rgba(13,14,19,.9);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-top:1px solid #ffffff12}
#tabs a{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;padding:6px 0;font-size:11px;color:var(--mu);border-radius:12px}
#tabs a .i{width:24px;height:24px}
#tabs a.on{color:var(--ac)}
.net{display:inline-flex;align-items:center;gap:5px;height:30px;padding:0 10px;border-radius:15px;background:var(--s2);color:var(--mu);font-size:12px;font-weight:600;white-space:nowrap}
.net .i{width:16px;height:16px}
.net.wifi{color:var(--ok)} .net.cell{color:var(--warn)} .net.offline{color:var(--err)}
.page-h{padding:18px 16px 4px}
.set{display:flex;align-items:center;gap:14px;margin:12px 16px;padding:14px 16px;background:var(--s1,#171923);border-radius:16px}
.set .tx{flex:1}.set .tx b{display:block;font-size:15px}.set .tx span{font-size:12px;color:var(--mu)}
.set .sw input{width:50px;height:30px;border-radius:15px}
.set .sw input::after{width:26px;height:26px}
.set .sw input:checked::after{left:22px}
.grp{margin:12px 16px;background:var(--s1,#171923);border-radius:16px;overflow:hidden}
.grp .gh{display:flex;align-items:center;gap:14px;padding:14px 16px;width:100%;border:0;background:none;color:inherit;font:inherit;text-align:left}
.grp .gh .tx{flex:1}.grp .gh b{display:block;font-size:15px}.grp .gh span{font-size:12px;color:var(--mu)}
.grp .gh .i{width:20px;height:20px;transition:transform .25s;color:var(--mu)}
.grp.open>.gh .i{transform:rotate(180deg)}
.fold{display:grid;grid-template-rows:0fr;transition:grid-template-rows .28s ease}
.fold>div{overflow:hidden;min-height:0}
.open>.fold,.fold.open{grid-template-rows:1fr}
.grp .row{display:flex;align-items:center;gap:14px;padding:12px 16px;border-top:1px solid #ffffff12}
.grp .row .tx{flex:1}.grp .row b{display:block;font-size:14px;font-weight:600}.grp .row span.d{font-size:12px;color:var(--mu)}
.grp .row.sub{padding-left:16px;background:#ffffff06;cursor:pointer}
.grp .row.sub input[type=checkbox]{appearance:none;-webkit-appearance:none;width:24px;height:24px;border-radius:7px;border:2px solid #ffffff44;background:none;position:relative;flex:none;margin:0}
.grp .row.sub input[type=checkbox]:checked{background:var(--ac);border-color:var(--ac)}
.grp .row.sub input[type=checkbox]:checked::after{content:"";position:absolute;left:6px;top:2px;width:6px;height:11px;border:solid #fff;border-width:0 2.5px 2.5px 0;transform:rotate(45deg)}
.grp .row.sub{opacity:1;transition:opacity .2s}.grp .row.sub.dis{opacity:.45}
.grp .skh{display:flex;align-items:center;gap:12px;padding:12px 16px;border-top:1px solid #ffffff12}
.grp .skh .tx{flex:1;min-width:0}.grp .skh b{display:block;font-size:14px;font-weight:600}.grp .skh span.d{font-size:12px;color:var(--mu)}
.grp .skh .arr{border:0;background:none;color:var(--mu);padding:4px;display:grid;place-items:center}
.grp .skh .arr .i{width:20px;height:20px;transition:transform .25s}
.grp .skg.open .skh .arr .i{transform:rotate(180deg)}
.grp .skg.open>.fold{grid-template-rows:1fr}

.grp .row.sub b{font-weight:500}
.grp .row .sw input{width:46px;height:28px;border-radius:14px}
.grp .row .sw input::after{width:24px;height:24px}
.grp .row .sw input:checked::after{left:20px}
/* ---------- верхня панель ---------- */
.bar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:8px;
  padding:calc(10px + var(--top)) 14px 10px;background:rgba(13,14,19,.82);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid #ffffff0d}
.hdr2{position:sticky;top:var(--barh,58px);z-index:19;background:rgba(13,14,19,.82);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid #ffffff0d;max-height:55vh;overflow-y:auto;will-change:transform,opacity;transition:transform .42s cubic-bezier(.22,1,.36,1),opacity .3s ease,filter .3s ease}
.hdr2.hide{transform:translateY(calc(-100% - var(--barh,58px) + 6px)) scale(.97);opacity:0;filter:blur(4px);pointer-events:none}
.bar h1{font-size:22px;margin:0;flex:1;letter-spacing:-.3px}
.bar .back{font-size:15px;color:var(--ac);background:none;border:0;padding:8px 4px}
.ib{position:relative;width:42px;height:42px;border-radius:13px;border:0;background:var(--s2);
  display:grid;place-items:center;font-size:19px}
.ib:active{background:var(--s3)}
.ib.pri{background:linear-gradient(135deg,var(--ac),#6a5cff);color:#fff}
.dot{position:absolute;top:-4px;right:-4px;min-width:18px;height:18px;border-radius:9px;
  background:var(--ac2);color:#06231c;font-size:11px;font-weight:700;display:grid;place-items:center;padding:0 4px}
.spin{animation:sp 1s linear infinite;display:inline-block}
.i{width:1.15em;height:1.15em;flex:none;vertical-align:-.2em;display:inline-block}
.ib .i{width:22px;height:22px}
.toast{display:flex;align-items:center;gap:8px;justify-content:center}
.toast .i{width:18px;height:18px}
.big .i{width:64px;height:64px;color:var(--mu)}
.ep .eye .i{width:21px;height:21px}
.ep .pl .i{width:17px;height:17px;margin-left:2px}
.ep .ck .i{width:15px;height:15px;stroke-width:3}
.tag .i{width:11px;height:11px;margin-right:3px;vertical-align:-1px}
.badge .i{width:11px;height:11px;margin-right:3px;vertical-align:-1px}
#player .msg .i{width:22px;height:22px;display:block;margin:0 auto 8px}
.btn .i{width:17px;height:17px}
.btn.sm .i{width:15px;height:15px}
.err .i{vertical-align:-.25em;margin-right:4px}
@keyframes sp{to{transform:rotate(360deg)}}

/* ---------- сітка ---------- */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;padding:14px}
.card{display:block}
.poster{position:relative;aspect-ratio:2/3;border-radius:var(--r);overflow:hidden;
  background:linear-gradient(160deg,#2b2f4d,#171926) center/cover no-repeat;
  box-shadow:0 8px 24px #0006}
.poster>span:not(.site){position:absolute;inset:0;display:grid;place-items:center;font-size:54px;font-weight:800;color:#ffffff30}
.poster .grad{position:absolute;inset:0;background:linear-gradient(to top,#000d 0%,#0000 55%)}
.poster .ct{position:absolute;left:10px;right:10px;bottom:12px}
.poster .t{overflow-wrap:anywhere;hyphens:auto;font-weight:650;font-size:14px;line-height:1.2;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.poster .s{font-size:12px;color:#ffffffb0;margin-top:3px}
.badge{position:absolute;top:8px;left:8px;font-size:12px;font-weight:700;padding:3px 8px;border-radius:9px;
  background:#0e9f84;color:#fff}
.pb{position:absolute;left:0;right:0;bottom:0;height:4px;background:#ffffff26}
.pb>i{display:block;height:100%;background:var(--ac)}
.empty{text-align:center;padding:90px 30px;color:var(--mu)}
.empty .big{font-size:64px;margin-bottom:10px}
.empty h2{color:var(--tx);margin:0 0 6px}

/* ---------- сторінка аніме ---------- */
.hero{position:relative;padding:18px 16px 14px;overflow:hidden;margin-bottom:14px}
.srcs.dis,.dubs.dis{pointer-events:none;opacity:.55}
.hero .bg{position:absolute;inset:-30px;background:center/cover;filter:blur(28px) brightness(.45) saturate(1.2);transform:scale(1.1)}
.hero .row{position:relative;display:flex;gap:14px}
.hero .pic{flex:0 0 118px;aspect-ratio:2/3;border-radius:14px;background:linear-gradient(160deg,#2b2f4d,#171926) center/cover;box-shadow:0 10px 30px #0008}
.hero h2{margin:2px 0 4px;font-size:22px;line-height:1.15;letter-spacing:-.3px}
.hero .orig{color:#ffffffa8;font-size:13px;margin-bottom:8px}
.hero .meta{font-size:13px;color:#ffffffc8}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{font-size:12px;padding:3px 9px;border-radius:99px;background:#ffffff1f;color:#ffffffd8}
.acts{display:flex;gap:10px;padding:4px 16px 12px;position:relative}
.btn{border:0;border-radius:14px;padding:13px 16px;background:var(--s2);font-weight:600;font-size:15px;display:inline-flex;align-items:center;justify-content:center;gap:7px}
.btn:active{background:var(--s3)}
.btn.pri{background:linear-gradient(135deg,var(--ac),#6a5cff);color:#fff;flex:1}
.btn.ok{background:var(--ok);color:#052214}
.btn.dng{background:#3a1d24;color:var(--err)}
.btn.sm{padding:8px 12px;font-size:13px;border-radius:11px}
.btn:disabled{opacity:.5}
.desc{padding:2px 16px 0;margin-bottom:8px;color:#c9ccdb;font-size:14.5px;white-space:pre-line}
.desc.cl{display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.hero .pic{cursor:zoom-in}
.dchip .dcnt{font-style:normal;font-size:11px;opacity:.7;margin-left:6px;white-space:nowrap}
.pv{position:fixed;inset:0;z-index:300;background:rgba(0,0,0,.94);display:flex;align-items:center;justify-content:center;overflow:hidden;touch-action:none;animation:pvin .18s ease}
@keyframes pvin{from{opacity:0}to{opacity:1}}
.pv img{max-width:100%;max-height:100%;object-fit:contain;transform-origin:0 0;will-change:transform;user-select:none;-webkit-user-drag:none;border-radius:6px}
.pv .x{position:absolute;top:calc(var(--top) + 12px);right:14px;width:40px;height:40px;border-radius:50%;border:0;background:#ffffff22;color:#fff;font-size:22px;line-height:40px;padding:0}
.cms{padding:0 16px 8px;display:flex;flex-direction:column;gap:10px}
.cm{background:var(--s1,#171926);border-radius:14px;padding:11px 13px}
.cm.rp{margin:8px 0 0 14px;background:var(--s2,#20233a)}
.cmh{display:flex;align-items:center;gap:10px}
.cav{flex:0 0 34px;width:34px;height:34px;border-radius:50%;background:#2b2f4d center/cover;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;color:#cfd3ee;overflow:hidden}
.cmh b{display:block;font-size:14px;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cmh small{display:block;font-size:11.5px;color:var(--mu)}
.cmh>div{min-width:0;flex:1}
.cml{font-size:12.5px;font-weight:700;color:var(--mu);padding:2px 8px;border-radius:9px;background:var(--s2,#20233a)}
.cml.pos{color:var(--ok)}.cml.neg{color:var(--err)}
.cmt{margin-top:8px;font-size:14px;line-height:1.42;color:#d6d9e8;white-space:pre-wrap;overflow-wrap:anywhere}
.cmr{margin-top:8px;background:none;border:0;color:var(--ac);font:inherit;font-size:13px;padding:0}
.cmst{padding:18px 16px;color:var(--mu);text-align:center;font-size:14px}
.cmmore{display:block;margin:4px auto 10px}
.more{background:none;border:0;color:var(--ac);padding:0 16px 8px;font-size:14px}
.tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:6px 16px 10px}
.tools .sp{flex:1}
select.q{background:var(--s2);border:0;border-radius:11px;padding:8px 10px;font-size:14px}
h3.sec{margin:14px 16px 6px;font-size:13px;letter-spacing:.8px;text-transform:uppercase;color:var(--mu)}
.eps{margin:0 10px;border-radius:var(--r);overflow:hidden;background:var(--s1)}
.ep{display:flex;align-items:center;gap:10px;padding:9px 10px 9px 12px;border-bottom:1px solid #ffffff0a}
.ep:last-child{border-bottom:0}
.ep.sel{background:#8b7bff1c}
.ep .ck{width:22px;height:22px;border-radius:7px;border:2px solid var(--s3);background:none;flex:none;padding:0;display:grid;place-items:center;font-size:14px;color:#fff}
.ep.sel .ck{background:var(--ac);border-color:var(--ac)}
.ep .mid{flex:1;min-width:0}
.ep .n{font-weight:600;font-size:15px}
.ep .tt{font-size:12.5px;color:var(--mu);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tags{display:flex;gap:5px;margin-top:3px;flex-wrap:wrap}
.tag{font-size:11px;padding:1px 7px;border-radius:99px;background:var(--s2);color:var(--mu)}
.seg{display:flex;background:var(--s2);border-radius:11px;padding:3px;gap:2px}
.seg button{border:0;background:none;color:var(--mu);width:38px;height:32px;border-radius:8px;display:grid;place-items:center;transition:background .2s,color .2s}
.seg button.on{background:var(--s3);color:#fff}.seg .i{width:18px;height:18px}
.rrow{display:flex;gap:10px;overflow-x:auto;overflow-y:hidden;overscroll-behavior-x:contain;padding:2px 16px 8px;scroll-snap-type:x proximity;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.rrow::-webkit-scrollbar{display:none}
.dtabs{display:flex;margin:10px 16px 8px;border-bottom:1px solid rgba(255,255,255,.09);overflow-x:auto;overflow-y:hidden;overscroll-behavior-x:contain;scrollbar-width:none}
.dtabs::-webkit-scrollbar{display:none}
.dtabs button{position:relative;flex:1 0 auto;border:0;background:none;padding:11px 14px 12px;color:var(--mu);font:inherit;font-size:14.5px;font-weight:600;white-space:nowrap;cursor:pointer;transition:color .2s}
.dtabs button small{margin-left:6px;padding:1px 7px;border-radius:9px;background:var(--s2);font-size:11px;font-weight:700;transition:background .2s}
.dtabs button.on{color:#fff}
.dtabs button.on small{background:var(--ac);color:#fff}
.dtabs button::after{content:'';position:absolute;left:50%;right:50%;bottom:-1px;height:3px;border-radius:3px 3px 0 0;background:linear-gradient(90deg,var(--ac),var(--ac2));box-shadow:0 0 10px var(--ac);transition:left .25s cubic-bezier(.2,.8,.2,1),right .25s cubic-bezier(.2,.8,.2,1)}
.dtabs button.on::after{left:14px;right:14px}
.sortrow{display:flex;gap:10px;align-items:center;margin:2px 16px 10px}
.sortrow .ssel{position:relative;flex:1 1 auto;min-width:0}
.sortrow select{width:100%;appearance:none;-webkit-appearance:none;background:var(--s1);border:1px solid rgba(255,255,255,.08);color:inherit;border-radius:12px;padding:9px 34px 9px 12px;font:inherit;font-size:13.5px;font-weight:600}
.sortrow .ssel::after{content:'';position:absolute;right:14px;top:50%;width:7px;height:7px;border-right:2px solid var(--mu);border-bottom:2px solid var(--mu);transform:translateY(-70%) rotate(45deg);pointer-events:none}
.sortrow .tg{flex:0 0 auto;border:1px solid rgba(255,255,255,.12);background:none;color:var(--mu);border-radius:12px;padding:9px 12px;font:inherit;font-size:13px;font-weight:600;cursor:pointer;transition:all .2s}
.nxbox{position:absolute;left:14px;right:14px;bottom:96px;z-index:7;background:#0c0f18ee;border:1px solid rgba(255,255,255,.14);border-radius:16px;padding:12px 14px 14px;backdrop-filter:blur(10px);box-shadow:0 10px 30px #000a}
.nxbox .nxt{font-size:14.5px;font-weight:600;margin-bottom:10px}.nxbox .nxb{display:flex;gap:8px}.nxbox .nxb .btn{flex:1}
.nxbar{display:block;height:3px;border-radius:2px;background:#ffffff22;margin-top:12px;overflow:hidden}.nxbar i{display:block;height:100%;width:0;background:var(--ac2);transition:width 1s linear}
.ntime{margin-left:auto;font-size:11px;color:var(--mu);white-space:nowrap;min-width:0;overflow:hidden;text-overflow:ellipsis}
.nfp{display:flex;flex-wrap:wrap;gap:8px;margin:0 16px 12px;align-items:center}.nfp .ssel{position:relative;flex:1 1 140px;min-width:0}.nfp select{width:100%;appearance:none;-webkit-appearance:none;background:var(--s1);border:1px solid rgba(255,255,255,.08);color:inherit;border-radius:12px;padding:9px 30px 9px 12px;font:inherit;font-size:13.5px}.nfp .ssel::after{content:'';position:absolute;right:14px;top:50%;width:7px;height:7px;border-right:2px solid var(--mu);border-bottom:2px solid var(--mu);transform:translateY(-70%) rotate(45deg);pointer-events:none}
.sortrow .tg.on{border-color:var(--ac);color:#fff;background:#8b7bff26}
.card.reo .arws{display:flex;gap:6px;margin-top:8px;transition:opacity .15s}
.card.reo .arw{flex:1;height:36px;border:0;border-radius:11px;background:var(--s2);color:#fff;display:grid;place-items:center}
.card.reo .arw .i{width:18px;height:18px}.card.reo .arw:active{background:var(--ac)}
.card.reo .arw .rot{display:grid;place-items:center}.card.reo .arw .rot.l{transform:rotate(90deg)}.card.reo .arw .rot.r{transform:rotate(-90deg)}
body.dragging .card .arws,body.dragging .card .xb,body.dragging .card .ab{opacity:0;pointer-events:none}
.card.stacked{display:none}
.card.stk{position:fixed;left:0;top:0;margin:0;z-index:59;pointer-events:none;opacity:.96;will-change:transform}
.card.drag[data-cnt]::after{content:attr(data-cnt);position:absolute;top:-8px;right:-8px;z-index:8;min-width:26px;height:26px;border-radius:13px;background:#ff453a;color:#fff;font-weight:800;font-size:14px;display:grid;place-items:center;padding:0 7px;box-shadow:0 2px 8px #0008}
.dz{position:fixed;bottom:calc(var(--bot) + 70px);width:78px;height:78px;margin-bottom:-12px;z-index:90;pointer-events:none;display:grid;place-items:center;border-radius:50%;color:#fff;--p:0;--c:14,159,132;
  background:radial-gradient(circle,rgba(var(--c),calc(.55 + .3*var(--p))) 0%,rgba(var(--c),.28) 62%,rgba(var(--c),0) 72%);
  transform:scale(calc(1 + .35*var(--p)));transition:transform .12s linear,filter .18s;animation:dzin .28s cubic-bezier(.2,.9,.3,1.3)}
.dz.r{right:10px}.dz.l{left:10px;--c:229,72,88}
.dz .ring{position:absolute;inset:6px;border-radius:50%;border:2px solid rgba(255,255,255,.55);opacity:calc(.25 + .6*var(--p))}
.dz .i{width:30px;height:30px;position:relative;transition:transform .2s}
.dz.on{filter:drop-shadow(0 0 16px rgba(var(--c),.95)) brightness(1.2);transform:scale(1.5)}
.dz.on .ring{animation:dzring .7s ease-out infinite}
.dz.on .i{animation:dzico .5s ease-in-out infinite alternate}
.dz.sink{animation:dzsink .42s ease-out forwards}
@keyframes dzring{from{transform:scale(.8);opacity:.9}to{transform:scale(1.5);opacity:0}}
@keyframes dzico{from{transform:rotate(-12deg) scale(1.05)}to{transform:rotate(12deg) scale(1.28)}}
@keyframes dzsink{0%{transform:scale(1.5)}40%{transform:scale(1.85);filter:brightness(1.6)}100%{transform:scale(1);opacity:0}}
@keyframes dzin{from{opacity:0;transform:translateY(40px) scale(.6)}to{opacity:1}}
body.pressing,body.pressing *{-webkit-user-select:none!important;user-select:none!important;-webkit-touch-callout:none!important}
.card.csel .poster{box-shadow:0 0 0 3px var(--ac2)}
.card .cks{position:absolute;top:8px;left:8px;z-index:5;width:26px;height:26px;border-radius:50%;border:2px solid #fff;background:#0007;display:grid;place-items:center;color:#fff;backdrop-filter:blur(4px)}
.card.csel .cks{background:var(--ac2);border-color:var(--ac2);color:#06231c}
.card .cks .i{width:15px;height:15px}
.card.reo .badge{left:40px}
.libbar{position:fixed;left:10px;right:10px;bottom:calc(70px + var(--bot));z-index:40;background:var(--s1);border:1px solid rgba(255,255,255,.1);border-radius:18px;padding:10px 12px;box-shadow:0 10px 30px #000a;animation:dzin .2s ease-out}
.libbar .c{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;font-size:14px;font-weight:600}
.libbar .dact{display:flex;gap:8px}.libbar .dact .btn{flex:1}
.cm{display:flex;flex-direction:column;gap:6px;margin-top:4px}
.cm button{display:flex;align-items:center;gap:12px;width:100%;border:0;background:var(--s1);color:inherit;font:inherit;font-size:15.5px;font-weight:600;text-align:left;padding:14px;border-radius:14px;cursor:pointer}
.cm button .i{width:20px;height:20px;color:var(--ac2)}
.cm button,[data-lp]{-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}.card.sr{-webkit-touch-callout:none}
.cm button:active{background:var(--s2)}
.cm button.dng,.cm button.dng .i{color:var(--err,#ff6b7a)}
.cm button.dng,.cm button.dng .i{color:#ff6b6b}
.card.reo .ab{position:absolute;top:-7px;right:-7px;z-index:6;width:30px;height:30px;border-radius:50%;border:2px solid var(--bg,#0e1116);background:var(--ac2);color:#06231c;display:grid;place-items:center;padding:0;box-shadow:0 2px 8px #0008}
.card.reo .ab .i{width:16px;height:16px}
.nh{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px}.nh h3{margin:0}
.nlist{display:flex;flex-direction:column;gap:6px;max-height:62vh;overflow-y:auto;margin:0 -4px;padding:0 4px}
.ntf{display:flex;align-items:center;gap:12px;width:100%;text-align:left;border:0;background:var(--s1);color:inherit;font:inherit;border-radius:14px;padding:11px 12px;cursor:pointer;opacity:.62}
.ntf.un{opacity:1;background:#8b7bff22;box-shadow:inset 0 0 0 1px #8b7bff55}
.ntf .nic{flex:none;width:36px;height:36px;border-radius:11px;display:grid;place-items:center;background:var(--s2);color:var(--ac2)}
.ntf.un .nic{background:var(--ac);color:#fff}
.ntf .nic .i{width:18px;height:18px}
.ntf .ntx{flex:1;min-width:0}.ntf .ntx b{display:block;font-size:14.5px;line-height:1.25}.ntf .ntx span{display:block;font-size:12.5px;color:var(--mu);margin-top:2px}
.ntf .ndot{flex:none;width:10px;height:10px;border-radius:50%;background:var(--ac2);box-shadow:0 0 8px var(--ac2)}
.nempty{text-align:center;color:var(--mu);padding:30px 0}
@keyframes flashrow{0%,100%{background:transparent}30%,70%{background:#0e9f8455}}
.flash{animation:flashrow 1.1s ease 2}
.lvprev{margin:4px 0 8px;padding:10px;border-radius:14px;background:var(--s1)}
.lvprev .lbl{font-size:11.5px;color:var(--mu);margin:0 2px 8px;text-transform:uppercase;letter-spacing:.6px}
.lvprev .grid{padding:0;pointer-events:none}
.rgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px 10px;padding:10px 16px 6px}
.rgrid .rc{flex:none;min-width:0}
.rc{flex:0 0 112px;scroll-snap-align:start;background:none;border:0;padding:0;text-align:left;color:inherit;font:inherit;cursor:pointer}
.rc:active{transform:scale(.97)}
.rc .rp{position:relative;aspect-ratio:2/3;border-radius:12px;overflow:hidden;background:var(--s1);box-shadow:0 0 0 1px rgba(255,255,255,.06)}
.rc .rp img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .25s}
.rc .rp img.ld{opacity:1}
.rc.cur .rp{box-shadow:0 0 0 2px var(--ac)}
.rc .rn{position:absolute;left:6px;top:6px;z-index:2;min-width:22px;height:22px;border-radius:11px;background:rgba(10,12,22,.78);color:#fff;font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center;padding:0 6px}
.rc .rb{position:absolute;left:6px;right:6px;bottom:6px;z-index:2;font-size:10.5px;font-weight:600;border-radius:8px;padding:2px 6px;background:rgba(10,12,22,.78);color:var(--ac2);text-align:center}
.rc.cur .rb{color:#fff;background:var(--ac)}
.rc .rt{margin-top:6px;font-size:12.5px;line-height:1.25;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.rc .rm{margin-top:2px;font-size:11px;color:var(--mu);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.epgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:0 12px}
@media(min-width:720px){.epgrid{grid-template-columns:repeat(3,minmax(0,1fr))}}
.ec{background:var(--s1);border-radius:16px;overflow:hidden;position:relative;transition:transform .15s,box-shadow .2s}
.ec:active{transform:scale(.98)}
.ec.sel{box-shadow:0 0 0 2px var(--ac)}
.ec .th{position:relative;display:block;aspect-ratio:16/9;background:linear-gradient(135deg,#2c2757,#171a2a);overflow:hidden}
.ec .th img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .35s}
.ec .th img.ld{opacity:1}.ec .th img.bl{filter:blur(14px);transform:scale(1.2)}
.ec .th::after{content:'';position:absolute;inset:0;background:linear-gradient(transparent 40%,#000b);z-index:1;pointer-events:none}
.ec .th .num{position:absolute;left:10px;bottom:5px;z-index:2;font-size:28px;font-weight:800;line-height:1;text-shadow:0 2px 10px #000b;font-variant-numeric:tabular-nums}
.ec .th .stt{position:absolute;right:8px;top:8px;z-index:3;font-size:11px;padding:2px 8px;border-radius:99px;background:#000a;display:flex;align-items:center;gap:3px;backdrop-filter:blur(4px)}
.ec .th .stt .i{width:11px;height:11px}.ec .th .stt.dl{color:var(--ok)}
.ec .th .rs{position:absolute;right:8px;bottom:7px;z-index:3;font-size:11px;padding:2px 8px;border-radius:99px;background:#000a;color:var(--warn)}
.ec .th .rs .i{width:11px;height:11px;margin-right:3px;vertical-align:-1px}
.ec .th .ck{position:absolute;left:8px;top:8px;z-index:3;width:24px;height:24px;border-radius:8px;border:2px solid #fff9;background:#0007;display:grid;place-items:center;padding:0;color:#fff}
.ec.sel .th .ck{background:var(--ac);border-color:var(--ac)}.ec .th .ck .i{width:15px;height:15px;stroke-width:3}
.ec .th .plc{position:absolute;inset:0;z-index:2;display:grid;place-items:center;pointer-events:none}
.ec .th .plc i{width:44px;height:44px;border-radius:50%;background:#000a;display:grid;place-items:center;opacity:0;transition:opacity .2s}
.ec:active .th .plc i{opacity:1}.ec .th .plc .i{width:20px;height:20px;margin-left:2px}
.ec .inf{display:flex;align-items:flex-start;gap:4px;padding:8px 6px 8px 10px}
.ec .inf .mid{flex:1;min-width:0}
.ec .inf .n{font-weight:600;font-size:14px}.ec .inf .tt{font-size:12px;color:var(--mu);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ec .inf .tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.ec .inf .bt{display:flex;flex-direction:column}
.ec .eye{width:36px;height:34px;border-radius:10px;border:0;background:none;opacity:.35;display:grid;place-items:center}
.ec .eye.on{opacity:1}.ec .eye .i{width:19px;height:19px}
.ec.w .th img,.ec.w .th .num{opacity:.6}.ec.w .th img.ld{opacity:.55}
.ec.enter{animation:pop .45s cubic-bezier(.2,.9,.3,1.2) both;animation-delay:calc(var(--i,0)*35ms)}
.pch{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 8px}
.pch button{border:0;border-radius:99px;padding:8px 14px;background:var(--s2);color:inherit;font-size:13px;transition:background .2s,transform .15s}
.pch button.on{background:var(--ac);color:#fff}.pch button:active{transform:scale(.95)}
.grp .row.col{display:block}
.card.reo{position:relative;cursor:default}
.grid,.grid *,#lvgrp,#lvgrp *,.sheet[data-kind=cmenu],.sheet[data-kind=cmenu] *{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
.sheet[data-kind=text] input{-webkit-user-select:text;user-select:text}
.card.reo{touch-action:pan-y;-webkit-user-select:none;user-select:none}
.card.reo .poster{animation:wig .24s ease-in-out infinite alternate}
.card.reo:nth-child(2n) .poster{animation-delay:-.12s;animation-direction:alternate-reverse}
@keyframes wig{from{transform:rotate(-1.3deg)}to{transform:rotate(1.3deg)}}
.card.reo .xb{position:absolute;top:-7px;left:-7px;z-index:6;width:30px;height:30px;border-radius:50%;border:2px solid var(--bg,#0e1116);background:#ff453a;color:#fff;font-size:22px;font-weight:700;line-height:1;display:grid;place-items:center;padding:0 0 2px;box-shadow:0 2px 8px #0008}
.card.drag{position:relative;z-index:60;filter:drop-shadow(0 14px 24px #000a);will-change:transform}
.card.drag .poster{animation:none}
body.dragging{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
.card{-webkit-touch-callout:none}
.jsum{font-size:13px;color:var(--mu);margin:-4px 0 12px}.jsum b{color:var(--fg,#fff);font-weight:600}
.tag.dl{background:#173a2b;color:var(--ok)} .tag.nw{background:#0e9f84;color:#fff;font-weight:700} .tag.rs{background:#3a2e14;color:var(--warn)}
.ep .eye{width:36px;height:36px;border-radius:11px;border:0;background:none;font-size:19px;opacity:.35}
.ep .eye.on{opacity:1}
.ep .pl{width:40px;height:40px;border-radius:50%;border:0;background:var(--s2);font-size:15px;display:grid;place-items:center}
.ep .pl:active{background:var(--ac)}
.ep.w .n,.ep.w .tt{opacity:.55}
.dlbar{position:fixed;left:10px;right:10px;bottom:calc(76px + var(--bot));z-index:30;display:flex;gap:8px;align-items:center;
  padding:10px;border-radius:18px;background:rgba(31,34,48,.96);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);box-shadow:0 10px 40px #000a}
.dlbar .c{flex:1;font-size:14px;font-weight:600}
.foot{padding:22px 16px 0;display:flex;justify-content:center}


/* ---------- пропорції кнопок і відступи ---------- */
.bar+.hero{margin-top:12px}
.btn{white-space:nowrap;min-height:46px}
.btn.sm{min-height:40px;padding:0 14px}
.acts{flex-wrap:wrap}
.acts .btn.pri{flex:1 1 100%}
.acts .btn.ok,.acts .btn:not(.pri):not(.ok){flex:1 1 0}
.acts a.btn{flex:0 0 52px;padding:0}
.tools{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:6px 16px 10px;align-items:stretch}
.tools .btn.sm{width:100%;justify-content:center}
.tools .row2x{grid-column:1/-1;display:flex;gap:8px;align-items:stretch}
.tools .row2x select.q{flex:1;height:40px;padding:0 12px}
.tools .seg{flex:none;height:40px;align-items:center}
.dlbar{padding:12px}
.dlbar .dact{flex:1 1 100%;display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:8px}
.dlbar .dact .btn{width:100%;padding:0 10px;font-size:14px}
/* ---------- пошук ---------- */
.poster .pi{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .3s}
.poster .pi.ld{opacity:1}
.sform{display:flex;gap:8px;padding:12px 14px 4px}
.sbox{flex:1;display:flex;align-items:center;gap:8px;background:var(--s2);border-radius:14px;padding:0 6px 0 12px;min-width:0}
.sbox .i{width:19px;height:19px;color:var(--mu);flex:none}
.sbox input{flex:1;min-width:0;background:none;border:0;outline:0;font-size:16px;padding:12px 0}
.sbox .sx{border:0;background:none;color:var(--mu);width:34px;height:34px;display:grid;place-items:center;flex:none}
.sform .btn{flex:none;padding:0 16px}
.sfil{display:flex;gap:8px;overflow-x:auto;overflow-y:hidden;padding:8px 14px 6px;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.sfil::-webkit-scrollbar{display:none}
.fc{flex:none;display:inline-flex;align-items:center;gap:6px;border:0;border-radius:99px;background:var(--s2);padding:6px 12px 6px 6px;font-size:13px;color:inherit;white-space:nowrap}
.fc.mix{padding-left:12px}
.fc.on{background:var(--ac);color:#fff}
.fc .site{width:20px;height:20px;border-radius:6px;font-size:9.5px}
.fc small{font-size:11px;opacity:.75}
.fc .i{width:14px;height:14px}
.fc.off{opacity:.45}
.sicons{position:absolute;top:8px;right:8px;display:flex;gap:4px;z-index:2}
.sicons .site{position:static!important;width:22px;height:22px;border-radius:7px;font-size:10px;box-shadow:0 2px 8px #0008}
.card.sr{cursor:pointer}
.poster>span.inlib{inset:auto;place-items:normal;font-size:11px!important;position:absolute;top:8px;left:8px;z-index:2;background:var(--ok);color:#fff;border-radius:9px;padding:3px 8px;font-size:11px;font-weight:700;display:flex;align-items:center;gap:3px}
.inlib .i{width:12px;height:12px;stroke-width:3;stroke:#fff;color:#fff}
.poster>span.srcn{display:block;inset:auto;color:#fff;place-items:normal;position:absolute;top:8px;right:36px;z-index:2;background:#000a;border-radius:8px;font-size:11px;font-weight:700;padding:3px 6px;backdrop-filter:blur(4px)}
.srcs{display:flex;gap:8px;overflow-x:auto;overflow-y:hidden;padding:0 16px 10px;scrollbar-width:none}
.srcs::-webkit-scrollbar{display:none}
.srcs .dchip{display:inline-flex;align-items:center;gap:6px;flex:none}
.srcs .dchip .site{width:20px;height:20px;border-radius:6px;font-size:9.5px}
.tmpbar{margin:0 16px 10px;padding:10px 12px;border-radius:14px;background:#1b2a2a;color:#bfeee0;font-size:13px;display:flex;gap:10px;align-items:center}
.tmpbar .i{width:18px;height:18px;flex:none}
.hint{margin:0 16px 8px;font-size:12.5px;color:var(--mu)}
.btn.on{background:var(--ac);color:#fff}
.ep,.ec{-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}
.ep .ck{width:28px;height:28px;border-radius:9px}
.ep .ck .i{width:17px;height:17px}
.ec .th .ck{width:28px;height:28px}
.dlbar{flex-wrap:wrap}
.dlbar .c{flex:1 1 100%;display:flex;align-items:center;justify-content:space-between}
.dlbar .c .btn{flex:none}
.rb2{border:0;border-radius:10px;background:var(--s2);color:#fff;width:38px;height:36px;display:grid;place-items:center;flex:none}
.rb2:disabled{opacity:.3}.rb2 .i{width:18px;height:18px}
.rb2.up .i{transform:rotate(180deg)}
.grp .row .site{width:26px;height:26px;border-radius:8px}
/* ---------- шторки ---------- */
.back-d{position:fixed;inset:0;z-index:50;background:#000a;display:flex;align-items:flex-end}
.sheet{width:100%;max-height:88vh;max-height:88dvh;display:flex;flex-direction:column;overflow:hidden;background:var(--s1);border-radius:22px 22px 0 0;
  padding:0;animation:up .22s ease-out;will-change:transform}
#sb{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;padding:0 16px calc(18px + var(--bot))}
@keyframes up{from{transform:translateY(40px);opacity:0}}
@keyframes dn{from{transform:translateY(-14px);opacity:0}}
.sheet h3{margin:0 0 12px;font-size:19px}
.grab{flex:none;height:30px;display:grid;place-items:center;touch-action:none;cursor:grab}
.grab::before{content:'';width:42px;height:5px;border-radius:3px;background:var(--s3)}
.in{width:100%;background:var(--s2);border:1px solid transparent;border-radius:13px;padding:13px 14px;font-size:16px;outline:0}
.in:focus{border-color:var(--ac)}
.row2{display:flex;gap:8px;margin:10px 0}
.res{display:flex;gap:12px;margin:12px 0;padding:12px;border-radius:14px;background:var(--s2)}
.res .pic{flex:0 0 70px;aspect-ratio:2/3;border-radius:10px;background:linear-gradient(160deg,#2b2f4d,#171926) center/cover}
.res .tx{min-width:0;font-size:13.5px;color:var(--mu)}
.res .tx b{display:block;color:var(--tx);font-size:16px;margin-bottom:3px}
.res .tx p{margin:6px 0 0;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.err{color:var(--err);font-size:14px;margin:8px 0}
.job{padding:11px 0;border-bottom:1px solid #ffffff0d}
.jg{background:var(--s2);border-radius:16px;margin:0 0 10px;overflow:hidden}
.jgh{display:flex;align-items:center;gap:10px;padding:10px 10px 10px 10px;cursor:pointer}
.jgp{flex:none;width:40px;height:56px;border-radius:9px;background:linear-gradient(160deg,#2b2f4d,#171926) center/cover no-repeat}
.jgt{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.jgt b{font-size:14.5px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.jgt span{font-size:12px;color:var(--mu);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.jgh .btn.sm{flex:none}
.jgh .jgx.conf{background:var(--err);color:#fff;font-size:12.5px;font-weight:700;padding:0 10px;width:auto}
.jga{flex:none;width:22px;height:22px;display:grid;place-items:center;color:var(--mu);transition:transform .25s}
.jga .i{width:18px;height:18px}
.jg.open .jga{transform:rotate(180deg)}
.jgb{display:grid;grid-template-rows:0fr;transition:grid-template-rows .28s ease}
.jgb>div{overflow:hidden;min-height:0}
.jg.open .jgb{grid-template-rows:1fr}
.jgi{padding:0 12px 4px}
.jgi .job:last-child{border-bottom:0}
#jobacts{flex-wrap:nowrap}#jobacts .btn{min-width:0;padding-left:8px;padding-right:8px;white-space:nowrap}
.job:last-child{border-bottom:0}
.job .h{display:flex;gap:8px;align-items:center}
.job .h .t{flex:1;min-width:0;font-size:14.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.job .st{font-size:12.5px;color:var(--mu);margin-top:4px}
.job .st.e{color:var(--err)} .job .st.d{color:var(--ok)}
.prog{height:6px;border-radius:3px;background:var(--s3);margin-top:7px;overflow:hidden}
.prog>i{display:block;height:100%;background:linear-gradient(90deg,var(--ac),var(--ac2));transition:width .3s}

/* ---------- плеєр ---------- */
#player{position:fixed;inset:0;z-index:80;background:#000}
#player .top{position:absolute;left:0;right:0;top:0;z-index:4;display:flex;align-items:center;gap:10px;padding:calc(8px + var(--top)) calc(12px + var(--rgt)) 10px calc(12px + var(--lft));
  background:rgba(10,11,16,.55);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid #ffffff14;color:#fff}
#player .top .tt{flex:1;min-width:0;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#player .top .tt small{display:block;color:var(--mu);font-weight:400;font-size:12px}
#player .stage{position:absolute;inset:0;background:#000}
#player video{position:absolute;inset:0;width:100%;height:100%;background:#000;object-fit:contain}
.vc{position:absolute;inset:0;transition:opacity .2s}
.vc.off{opacity:0}.vc.off *{pointer-events:none}
.vc .mid3{position:absolute;left:0;right:0;top:50%;transform:translateY(-50%);display:flex;justify-content:center;align-items:center;gap:34px;pointer-events:none}
.vc .cb{pointer-events:auto;width:56px;height:56px;border-radius:50%;border:0;background:#000a;color:#fff;display:grid;place-items:center}
.vc .cb.big{width:72px;height:72px}
.vc .cb .i{width:28px;height:28px}.vc .cb.big .i{width:34px;height:34px}
.vc .sk{position:absolute;left:calc(10px + var(--lft));right:calc(10px + var(--rgt));bottom:calc(8px + var(--bot));display:flex;align-items:center;gap:10px;background:#000b;border-radius:14px;padding:8px 12px;font-size:12px;color:#ddd;font-variant-numeric:tabular-nums}
.vc .sk input{position:absolute;inset:0;width:100%;height:100%;margin:0;opacity:0;z-index:3}
.vc .sk .tr{position:relative;flex:1;min-width:0;height:24px}
.vc .sk .tr .tbar{position:absolute;left:0;right:0;top:10px;height:4px;border-radius:2px;background:#ffffff33;overflow:hidden}
.vc .sk .tr .tfill{position:absolute;left:0;top:0;bottom:0;width:0;background:var(--ac)}
.vc .sk .tr .mk{position:absolute;top:8px;height:8px;border-radius:2px;pointer-events:none;z-index:1;opacity:.9}
.vc .sk .tr .mk.op{background:#ffb020}.vc .sk .tr .mk.ed{background:#38c6ff}
.vc .sk .tr .knob{position:absolute;top:6px;width:12px;height:12px;margin-left:-6px;border-radius:50%;background:#fff;pointer-events:none;z-index:2}
.skipbtn{position:absolute;right:calc(14px + var(--rgt));bottom:calc(78px + var(--bot));z-index:5;border:0;border-radius:12px;padding:11px 16px;background:#fffe;color:#111;font-weight:700;font-size:14px;display:flex;gap:6px;align-items:center;box-shadow:0 2px 12px #0006;animation:pop .2s}
.skipbtn.hide{display:none}
.skipbtn .cnt{opacity:.6;font-weight:500}
#player .tt small{display:block}
.vc .cb.sm{width:42px;height:42px;background:#0008}.vc .cb.sm .i{width:20px;height:20px}
.vc .sk .xb{pointer-events:auto;flex:none;width:32px;height:32px;border:0;border-radius:10px;background:#ffffff1a;color:#fff;display:grid;place-items:center;padding:0;position:relative;z-index:4}
.vc .sk .xb .i{width:18px;height:18px}
.vvp{position:absolute;right:calc(10px + var(--rgt));bottom:calc(70px + var(--bot));z-index:6;display:flex;align-items:center;gap:12px;width:min(320px,calc(100% - 20px - var(--lft) - var(--rgt)));padding:10px 16px;border-radius:16px;background:#000d;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);color:#fff}
.vvp.hide{display:none}.vvp .i{width:22px;height:22px;flex:none}.vvp b{flex:none;width:46px;text-align:right;font-size:14px;font-variant-numeric:tabular-nums}.vvp.boost b{color:#ffb020}
.vsl{position:relative;flex:1;height:36px;--p:100%;--m:100}
.vsl input{-webkit-appearance:none;appearance:none;width:100%;height:36px;margin:0;background:transparent;position:relative;z-index:2;touch-action:none}
.vsl:before{content:'';position:absolute;left:0;right:0;top:14px;height:8px;border-radius:4px;background:#ffffff2e}
.vsl:after{content:'';position:absolute;left:0;top:14px;height:8px;border-radius:4px;width:var(--p);background:linear-gradient(90deg,var(--ac) var(--bs,100%),#ffb020 var(--bs,100%))}
.vsl input::-webkit-slider-runnable-track{height:36px;background:transparent}
.vsl input::-webkit-slider-thumb{-webkit-appearance:none;width:26px;height:26px;margin-top:5px;border-radius:50%;background:#fff;box-shadow:0 2px 8px #0008}
.vsl input::-moz-range-track{background:transparent;height:36px}.vsl input::-moz-range-thumb{width:26px;height:26px;border:0;border-radius:50%;background:#fff}
.vsl .v100{position:absolute;top:9px;width:2px;height:18px;border-radius:1px;background:#ffffffaa;z-index:1;pointer-events:none;display:none}
.vc .mid3{gap:16px}
@media (orientation:landscape){.vc .mid3{gap:40px}}
#player .top .ib{color:#fff}
.selmenu{z-index:120}
.selmenu .sheet{padding-bottom:calc(10px + var(--bot))}
.selmenu .sbody{overflow-y:auto;padding:2px 12px 8px;-webkit-overflow-scrolling:touch}
.selmenu .sh{padding:6px 18px 8px;font-size:15px;color:var(--mu)}
.selopt{display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;border:0;background:none;color:var(--tx);font-size:16px;padding:14px 12px;border-radius:12px;text-align:left}
.selopt.on{color:var(--ac);background:#ffffff0d}
.selopt .i{width:20px;height:20px;flex:none}
.selpop-b{position:fixed;inset:0;z-index:130;background:transparent}
.selpop{position:fixed;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;padding:6px;border-radius:16px;background:#1c1f2bf2;-webkit-backdrop-filter:blur(18px) saturate(1.4);backdrop-filter:blur(18px) saturate(1.4);border:1px solid #ffffff1f;box-shadow:0 14px 40px #000a;animation:selpop .16s cubic-bezier(.2,.9,.3,1.1) both}
.selpop .selopt{padding:11px 12px;font-size:15.5px}
@keyframes selpop{from{opacity:0;transform:scale(.94) translateY(-4px)}to{opacity:1;transform:none}}

body{padding-left:var(--lft);padding-right:var(--rgt)}
#tabs{padding-left:calc(6px + var(--lft));padding-right:calc(6px + var(--rgt))}
#player .msg{position:absolute;left:0;right:0;top:50%;transform:translateY(-50%);text-align:center;padding:0 30px;color:#ddd;pointer-events:none}
#player .msg a{pointer-events:auto;color:var(--ac)}
.sw{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--mu)}
.sw input{width:38px;height:22px;appearance:none;-webkit-appearance:none;border-radius:11px;background:var(--s3);position:relative;outline:0}
.sw input::after{content:"";position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;background:#fff;transition:.15s}
.sw input:checked{background:var(--ac)} .sw input:checked::after{left:18px}

#toast{position:fixed;left:50%;top:calc(66px + var(--top));transform:translateX(-50%);z-index:100;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none;width:92%}
.toast{background:#2b2f44;color:#fff;border-radius:13px;padding:10px 14px;font-size:14px;box-shadow:0 8px 30px #000a;max-width:100%;animation:dn .2s}
.toast.e{background:#4a1f28}

/* ---------- анімації ---------- */
@keyframes fadein{from{opacity:0}}
@keyframes fadeout{to{opacity:0}}
@keyframes down{to{transform:translateY(60px);opacity:0}}
@keyframes pop{0%{transform:scale(.86);opacity:0}60%{transform:scale(1.04);opacity:1}100%{transform:scale(1)}}
@keyframes rowin{from{transform:translateY(14px);opacity:0}}
@keyframes slidein{from{transform:translateX(26px);opacity:0}}
@keyframes bump{0%{transform:scale(1)}40%{transform:scale(1.4)}100%{transform:scale(1)}}
@keyframes shimmer{to{background-position:-200% 0}}
@keyframes stripes{to{background-position:20px 0}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
@keyframes spring{0%{transform:translateY(-16px) scale(.9);opacity:0}60%{transform:translateY(3px) scale(1.02);opacity:1}100%{transform:none}}
button,.card,.btn,.ib,#tabs a,.dchip{transition:transform .14s cubic-bezier(.2,.9,.3,1.3),background .18s,opacity .18s,box-shadow .18s;-webkit-tap-highlight-color:transparent}
button:active,.card:active,.btn:active,.ib:active,#tabs a:active,.dchip:active{transform:scale(.94)}
.card:active{transform:scale(.97)}
.btn.pri,.ib.pri{box-shadow:0 6px 18px #6a5cff44}
.ep{transition:background .2s}
.ep:active{background:#ffffff08}
.bump{animation:bump .38s cubic-bezier(.2,.9,.3,1.3)}
.enter-page{animation:fadein .26s both}
#root.detail .enter-page{animation:slidein .32s cubic-bezier(.2,.8,.2,1) both}
.card.enter{animation:pop .5s cubic-bezier(.2,.9,.3,1.2) both;animation-delay:calc(var(--i,0)*45ms)}
.ep.enter{animation:rowin .38s cubic-bezier(.2,.8,.2,1) both;animation-delay:calc(var(--i,0)*28ms)}
#tabs a.on .i{animation:pop .4s}
.back-d{animation:fadein .2s}
.sheet{animation:up .32s cubic-bezier(.2,.9,.3,1.05)}
.back-d.out{animation:fadeout .18s forwards}
.back-d.pop{background:#0003;display:block}
.back-d.pop .sheet{position:fixed;width:208px;border-radius:16px;padding:6px;background:#1c1f2bf2;-webkit-backdrop-filter:blur(18px) saturate(1.4);backdrop-filter:blur(18px) saturate(1.4);box-shadow:0 14px 40px #000c;border:1px solid #ffffff1f;animation:popin .16s cubic-bezier(.2,.9,.3,1.1);transform-origin:var(--po,center)}
.back-d.pop .sheet .grab{display:none}
.back-d.pop #sb{padding:0}
.back-d.pop .cm{gap:2px;margin:0;padding:0;background:none;border-radius:0}.back-d.pop .cm button{padding:11px 12px;font-size:15px;font-weight:500;gap:12px;background:transparent;border-radius:11px;line-height:1.2}.back-d.pop .cm button:active{background:#ffffff14}.back-d.pop .cm button .i{width:19px;height:19px;flex:none}
.back-d.pop .sheet h3{font-size:12px;color:var(--mu);margin:6px 12px 6px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.back-d.pop.out .sheet{animation:fadeout .12s forwards}
@keyframes popin{from{opacity:0;transform:scale(.9)}to{opacity:1;transform:none}}
.back-d.out .sheet{animation:down .18s forwards}
.toast{animation:spring .38s cubic-bezier(.2,.9,.3,1.2)}
.net.flip{animation:pop .45s}
.prog>i{background-image:linear-gradient(45deg,#ffffff30 25%,transparent 25%,transparent 50%,#ffffff30 50%,#ffffff30 75%,transparent 75%);background-size:20px 20px;animation:stripes .8s linear infinite;transition:width .45s cubic-bezier(.2,.8,.2,1)}
.prog.idle>i{animation:none;background-image:none}
.sk{background:linear-gradient(90deg,var(--s2) 25%,var(--s3) 50%,var(--s2) 75%);background-size:200% 100%;animation:shimmer 1.3s linear infinite;border-radius:12px}
.job{animation:rowin .3s both}
.job.gone{animation:fadeout .2s forwards}
.site{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:7px;font-size:11px;font-weight:800;color:#fff;flex:none;letter-spacing:-.3px}
.site.yummyani{background:linear-gradient(135deg,#ff5d8f,#ff8f5d)}
.site.aniliberty{background:linear-gradient(135deg,#5b8cff,#8f5bff)}
.site.animru{background:linear-gradient(135deg,#17c3a3,#2f8fd8)}
.site.animevost{background:linear-gradient(135deg,#f5a524,#e5484d)}
.site.other{background:#4a4f6a}
.lgopt.dis{opacity:.4;pointer-events:none}
.site.lg,.site.lg.yummyani,.site.lg.aniliberty,.site.lg.animru,.site.lg.animevost{background:#12141cd9;-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);padding:0;overflow:hidden;place-items:stretch}
.site.lg img{width:76%;height:76%;justify-self:center;align-self:center;object-fit:contain;display:block;pointer-events:none}
.site.lg.animevost img{width:96%;height:96%}
.card .site{position:absolute;top:8px;right:8px;width:24px;height:24px;border-radius:8px;font-size:10.5px;box-shadow:0 2px 8px #0008;z-index:2}
.chip.sitechip{display:inline-flex;align-items:center;gap:6px;padding:2px 9px 2px 3px}
.chip.sitechip .site{width:18px;height:18px;border-radius:6px;font-size:9px}
.dubs{display:flex;gap:8px;overflow-x:auto;overflow-y:hidden;padding:2px 16px 10px;scrollbar-width:none}
.dubs::-webkit-scrollbar{display:none}
.dchip{flex:none;display:inline-flex;align-items:center;gap:6px;border:1px solid #ffffff14;background:var(--s2);border-radius:99px;padding:7px 13px;font-size:13px;font-weight:600;color:var(--mu)}
.dchip small{font-weight:700;font-size:11px;background:#173a2b;color:var(--ok);border-radius:99px;padding:1px 6px}
.dchip.on{background:linear-gradient(135deg,var(--ac),#6a5cff);color:#fff;border-color:transparent;box-shadow:0 4px 14px #6a5cff55}
.dchip.on small{background:#ffffff30;color:#fff}
.findbox{display:flex;align-items:center;gap:12px;margin:14px 0 4px;padding:13px 14px;border-radius:14px;background:var(--s2);animation:rowin .25s both}
.findbox .i{width:22px;height:22px;color:var(--ac)}
.findbox .tx{flex:1;min-width:0;font-size:14px}
.findbox .tx small{display:block;color:var(--mu);font-size:12px;margin-top:2px}
.job .st{display:flex;align-items:center;gap:6px}
.job .dub{font-weight:500;color:var(--mu);font-size:12px}
.job .h .btn{flex:none}
@media (prefers-reduced-motion:reduce){*{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
.card .cd{white-space:nowrap;margin:0 0 5px;padding:2px 8px;border-radius:8px;font-size:11px;background:#000a;backdrop-filter:blur(4px)}
.card .cd .i{width:12px;height:12px}
.card.hidet .ct .t{display:none}
.card .sn{display:inline-block;margin-bottom:4px;font-size:12px;font-weight:700;padding:2px 8px;border-radius:8px;background:var(--ac);color:#fff}
.grid.c3{grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;padding:12px 10px}
.grid.c3 .card .t{font-size:11.5px;-webkit-line-clamp:3}
.grid.c3 .poster .ct{left:7px;right:7px;bottom:9px}
.grid.c3 .poster .s{font-size:10.5px;margin-top:2px}
.grid.c3 .poster .s .i{width:10px;height:10px}
.grid.c3 .poster .sx{display:none}
.grid.c3 .card .site{top:6px;right:6px;width:19px;height:19px;border-radius:6px;font-size:9px}
.grid.c3 .poster>span.srcn{top:29px;right:6px;font-size:9.5px;padding:1px 5px;border-radius:6px}
.grid.c3 .badge{top:6px;left:6px;font-size:10px;padding:2px 6px;border-radius:7px;max-width:calc(100% - 34px);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.grid.c3 .badge .i{width:10px;height:10px}
.grid.c3 .card .cd{font-size:9.5px;padding:2px 6px;max-width:100%;overflow:hidden}
.grid.c3 .card .cd span{overflow:hidden;text-overflow:ellipsis}
.grid.c3 .card .cd .i{display:none}
.grid.c3 .card .sn{font-size:10.5px;padding:1px 6px}
.card .cd{overflow:hidden}
.cd{display:inline-flex;align-items:center;gap:7px;margin-top:10px;padding:6px 12px;border-radius:12px;background:#ffffff1c;color:#fff;font-size:13px;font-weight:600;font-variant-numeric:tabular-nums;max-width:100%}
.cd .i{width:16px;height:16px;flex:none;color:var(--ac2)}
.cd.soon{background:linear-gradient(135deg,#6a5cff66,#00cec955);animation:pulse 1.6s ease-in-out infinite}
.cd.due{background:#ffb02e33;color:#ffd27a}
.cd small{font-weight:500;opacity:.75}
.job .st{align-items:flex-start;overflow-wrap:anywhere;word-break:break-word;line-height:1.35}
.job .st .i{margin-top:2px}
.job .st .tx{min-width:0}
.sheet[data-kind=jobs] #sb{display:flex;flex-direction:column;overflow:hidden;padding-bottom:0}
.sheet[data-kind=jobs] #joblist{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;margin:0 -16px;padding:0 16px 8px}
.sheet[data-kind=jobs] #jobempty{flex:none}
#jobacts{flex:none;display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:0 -16px;padding:12px 16px calc(12px + var(--bot));background:var(--s1);border-top:1px solid #ffffff10}
#jobacts .btn{width:100%;min-height:46px;border-radius:14px}#jobacts .btn:nth-last-child(1):nth-child(odd),#jobacts .jgbtn.wide{grid-column:1/-1}#jobacts:empty{display:none}
.jg{contain:layout paint}
.jgp{position:relative;overflow:hidden}.jgp{background:#ffffff0d}.jgp .pi{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.jgp[data-p]:not([data-p='']){cursor:pointer}
.jgpb{width:40px;padding:0}.jgpb[hidden]{display:none!important}
#hdrtog{position:fixed;left:50%;top:calc(var(--barh,58px) + 8px);z-index:19;width:58px;height:28px;border-radius:14px;border:1px solid #ffffff22;background:rgba(13,14,19,.78);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);color:var(--tx,#fff);display:flex;align-items:center;justify-content:center;padding:0;opacity:0;visibility:hidden;pointer-events:none;transform:translate(-50%,-10px) scale(.9);transition:opacity .3s ease,transform .42s cubic-bezier(.22,1,.36,1),visibility 0s .42s}
#hdrtog.on{opacity:1;visibility:visible;pointer-events:auto;transform:translate(-50%,0) scale(1);transition:opacity .3s ease .18s,transform .42s cubic-bezier(.22,1,.36,1) .18s,visibility 0s}#hdrtog:active{transform:translate(-50%,0) scale(.92)}#hdrtog svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.jgf{padding:0 12px 12px}.jgf:empty{display:none}
.jgbtn{width:100%;min-height:42px;border-radius:12px;background:transparent;border:1px solid #ffffff1f;color:var(--mu);font:inherit;font-weight:600;font-size:13.5px}
.jgbtn.res{color:var(--tx,#fff)}.jgbtn.conf{border-color:var(--err);color:var(--err);background:#ff6b6b14}
.jdiv{display:flex;align-items:center;gap:10px;margin:16px 2px 10px;color:var(--mu);font-size:12.5px;font-weight:600;text-transform:uppercase;letter-spacing:.06em}.jdiv:before,.jdiv:after{content:'';flex:1;height:1px;background:#ffffff18}.jdiv:before{flex:0 0 14px}
.jg.cx{opacity:.85}
.job .h .btn.jx{min-height:34px;min-width:34px;padding:0 9px;background:transparent;color:var(--mu)}.job .h .btn.jx.conf{background:#ff6b6b1f;color:var(--err);font-size:12.5px;font-weight:700}
.toast{overflow-wrap:anywhere}
html{-webkit-text-size-adjust:100%;overflow-x:hidden}
body{min-height:100vh;min-height:100dvh;-webkit-touch-callout:none}
button,a,.card,.ep,.dchip,#tabs a{touch-action:manipulation;-webkit-user-select:none;user-select:none}
input,select,textarea{font-size:16px}
.ep .eye{width:44px;height:44px}
.ep .ck{position:relative}
.ep .ck::after{content:'';position:absolute;inset:-12px}
.btn.sm{min-height:38px}

.stblk{text-decoration:none;color:inherit}.stblk .tx{flex:1;min-width:0}.stblk>svg{transform:rotate(-90deg);opacity:.5}
.stbar{display:flex;height:8px;border-radius:4px;background:#ffffff10;overflow:hidden;margin-top:8px;gap:1px}.stbar>i{display:block;height:100%}.stbar.sm{height:5px;margin-top:6px}
.stcard{margin:12px 16px;padding:14px 16px;background:var(--s1,#171923);border-radius:16px}.stcard>b{display:block;margin-bottom:2px}.stcard .btn{margin-top:10px}
.opst{display:flex;align-items:center;gap:8px;margin-top:10px;color:var(--mu);font-size:14px}.opst .spin{display:inline-flex;width:18px;height:18px;animation:spin 1s linear infinite}.opst .spin svg{width:100%;height:100%}
#oppage{position:fixed;inset:0;z-index:40;background:var(--bg,#0d0e14);overflow:hidden;padding-top:env(safe-area-inset-top)}
.bkc{margin:12px 16px;padding:14px 16px;background:var(--s1,#171923);border-radius:16px;display:block}.bkc>b{display:block;margin-bottom:4px}.bkd{display:block;color:var(--mu);font-size:13.5px;line-height:1.4;overflow-wrap:anywhere}.bkc .btn{margin-top:10px}.bkopts{display:flex;flex-direction:column;gap:6px;margin-top:8px}.bko{display:flex;gap:10px;align-items:flex-start;text-align:left;background:transparent;border:1px solid var(--s3,#2a2d3f);border-radius:12px;padding:10px 12px;color:inherit;font:inherit}.bko i{flex:0 0 16px;height:16px;border-radius:50%;border:2px solid var(--mu);margin-top:2px}.bko.on{border-color:var(--ac,#8b7bff);background:#8b7bff18}.bko.on i{border-color:var(--ac,#8b7bff);background:radial-gradient(circle,var(--ac,#8b7bff) 45%,transparent 50%)}.bko span{display:block}.bko b{display:block;font-weight:600}.bko em{display:block;font-style:normal;color:var(--mu);font-size:12.5px;line-height:1.35;margin-top:2px}.bkc .row2 .btn{margin-top:0}.bkc .strow{display:flex;align-items:center;gap:8px;margin-top:10px}.bkc .strow .tx{flex:1;min-width:0}.bkc .strow .tx span{display:block;color:var(--mu);font-size:12.5px}.bkc .strow .btn{margin-top:0}
.stcard.hero{display:flex;justify-content:center;padding:22px 16px}
.stdn{position:relative;width:210px;height:210px}.stdn svg{width:100%;height:100%}.stdn circle{transition:stroke-dasharray .5s}
.stdn .c{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}.stdn .c b{font-size:26px}.stdn .c span{font-size:12px;color:var(--mu)}
.strow{display:flex;align-items:center;gap:12px;padding:12px 16px;border-top:1px solid #ffffff12}.strow:first-child{border-top:0}
.strow .sdt{width:12px;height:12px;border-radius:4px;flex:none}.strow .tx{flex:1;min-width:0}.strow .tx b{display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.strow .tx span{font-size:12px;color:var(--mu)}
.strow .sz{font-variant-numeric:tabular-nums;font-size:14px;white-space:nowrap}
.stbtn{background:#ffffff12;border:0;border-radius:10px;width:36px;height:36px;color:var(--tx);display:flex;align-items:center;justify-content:center;flex:none}.stbtn svg{width:18px;height:18px}
.sth{margin:20px 20px 0;font-size:15px;color:var(--mu);font-weight:600}

.card.arch .poster::before,.poster.garch::before{content:'';position:absolute;inset:0;background:#141a33;background:color-mix(in srgb,#1a2142 28%,transparent);-webkit-backdrop-filter:grayscale(1) contrast(1.06) brightness(.86);backdrop-filter:grayscale(1) contrast(1.06) brightness(.86);pointer-events:none}
.card.arch .pb>i{background:#7fd6a4}
.seen{position:absolute;left:9px;top:9px;width:24px;height:24px;border-radius:50%;display:grid;place-items:center;background:#ffffff26;-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);color:#bff0d4}.seen .i{width:14px;height:14px}
.card.mrg .poster,.card.mrg .gst{outline:3px solid var(--ac2);outline-offset:3px;transform:scale(.95);transition:transform .2s}
.card.mrgd{opacity:.9}
.gcard{cursor:pointer}
.gst{position:relative}
.grow{display:flex;gap:8px;padding:14px 16px 6px;flex-wrap:wrap}
.gtl{font-size:26px;letter-spacing:-.4px;margin:22px 16px 0;line-height:1.15}
#root.fill{min-height:calc(100dvh - 84px - var(--bot))}
.gst .gl{position:absolute;inset:0;border-radius:var(--r);background:linear-gradient(160deg,#2b2f4d,#171926) center/cover;box-shadow:0 3px 10px #0007}
.gst .gl::after{content:'';position:absolute;inset:0;border-radius:inherit;background:#0006}
.gst .gl.l1{transform:translate(6px,-6px)}
.gst .gl.l2{transform:translate(12px,-12px)}
.gst .gl.arch::after{background:#1a214260;-webkit-backdrop-filter:grayscale(1) brightness(.86);backdrop-filter:grayscale(1) brightness(.86)}
.gcard .poster{box-shadow:0 0 0 1.5px #ffffff30}
.gcard.csel .poster{box-shadow:none}
.gh2{padding:0 0 10px}.gh2 h3{margin:0}.gh2 span{font-size:13px;color:var(--mu)}
.grid.gsh{padding:6px 0;max-height:62vh;overflow:auto;grid-template-columns:repeat(2,1fr)}.grid.gsh.c3{grid-template-columns:repeat(3,1fr)}
.strow.go{cursor:pointer}.strow .gochev{display:inline-flex;transform:rotate(-90deg);opacity:.5}
.sth2{width:34px;height:50px;border-radius:7px;background:#ffffff14 center/cover;flex:none;display:grid;place-items:center;font-weight:700;color:#ffffff55}
.sth.flash{animation:stfl 1.2s}@keyframes stfl{0%,60%{color:var(--ac)}100%{color:var(--mu)}}
</style>
<script>
/* Безпечні зони (виріз / Dynamic Island / індикатор «Home»). Якщо WebView розтягнутий на весь екран, а env() віддає 0 —
   рахуємо відступи за розміром екрана. Працює лише всередині iPhone-застосунку (data-ios). */
(function () {
  var root = document.documentElement;
  function probe(side) {
    var d = document.createElement('div');
    d.style.cssText = 'position:fixed;visibility:hidden;pointer-events:none;left:0;top:0;padding-' + side + ':env(safe-area-inset-' + side + ',0px)';
    document.documentElement.appendChild(d);
    var cs = getComputedStyle(d), v = parseFloat(cs['padding' + side.charAt(0).toUpperCase() + side.slice(1)]) || 0;
    d.remove();
    return v;
  }
  function guess() {
    var long = Math.max(screen.width, screen.height), short = Math.min(screen.width, screen.height);
    var top = 20, bot = 0;
    if (long >= 874) { top = 62; bot = 34; }                 // iPhone 16 Pro / Pro Max, 17
    else if (long === 852 || long === 932) { top = 59; bot = 34; }  // 14 Pro/15/16, 14 Pro Max/15 Plus/15 Pro Max
    else if (long >= 812) { top = 47; bot = 34; }             // iPhone X … 14 Plus (виріз)
    return {top: top, bot: bot, short: short};
  }
  window.__insets = function (t, l, b, r) {  // нативні безпечні зони з iOS (надійніше, ніж env())
    window.__nat = 1;
    root.style.setProperty('--top', t + 'px'); root.style.setProperty('--bot', b + 'px');
    root.style.setProperty('--lft', l + 'px'); root.style.setProperty('--rgt', r + 'px');
  };
  function apply() {
    if (window.__nat) return;
    var landscape = window.innerWidth > window.innerHeight;
    var t = probe('top'), b = probe('bottom');
    if (root.getAttribute('data-ios') === '1' && !t) {
      // WebView на весь екран (innerHeight ≈ висоті екрана) і env() порожній — підставляємо розмір із таблиці
      var full = Math.abs(window.innerHeight - (landscape ? Math.min(screen.width, screen.height) : Math.max(screen.width, screen.height))) < 6;
      if (full) {
        var g = guess();
        t = landscape ? 0 : g.top;
        b = landscape ? Math.min(g.bot, 21) : g.bot;
      }
    }
    root.style.setProperty('--top', t + 'px');
    root.style.setProperty('--bot', b + 'px');
  }
  apply();
  window.addEventListener('resize', apply);
  window.addEventListener('orientationchange', function () { setTimeout(apply, 250); });
  document.addEventListener('DOMContentLoaded', apply);
})();
</script>
</head>
<body>
<div id="root"></div>
<nav id="tabs"></nav>
<div id="layer"></div>
<div id="player" class="hide">
  <div class="stage" id="stage">
    <video id="vid" playsinline webkit-playsinline preload="auto"></video>
    <button class="skipbtn hide" id="vskip"></button>
    <div class="nxbox hide" id="vnx"><div class="nxt" id="vnxt"></div><div class="nxb"><button class="btn sm" data-act="pnxnow">Зараз</button><button class="btn sm" data-act="pnxcancel">Скасувати</button></div><div class="nxbar"><i id="vnxbar"></i></div></div>
    <div class="vc off" id="vc">
      <div class="top">
        <button class="ib" data-act="pclose" aria-label="Закрити"><i data-ico="close"></i></button>
        <div class="tt" id="ptitle"></div>
        <label class="sw"><input type="checkbox" id="pauto"> Далі</label>
      </div>
      <div class="mid3">
        <button class="cb sm" data-act="pprev" aria-label="Попередня серія"><i data-ico="prev"></i></button>
        <button class="cb" id="vback" aria-label="Назад 10 с"><i data-ico="back10"></i></button>
        <button class="cb big" id="vpp" aria-label="Пауза"><i data-ico="pause"></i></button>
        <button class="cb" id="vfwd" aria-label="Вперед 10 с"><i data-ico="fwd10"></i></button>
        <button class="cb sm" data-act="pnext" aria-label="Наступна серія"><i data-ico="next"></i></button>
      </div>
      <div class="vvp hide" id="vvp"><i data-ico="vol"></i><div class="vsl" id="vsl"><input type="range" id="vvr" min="0" max="100" step="1" value="100" aria-label="Гучність"><span class="v100" id="v100"></span></div><b id="vvn">100%</b></div>
      <div class="sk"><span id="vcur">0:00</span><div class="tr" id="vtr"><div class="tbar"><div class="tfill" id="vfill"></div></div><div class="knob" id="vknob"></div><input type="range" id="vseek" min="0" max="1000" value="0" step="1"></div><span id="vrem">0:00</span>
        <button class="xb" id="vvol" aria-label="Гучність"><i data-ico="vol"></i></button>
        <button class="xb" id="vpip" aria-label="Картинка в картинці"><i data-ico="pip"></i></button>
        <button class="xb" id="vfs" aria-label="На весь екран"><i data-ico="fs"></i></button></div>
    </div>
  </div>
  <div class="msg hide" id="pmsg"></div>
</div>
<div id="toast"></div>

<script>
'use strict';
const $ = (s, el = document) => el.querySelector(s);
const IOS_APP = document.documentElement.dataset.ios === '1';  // працюємо всередині iPhone-застосунку
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ls = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
};
const S = {
  items: [], tmp: [], selMode: false, libSel: new Set(), dtab: 'eps', dtabMap: {}, libTab: 'lib', nw: {site: null, data: {}, f: {year: '', type: '', genre: '', hide: false, sort: 'def'}, open: false}, search: {q: '', sel: new Set(), res: null, loading: false, err: '', vis: [], seq: 0}, siteOpen: false, ffmpeg: true, jobs: [], activeJobs: false,
  sel: new Set(), q: ls.get('q', '720p'), descOpen: false,
  sheet: null, busy: new Set(), checking: false, lookup: null, addErr: '',
  cur: null, // що зараз грає: {id, n}
  net: {state: 'unknown'}, set: {site_order: ['aniliberty', 'yummyani', 'animevost', 'animru'], mobile_download: false, auto_next: true, auto_check: true, auto_check_hours: 3, blur_thumbs: false, logging: false, multitask: true, ios_player: true}, st: {data: null, t: 0, loading: false},
  epView: (function () { try { return localStorage.getItem('epv') || 'list'; } catch (e) { return 'list'; } })(), reorder: false, acOpen: false, ac: null,
};

/* ---------- звіти про помилки ---------- */
(function () {
  let n = 0;
  const send = m => { if (n++ < 8) { try { fetch('/api/clientlog', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({msg: m + '\n' + location.href})}); } catch (e) {} } };
  window.addEventListener('error', e => send('JS: ' + e.message + ' @' + (e.filename || '') + ':' + e.lineno));
  window.addEventListener('unhandledrejection', e => send('Promise: ' + (e.reason && (e.reason.stack || e.reason.message) || e.reason)));
})();

/* ---------- мережа ---------- */
async function api(path, body, signal) {
  const opt = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  if (signal) opt.signal = signal;
  let r;
  for (let i = 0; ; i++) {  // iOS у фоні вимикає сервер; програма його піднімає — коротко чекаємо й повторюємо
    try { r = await fetch(path, opt); break; }
    catch (e) {
      if ((e && e.name === 'AbortError') || (signal && signal.aborted)) throw e;
      if (i >= 10) throw new Error('Здається, зв\'язок з Python втрачено!' + (IOS_APP ? ' Спробуйте закрити й знову відкрити програму.' : ''));
      await new Promise(res => setTimeout(res, 600));
    }
  }
  let j = {};
  try { j = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(j.error || ('Помилка ' + r.status));
  return j;
}
const ICONS = {
  share: '<path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><path d="m16 6-4-4-4 4"/><path d="M12 2v14"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/>',
  download: '<path d="M12 4v11"/><path d="m7 11 5 5 5-5"/><path d="M5 20h14"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  book: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5z"/><path d="M4 20.5A2.5 2.5 0 0 0 6.5 23H20v-5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  clip: '<rect x="8" y="3" width="8" height="4" rx="1.5"/><path d="M16 5h2a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2"/>',
  play: '<path d="M7 4.5v15l13-7.5z"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  cloud: '<path d="M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 9.5a4.2 4.2 0 0 1-.5 8.5z"/>',
  trash: '<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M6 7l1 13h10l1-13"/><path d="M10 11v5M14 11v5"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.8 3 2.8 15 0 18M12 3c-2.8 3-2.8 15 0 18"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  chev: '<path d="m6 9 6 6 6-6"/>',
  sort: '<path d="M7 4v16M7 4 4 7M7 4l3 3M17 20V4M17 20l-3-3M17 20l3-3"/>',
  vlist: '<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" stroke-width="2.4"/>',
  vgrid: '<rect x="3.5" y="4" width="7" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="7" height="6.5" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="6.5" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="6.5" rx="1.5"/>',
  fs: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  vol: '<path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M17.8 6.5a7.5 7.5 0 0 1 0 11"/>',
  pip: '<rect x="3" y="5" width="18" height="14" rx="2"/><rect x="11.5" y="11" width="7" height="5" rx="1" fill="currentColor"/>',
  prev: '<path d="M6 5v14"/><path d="M19 5v14L8 12z"/>',
  next: '<path d="M18 5v14"/><path d="M5 5v14l11-7z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  spark: '<path d="M12 3l1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4z"/>',
  alert: '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17v.5"/>',
  error: '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>',
  ok: '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 3 3 5-6"/>',
  plug: '<path d="M9 3v5M15 3v5"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v4"/>',
  hourglass: '<path d="M6 3h12M6 21h12"/><path d="M7 3v3a5 5 0 0 0 10 0V3M7 21v-3a5 5 0 0 1 10 0v3"/>',
  pause: '<path d="M8 5v14M16 5v14" stroke-width="3.2"/>',
  back10: '<path d="M4 12a8 8 0 1 0 2.5-5.8"/><path d="M4 4v4.5h4.5"/><text x="12" y="15.5" font-size="7.5" font-weight="700" text-anchor="middle" fill="currentColor" stroke="none">10</text>',
  fwd10: '<path d="M20 12a8 8 0 1 1-2.5-5.8"/><path d="M20 4v4.5h-4.5"/><text x="12" y="15.5" font-size="7.5" font-weight="700" text-anchor="middle" fill="currentColor" stroke="none">10</text>',
  wifi: '<path d="M2 9a15 15 0 0 1 20 0"/><path d="M5.5 12.6a10 10 0 0 1 13 0"/><path d="M9 16.2a5 5 0 0 1 6 0"/><path d="M12 20h.01" stroke-width="3"/>',
  cell: '<path d="M5 20v-3M10 20v-7M15 20V9M20 20V4"/>',
  offline: '<path d="M2 9a15 15 0 0 1 4-2.6M22 9a15 15 0 0 0-9-4.9"/><path d="M5.5 12.6a10 10 0 0 1 3-1.8M18.5 12.6a10 10 0 0 0-3-1.8"/><path d="M9 16.2a5 5 0 0 1 2.2-1"/><path d="M3 3l18 18"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1"/>',
  spinner: '<path d="M12 3a9 9 0 1 0 9 9"/>'
};
const FILLED = {play: 1, prev: 1, next: 1, spark: 1};
function ico(n, cls) {
  const st = FILLED[n] ? ' fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"'
    : ' fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
  return '<svg class="i' + (n === 'spinner' ? ' spin' : '') + (cls ? ' ' + cls : '') + '" viewBox="0 0 24 24"' + st + ' aria-hidden="true">' + ICONS[n] + '</svg>';
}
function fillIcons(root) {
  (root || document).querySelectorAll('[data-ico]').forEach(e => { e.outerHTML = ico(e.dataset.ico); });
}
function toast(msg, err, icon) {
  const d = document.createElement('div');
  d.className = 'toast' + (err ? ' e' : '');
  if (icon) d.insertAdjacentHTML('beforeend', ico(icon));
  d.appendChild(document.createTextNode(msg));
  $('#toast').appendChild(d);
  setTimeout(() => d.remove(), err ? 5200 : 2800);
}
let LIBSEQ = 0;
async function loadLib(quiet) {
  const my = ++LIBSEQ;
  const j = await api('/api/library');
  if (my !== LIBSEQ) return;  // прийшла застаріла відповідь — новіший запит уже в дорозі/виконаний
  const sig = JSON.stringify(j.items) + '|' + JSON.stringify(j.tmp || []);
  if (quiet === 'vis' && S._libSig === sig) return;  // повернулись із фону, а нічого не змінилось — не перемальовуємо (інакше картки «блимають»)
  S._libSig = sig; S.items = j.items; S.tmp = j.tmp || []; S.ffmpeg = j.ffmpeg;
  if (quiet !== true) render();
}
const item = id => S.items.find(x => x.id === id) || S.tmp.find(x => x.id === id);
const fmtSize = b => b >= 1073741824 ? (b / 1073741824).toFixed(2) + ' ГБ' : b >= 1048576 ? (b / 1048576).toFixed(b >= 104857600 ? 0 : 1) + ' МБ' : Math.max(1, Math.round(b / 1024)) + ' КБ';
const fmtDur = t => { t = Math.round(t); if (t < 60) return Math.max(1, t) + ' с'; const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60);
  if (h) return h + ' год ' + m + ' хв'; return m < 10 && t % 60 >= 5 ? m + ' хв ' + (t % 60) + ' с' : m + ' хв'; };
function qSelHTML(it) {
  const list = (it.quals && it.quals.length) ? it.quals : ['1080p', '720p', '480p'];
  let cur = S.q;
  if (cur !== 'best' && !list.includes(cur)) { const n = parseInt(cur) || 0, lo = list.filter(q => parseInt(q) <= n); cur = lo.length ? lo[0] : list[list.length - 1]; }
  return `<option value="best"${cur === 'best' ? ' selected' : ''}>Найкраща</option>` + list.map(q => `<option value="${q}"${q === cur ? ' selected' : ''}>${q}</option>`).join('');
}
const pkW = n => { const a = n % 100, b = n % 10; return n + (a > 10 && a < 15 ? ' пакетів' : b === 1 ? ' пакет' : (b > 1 && b < 5) ? ' пакети' : ' пакетів'); };
const fmtT = t => { t = Math.floor(t || 0); const m = Math.floor(t / 60), s = t % 60; return m + ':' + String(s).padStart(2, '0'); };

/* ---------- маршрути ---------- */
function route() {
  const m = location.hash.match(/^#\/a\/(\w+)/);
  if (m) return {name: 'detail', id: m[1]};
  const g = location.hash.match(/^#\/g\/(\w+)/);
  if (g) return {name: 'group', id: g[1]};
  const t = location.hash.match(/^#\/(search|new|settings|storage)/);
  return t ? {name: t[1]} : {name: 'home'};
}
const NETLBL = {wifi: 'Wi‑Fi', cell: 'Cellular', offline: 'Offline', online: 'Online', unknown: '—'};
const NETICO = {wifi: 'wifi', cell: 'cell', offline: 'offline', online: 'globe', unknown: 'wifi'};
function netHTML() {
  const st = S.net.state;
  return `<span class="net ${st}" id="netpill" title="Network${S.net.iface ? ' · ' + S.net.iface : ''}">${ico(NETICO[st])}${NETLBL[st]}</span>`;
}
function paintNet() {
  const el = $('#netpill'); if (!el) return;
  const st = S.net.state;
  el.title = 'Network' + (S.net.iface ? ' · ' + S.net.iface : '');
  el.className = 'net ' + st + ' flip'; el.innerHTML = ico(NETICO[st]) + NETLBL[st];
}
async function pollNet() {
  let st = 'online', ifn = '';
  try { const r = await api('/api/net'); st = r.state; ifn = r.iface || ''; } catch (e) { st = navigator.onLine === false ? 'offline' : S.net.state; }
  S.net.iface = ifn;
  const nc = navigator.connection;
  if (st === 'online' && nc && nc.type) st = nc.type === 'cellular' ? 'cell' : (nc.type === 'wifi' ? 'wifi' : st);
  if (navigator.onLine === false) st = 'offline';
  if (st !== S.net.state) { S.net.state = st; paintNet(); }
  if ((S._islTry || 0) < 8 && S.set && !S.set.island_ok) {  // нативна частина визначає наявність Dynamic Island трохи пізніше за старт сторінки
    S._islTry = (S._islTry || 0) + 1;
    api('/api/settings').then(j => { if (j.island_ok && S.set) { S.set.island_ok = true; if (route().name === 'settings') render(); } }).catch(() => {});
  }
}
window.addEventListener('online', pollNet); window.addEventListener('offline', pollNet);
setInterval(pollNet, 5000);
const TABS = [['home', '#/', 'book', 'Бібліотека'], ['search', '#/search', 'search', 'Пошук'],
  ['new', '#/new', 'spark', 'Новинки'], ['settings', '#/settings', 'gear', 'Налаштування']];
function tabsHTML(cur) {
  return TABS.map(t => `<a href="${t[1]}" class="${t[0] === cur ? 'on' : ''}">${ico(t[2])}${t[3]}</a>`).join('');
}
fillIcons();
const SCR = {};  // збережена прокрутка списків (бібліотека, група, пошук…) — щоб після закриття аніме лишатись там, де були
window.addEventListener('hashchange', () => {
  if (S.lastRender) SCR[S.lastRender] = window.scrollY;
  const rr = route(); S.dtab = (rr.name === 'detail' && S.dtabMap[rr.id]) || 'eps'; S.sel.clear(); S.selMode = false; S.descOpen = false; fx(); render();
  const key = rr.name + ':' + (rr.id || ''), y = rr.name !== 'detail' && SCR[key];
  window.scrollTo(0, y || 0);
  if (y) { requestAnimationFrame(() => window.scrollTo(0, y)); setTimeout(() => { if (Math.abs(window.scrollY - y) > 2) window.scrollTo(0, y); }, 120); }
});
// тримає на місці картку, що була вгорі екрана, поки список перемальовується (вхід/вихід з редагування)
function keepAnchor(fn) {
  let id = null, top = 0;
  for (const c of document.querySelectorAll('#root .card[data-id]')) { const r = c.getBoundingClientRect(); if (r.bottom > 90) { id = c.dataset.id; top = r.top; break; } }
  fn();
  if (id == null) return;
  const c = [...document.querySelectorAll('#root .card[data-id]')].find(x => x.dataset.id === id);
  if (c) window.scrollBy(0, c.getBoundingClientRect().top - top);
}

/* ---------- відмальовка ---------- */
function morphAttrs(a, b) {
  for (const at of [...a.attributes]) {
    if (!b.hasAttribute(at.name)) {
      if (a.tagName === 'IMG' && at.name === 'src') continue;  // картинки, вивантажені/завантажені скриптом, не чіпаємо
      if (a.tagName === 'IMG' && at.name === 'class') { a.className = [...a.classList].filter(c => c === 'ld').join(' '); continue; }  // лишаємо «вже показано», інакше обкладинки зникають
      a.removeAttribute(at.name);
    }
  }
  for (const at of [...b.attributes]) {
    let v = at.value;
    if (at.name === 'class' && a.tagName === 'IMG' && a.classList.contains('ld')) v += ' ld';  // «картинка вже показана» — не блимаємо
    if (a.getAttribute(at.name) !== v) a.setAttribute(at.name, v);
  }
}
function morphNode(a, b) {
  if (a.nodeType !== 1) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }
  morphAttrs(a, b);
  if (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA') {
    if (document.activeElement !== a) { if (a.tagName === 'INPUT') { a.value = b.getAttribute('value') || ''; a.checked = b.hasAttribute('checked'); } }
    return;
  }
  morphKids(a, b);
  if (a.tagName === 'SELECT') { const o = [...b.options].find(x => x.hasAttribute('selected')); if (o) a.value = o.value; }
}
function nodeKey(n) { return n.nodeType === 1 ? (n.getAttribute('data-n') || n.getAttribute('data-id') || n.id || '') : ''; }
function sameNode(a, b) { return a.nodeType === b.nodeType && (a.nodeType !== 1 || (a.tagName === b.tagName && nodeKey(a) === nodeKey(b))); }
function morphKids(a, b) {  // оновлюємо DOM на місці: незмінені вузли (картинки, прокрутка) лишаються, нічого не мерехтить
  const bk = [...b.childNodes];
  bk.forEach((nb, i) => {
    const na = a.childNodes[i] || null;
    if (na && sameNode(na, nb)) { morphNode(na, nb); return; }
    const k = nodeKey(nb);
    let found = null;
    if (k) for (let j = i + 1; j < a.childNodes.length; j++) { const c = a.childNodes[j]; if (c.nodeType === 1 && sameNode(c, nb)) { found = c; break; } }
    if (found) { a.insertBefore(found, na); morphNode(found, nb); }
    else a.insertBefore(nb, na);
  });
  while (a.childNodes.length > bk.length) a.removeChild(a.lastChild);
}
function centerChips() {
  document.querySelectorAll('.srcs, .dubs').forEach(el => {
    const c = el.querySelector('.dchip.on'); if (!c) return;
    const er = el.getBoundingClientRect(), cr = c.getBoundingClientRect();
    if (cr.left >= er.left + 4 && cr.right <= er.right - 4) return;  // уже видно — не чіпаємо
    el.scrollTo({left: Math.max(0, cr.left - er.left + el.scrollLeft - (er.width - cr.width) / 2), behavior: 'auto'});
  });
}
async function waitTask(tid, limitMs) {
  const t0 = Date.now(); let bad = 0;
  for (;;) {
    if (Date.now() - t0 > (limitMs || 90000)) { try { await api('/api/lookup_cancel', {task: tid}); } catch (e) {} throw new Error('Сайт відповідає занадто довго. Спробуйте ще раз пізніше.'); }
    try {
      const st = await api('/api/lookup_status?task=' + tid); bad = 0;
      if (st.done) { if (st.error) throw new Error(st.error); return st.result; }
    } catch (e) { if (e.message && !/fetch|network|Failed|Load failed/i.test(e.message)) throw e; if (++bad > 4) throw new Error('Здається, зв\'язок з Python втрачено!'); }
    await new Promise(r2 => setTimeout(r2, 400));
  }
}
function morphInto(root, html) { const t = document.createElement('template'); t.innerHTML = html; morphKids(root, t.content); }
function render() {
  if (DR.on) return;  // триває перетягування картки
  const r = route();
  const pages = {detail: () => detailHTML(r.id), search: searchHTML, new: newHTML, settings: settingsHTML, storage: storageHTML, group: () => groupHTML(r.id)};
  const root = $('#root');
  ENTER = S.enterPending; S.enterPending = false;
  const ekey = r.name + ':' + (r.id || '') + (r.name === 'home' ? ':' + S.libTab : '');
  if (ENTER) { if (ENTERED.has(ekey)) ENTER = false; else ENTERED.add(ekey); }  // анімація появи — раз за сесію для кожної сторінки/вкладки
  const html = (pages[r.name] || homeHTML)();
  const rkey = r.name + ':' + (r.id || '');
  const keepY = (!ENTER && S.lastRender === rkey) ? window.scrollY : -1;  // та сама сторінка перемальовується (напр. «Пам'ять» під час збереження) — не стрибаємо вгору
  if (!ENTER && S.lastRender === rkey && (r.name === 'detail' || r.name === 'home' || r.name === 'group' || r.name === 'new' || r.name === 'search') && root.firstChild) { try { morphInto(root, html); } catch (e) { root.innerHTML = html; } }
  else root.innerHTML = html;
  if (keepY > 0 && Math.abs(window.scrollY - keepY) > 1) window.scrollTo(0, keepY);
  const rchg = S.lastRender !== rkey;
  S.lastRender = rkey;
  if (rchg) S.hdrHide = false;
  if (r.name === 'detail' && (rchg || S.chipFix)) { S.chipFix = false; requestAnimationFrame(centerChips); setTimeout(centerChips, 350); }
  if (ENTER) [...root.children].forEach(c => c.classList.add('enter-page'));
  if (window.__fitBar) window.__fitBar();
  ENTER = false;
  const dit = r.name === 'detail' ? item(r.id) : null;
  if (['home', 'search', 'new'].includes(r.name)) { S.lastList = r.name; S.listFresh = true; }
  if (dit) {  // з якої вкладки відкрили аніме — на ній і лишаємось (також після «Додати в бібліотеку» та перезавантаження)
    let om = {}; try { om = JSON.parse(sessionStorage.getItem('origTab') || '{}'); } catch (e) {}
    if (!om[r.id] || S.listFresh) { S.listFresh = false; om[r.id] = dit.temp ? (S.lastList === 'new' ? 'new' : 'search') : (S.lastList || 'home'); try { sessionStorage.setItem('origTab', JSON.stringify(om)); } catch (e) {} }
    S.origTab = om[r.id] || (dit.temp ? 'search' : 'home');
  }
  const tab = r.name === 'detail' ? S.origTab : r.name === 'storage' ? 'settings' : r.name === 'group' ? 'home' : r.name;
  if (r.name === 'settings' || r.name === 'storage') storLoad();
  if (S.tabKey !== tab) { S.tabKey = tab; $('#tabs').innerHTML = tabsHTML(tab); }
  root.classList.toggle('detail', r.name === 'detail');
  root.classList.toggle('fill', r.name === 'home' || r.name === 'group');
  if (r.name === 'detail') maybeWarm(r.id);
  if (S.scrollEp && r.name === 'detail' && r.id === S.scrollEp.kid) {  // з повідомлення: прокручуємо до нової серії, але не вмикаємо її
    const q = S.scrollEp; S.scrollEp = null;
    setTimeout(() => {
      const el = document.querySelector(`#root [data-n="${CSS.escape(String(q.n))}"]`);
      if (el) { el.scrollIntoView({block: 'center', behavior: 'smooth'}); el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 2400); }
    }, 120);
  }
  tickCd();
  if (r.name === 'search') observeImgs();
  if (r.name === 'new') { observeImgs(); nwEnsure(); }
}
let ENTER = false;
const ENTERED = new Set();
function fx() { S.enterPending = true; }  // анімація входу — лише для найближчого render() після зміни сторінки
function bump(sel) { const el = document.querySelector(sel); if (el) { el.classList.remove('bump'); void el.offsetWidth; el.classList.add('bump'); } }
S.warmed = new Set();
function warm(id, n, dub) {
  const key = id + '|' + (dub || '') + '|' + n; if (S.warmed.has(key) || S.net.state === 'offline') return;
  S.warmed.add(key);
  api('/api/warm', {id, n}).then(j => {
    const v = j && j.view, it = item(id); if (!v || !it) return;
    it.quals = v.quals;
    v.episodes.forEach(x => { const e = it.episodes.find(y => y.n === x.n); if (e) { e.qs = x.qs; e.q = x.q; e.size = x.size; } });
    const sel = $('#qsel'), r = route();
    if (sel && r.name === 'detail' && r.id === id) sel.innerHTML = qSelHTML(it);
  }).catch(() => {});
}
function maybeWarm(id) {
  const it = item(id); if (!it) return;
  const nx = nextEp(it) || it.episodes[0];
  if (nx && !nx.downloaded) warm(id, nx.n, it.dubbing);
}

const MONTHS = ['січня', 'лютого', 'березня', 'квітня', 'травня', 'червня', 'липня', 'серпня', 'вересня', 'жовтня', 'листопада', 'грудня'];
function countdownHTML(nx) {
  if (!nx || !nx.ts) return '';
  return `<div class="cd" data-ts="${nx.ts}" data-ep="${esc(nx.ep ?? '')}" data-approx="${nx.approx ? 1 : 0}" data-date="${nx.date_only ? 1 : 0}">${ico('clock')}<span></span></div>`;
}
function tickCd() {
  document.querySelectorAll('.cd').forEach(el => {
    const ts = Number(el.dataset.ts) * 1000, diff = ts - Date.now();
    const cmp = el.dataset.c === '1';
    const ep = cmp ? (el.dataset.ep ? 'С' + el.dataset.ep : 'Нова') : el.dataset.ep ? 'Серія ' + el.dataset.ep : 'Наступна серія';
    let txt, cls = 'cd';
    if (diff <= 0) {
      cls += ' due';
      txt = el.dataset.date === '1' && diff > -86400000 ? `${ep} • сьогодні` : cmp ? `${ep} • вийшла?` : `${ep} • має вийти — натисніть «Нові»`;
    } else {
      const t = Math.floor(diff / 1000), d = Math.floor(t / 86400), h = Math.floor(t % 86400 / 3600), m = Math.floor(t % 3600 / 60), sec = t % 60;
      const p2 = n => String(n).padStart(2, '0');
      txt = cmp ? `${ep} • ${d ? d + 'д ' + h + 'г' : h ? h + 'г ' + p2(m) + 'хв' : m + 'хв'}` : `${ep} • ${d ? d + 'д ' : ''}${p2(h)}:${p2(m)}:${p2(sec)}`;
      if (diff < 3600000) cls += ' soon';
    }
    const sp = el.querySelector('span'); if (sp.textContent !== txt) sp.textContent = txt;
    if (el.className !== cls) el.className = cls;
  });
}
setInterval(tickCd, 1000);
function pageHead(t) { return `<div class="bar"><h1>${t}</h1>${netHTML()}</div>`; }
const SITE_LIST = [['aniliberty', 'AniLiberty'], ['yummyani', 'YummyAnime'], ['animevost', 'AnimeVost'], ['animru', 'Anim-RU']];
function siteRank(id) { const o = S.set.site_order || []; const i = o.indexOf(id); return i < 0 ? 99 : i; }
function siteList() { return SITE_LIST.slice().sort((a, b) => siteRank(a[0]) - siteRank(b[0])); }  // порядок — як у налаштуваннях
const SITE_MARK = {yummyani: 'Y', aniliberty: 'AL', animru: 'A', animevost: 'V'};
const normU = u => String(u || '').replace(/^https?:\/\/(www\.)?/, '').replace(/\/(episodes)?\/?$/, '').replace(/\/page-\d+-/, '/');
function inLib(its) {
  const set = new Set(its.map(x => normU(x.url)));
  return S.items.some(it => (it.sources || []).some(sx => set.has(normU(sx.url))) || set.has(normU(it.url)));
}
function filHTML() {
  const Q = S.search, sites = (Q.res && Q.res.sites) || {};
  const mix = !Q.sel.size;
  let h = `<button class="fc mix${mix ? ' on' : ''}" data-act="sfil" data-s="">${ico('globe')}Mix</button>`;
  h += siteList().map(([id, nm]) => {
    const st = sites[id], on = Q.sel.has(id);
    const tail = !st ? '' : st.state === 'run' ? `<span class="spin">${ico('spinner')}</span>` : st.state === 'err' ? `<span title="${esc(st.err)}">${ico('alert')}</span>` : `<small>${st.n}</small>`;
    return `<button class="fc${on ? ' on' : ''}${!mix && !on ? ' off' : ''}" data-act="sfil" data-s="${id}"><span class="site ${id}">${SITE_MARK[id]}</span>${nm}${tail}</button>`;
  }).join('');
  return h;
}
function resHTML() {
  const Q = S.search;
  if (Q.err) return `<p class="err" style="padding:8px 16px">${ico('alert')}${esc(Q.err)}</p>`;
  if (!Q.res) return Q.loading ? `<div class="empty" style="padding:50px"><span class="spin">${ico('spinner')}</span></div>`
    : `<div class="empty"><div class="big">${ico('search')}</div><h2>Пошук по всіх сайтах</h2><p>Mix показує результати одразу з усіх сайтів, однакові аніме об'єднані в одну картку. Фільтр зверху залишає лише потрібні сайти.</p></div>`;
  const sel = Q.sel, vis = [];
  for (const g of Q.res.groups) {
    const its = (sel.size ? g.items.filter(x => sel.has(x.site.id)) : g.items.slice()).sort((a, b) => siteRank(a.site.id) - siteRank(b.site.id));
    if (its.length) vis.push({g, its});
  }
  Q.vis = vis;
  const bad = Object.entries(Q.res.sites).filter(([id, v]) => v.state === 'err' && (!sel.size || sel.has(id))).map(([id, v]) => v.name + (v.err ? ' — ' + v.err : ''));
  const note = bad.length && Q.res.done ? `<p class="hint">Не вдалось: ${esc(bad.join('; '))}. Можна повторити пошук.</p>` : '';
  if (!vis.length) return Q.res.done ? `<div class="empty" style="padding:50px 30px"><h2>Нічого не знайдено</h2><p>Спробуйте іншу назву (російською або англійською).</p></div>${note}` : `<div class="empty" style="padding:50px"><span class="spin">${ico('spinner')}</span></div>`;
  const lim = Q.limit || 36;
  return note + `<div class="grid">` + vis.slice(0, lim).map(({g, its}, i) => {
    const p = (its.find(x => x.poster) || {}).poster;
    const title = sel.size ? its[0].title : g.title;
    return `<div class="card sr" data-act="openres" data-g="${i}">
      <div class="poster">
        <span>${esc((title || '?')[0])}</span>
        ${p ? `<img class="pi" decoding="async" alt="" data-o="${esc(p)}" data-s="/img?u=${encodeURIComponent(p)}" onload="this.classList.add('ld')" onerror="imgErr(this)">` : ''}
        <div class="sicons">${its.map(x => siteIcon(x.site)).join('')}</div>
        ${inLib(its) ? `<span class="inlib">${ico('check')}У бібліотеці</span>` : ''}
        <div class="grad"></div>
        <div class="ct"><div class="t">${esc(title)}</div><div class="s">${[g.year, its.length > 1 ? its.length + ' сайти' : its[0].site.name].filter(Boolean).join(' · ')}</div></div>
      </div></div>`;
  }).join('') + `</div>` + (vis.length > lim ? `<div style="padding:4px 16px 24px"><button class="btn" style="width:100%" data-act="smore">Показати ще (${vis.length - lim})</button></div>` : '');
}
function imgErr(el) {  // обкладинка не завантажилась (сервер ще качає з повільного сайту): повторюємо з паузами, потім напряму з сайту
  const st = Number(el.dataset.r || 0), o = el.dataset.o;
  el.dataset.r = st + 1;
  if (!o) { el.remove(); return; }
  const go = (ms, fn) => setTimeout(() => { if (el.isConnected) fn(); }, ms);
  if (st === 0) go(1500, () => { el.referrerPolicy = ''; el.src = (el.dataset.rp ? '/relposter?u=' : '/img?u=') + encodeURIComponent(o) + '&r=1'; });
  else if (st === 1) go(3000, () => { el.referrerPolicy = ''; el.src = (el.dataset.rp ? '/relposter?u=' : '/img?u=') + encodeURIComponent(o) + '&r=2'; });
  else if (st === 2) { if (el.dataset.rp) { el.remove(); return; } el.referrerPolicy = 'no-referrer'; el.src = o; }
  else if (st === 3) go(8000, () => { el.referrerPolicy = ''; el.src = (el.dataset.rp ? '/relposter?u=' : '/img?u=') + encodeURIComponent(o) + '&r=' + Date.now(); });
  else el.remove();
}
let IMG_IO = null, IMG_ACT = 0;
const IMG_SET = new Set();
setInterval(() => { for (const im of [...IMG_SET]) if (!im.isConnected) { if (IMG_IO) IMG_IO.unobserve(im); IMG_SET.delete(im); im.removeAttribute('src'); } }, 8000);  // чистимо картинки зі сторінок, яких уже немає
const IMG_Q = [];
function pumpImg() {  // не більше 4 картинок одночасно: з'єднань до локального сервера мало, і вони потрібні пошуку
  while (IMG_ACT < 4 && IMG_Q.length) {
    const im = IMG_Q.pop();  // найсвіжіші (ті, що зараз на екрані) — першими
    if (!im.isConnected || im.getAttribute('src') || !im.dataset.s || im.dataset.vis === '0') continue;
    IMG_ACT++;
    let fin = false;
    const done = () => { if (fin) return; fin = true; IMG_ACT = Math.max(0, IMG_ACT - 1); pumpImg(); };
    im.addEventListener('load', done, {once: true}); im.addEventListener('error', done, {once: true});
    const t0 = Date.now(), iv = setInterval(() => { if (fin) clearInterval(iv); else if (!im.isConnected || Date.now() - t0 > 9000) { clearInterval(iv); done(); } }, 600);  // картинку прибрало з DOM — слот не тримаємо
    im.src = im.dataset.s;
  }
}
function observeImgs() {  // картинки без src: вантажимо, коли наближаються до екрана, і вивантажуємо, коли далеко (пам'ять WebView обмежена; native lazy у WebKit ненадійний)
  const imgs = document.querySelectorAll('#sres .pi, #nres .pi');
  if (!('IntersectionObserver' in window)) { imgs.forEach(im => { if (!im.getAttribute('src') && im.dataset.s) im.src = im.dataset.s; }); return; }
  if (!IMG_IO) IMG_IO = new IntersectionObserver(es => es.forEach(en => {
    const im = en.target;
    if (!im.isConnected) { IMG_IO.unobserve(im); IMG_SET.delete(im); im.removeAttribute('src'); return; }  // елемент прибрано зі сторінки — не тримаємо його (і розпаковану картинку) в пам'яті
    im.dataset.vis = en.isIntersecting ? '1' : '0';
    if (en.isIntersecting) { if (!im.getAttribute('src') && im.dataset.s) { IMG_Q.push(im); pumpImg(); } }
    else if (im.getAttribute('src') && im.classList.contains('ld')) { im.dataset.s = im.getAttribute('src'); im.classList.remove('ld'); im.removeAttribute('src'); }
  }), {rootMargin: '600px 0px'});
  imgs.forEach(im => { if (!im.dataset.io) { im.dataset.io = 1; IMG_IO.observe(im); IMG_SET.add(im); } });
}
function paintSearch(force) {
  const f = $('#sfil'), r = $('#sres'), Q = S.search;
  const fs = filHTML(); if (f && (force || Q.fsig !== fs)) { Q.fsig = fs; f.innerHTML = fs; }
  const rs = resHTML(); if (r && (force || Q.rsig !== rs)) { Q.rsig = rs; r.innerHTML = rs; observeImgs(); }  // без зайвих перемальовувань: картинки не перезавантажуються
}
function searchHTML() {
  const Q = S.search;
  return pageHead('Пошук') + `<div class="hdr2"><form class="sform" data-form="search"><div class="sbox">${ico('search')}
      <input id="sq" type="search" enterkeyhint="search" autocomplete="off" autocapitalize="off" autocorrect="off" placeholder="Назва аніме" value="${esc(Q.q)}">
      <button type="button" class="sx" data-act="sclear" aria-label="Очистити">${ico('close')}</button></div>
    <button class="btn pri" type="submit">Знайти</button></form>
  <div class="sfil" id="sfil">${filHTML()}</div></div><div id="sres">${resHTML()}</div>`;
}
// ─────────── Новинки: каталоги онгоїнгів на сайтах ───────────
const NW_SORTS = [['def', 'Як на сайті'], ['year', 'Новіші за роком'], ['title', 'За назвою А–Я'], ['rating', 'За рейтингом']];
const nwKey = t => String(t || '').toLowerCase().replace(/ё/g, 'е').replace(/[^a-zа-яіїєґ0-9]+/g, '');
function nwSite() { return S.nw.site || (S.nw.site = (() => { try { return localStorage.getItem('nwsite'); } catch (e) { return null; } })() || 'mix'); }
function nwSites() { return nwSite() === 'mix' ? siteList().map(x => x[0]) : [nwSite()]; }
function nwSD(s) { return S.nw.data[s] || (S.nw.data[s] = {items: [], page: 0, more: true, loading: false, err: '', t: 0, tries: 0, hyd: false}); }
function nwHydrate(s) {  // показуємо останній відомий список одразу, поки оновлюється
  const D = nwSD(s); if (D.hyd) return; D.hyd = true;
  try { const c = JSON.parse(localStorage.getItem('nwc_' + s) || 'null'); if (c && c.items && c.items.length) { D.items = c.items; D.page = 1; D.more = true; D.t = c.t || 0; D.stale = true; } } catch (e) {}
}
function nwD() {  // зведений стан поточного вигляду (для Mix — злитий список з усіх сайтів, однакові аніме об'єднані)
  const ss = nwSites(); ss.forEach(nwHydrate);
  const Ds = ss.map(nwSD);
  if (ss.length === 1) return Ds[0];
  const out = [], idx = {}, max = Math.max(0, ...Ds.map(D => D.items.length));
  for (let i = 0; i < max; i++) Ds.forEach(D => {
    const x = D.items[i]; if (!x) return;
    const k = nwKey(x.title);
    if (k && idx[k] !== undefined) {
      const o = out[idx[k]];
      if (!o.sites.includes(x.site)) { o.sites.push(x.site); o.srcs.push({url: x.url, title: x.title}); }
      if (!o.poster && x.poster) o.poster = x.poster;
      if (!o.year && x.year) o.year = x.year;
      if (!o.type && x.type) o.type = x.type;
      if (!(o.genres || []).length && (x.genres || []).length) o.genres = x.genres;
      if (!o.rating && x.rating) o.rating = x.rating;
      if (x.lib) o.lib = true;
    } else { if (k) idx[k] = out.length; out.push({...x, sites: [x.site], srcs: [{url: x.url, title: x.title}]}); }
  });
  const ts = Ds.map(D => D.t).filter(Boolean);
  return {items: out, more: Ds.some(D => D.more && !D.err), loading: Ds.some(D => D.loading), t: ts.length ? Math.min(...ts) : 0, tries: 0,
          err: Ds.some(D => D.items.length) ? '' : (Ds.map(D => D.err).find(Boolean) || ''),
          errs: ss.map((id, i) => Ds[i].err ? ((SITE_LIST.find(y => y[0] === id) || [id, id])[1] + ' — ' + Ds[i].err) : '').filter(Boolean)};
}
function nwFilterCount() { const f = S.nw.f; return (f.year ? 1 : 0) + (f.type ? 1 : 0) + (f.genre ? 1 : 0) + (f.sort !== 'def' ? 1 : 0); }
function nwVisible(D) {
  const f = S.nw.f;
  let list = D.items.map((x, i) => ({x, i})).filter(({x}) =>
    (!f.year || String(x.year) === f.year) && (!f.type || x.type === f.type) && (!f.genre || (x.genres || []).includes(f.genre)) && (!f.hide || !(x.lib || inLib(x.srcs || [x]))));
  if (f.sort === 'year') list.sort((a, b) => (b.x.year || 0) - (a.x.year || 0));
  else if (f.sort === 'title') list.sort((a, b) => a.x.title.localeCompare(b.x.title, 'uk'));
  else if (f.sort === 'rating') list.sort((a, b) => (b.x.rating || 0) - (a.x.rating || 0));
  return list;
}
function nwSelect(key, label, vals, cur) {
  if (!vals.length) return '';
  return `<div class="ssel"><select data-nf="${key}" aria-label="${label}"><option value="">${label}: усі</option>${vals.map(v => `<option value="${esc(v)}"${String(v) === cur ? ' selected' : ''}>${esc(v)}</option>`).join('')}</select></div>`;
}
function nwHash(u) { let h = 5381; for (let i = 0; i < u.length; i++) h = ((h << 5) + h + u.charCodeAt(i)) | 0; return (h >>> 0).toString(36); }
function nwTime(t) { return t ? new Date(t).toLocaleTimeString('uk-UA', {hour: '2-digit', minute: '2-digit'}) : ''; }
function nwCtlHTML() {
  const D = nwD(), f = S.nw.f, cnt = nwFilterCount(), cur = nwSite();
  const uniq = fn => [...new Set(D.items.flatMap(fn).filter(Boolean))];
  const years = uniq(x => [x.year]).sort((a, b) => b - a).map(String), types = uniq(x => [x.type]).sort(), genres = uniq(x => x.genres || []).sort((a, b) => a.localeCompare(b, 'uk'));
  let h = `<div class="sfil"><button class="fc mix${cur === 'mix' ? ' on' : ''}" data-act="nsite" data-s="mix">${ico('globe')}Mix</button>${siteList().map(([id, nm]) => `<button class="fc${cur === id ? ' on' : cur !== 'mix' ? ' off' : ''}" data-act="nsite" data-s="${id}"><span class="site ${id}">${SITE_MARK[id]}</span>${nm}</button>`).join('')}</div>
    <div class="sortrow"><button class="tg${S.nw.open || cnt ? ' on' : ''}" data-act="nfilt">Фільтри${cnt ? ' · ' + cnt : ''}</button>
      <button class="tg${f.hide ? ' on' : ''}" data-act="nhide">Без наявних</button>
      <span class="ntime">${D.t ? 'Оновлено ' + nwTime(D.t) : ''}</span>
      <button class="tg" data-act="nrefresh" aria-label="Оновити">${ico(D.loading ? 'spinner' : 'refresh')}</button></div>`;
  if (S.nw.open) {
    h += `<div class="nfp">${nwSelect('year', 'Рік', years, f.year)}${nwSelect('type', 'Тип', types, f.type)}${nwSelect('genre', 'Жанр', genres, f.genre)}
      <div class="ssel"><select data-nf="sort" aria-label="Сортування">${NW_SORTS.map(([k, l]) => `<option value="${k}"${f.sort === k ? ' selected' : ''}>${l}</option>`).join('')}</select></div>
      ${cnt ? `<button class="btn sm" data-act="nreset">Скинути фільтри</button>` : ''}
      ${D.items.length && !years.length && !genres.length ? `<p class="hint" style="margin:0">Цей сайт не віддає рік і жанри у списку — доступні лише наявні поля.</p>` : ''}</div>`;
  }
  return h;
}
function nwResHTML() {
  const D = nwD(), vis = nwVisible(D), lim = S.nw.limit || 24;
  if (!D.items.length) {
    if (D.loading) return `<div class="empty" style="padding:50px"><span class="spin">${ico('spinner')}</span><p>Завантажую список…</p></div>`;
    return `<p class="err" style="padding:8px 16px">${ico('alert')}${esc(D.err || 'Список порожній.')}</p><div style="padding:4px 16px"><button class="btn" style="width:100%" data-act="nrefresh">Спробувати ще раз</button></div>`;
  }
  let h = '';
  const bad = D.errs || (D.err ? [D.err] : []);
  if (bad.length && !D.loading) h += `<p class="hint" style="padding:0 16px">Не вдалось: ${esc(bad.join('; '))}</p>`;
  if (!vis.length) h += `<div class="empty" style="padding:40px 30px"><h2>Нічого не підійшло</h2><p>${D.more ? 'У завантаженому немає збігів. Можна підвантажити ще сторінки або змінити фільтри.' : 'Змініть або скиньте фільтри.'}</p></div>`;
  else h += `<div class="grid">` + vis.slice(0, lim).map(({x, i}) => {
    const lib = x.lib || inLib(x.srcs || [x]);
    return `<div class="card sr" id="nc${nwHash(x.srcs ? x.srcs[0].url : x.url)}" data-act="bopen" data-i="${i}">
      <div class="poster">
        <span>${esc((x.title || '?')[0])}</span>
        ${x.poster ? `<img class="pi" decoding="async" alt="" data-o="${esc(x.poster)}" data-s="/img?u=${encodeURIComponent(x.poster)}" onload="this.classList.add('ld')" onerror="imgErr(this)">` : ''}
        <div class="sicons">${(x.sites || [x.site]).map(id => siteIcon({id, name: (SITE_LIST.find(y => y[0] === id) || [id, id])[1]})).join('')}</div>
        ${lib ? `<span class="inlib">${ico('check')}У бібліотеці</span>` : ''}
        <div class="grad"></div>
        <div class="ct"><div class="t">${esc(x.title)}</div><div class="s">${esc([x.type, x.year, x.info].filter(Boolean).join(' · '))}</div></div>
      </div></div>`;
  }).join('') + `</div>`;
  if (vis.length > lim) h += `<div style="padding:4px 16px 24px"><button class="btn" style="width:100%" data-act="nlim">Показати ще (${vis.length - lim})</button></div>`;
  else if (D.more) h += `<div style="padding:4px 16px 24px"><button class="btn" style="width:100%" data-act="nmore" ${D.loading ? 'disabled' : ''}>${ico(D.loading ? 'spinner' : 'plus')} Завантажити наступну сторінку</button></div>`;
  else h += `<p class="hint" style="text-align:center;padding:4px 16px 24px">Це все, що показує сайт.</p>`;
  return h;
}
function newHTML() {
  return pageHead('Новинки') + `<div class="hdr2"><div id="nctl">${nwCtlHTML()}</div></div><div id="nres">${nwResHTML()}</div>`;
}
function paintNew() {
  if (route().name !== 'new') return;
  const c = $('#nctl'), r = $('#nres'); if (!c || !r) { render(); return; }
  const sy = window.scrollY;
  try { morphInto(c, nwCtlHTML()); morphInto(r, nwResHTML()); } catch (e) { c.innerHTML = nwCtlHTML(); r.innerHTML = nwResHTML(); }
  observeImgs();
  if (Math.abs(window.scrollY - sy) > 2) window.scrollTo(0, sy);
}
async function nwLoadSite(s, more, force) {  // more=false: перша сторінка (замінює список, старий видно, доки не прийде новий)
  const D = nwSD(s); if (D.loading) return;
  const page = more ? D.page + 1 : 1;
  D.loading = true; D.err = ''; paintNew();
  let lastErr = '';
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      const ctl = new AbortController(), to = setTimeout(() => ctl.abort(), 45000);
      try {
        const j = await api('/api/browse', {site: s, page, force: !!force && attempt === 0}, ctl.signal);
        clearTimeout(to);
        if (j.cancelled) { lastErr = ''; break; }  // сервер перервав запит, бо користувач відкрив аніме/пошук
        const items = (j.items || []).map(x => ({...x, site: s}));
        if (page === 1) D.items = items;
        else { const seen = new Set(D.items.map(x => x.url)); items.forEach(x => { if (!seen.has(x.url)) { seen.add(x.url); D.items.push(x); } }); }
        D.page = page; D.more = !!j.has_more; D.t = Date.now(); D.stale = false; lastErr = '';
        if (page === 1) { try { localStorage.setItem('nwc_' + s, JSON.stringify({t: D.t, items: items.slice(0, 40)})); } catch (e) {} }
        break;
      } catch (e) {
        clearTimeout(to);
        lastErr = ctl.signal.aborted ? 'Сайт надто довго не відповідає.' : e.message;
        if (attempt === 0) await new Promise(r => setTimeout(r, 1500));
      }
    }
  } finally {
    D.err = lastErr; D.loading = false;
    try { paintNew(); } catch (e) {}
  }
}
async function nwLoad(more, force) {
  const ss = nwSites().filter(s => !more || nwSD(s).more);
  await Promise.all(ss.map(s => nwLoadSite(s, more, force)));
  const D = nwD();
  if (more !== undefined && D.more && (nwFilterCount() + (S.nw.f.hide ? 1 : 0)) && nwVisible(D).length < 12 && (S.nw.tries = (S.nw.tries || 0) + 1) <= 4) nwLoad(true);  // фільтри лишили мало — підвантажуємо наступну сторінку
}
function nwEnsure() {
  nwSites().forEach(s => {
    const D = nwSD(s); nwHydrate(s);
    if (D.loading) return;
    if (!D.items.length) { if (!D.err) nwLoadSite(s, false); }
    else if (Date.now() - D.t > 300000) nwLoadSite(s, false);  // старіше 5 хв (або показано з кешу) — тихо оновлюємо
  });
}
const AC_PRESETS = [[1, '1 год'], [3, '3 год'], [6, '6 год'], [12, '12 год'], [24, '1 день'], [48, '2 дні'], [72, '3 дні']];
function acLabel(h) { const p = AC_PRESETS.find(x => x[0] === Number(h)); return p ? p[1] : h + ' год'; }
function acSub() { return S.set.auto_check ? 'Кожні ' + acLabel(S.set.auto_check_hours) : 'Вимкнено'; }
function fmtWhen(ts) { return ts ? new Date(ts * 1000).toLocaleString('uk-UA', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}) : '—'; }
function acStat() {
  const a = S.ac; if (!a) return 'Статус завантажується…';
  if (!a.enabled) return 'Автоперевірка вимкнена.';
  return (a.running ? 'Перевіряю зараз… · ' : '') + 'Остання: ' + fmtWhen(a.last_run) + ' · наступна: ' + fmtWhen(a.next_run);
}
let acPollT = null;
async function pollAC(kick) {
  try {
    const j = await api('/api/autocheck' + (kick === true ? '?kick=1' : '')), prev = S.ac; S.ac = j;
    if (prev && j.seq !== prev.seq) {
      await loadLib();
      const f = (j.found || []).slice(-Math.max(1, j.seq - prev.seq));
      toast('Нові серії: ' + f.map(x => x.title + ' — ' + x.new.join(', ')).join('; ').slice(0, 140), false, 'spark');
    }
    const st = $('#acstat'); if (st) st.textContent = acStat();
  } catch (e) { /* сервер недоступний */ }
}
function siteOrder() {
  const o = (S.set.site_order || []).filter(x => SITE_LIST.some(y => y[0] === x));
  return o.concat(SITE_LIST.map(y => y[0]).filter(x => !o.includes(x)));
}
function siteGroupHTML() {
  const ord = siteOrder(), nm = id => (SITE_LIST.find(y => y[0] === id) || [id, id])[1];
  return `<div class="grp ${S.siteOpen ? 'open' : ''}" id="sitegrp">
    <button class="gh" data-act="sitetog"><div class="tx"><b>Основний сайт</b><span>${esc(nm(ord[0]))} · порядок сайтів</span></div>${ico('chev')}</button>
    <div class="fold"><div>
      <div class="row col"><span class="d">Нове аніме відкривається на першому сайті зі списку, де воно є; якщо там його немає — на наступному. Автоперевірка нових серій дивиться лише на той сайт, який вибрано для аніме, інші не навантажує.</span></div>
      ${ord.map((id, i) => `<div class="row"><span class="site ${id}">${SITE_MARK[id]}</span><div class="tx"><b>${nm(id)}</b><span class="d">${i === 0 ? 'Основний' : 'Запасний ' + i}</span></div>
        <button class="rb2 up" data-act="sitemv" data-s="${id}" data-d="-1" ${i === 0 ? 'disabled' : ''} aria-label="Вище">${ico('chev')}</button>
        <button class="rb2" data-act="sitemv" data-s="${id}" data-d="1" ${i === ord.length - 1 ? 'disabled' : ''} aria-label="Нижче">${ico('chev')}</button></div>`).join('')}
    </div></div></div>`;
}

/* ---------- Пам'ять ---------- */
const ST_COL = {anime: '#8b7bff', partial: '#ffb454', img: '#5ee0c1', thumbs: '#4cc9f0', posters: '#ff7eb6', logs: '#ff6b6b', data: '#9aa3c7', orphans: '#c7a6ff', backups: '#7cc4ff', app: '#6ee7a0', work: '#ffd166'};
const fmtS = b => b > 0 ? fmtSize(b) : '0';
function stBlockHTML() {
  const d = S.st.data;
  if (!d) return `<a class="set stblk" href="#/storage" id="stblk"><div class="tx"><b>Пам'ять</b><span>Рахуємо…</span></div></a>`;
  return `<a class="set stblk" href="#/storage" id="stblk">${stBlockInner(d)}</a>`;
}
function stBlockInner(d) {
  const tot = d.total || 1;
  const bar = d.cats.filter(c => c.size > 0).map(c => `<i style="width:${(c.size / tot * 100).toFixed(2)}%;background:${ST_COL[c.id]}"></i>`).join('');
  return `<div class="tx"><b>Пам'ять · ${fmtS(d.total)}</b><span>Аніме, кеш, логи, програма — докладніше й очищення</span><div class="stbar">${bar}</div></div>${ico('chev')}`;
}
async function storLoad(force) {
  const T = S.st;
  if (T.loading || (!force && T.data && Date.now() - T.t < 4000)) return;
  T.loading = true;
  try { T.data = await api('/api/storage'); T.t = Date.now(); } catch (e) { T.loading = false; return; }
  T.loading = false;
  const r = route();
  if (r.name === 'storage') { const y = window.scrollY; render(); window.scrollTo(0, y); }
  else if (r.name === 'settings') { const b = $('#stblk'); if (b) b.innerHTML = stBlockInner(T.data); }
}
function donutHTML(d) {
  const tot = d.total || 1; let cum = 0;
  const segs = d.cats.filter(c => c.size > 0).map(c => { const v = c.size / tot * 100; const o = `<circle r="15.9155" cx="21" cy="21" fill="none" stroke="${ST_COL[c.id]}" stroke-width="5" stroke-dasharray="${Math.max(v - 0.6, 0.2).toFixed(2)} ${(100 - Math.max(v - 0.6, 0.2)).toFixed(2)}" stroke-dashoffset="${(-cum).toFixed(2)}"/>`; cum += v; return o; }).join('');
  return `<div class="stdn"><svg viewBox="0 0 42 42"><circle r="15.9155" cx="21" cy="21" fill="none" stroke="#ffffff10" stroke-width="5"/><g transform="rotate(-90 21 21)">${segs}</g></svg><div class="c"><b>${fmtS(d.total)}</b><span>займає програма</span></div></div>`;
}
function storageHTML() {
  const d = S.st.data;
  const head = `<div class="bar"><button class="back" data-act="stback">‹ Налаштування</button><h1>Пам'ять</h1></div>`;
  if (!d) return head + `<div class="empty">Рахуємо…</div>`;
  const tot = d.total || 1;
  const CLR = {img: ['Очистити кеш картинок', 'Картинки з сайтів завантажаться знову, коли знадобляться.'], thumbs: ['Очистити мініатюри', 'Скріншоти серій створяться наново.'], logs: ['Очистити логи', 'Видаляє звіти про збої та журнали.'], partial: ['Видалити недокачане', 'Незавершені файли завантажень (крім тих, що качаються зараз).']};
  const an0 = d.anime.some(a => a.size > 0 || a.partial > 0);
  const rows = d.cats.filter(c => c.id !== 'work' || c.size).map(c => {
    const pct = c.size / tot * 100;
    const cl = CLR[c.id];
    return `<div class="strow${c.id === 'anime' ? ' go' : ''}"${c.id === 'anime' ? ' data-act="stgo"' : ''}><i class="sdt" style="background:${ST_COL[c.id]}"></i><div class="tx"><b>${c.name}</b><span>${c.count && c.unit ? c.count + ' ' + c.unit + ' · ' : ''}${pct < 0.1 ? '<0.1' : pct.toFixed(1)}%</span><div class="stbar sm"><i style="width:${Math.max(pct, c.size ? 1 : 0).toFixed(2)}%;background:${ST_COL[c.id]}"></i></div></div><div class="sz">${fmtS(c.size)}</div>${c.id === 'anime' && an0 ? `<span class="gochev">${ico('chev')}</span>` : ''}${cl && c.size ? `<button class="stbtn" data-act="stclear" data-w="${c.id}" data-t="${esc(cl[0])}" data-d="${esc(cl[1])}">${ico('trash')}</button>` : ''}</div>`;
  }).join('');
  const mx = Math.max(1, ...d.anime.map(a => a.size));
  const an = d.anime.filter(a => a.size > 0 || a.partial > 0).map(a => { const pi = item(a.id), pp = pi && pi.meta && pi.meta.poster; return `<div class="strow"><div class="sth2"${pp ? ` data-act="poster" data-src="${esc(pp)}" style="background-image:url('${esc(pp)}');cursor:zoom-in"` : ''}>${pp ? '' : esc((a.title || '?')[0])}</div><div class="tx"><b>${esc(a.title)}${a.archived ? ' · архів' : ''}</b><span>${a.eps} серій${a.partial ? ' · недокачано ' + fmtS(a.partial) : ''}</span><div class="stbar sm"><i style="width:${(a.size / mx * 100).toFixed(1)}%;background:${ST_COL.anime}"></i></div></div><div class="sz">${fmtS(a.size)}</div>${a.size ? `<button class="stbtn" data-act="stclear" data-w="anime" data-id="${a.id}" data-t="Видалити скачані серії?" data-d="${esc(a.title)}: файли серій буде видалено з пристрою. Аніме лишиться в бібліотеці."> ${ico('trash')}</button>` : ''}</div>`; }).join('');
  const orph = d.orphans.map(o => `<div class="strow"><div class="tx"><b>${esc(o.name)}</b><span>${o.files} файлів${o.safe ? '' : ' · не з додатка, видалити можна лише вручну'}</span></div><div class="sz">${fmtS(o.size)}</div>${o.safe ? `<button class="stbtn" data-act="stclear" data-w="orphan" data-name="${esc(o.name)}" data-t="Видалити папку?" data-d="Папка «${esc(o.name)}» не належить жодному аніме з бібліотеки.">${ico('trash')}</button>` : ''}</div>`).join('');
  const disk = d.disk ? (() => { const used = d.disk.total - d.disk.free; const ap = d.total / d.disk.total * 100; return `<div class="stcard"><b>Пристрій</b><div class="stbar"><i style="width:${ap.toFixed(2)}%;background:${ST_COL.anime}"></i><i style="width:${Math.max(0, (used - d.total) / d.disk.total * 100).toFixed(2)}%;background:#ffffff30"></i></div><span class="d">Вільно ${fmtS(d.disk.free)} із ${fmtS(d.disk.total)} · програма займає ${ap < 0.1 ? '<0.1' : ap.toFixed(1)}%</span></div>`; })() : '';
  const ram = d.ram ? `<div class="stcard"><b>Оперативна пам'ять</b><span class="d">Програма зараз: ${d.ram.peak ? Math.round(d.ram.peak) + ' МБ' : '—'}${d.ram.avail ? ' · iOS дозволяє ще ≈' + Math.round(d.ram.avail) + ' МБ' : ''} · потоків: ${d.ram.threads}</span></div>` : '';
  return head + `<div class="stcard hero">${donutHTML(d)}</div>
  <div class="grp open stlist">${rows}</div>
  ${d.watched_total ? `<div class="stcard"><b>Переглянуті серії</b><span class="d">Скачані серії, які ви вже подивились, займають ${fmtS(d.watched_total)}.</span><button class="btn sm" data-act="stclear" data-w="watched" data-t="Видалити переглянуті серії?" data-d="Файли переглянутих серій буде видалено. У списку вони лишаться, їх можна скачати знову.">${ico('trash')} Видалити переглянуті</button></div>` : ''}
  ${disk}${ram}
  ${an ? `<h2 class="sth" id="stanime">За аніме</h2><div class="grp open stlist">${an}</div>` : ''}
  ${orph ? `<h2 class="sth">Інші папки</h2><div class="grp open stlist">${orph}</div>` : ''}
  ${backupBox()}
  <div class="set"><div class="tx"><b>Вести логи</b><span>Вимкнено — нічого не пишеться на диск.</span></div><label class="sw"><input type="checkbox" data-set="logging" ${S.set.logging ? 'checked' : ''}></label></div>
  <div style="height:24px"></div>`;
}
function settingsHTML() {
  return pageHead('Налаштування') + stBlockHTML() + `
  <div class="set"><div class="tx"><b>Качати через мобільний інтернет</b>
    <span>Якщо вимкнено, завантаження працюють лише через Wi‑Fi. Перегляд онлайн це не обмежує.</span></div>
    <label class="sw"><input type="checkbox" id="mobsw" ${S.set.mobile_download ? 'checked' : ''}></label></div>
  <div class="grp ${S.lvOpen ? 'open' : ''}" id="lvgrp">
    <button class="gh" data-act="lvtog"><div class="tx"><b>Вигляд бібліотеки</b><span>Що показувати на картках аніме</span></div>${ico('chev')}</button>
    <div class="fold"><div>
      <div class="lvprev" id="lvprev">${lvPrevHTML()}</div>
      ${[['timer', 'Таймер до наступної серії', 'Скільки лишилось до виходу (якщо сайт знає).'], ['title', 'Назва аніме', 'Вимкніть, щоб лишився лише постер.'],
         ['seasonOnly', 'Лише номер сезону', 'Показувати «Сезон N» над назвою.'], ['site', 'Значок сайту', 'Де йде аніме (і кількість інших сайтів).'],
         ['stats', 'Прогрес і розмір', 'Переглянуто / скачано / розмір / озвучка.'], ['badge', 'Позначка нових серій', 'Бейдж із кількістю нових.'], ['gtitle', 'Назви груп', 'Показувати назву на стопці груп (групу можна створити й без назви).']]
        .map(([k, t, d]) => `<div class="row"><div class="tx"><b>${t}</b><span class="d">${d}</span></div><label class="sw"><input type="checkbox" data-lv="${k}" ${S.view[k] ? 'checked' : ''}></label></div>`).join('')}
      <div class="row"><div class="tx"><b>Логотипи сайтів</b><span class="d">Замість літер на значках показувати справжні лого.</span></div><label class="sw"><input type="checkbox" data-lv="logo" ${S.view.logo !== false ? 'checked' : ''}></label></div>
      <div class="row lgopt${S.view.logo !== false ? '' : ' dis'}"><div class="tx"><b>AnimeVost</b></div><div class="pch">${[['a', 'Лисиця'], ['b', 'Знак']].map(([k, t]) => `<button data-act="lgv" data-k="logoVost" data-v="${k}" class="${(S.view.logoVost === 'b' ? 'b' : 'a') === k ? 'on' : ''}">${t}</button>`).join('')}</div></div>
      <div class="row lgopt${S.view.logo !== false ? '' : ' dis'}"><div class="tx"><b>AniLiberty</b></div><div class="pch">${[['red', 'Червоний'], ['purple', 'Фіолетовий']].map(([k, t]) => `<button data-act="lgv" data-k="logoLib" data-v="${k}" class="${(S.view.logoLib === 'purple' ? 'purple' : 'red') === k ? 'on' : ''}">${t}</button>`).join('')}</div></div>
      <div class="row"><div class="tx"><b>Колонок у сітці</b></div><div class="pch">${[2, 3].map(n => `<button data-act="lvcols" data-v="${n}" class="${S.view.cols === n ? 'on' : ''}">${n}</button>`).join('')}</div></div>
    </div></div>
  </div>
  <div class="grp ${S.pOpen ? 'open' : ''}" id="pgrp">
    <button class="gh" data-act="ptog"><div class="tx"><b>Плеєр</b><span>Автоматична наступна серія та пропуск інтро / аутро</span></div>${ico('chev')}</button>
    <div class="fold"><div>
      <div class="row"><div class="tx"><b>Автоматично наступна серія</b><span class="d">Після завершення серії вмикає наступну.</span></div>
        <label class="sw"><input type="checkbox" data-set="auto_next" ${S.set.auto_next !== false ? 'checked' : ''}></label></div>
      <div class="row"><div class="tx"><b>Обкладинка серії на екрані блокування</b><span class="d">Увімкнено — кадр самої серії; вимкнено — обкладинка аніме. Видно в Центрі керування й на екрані блокування.</span></div>
        <label class="sw"><input type="checkbox" data-set="np_ep_cover" ${S.set.np_ep_cover !== false ? 'checked' : ''}></label></div>
      ${IOS_APP ? `<div class="row"><div class="tx"><b>Віддавати перевагу плеєру iOS</b><span class="d">Відео відкривається в стандартному плеєрі iOS: Picture-in-Picture, AirPlay, керування з екрана блокування. Вимкніть, щоб грати у вбудованому плеєрі застосунку.</span></div>
        <label class="sw"><input type="checkbox" data-set="ios_player" ${S.set.ios_player !== false ? 'checked' : ''}></label></div>` : ''}
      <div class="skg ${S.skOpen !== false && S.set.auto_skip ? 'open' : ''}" id="skg">
        <div class="skh" data-act="sktog"><div class="tx"><b>Автопропуск</b><span class="d">Сам пропускає вибрані частини серії.</span></div>
          <label class="sw"><input type="checkbox" id="skmaster" ${S.set.auto_skip ? 'checked' : ''}></label>
          <button class="arr" aria-label="Розгорнути">${ico('chev')}</button></div>
        <div class="fold"><div>
          <label class="row sub ${S.set.auto_skip ? '' : 'dis'}"><input type="checkbox" data-set="auto_skip_opening" ${S.set.auto_skip_opening ? 'checked' : ''}><div class="tx"><b>Пропускати інтро</b><span class="d">Опенінг пропускається, щойно починається.</span></div></label>
          <label class="row sub ${S.set.auto_skip ? '' : 'dis'}"><input type="checkbox" data-set="auto_skip_ending" ${S.set.auto_skip_ending ? 'checked' : ''}><div class="tx"><b>Пропускати аутро</b><span class="d">Якщо після титрів є уривок серії — перемотає до нього, інакше піде далі.</span></div></label>
          <div class="row col ${S.set.auto_skip ? '' : 'dis'}"><b>Тип перемотки</b>
            <div class="pch"><button data-act="skmode" data-m="instant" class="${S.set.skip_mode === 'soft' ? '' : 'on'}">Миттєва</button><button data-act="skmode" data-m="soft" class="${S.set.skip_mode === 'soft' ? 'on' : ''}">Лагідна</button></div>
            <span class="d">Лагідна — кнопка «Пропустити» з відліком на пару секунд, і лише потім перемотка. Натиснути її можна й раніше.</span></div>
        </div></div>
      </div>
      <div class="row col"><b>Максимальна гучність у плеєрі</b>
        <div class="pch"><button data-act="volmax" data-m="100" class="${volMax() === 100 ? 'on' : ''}">100%</button><button data-act="volmax" data-m="200" class="${volMax() === 200 ? 'on' : ''}">200%</button></div>
        <span class="d">Власний повзунок гучності в плеєрі працює окремо від кнопок iPhone. 200% підсилює звук вдвічі — корисно для тихих серій (може хрипіти).</span></div>
    </div></div>
  </div>
  <div class="grp ${S.acOpen ? 'open' : ''}" id="acgrp">
    <button class="gh" data-act="actog"><div class="tx"><b>Автоперевірка нових серій</b><span id="acsub">${acSub()}</span></div>${ico('chev')}</button>
    <div class="fold"><div>
      <div class="row"><div class="tx"><b>Перевіряти автоматично</b><span class="d">Працює, поки додаток відкритий.</span></div>
        <label class="sw"><input type="checkbox" data-set="auto_check" ${S.set.auto_check ? 'checked' : ''}></label></div>
      <div class="row col"><b>Як часто</b>
        <div class="pch">${AC_PRESETS.map(([h, t]) => `<button data-act="achours" data-h="${h}" class="${Number(S.set.auto_check_hours) === h ? 'on' : ''}">${t}</button>`).join('')}</div>
        <span class="d">Перевірка розумна: аніме, чия наступна серія ще не вийшла за таймером, чекає її часу; давно завершені — раз на тиждень (на випадок нових серій після «12/12»). Перевірки йдуть по одній, з паузами.</span></div>
      <div class="row"><div class="tx"><span class="d" id="acstat">${acStat()}</span></div></div>
    </div></div>
  </div>
  ${siteGroupHTML()}
  ${IOS_APP ? `<div class="grp ${S.bgOpen ? 'open' : ''}" id="bggrp">
    <button class="gh" data-act="grptog" data-g="bgOpen" data-id="bggrp"><div class="tx"><b>Робота у фоні</b><span>Щоб серії докачувались, а перегляд зберігався при заблокованому екрані та в PiP.</span></div>${ico('chev')}</button>
    <div class="fold"><div>
      <label class="row sub"><input type="checkbox" data-set="bg_keep" ${S.set.bg_keep !== false ? 'checked' : ''}><div class="tx"><b>Не давати iOS приспати програму</b><span class="d">Поки йдуть завантаження або відтворюється відео (після відео ще ~1,5 хв, щоб зберегти місце, де зупинились).</span></div></label>
      <label class="row sub"><input type="checkbox" data-set="bg_check" ${S.set.bg_check ? 'checked' : ''}><div class="tx"><b>Також для перевірки нових серій</b><span class="d">Програма працює постійно, поки є зв'язок із Python (помітніше розряджає батарею). Потрібна автоперевірка вище.</span></div></label>
    </div></div>
  </div>
  ${S.set.island_ok ? `<div class="grp"><div class="skg ${S.isOpen !== false && S.set.island !== false ? 'open' : ''}" id="isg">
    <div class="skh" data-act="istog" style="border-top:0"><div class="tx"><b>Dynamic Island</b><span class="d">Живий індикатор на острові й екрані блокування.</span></div>
      <label class="sw"><input type="checkbox" id="ismaster" ${S.set.island !== false ? 'checked' : ''}></label>
      <button class="arr" aria-label="Розгорнути">${ico('chev')}</button></div>
    <div class="fold"><div>
      <label class="row sub ${S.set.island !== false ? '' : 'dis'}"><input type="checkbox" data-set="island_dl" ${S.set.island_dl !== false ? 'checked' : ''}><div class="tx"><b>Завантаження</b><span class="d">Прогрес завантаження серій; у розгорнутому вигляді — пауза та скасування (скасування з підтвердженням).</span></div></label>
      <label class="row sub ${S.set.island !== false ? '' : 'dis'}"><input type="checkbox" data-set="island_check" ${S.set.island_check ? 'checked' : ''}><div class="tx"><b>Таймер перевірки</b><span class="d">Лише відлік до наступної перевірки нових серій.</span></div></label>
    </div></div>
  </div></div>` : ''}` : ''}
  <div class="set"><div class="tx"><b>Розмивати скріншоти серій</b><span>Захист від спойлерів у картковому вигляді серій.</span></div>
    <label class="sw"><input type="checkbox" data-set="blur_thumbs" ${S.set.blur_thumbs ? 'checked' : ''}></label></div>
  <div class="set"><div class="tx"><b>Вести логи</b><span>Журнали помилок і збоїв для діагностики. Вимкнені за замовчуванням — нічого не пишеться на диск.</span></div>
    <label class="sw"><input type="checkbox" data-set="logging" ${S.set.logging ? 'checked' : ''}></label></div>
  <div class="set"><div class="tx"><b>Багатозадачність</b><span>Експеримент: до 3 серій качаються одночасно, а запити сторінок, пошуку й картинок виконуються паралельно ширше. Швидше, але більше пам'яті та батареї; на iPhone iOS може закрити застосунок. Вимкнено — безпечний режим.</span></div>
    <label class="sw"><input type="checkbox" data-set="multitask" ${S.set.multitask ? 'checked' : ''}></label></div>
  <div class="set"><div class="tx"><b>Мережа зараз</b><span>Визначається автоматично: Wi‑Fi, мобільна мережа чи офлайн.</span></div>${netHTML()}</div>`;
}
const VIEW_DEF = {gtitle: true, sort: 'my', ongoing: false, timer: false, title: true, seasonOnly: false, site: true, stats: true, badge: true, cols: 2};
try { S.view = {...VIEW_DEF, ...JSON.parse(localStorage.getItem('libview') || '{}')}; } catch (e) { S.view = {...VIEW_DEF}; }
let viewT = 0;
function saveView() {
  try { localStorage.setItem('libview', JSON.stringify(S.view)); } catch (e) {}
  clearTimeout(viewT); viewT = setTimeout(() => { api('/api/settings', {view: S.view}).then(j => { if (S.set) S.set.view = j.view; }).catch(() => {}); }, 400);  // дублюємо в дані програми — переживає перенесення збереження
}
const ORDW = {первый: 1, второй: 2, третий: 3, четвертый: 4, четвёртый: 4, пятый: 5, шестой: 6, седьмой: 7};
function seasonOf(title) {
  const t = String(title || '').toLowerCase();
  let m = t.match(/(первый|второй|третий|четв[её]ртый|пятый|шестой|седьмой)\s+сезон/); if (m) return ORDW[m[1]];
  m = t.match(/(\d+)\s*-?\s*(?:й|ой|ый)?\s*сезон/) || t.match(/сезон\s*(\d+)/) || t.match(/(\d+)(?:st|nd|rd|th)\s+season/) || t.match(/season\s*(\d+)/); if (m) return Number(m[1]);
  m = t.match(/\s([2-9])(?:\s*[\[(].*)?$/); if (m) return Number(m[1]);
  return 1;
}
const arcTab = () => route().name !== 'group' && S.libTab === 'arc';
function visibleItems() {
  const rt = route(); if (rt.name === 'group') return groupMembers(rt.id);
  let list = S.items.filter(x => S.libTab === 'all' || !!x.archived === (S.libTab === 'arc'));
  if (S.reorder) return list;
  if (S.view.ongoing) list = list.filter(x => x.meta && x.meta.next && x.meta.next.ts);
  const v = S.view.sort;
  if (v === 'next') list = [...list].sort((a, b) => ((a.meta.next && a.meta.next.ts) || 1e15) - ((b.meta.next && b.meta.next.ts) || 1e15));
  else if (v === 'new') list = [...list].sort((a, b) => (b.stats.new || 0) - (a.stats.new || 0));
  else if (v === 'title') list = [...list].sort((a, b) => lname(a).localeCompare(lname(b), 'uk'));
  else if (v === 'recent') list = [...list].sort((a, b) => (b.last_watch || 0) - (a.last_watch || 0));
  return list;
}
function demoItems() {
  const now = Math.floor(Date.now() / 1000), mk = (i, t, hue, nxt, nw) => ({id: 'demo' + i, title: t, hue, dubbing: 'AniLibria', archived: false,
    site: {id: ['aniliberty', 'yummyani', 'animevost'][i % 3], name: ''}, sources: [{}, {}], episodes: [],
    meta: {poster: null, next: nxt ? {ts: now + nxt, ep: 6 + i, approx: false} : null}, stats: {watched: 3 + i, total: 12, downloaded: 2 + i, size: 612368384, new: nw}});
  return [mk(0, 'Герой-мерзавец 2 сезон', '#6a5cff,#ff5e9a', 183000, 2), mk(1, 'Рыцарь-скелет в ином мире', '#00cec9,#2d3a8c', 52000, 0), mk(2, 'Фермерская жизнь 3', '#f7971e,#a8325e', 0, 1)];
}
function lvPrevHTML() {
  const list = demoItems().slice(0, S.view.cols === 3 ? 3 : 2);
  return `<div class="lbl">Так виглядатиме картка</div><div class="grid${S.view.cols === 3 ? ' c3' : ''}">${list.map((it, i) => cardHTML(it, i, true)).join('')}</div>`;
}
function paintLvPrev() { const e = $('#lvprev'); if (e) { e.innerHTML = lvPrevHTML(); tickCd(); } }
function hdr2(h) { return h ? `<div class="hdr2">${h}</div>` : ''; }  // панель фільтрів: ховається при прокрутці вниз і з'являється при прокрутці вгору
function homeTabsHTML() {
  const na = S.items.filter(x => x.archived).length, nl = S.items.length - na;
  if (S.reorder) return '';
  let h = '';
  if (na || S.libTab !== 'lib') h += `<div class="dtabs">${[['all', 'Усе', S.items.length], ['lib', 'Бібліотека', nl], ['arc', 'Переглянуті', na]]
    .map(([k, l, n]) => `<button class="${S.libTab === k ? 'on' : ''}" data-act="libtab" data-t="${k}">${l}<small>${n}</small></button>`).join('')}</div>`;
  if (S.items.length > 1) {
    const opt = [['my', 'Мій порядок'], ['next', 'Найближча серія'], ['new', 'Нові серії'], ['recent', 'Недавній перегляд'], ['title', 'За назвою А–Я']];
    h += `<div class="sortrow"><div class="ssel"><select id="vsel" aria-label="Сортування">${opt.map(([k, l]) => `<option value="${k}"${S.view.sort === k ? ' selected' : ''}>${l}</option>`).join('')}</select></div>
      <button class="tg${S.view.ongoing ? ' on' : ''}" data-act="vongoing">Виходять зараз</button></div>`;
  }
  return h;
}
const plU = (n, one, few, many) => { const a = n % 100, b = n % 10; return a > 10 && a < 15 ? many : b === 1 ? one : (b > 1 && b < 5) ? few : many; };
const newW = n => n + ' ' + plU(n, 'нова', 'нові', 'нових');
const epsW = n => n + ' ' + plU(n, 'серія', 'серії', 'серій');
function ago(ts) {
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 90) return 'щойно'; if (d < 3600) return Math.round(d / 60) + ' хв тому'; if (d < 86400) return Math.round(d / 3600) + ' год тому';
  return Math.round(d / 86400) + ' дн тому';
}
S.notifs = {items: [], unread: 0};
async function pollNotifs() {
  try {
    const j = await api('/api/notifs'), old = S.notifs.unread;
    S.notifs = j;
    if (S.sheet && S.sheet.kind === 'notifs') renderSheet();
    else if (old !== j.unread && route().name === 'home' && !S.sheet && !S.reorder) render();
  } catch (e) { /* сервер недоступний */ }
}
setInterval(pollNotifs, 20000);
function notifsHTML() {
  const n = S.notifs, items = n.items || [];
  const row = x => {
    let t, d;
    if (x.type === 'new') {
      const e = x.eps || [];
      t = esc(x.title);
      d = (e.length === 1 ? 'Вийшла серія ' + e[0] : 'Вийшли серії ' + e.slice(0, 6).join(', ') + (e.length > 6 ? '…' : '')) + ' · ' + ago(x.ts);
    } else {
      t = 'Завантаження завершено';
      d = [x.count ? 'скачано ' + epsW(x.count) : '', x.bytes ? fmtSize(x.bytes) : '', x.secs ? 'за ' + fmtDur(x.secs) : '', x.err ? 'помилок: ' + x.err : ''].filter(Boolean).join(' · ') + ' · ' + ago(x.ts);
    }
    return `<button class="ntf${x.read ? '' : ' un'}" data-act="nopen" data-id="${x.id}" data-kid="${esc(x.kid || '')}" data-ep="${esc((x.eps || [])[0] ?? '')}">
      <i class="nic">${ico(x.type === 'new' ? 'spark' : 'download')}</i><div class="ntx"><b>${t}</b><span>${esc(d)}</span></div>${x.read ? '' : '<i class="ndot"></i>'}</button>`;
  };
  return `<div class="nh"><h3>Повідомлення</h3><button class="btn sm" data-act="nreadall" ${n.unread ? '' : 'disabled'}>${ico('check')} Прочитати все</button></div>
    ${items.some(x => x.read) ? `<div style="padding:0 0 8px"><button class="btn sm" data-act="nclear" style="width:100%">${ico('trash')} Очистити прочитані (${items.filter(x => x.read).length})</button></div>` : ''}
    ${items.length ? `<div class="nlist">${items.map(row).join('')}</div>` : '<div class="nempty">Поки нічого немає. Тут з\'являтимуться нові серії та підсумки завантажень.</div>'}`;
}
function libBarHTML() {
  const ids = [...S.libSel].filter(id => item(id)), n = ids.length;
  const toArc = ids.some(id => !item(id).archived) && !arcTab();
  return `<div class="libbar"><div class="c"><span>Вибрано: ${n}</span><button class="btn sm" data-act="libclr">Зняти вибір</button></div>
    <div class="dact"><button class="btn ok" data-act="libarc">${ico(toArc ? 'check' : 'refresh')} ${toArc ? 'У переглянуті' : 'У бібліотеку'}</button>
    <button class="btn dng" data-act="libdel">${ico('trash')} Видалити</button></div>
    ${n >= 2 || ids.some(id => item(id).group) ? `<div class="dact" style="margin-top:8px">${n >= 2 ? `<button class="btn" data-act="libgrp">${ico('plus')} Згрупувати</button>` : ''}${ids.some(id => item(id).group) ? `<button class="btn" data-act="libungrp">Розгрупувати</button>` : ''}</div>` : ''}</div>`;
}
function homeHTML() {
  const dlBadge = S.jobs.filter(j => j.status === 'running' || j.status === 'queued').length;
  const vis = visibleItems();
  const tiles = visibleTiles();
  const cards = tiles.map((t, i) => t.g ? gcardHTML(t, i) : cardHTML(t.it, i)).join('');
  if (S.reorder && !S.items.length) S.reorder = false;
  return `
  ${S.reorder ? `<div class="bar"><h1>Редагування</h1><button class="btn pri sm" data-act="reorder">${ico('check')} Готово</button></div>
  <p style="margin:0 16px 4px;color:var(--mu);font-size:13px">Торкайтесь карток, щоб вибрати кілька. Утримайте й перетягніть — змінити порядок; потримайте картку над серединою іншої — згрупувати (вибрані поїдуть разом; другим пальцем можна додати ще й гортати). Правий кут — «переглянуті», лівий — видалити з бібліотеки. Або користуйтесь стрілками.</p>` : `<div class="bar"><h1>Мої аніме</h1>${netHTML()}
    <button class="ib" data-act="notifs" data-lp="bellall" aria-label="Повідомлення">${ico('bell')}${S.notifs.unread ? `<span class="dot">${S.notifs.unread > 99 ? '99+' : S.notifs.unread}</span>` : ''}</button>
    <button class="ib" data-act="checkall" aria-label="Перевірити нові серії">${ico(S.checking ? 'spinner' : 'refresh')}</button>
    <button class="ib" data-act="jobs" aria-label="Завантаження">${ico('download')}${dlBadge ? `<span class="dot">${dlBadge}</span>` : ''}</button>
    <button class="ib pri" data-act="add" aria-label="Додати">${ico('plus')}</button>
  </div>`}
  ${hdr2(homeTabsHTML())}
  ${tiles.length ? `<div class="grid${S.view.cols === 3 ? ' c3' : ''}">${cards}</div>` : S.items.length ? `<div class="empty"><h2>${S.libTab === 'arc' ? 'Тут поки порожньо' : 'Нічого не знайдено'}</h2><p>${S.libTab === 'arc' ? 'Додавайте сюди переглянуте аніме кнопкою «Переглянуто» в його картці.' : 'Змініть фільтр «Виходять зараз».'}</p></div>` : `
  <div class="empty"><div class="big">${ico('book')}</div><h2>Бібліотека порожня</h2>
    <p>Додайте аніме за посиланням на його сторінку — і воно з'явиться тут з обкладинкою та описом.</p>
    <p><button class="btn pri" data-act="add">${ico('plus')} Додати аніме</button></p></div>`}
  ${S.reorder && S.libSel.size ? libBarHTML() : ''}
  ${!S.ffmpeg && S.items.length ? '<p class="err" style="padding:0 16px">' + ico('alert') + 'ffmpeg не знайдено — завантаження не працюватимуть.</p>' : ''}`;
}

function siteIcon(site) {
  const m = {yummyani: 'Y', aniliberty: 'AL', animru: 'A', animevost: 'V'};
  const id = (site && site.id) || 'other';
  const nm = esc((site && site.name) || '');
  if (S.view.logo !== false && LOGO_OK[id]) {
    const v = id === 'animevost' ? (S.view.logoVost === 'b' ? 'b' : 'a') : id === 'aniliberty' ? (S.view.logoLib === 'purple' ? 'purple' : 'red') : 'a';
    return `<span class="site lg ${id}" title="${nm}"><img src="/logo/${id}/${v}.svg" alt="" draggable="false"></span>`;
  }
  return `<span class="site ${id}" title="${nm}">${m[id] || esc(((site && site.name) || '?')[0].toUpperCase())}</span>`;
}
const LOGO_OK = {yummyani: 1, aniliberty: 1, animru: 1, animevost: 1};

const lname = it => it.alias || it.title;
function gstats(m) { return {watched: m.reduce((a, x) => a + x.stats.watched, 0), total: m.reduce((a, x) => a + x.stats.total, 0), downloaded: m.reduce((a, x) => a + x.stats.downloaded, 0), size: m.reduce((a, x) => a + (x.stats.size || 0), 0), new: m.reduce((a, x) => a + (x.stats.new || 0), 0)}; }
function cardHTML(it, i, demo, go, G) {
  const st = G ? gstats(G.m) : it.stats, p = it.meta.poster, V = S.view;
  const key = G ? G.m[0].id : it.id, arch = G ? G.m.every(x => x.archived) : it.archived;
  const sel = reo => reo && (G ? G.m.every(x => S.libSel.has(x.id)) : S.libSel.has(it.id));
  const ratio = st.total ? st.watched / st.total : 0;
  const reo = S.reorder && !demo;
  const nx = it.meta && it.meta.next;
  const cd = V.timer && nx && nx.ts ? `<div class="cd" data-c="1" data-ts="${nx.ts}" data-ep="${esc(nx.ep ?? '')}" data-approx="${nx.approx ? 1 : 0}" data-date="${nx.date_only ? 1 : 0}">${ico('clock')}<span></span></div>` : '';
  const dv = reo || demo || go || G, SEL = sel(reo);
  const psx = x => { const q = x.meta && x.meta.poster; return q ? `background-image:url('${esc(q)}')` : ''; };
  const rest = G ? G.m.filter(x => x !== it) : [];
  const lay = (x, c) => x ? `<i class="gl ${c}${x.archived ? ' arch' : ''}" style="${psx(x)}"></i>` : `<i class="gl ${c}"></i>`;
  const layers = G ? lay(rest[1], 'l2') + lay(rest[0], 'l1') : '';
  const ttl = G ? (gname(G.m) || lname(it)) : lname(it);
  return `<${dv ? 'div' : 'a'} class="card${G ? ' gcard' : ''}${arch ? ' arch' : ''}${ENTER ? ' enter' : ''}${reo ? ' reo' : ''}${SEL ? ' csel' : ''}${V.title && !(G && V.gtitle === false) ? '' : ' hidet'}" data-id="${key}"${G ? ` data-gid="${esc(G.g)}"` : ''} ${go ? 'data-act="gmopen"' : G ? (reo ? '' : 'data-act="gopen"') : dv ? '' : `href="#/a/${it.id}"`} style="--i:${Math.min(i || 0, 12)}">
    ${G ? `<div class="gst">${layers}` : ''}<div class="poster""${reo ? ' data-act="csel"' : ''}${demo ? ` style="background-image:linear-gradient(160deg,${it.hue})"` : p ? ` style="background-image:url('${esc(p)}'),linear-gradient(160deg,#2b2f4d,#171926)"` : ''}>
      ${arch && !reo ? `<i class="seen">${ico('check')}</i>` : ''}
      ${reo ? `<span class="cks">${SEL ? ico('check') : ''}</span>` : ''}
      ${p ? '' : `<span>${esc((it.title || '?')[0])}</span>`}
      ${V.site ? siteIcon(it.site) + ((it.sources || []).length > 1 ? `<span class="srcn">+${it.sources.length - 1}</span>` : '') : ''}
      ${V.badge && st.new ? `<b class="badge">${ico('spark')}${newW(st.new)}</b>` : ''}
      <div class="grad"></div>
      <div class="ct">${cd}${V.seasonOnly ? `<div class="sn">Сезон ${seasonOf(it.title)}</div>` : ''}<div class="t">${esc(ttl)}</div>
        ${V.stats ? `<div class="s">${st.watched}/${st.total} · ${ico('download')} ${st.downloaded}<span class="sx">${st.size ? ' · ' + fmtSize(st.size) : ''}${G ? ' · ' + G.m.length + ' аніме' : it.dubbing ? ' · ' + esc(it.dubbing) : ''}</span></div>` : ''}</div>
      <i class="pb"><i style="width:${(ratio * 100).toFixed(0)}%"></i></i>
    </div>${G ? '</div>' : ''}
    ${reo ? `<div class="arws"><button class="arw" data-act="mv" data-id="${key}" data-d="top" aria-label="На початок">${ico('prev')}</button>
      <button class="arw" data-act="mv" data-id="${key}" data-d="-1" aria-label="Раніше"><span class="rot l">${ico('chev')}</span></button>
      <button class="arw" data-act="mv" data-id="${key}" data-d="1" aria-label="Пізніше"><span class="rot r">${ico('chev')}</span></button></div>` : ''}
  </${dv ? 'div' : 'a'}>`;
}
/* ---------- групи ---------- */
function groupMembers(gid) { return gid ? S.items.filter(x => x.group === gid) : []; }
function visibleTiles() {  // елементи сітки: окремі картки або групи (група займає місце першого свого учасника)
  const vis = visibleItems();
  if (route().name === 'group') return vis.map(x => ({it: x}));
  const out = [], seen = new Set();
  for (const x of vis) {
    const m = x.group ? groupMembers(x.group) : [];
    if (m.length < 1) { out.push({it: x}); continue; }
    if (seen.has(x.group)) continue;
    seen.add(x.group);
    if (S.libTab === 'arc' && m.some(y => !y.archived)) continue;  // група з непереглянутими живе в бібліотеці
    out.push({g: x.group, m});
  }
  return out;
}
function gname(m) { return (m.find(x => x.gname) || {}).gname || ''; }
function gcardHTML(t, i) { return cardHTML(t.m.find(x => !x.archived) || t.m[0], i, false, false, t); }
function tileIds(c) { return c.dataset.gid ? groupMembers(c.dataset.gid).map(x => x.id) : [c.dataset.id]; }
function tileSel(c) { const ids = tileIds(c); return ids.length > 0 && ids.every(id => S.libSel.has(id)); }
function groupHTML(gid) {
  const m = groupMembers(gid);
  if (m.length < 1) { setTimeout(() => { if (route().name === 'group') location.hash = '#/'; }, 50); return `<div class="empty"><h2>Групу розгруповано</h2></div>`; }
  const cards = m.map((it, i) => cardHTML(it, i)).join('');
  const nm = S.view.gtitle === false ? '' : gname(m);
  return `${S.reorder ? `<div class="bar"><h1>Редагування</h1><button class="btn pri sm" data-act="reorder">${ico('check')} Готово</button></div>
    <p style="margin:12px 16px 4px;color:var(--mu);font-size:13px">Торкайтесь карток, щоб вибрати. Утримайте й перетягніть — змінити порядок. «Розгрупувати» виводить вибрані з групи.</p>
    <div class="grow"><button class="btn sm" data-act="grename" data-g="${esc(gid)}">${ico('edit')} Назва групи${nm ? ': ' + esc(nm) : ''}</button></div>`
    : `<div class="bar"><button class="back" data-act="gback">‹ Бібліотека</button></div>
    ${nm ? `<h1 class="gtl">${esc(nm)}</h1>` : ''}
    <div class="grow"><button class="btn sm" data-act="reorder">${ico('edit')} Редагувати</button><button class="btn sm dng" data-act="gungroup" data-g="${esc(gid)}">Розгрупувати</button></div>`}
  <div class="grid${S.view.cols === 3 ? ' c3' : ''}">${cards}</div>
  ${S.reorder && S.libSel.size ? libBarHTML() : ''}`;
}
function groupSheetHTML(gid) {
  const m = groupMembers(gid);
  if (m.length < 2) return `<h3>Група</h3><p style="color:var(--mu)">Групу розформовано.</p>`;
  return `<div class="gh2"><h3>${esc(gname(m) || 'Група')}</h3><span>${m.length} аніме · ${m.filter(x => x.archived).length} переглянуто</span></div>
    <div class="grid gsh${S.view.cols === 3 ? ' c3' : ''}">${m.map((it, i) => cardHTML(it, i, false, true)).join('')}</div>
    <div class="row2" style="margin-top:14px"><button class="btn" data-act="grename" data-g="${esc(gid)}" style="flex:1">${ico('edit')} Перейменувати</button>
      <button class="btn dng" data-act="gungroup" data-g="${esc(gid)}" style="flex:1">Розгрупувати</button></div>`;
}

function nextEp(it) {
  return it.episodes.find(e => !e.watched) || null;
}

function detailHTML(id) {
  const it = item(id);
  if (!it) return `<div class="bar"><button class="back" data-act="back">‹ Назад</button><h1></h1></div><div class="empty"><h2>Не знайдено</h2></div>`;
  const m = it.meta || {}, p = m.poster, st = it.stats, tmp = !!it.temp;
  const nx = nextEp(it);
  const desc = (m.description || '').trim();
  const chips = `<span class="chip sitechip">${siteIcon(it.site)}${esc((it.site || {}).name || '')}</span>` + [m.year, ...(m.genres || []).slice(0, 5)].filter(Boolean)
    .map(c => `<span class="chip">${esc(c)}</span>`).join('');
  const sel = S.sel.size, sm = S.selMode;
  const dlN = S.jobs.filter(j => j.status === 'running' || j.status === 'queued').length;
  const cardsMode = S.epView === 'cards';
  const eps = it.episodes.map((e, i) => cardsMode ? epCardHTML(it, e, i) : epHTML(it, e, i)).join('');
  const rc = relCounts(it), hasRel = rc.ord + rc.sim > 0;
  const canCm = !!(it.site && ['yummyani', 'aniliberty', 'animru', 'animevost'].includes(it.site.id));
  if (S.dtab === 'cm' ? !canCm : (!(hasRel || canCm) || (S.dtab === 'sim' && !rc.sim) || (S.dtab === 'ord' && !rc.ord))) S.dtab = 'eps';
  const onEps = S.dtab === 'eps';
  const tabsHTML = (hasRel || canCm) ? `<div class="dtabs"><button class="${onEps ? 'on' : ''}" data-act="dtab" data-t="eps">Серії<small>${it.episodes.length}</small></button>
    ${rc.ord ? `<button class="${S.dtab === 'ord' ? 'on' : ''}" data-act="dtab" data-t="ord">Пов'язане<small>${rc.ord}</small></button>` : ''}
    ${rc.sim ? `<button class="${S.dtab === 'sim' ? 'on' : ''}" data-act="dtab" data-t="sim">Схожі<small>${rc.sim}</small></button>` : ''}
    ${canCm ? `<button class="${S.dtab === 'cm' ? 'on' : ''}" data-act="dtab" data-t="cm">Коментарі</button>` : ''}</div>` : '';
  const checking = S.busy.has('check:' + id);
  const srcs = it.sources || [];
  return `
  <div class="bar"><button class="back" data-act="${tmp ? 'back' : 'home'}">‹ ${tmp ? 'Назад' : 'Бібліотека'}</button><h1></h1>${netHTML()}
    <button class="ib" data-act="jobs" aria-label="Завантаження">${ico('download')}${dlN ? `<span class="dot">${dlN}</span>` : ''}</button></div>
  <div class="hero">
    <div class="bg"${p ? ` style="background-image:url('${esc(p)}')"` : ''}></div>
    <div class="row">
      <div class="pic"${p ? ` data-act="poster" data-src="${esc(p)}" role="button" aria-label="Відкрити обкладинку на весь екран" style="background-image:url('${esc(p)}')"` : ''}></div>
      <div style="min-width:0">
        <h2>${esc(it.title)}</h2>
        ${m.original ? `<div class="orig">${esc(m.original)}</div>` : ''}
        <div class="meta">Переглянуто ${st.watched} з ${st.total} · скачано ${st.downloaded}${(it.dlq && it.dlq.length) ? ' · ' + esc(it.dlq.join(', ')) : ''}${st.size ? ' · ' + fmtSize(st.size) : ''}</div>
        ${countdownHTML(m.next)}
        <div class="chips">${chips}</div>
      </div>
    </div>
  </div>
  ${tmp && it.preview_of && item(it.preview_of) ? `<div class="tmpbar">${ico('eye')}<span>Перегляд на сайті ${esc((it.site || {}).name || '')}. У бібліотеці це аніме лишається на ${esc((item(it.preview_of).site || {}).name || 'іншому сайті')} — запис не змінено.</span></div>` : tmp ? `<div class="tmpbar">${ico('eye')}<span>Перегляд без додавання в бібліотеку. Запис зникне сам, якщо його не додати (а завантаження додає аніме автоматично).</span></div>` : ''}
  <div class="srcs${S.busy.has('sw') ? ' dis' : ''}">${srcs.length > 1 ? srcs.map(x => `<button class="dchip${x.active ? ' on' : ''}" data-act="setsite" data-url="${esc(x.url)}">${siteIcon(x.site)}${esc(x.site.name)}</button>`).join('') : ''}
    <button class="dchip" data-act="findalts" ${S.busy.has('alts') ? 'disabled' : ''} aria-label="Знайти на інших сайтах">${ico(S.busy.has('alts') ? 'spinner' : 'search')}${srcs.length > 1 ? '' : ' Знайти на інших сайтах'}</button></div>
  ${(it.dubs || []).length > 1 ? `<div class="dubs${S.busy.has('sw') ? ' dis' : ''}">${it.dubs.map(d => `<button class="dchip${d.active ? ' on' : ''}" data-act="setdub" data-dub="${esc(d.name)}">${esc(d.name)}${d.count ? `<em class="dcnt">${d.count} сер.</em>` : ''}${d.downloaded ? `<small>${ico('download')}${d.downloaded}</small>` : ''}</button>`).join('')}</div>` : ''}
  <div class="acts">
    <button class="btn pri" data-act="playnext" ${nx || it.episodes.length ? '' : 'disabled'}>${ico('play')} ${nx ? (nx.pos > 5 ? 'Продовжити · серія ' + esc(nx.n) : 'Дивитись · серія ' + esc(nx.n)) : 'Дивитись спочатку'}</button>
    ${tmp && it.preview_of && item(it.preview_of) ? `<button class="btn ok" data-act="openlib" data-id="${esc(it.preview_of)}">${ico('book')} Відкрити в бібліотеці</button>` : tmp ? `<button class="btn ok" data-act="keep" data-lp="gpick" data-cx="tmp" data-m="lib" ${S.busy.has('keep') ? 'disabled' : ''}>${ico('plus')} В бібліотеку</button><button class="btn" data-act="keepseen" data-lp="gpick" data-cx="tmp" data-m="seen" ${S.busy.has('keep') ? 'disabled' : ''}>${ico('check')} Переглянуте</button>`
          : `<button class="btn" data-act="check" ${checking ? 'disabled' : ''}>${ico(checking ? 'spinner' : 'refresh')} Нові</button>`}
    <button class="btn" data-act="site" data-id="${it.id}" aria-label="Відкрити сайт (подвійний тап — скопіювати посилання)">${ico('globe')}</button>
  </div>
  ${desc ? `<div class="desc ${S.descOpen ? '' : 'cl'}">${esc(desc)}</div>${desc.length > 220 ? `<button class="more" data-act="more">${S.descOpen ? 'Згорнути' : 'Читати далі'}</button>` : ''}` : ''}
  ${tabsHTML}
  ${onEps ? `<div class="tools">
    ${sm ? `<button class="btn sm on" data-act="selmode">${ico('check')} Готово</button>
      <button class="btn sm" data-act="selall">Вибрати всі</button>
      <button class="btn sm" data-act="selmissing" style="grid-column:1/-1">${ico('cloud')} Лише нескачані</button>`
         : `<button class="btn sm" data-act="selmode">${ico('check')} Вибрати</button>
      <button class="btn sm" data-act="upto">${ico('eye')} Подивився до…</button>`}
    <div class="row2x">
    <div class="seg" role="group" aria-label="Вигляд серій">
      <button class="${cardsMode ? '' : 'on'}" data-act="epview" data-v="list" aria-label="Список">${ico('vlist')}</button>
      <button class="${cardsMode ? 'on' : ''}" data-act="epview" data-v="cards" aria-label="Картки">${ico('vgrid')}</button></div>
    <select class="q" id="qsel" aria-label="Якість">${qSelHTML(it)}</select>
    </div>
  </div>
  <h3 class="sec">Серії</h3>
  ${sm ? `<p class="hint"${sel ? ' style="display:none"' : ''}>Торкайтесь серій, щоб вибрати кілька. Серію можна також утримати пальцем.</p>` : ''}
  ${eps ? `<div class="${cardsMode ? 'epgrid' : 'eps'}">${eps}</div>` : '<div class="eps"><div style="padding:18px;color:var(--mu)">Серій поки немає.</div></div>'}` : S.dtab === 'cm' ? commentsHTML(it) : relHTML(it)}
  ${tmp ? '' : `<div class="foot"><button class="btn sm" data-act="archive" style="margin-right:8px">${ico('check')} ${it.archived ? 'Повернути з переглянутих' : 'Переглянуто — в архів'}</button><button class="btn dng sm" data-act="remove">${ico('trash')} Видалити з бібліотеки</button></div>
  <p style="text-align:center;color:var(--mu);font-size:12px;margin-top:14px">Остання перевірка: ${esc(it.checked || '—')}</p>`}
  ${sel && onEps ? `<div class="dlbar">${dlbarInner(it)}</div><div class="dlsp" style="height:150px"></div>` : ''}`;
}

const CM = {};
function cmAge(c) { return Date.now() - (c.t || 0); }
async function cmLoad(id, append, parent) {
  const it = item(id); if (!it) return;
  let c = CM[id];
  if (!parent) {
    if (c && c.loading) return;
    if (!c || (!append && c.key !== it.url)) c = CM[id] = {items: [], page: 0, more: false, rep: {}};
    const page = append ? c.page + 1 : 0;
    c.loading = true; c.err = ''; c.key = it.url; render();
    try {
      const r = await api(`/api/comments?id=${encodeURIComponent(id)}&page=${page}`);
      if (append) c.items = c.items.concat(r.items || []); else { c.items = r.items || []; c.rep = {}; }
      c.page = page; c.more = !!r.more; c.t = Date.now();
    } catch (e) { c.err = e.message; c.t = Date.now(); }
    c.loading = false; render();
  } else {
    if (!c) return;
    const rp = c.rep[parent] || (c.rep[parent] = {items: [], page: 0, more: false, open: true});
    if (rp.loading) return;
    const page = append ? rp.page + 1 : 0;
    rp.loading = true; rp.err = ''; rp.open = true; render();
    try {
      const r = await api(`/api/comments?id=${encodeURIComponent(id)}&page=${page}&parent=${encodeURIComponent(parent)}`);
      rp.items = append ? rp.items.concat(r.items || []) : (r.items || []); rp.page = page; rp.more = !!r.more;
    } catch (e) { rp.err = e.message; }
    rp.loading = false; render();
  }
}
function cmEnsure(id) {
  const it = item(id), c = CM[id];
  if (!it || S.dtab !== 'cm' || (c && c.loading)) return;
  if (c && c.key === it.url && (c.err || cmAge(c) < 300000)) return;
  cmLoad(id, false);
}
function cmItemHTML(x, id, rp) {
  const ini = esc((x.author || '?').trim().charAt(0).toUpperCase());
  const av = x.avatar ? ` style="background-image:url('/img?u=${encodeURIComponent(x.avatar)}')"` : '';
  const lk = typeof x.likes === 'number' ? `<span class="cml${x.likes > 0 ? ' pos' : x.likes < 0 ? ' neg' : ''}">${x.likes > 0 ? '+' : ''}${x.likes}</span>` : '';
  let rep = '';
  if (!rp && x.children) {
    const r = ((CM[id] || {}).rep || {})[x.id];
    if (r && r.open) {
      rep = r.items.map(y => cmItemHTML(y, id, true)).join('') + (r.loading ? '<div class="cmst">Завантажую…</div>' : '')
        + (r.err ? `<div class="cmst">${esc(r.err)}</div>` : '')
        + (r.more && !r.loading ? `<button class="cmr" data-act="cmrep" data-cid="${esc(x.id)}" data-more="1">Ще відповіді</button>` : '')
        + `<button class="cmr" data-act="cmrepx" data-cid="${esc(x.id)}">Сховати відповіді</button>`;
    } else rep = `<button class="cmr" data-act="cmrep" data-cid="${esc(x.id)}">Відповіді (${x.children})</button>`;
  }
  return `<div class="cm${rp ? ' rp' : ''}"><div class="cmh"><span class="cav"${av}>${ini}</span><div><b>${esc(x.author)}</b>${x.date ? `<small>${esc(x.date)}</small>` : ''}</div>${lk}</div>
    <div class="cmt">${esc(x.text)}</div>${rep}</div>`;
}
function commentsHTML(it) {
  const c = CM[it.id], sn = (it.site || {}).name || '';
  setTimeout(() => cmEnsure(it.id), 0);
  let body = '';
  if (c && c.key === it.url && c.items.length) body = `<div class="cms">${c.items.map(x => cmItemHTML(x, it.id, false)).join('')}</div>`;
  let st = '';
  if (!c || c.key !== it.url || c.loading) st = `<div class="cmst">${ico('spinner')} Завантажую коментарі…</div>`;
  else if (c.err) st = `<div class="cmst">${esc(c.err)}<br><button class="btn sm" style="margin-top:10px" data-act="cmreload">${ico('refresh')} Повторити</button></div>`;
  else if (!c.items.length) st = `<div class="cmst">Коментарів поки немає.</div>`;
  const moreB = c && c.key === it.url && c.more && !c.loading && !c.err ? `<button class="btn sm cmmore" data-act="cmmore">Завантажити ще</button>` : '';
  return `<h3 class="sec">Коментарі з ${esc(sn)}</h3>${body}${st}${moreB}`;
}
function openPoster(src) {
  if (!src || document.querySelector('.pv')) return;
  const v = document.createElement('div'); v.className = 'pv';
  v.innerHTML = `<img alt="" draggable="false"><button class="x" aria-label="Закрити">×</button>`;
  const im = v.querySelector('img'); im.src = src;
  let sc = 1, tx = 0, ty = 0, moved = false, lastTap = 0;
  const pts = new Map(); let d0 = 0, s0 = 1;
  const apply = () => { im.style.transform = `translate(${tx}px,${ty}px) scale(${sc})`; };
  const close = () => { v.remove(); document.removeEventListener('keydown', key); };
  const key = e => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', key);
  const fit = () => { const r = im.getBoundingClientRect(); return r; };
  v.querySelector('.x').addEventListener('click', e => { e.stopPropagation(); close(); });
  v.addEventListener('pointerdown', e => { if (e.target.closest('.x')) return; v.setPointerCapture(e.pointerId); pts.set(e.pointerId, [e.clientX, e.clientY]); moved = false; if (pts.size === 2) { const [a, b] = [...pts.values()]; d0 = Math.hypot(a[0] - b[0], a[1] - b[1]); s0 = sc; } });
  v.addEventListener('pointermove', e => {
    if (!pts.has(e.pointerId)) return;
    const o = pts.get(e.pointerId);
    if (pts.size === 2) {
      pts.set(e.pointerId, [e.clientX, e.clientY]);
      const [a, b] = [...pts.values()]; const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      if (d0) { sc = Math.min(6, Math.max(1, s0 * d / d0)); moved = true; if (sc === 1) { tx = ty = 0; } apply(); }
    } else if (sc > 1) {
      tx += e.clientX - o[0]; ty += e.clientY - o[1]; pts.set(e.pointerId, [e.clientX, e.clientY]); moved = true; apply();
    } else if (Math.hypot(e.clientX - o[0], e.clientY - o[1]) > 8) moved = true;
  });
  const up = e => {
    if (!pts.has(e.pointerId)) return;
    pts.delete(e.pointerId);
    if (pts.size === 0 && !moved && !e.target.closest('.x')) {
      const now = Date.now();
      if (now - lastTap < 300) { sc = sc > 1 ? 1 : 2.5; tx = ty = 0; apply(); lastTap = 0; }
      else { lastTap = now; setTimeout(() => { if (lastTap === now && sc === 1) close(); }, 310); }
    }
  };
  v.addEventListener('pointerup', up); v.addEventListener('pointercancel', e => pts.delete(e.pointerId));
  document.body.appendChild(v);
}
function relCounts(it) {
  const rel = (it.meta && it.meta.related) || [];
  return {ord: rel.filter(x => x.kind === 'order').length, sim: rel.filter(x => x.kind !== 'order').length};
}
function relHTML(it) {
  const rel = (it.meta && it.meta.related) || [];
  if (!rel.length) return '';
  const card = (x, num) => {
    const o = x.rp ? x.url : (() => { try { return decodeURIComponent((x.poster.split('u=')[1] || '')); } catch (e) { return ''; } })();
    return `<button class="rc${x.cur ? ' cur' : ''}" data-act="openrel" data-url="${esc(x.url)}" data-lib="${esc(x.lib || '')}" data-t="${esc(x.title)}"><div class="rp">
      ${num ? `<span class="rn">${num}</span>` : ''}<img decoding="async" loading="lazy" alt="" src="${esc(x.poster)}" data-o="${esc(o)}"${x.rp ? ' data-rp="1"' : ''} onload="this.classList.add('ld')" onerror="imgErr(this)">
      ${x.cur ? '<span class="rb">Зараз</span>' : x.lib ? '<span class="rb">У бібліотеці</span>' : ''}</div>
      <div class="rt">${esc(x.title)}</div>${x.note ? `<div class="rm">${esc(x.note)}</div>` : ''}</button>`;
  };
  const list = rel.filter(x => (S.dtab === 'sim') === (x.kind !== 'order'));
  return `<div class="rgrid">${list.map((x, i) => card(x, S.dtab === 'sim' ? 0 : i + 1)).join('')}</div>`;
}

function dlbarInner(it) {
  const sel = S.sel.size;
  const nDl = it.episodes.filter(e => S.sel.has(String(e.n)) && e.downloaded).length, nUp = sel - nDl;
  const selW = it.episodes.filter(e => S.sel.has(String(e.n)));
  const allW = selW.length && selW.every(e => e.watched);
  return `<div class="c"><span>Вибрано: ${sel}</span><button class="btn sm" data-act="selnone">Зняти вибір</button></div>
     <div class="dact">
     ${nUp ? `<button class="btn ok" data-act="download" ${S.ffmpeg ? '' : 'disabled'}>${ico('download')} Скачати · ${nUp}</button>` : ''}
     ${nDl ? `<button class="btn dng" data-act="delsel">${ico('trash')} Видалити · ${nDl}</button>` : ''}
     <button class="btn" data-act="watchsel" aria-label="Переглянуто">${ico('eye')} ${allW ? 'Не дивився' : 'Переглянуто'}</button></div>`;
}
function applySelMode() {  // вхід/вихід з режиму вибору без повного перемальовування: нічого не блимає
  if (S.dtab !== 'eps') { render(); return; }
  const r = route(), it = r.name === 'detail' ? item(r.id) : null;
  if (!it) { render(); return; }
  const cards = S.epView === 'cards', sm = S.selMode, sel = S.sel.size;
  const saved = ENTER; ENTER = false;
  it.episodes.forEach((e, i) => {
    const row = document.querySelector(`#root ${cards ? '.ec' : '.ep'}[data-n="${CSS.escape(String(e.n))}"]`);
    if (!row) return;
    const t = document.createElement('div'); t.innerHTML = cards ? epCardHTML(it, e, i) : epHTML(it, e, i);
    const nr = t.firstElementChild; nr.classList.remove('enter'); nr.style.animation = 'none';
    const oi = row.querySelector('img'), ni = nr.querySelector('img');
    if (oi && ni) ni.replaceWith(oi);
    row.replaceWith(nr);
  });
  ENTER = saved;
  const tools = $('#root .tools');
  if (tools) {
    const first = [...tools.children].slice(0, sm ? 3 : 2);
    const tmp = document.createElement('div');
    tmp.innerHTML = sm ? `<button class="btn sm on" data-act="selmode">${ico('check')} Готово</button>
      <button class="btn sm" data-act="selall">Вибрати всі</button>
      <button class="btn sm" data-act="selmissing" style="grid-column:1/-1">${ico('cloud')} Лише нескачані</button>`
      : `<button class="btn sm" data-act="selmode">${ico('check')} Вибрати</button>
      <button class="btn sm" data-act="upto">${ico('eye')} Подивився до…</button>`;
    const old = [...tools.children].filter(c => c.matches('button[data-act=selmode],button[data-act=selall],button[data-act=selmissing],button[data-act=upto]'));
    const anchor = old[0];
    [...tmp.children].forEach(n => tools.insertBefore(n, anchor));
    old.forEach(n => n.remove());
  }
  let hint = $('#root .hint');
  if (sm && !hint) { const sec = $('#root h3.sec'); if (sec) sec.insertAdjacentHTML('afterend', `<p class="hint"${sel ? ' style="display:none"' : ''}>Торкайтесь серій, щоб вибрати кілька. Серію можна також утримати пальцем.</p>`); }
  else if (!sm && hint) hint.remove();
  paintSel();
}
function paintSel() {  // зміна вибору без перемальовування сторінки: нічого не блимає, картинки не перезавантажуються
  const r = route(), it = r.name === 'detail' ? item(r.id) : null;
  if (!it) { render(); return; }
  const sel = S.sel.size;
  document.querySelectorAll('#root .ep[data-n], #root .ec[data-n]').forEach(row => {
    const on = S.sel.has(row.dataset.n); row.classList.toggle('sel', on);
    const ck = row.querySelector('.ck'); if (ck) { const h = on ? ico('check') : ''; if (ck.innerHTML !== h) ck.innerHTML = h; }
  });
  const hint = $('#root .hint'); if (hint) hint.style.display = sel ? 'none' : '';
  const bar = $('#root .dlbar'), sp = $('#root .dlsp');
  if (sel) {
    const h = dlbarInner(it);
    if (bar) { if (bar._h !== h) { bar.innerHTML = h; bar._h = h; } }
    else { $('#root').insertAdjacentHTML('beforeend', `<div class="dlbar">${h}</div><div class="dlsp" style="height:150px"></div>`); const nb = $('#root .dlbar'); if (nb) nb._h = h; }
  } else { if (bar) bar.remove(); if (sp) sp.remove(); }
}
S.thOk = new Set();
function thOk(img) { img.classList.add('ld'); if (img.dataset.k) S.thOk.add(img.dataset.k); }  // запам'ятовуємо: при перемальовуванні картинка одразу видима, без блимання

function epCardHTML(it, e, i) {
  const sel = S.sel.has(String(e.n)), sm = S.selMode;
  const tag = e.downloaded
    ? `<span class="tag dl">${ico('download')}${esc([e.q, e.size ? fmtSize(e.size) : ''].filter(Boolean).join(' · ') || 'скачано')}</span>`
    : `<span class="tag">${ico('cloud')}онлайн${e.qs && e.qs.length ? ' · до ' + esc(e.qs[0]) : ''}</span>`;
  const tk = it.id + '|' + e.n + '|' + (e.downloaded ? 1 : 0);
  const img = e.th ? `<img loading="lazy" class="${S.set.blur_thumbs ? 'bl' : ''}${S.thOk.has(tk) ? ' ld' : ''}" data-k="${esc(tk)}" alt="" src="/thumb/${it.id}/${encodeURIComponent(e.n)}?v=${e.downloaded ? 1 : 0}" onload="thOk(this)" onerror="this.remove()">` : '';
  return `<div class="ec${ENTER ? ' enter' : ''}${sel ? ' sel' : ''}${e.watched ? ' w' : ''}" data-n="${esc(e.n)}" style="--i:${Math.min(i || 0, 12)}">
    <div class="th" data-act="${sm ? 'sel' : 'play'}">${img}
      <span class="num">${esc(e.n)}</span>
      ${sm ? `<span class="ck">${sel ? ico('check') : ''}</span>` : ''}
      ${e.new ? `<span class="stt" style="right:auto;left:${sm ? 42 : 8}px;background:#0e9f84;color:#fff;font-weight:700">${ico('spark')}нова</span>` : ''}
      ${e.pos > 5 && !e.watched ? `<span class="rs">${ico('clock')}${fmtT(e.pos)}</span>` : ''}
      <span class="plc"><i>${ico(sm ? 'check' : 'play')}</i></span>
    </div>
    <div class="inf"><div class="mid" ${sm ? 'data-act="sel"' : ''}><div class="n">Серія ${esc(e.n)}</div>${e.title ? `<div class="tt">${esc(e.title)}</div>` : ''}<div class="tags">${tag}</div></div>
      ${sm ? '' : `<div class="bt"><button class="eye${e.watched ? ' on' : ''}" data-act="watch" aria-label="Переглянуто">${ico('eye')}</button></div>`}</div>
  </div>`;
}

function epHTML(it, e, i) {
  const sel = S.sel.has(String(e.n)), sm = S.selMode;
  const tags = [
    e.downloaded ? '<span class="tag dl">' + ico('download') + 'скачано' + (e.q ? ' · ' + esc(e.q) : '') + (e.size ? ' · ' + fmtSize(e.size) : '') + '</span>'
      : '<span class="tag">' + ico('cloud') + 'онлайн' + (e.qs && e.qs.length ? ' · до ' + esc(e.qs[0]) : '') + '</span>',
    e.new ? '<span class="tag nw">' + ico('spark') + 'нова</span>' : '',
    e.pos > 5 && !e.watched ? `<span class="tag rs">${ico('clock')}${fmtT(e.pos)}</span>` : '',
  ].join('');
  return `<div class="ep${ENTER ? ' enter' : ''}${sel ? ' sel' : ''}${e.watched ? ' w' : ''}" data-n="${esc(e.n)}" style="--i:${Math.min(i || 0, 14)}">
    ${sm ? `<button class="ck" data-act="sel" aria-label="Вибрати">${sel ? ico('check') : ''}</button>` : ''}
    <div class="mid" data-act="${sm ? 'sel' : 'play'}"><div class="n">Серія ${esc(e.n)}</div>
      ${e.title ? `<div class="tt">${esc(e.title)}</div>` : ''}<div class="tags">${tags}</div></div>
    ${sm ? '' : `<button class="eye${e.watched ? ' on' : ''}" data-act="watch" aria-label="Переглянуто">${ico('eye')}</button>
    <button class="pl" data-act="play" aria-label="Грати">${ico('play')}</button>`}
  </div>`;
}

/* ---------- шторки та діалоги ---------- */
function openSheet(kind, extra) { S.sheet = {kind, ...extra}; renderSheet(); }
function cancelFind(silent) {
  const F = S.find; if (!F) return;
  F.off = true; S.find = null;
  if (F.task) api('/api/lookup_cancel', {task: F.task}).catch(() => {});
  if (!silent) { S.addErr = ''; renderSheet(); toast('Пошук скасовано', false, 'close'); }
}
function closeSheet() {
  if (S.find) cancelFind(true);
  if (S.opening) { const o = S.opening; S.opening = null; o.off = true; if (o.task) api('/api/lookup_cancel', {task: o.task}).catch(() => {}); }
  S.sheet = null;
  if (!S.cur) document.body.style.overflow = '';
  const bd = $('#layer .back-d'); if (!bd) return;
  if (S.skipOut) { S.skipOut = false; $('#layer').innerHTML = ''; return; }
  bd.classList.add('out');
  setTimeout(() => { if (!S.sheet) $('#layer').innerHTML = ''; }, 190);
}
function placePop(r) {  // меню збоку від картки: ліва картка — праворуч, права — ліворуч
  const sh = $('#layer .sheet'); if (!sh) return;
  const vw = innerWidth, vh = innerHeight, w = Math.min(208, vw - 16), h = sh.offsetHeight || 170;
  let x, ox;
  if (!r) { x = (vw - w) / 2; ox = 'center'; }
  else if (r.left + r.width / 2 < vw / 2) { x = r.right + 8; ox = 'left'; }
  else { x = r.left - 8 - w; ox = 'right'; }
  x = Math.max(8, Math.min(vw - w - 8, x));
  let y = r ? r.top + 6 : (vh - h) / 2;
  y = Math.max(10, Math.min(vh - h - 10, y));
  sh.style.width = w + 'px'; sh.style.left = x + 'px'; sh.style.top = y + 'px'; sh.style.setProperty('--po', ox + ' top');
}
function renderSheet() {
  const s = S.sheet;
  if (!s) return;
  let inner = '';
  if (s.kind === 'add') inner = addHTML();
  else if (s.kind === 'jobs') inner = jobsHTML();
  else if (s.kind === 'notifs') inner = notifsHTML();
  else if (s.kind === 'cmenu' && s.res) inner = `<h3>${esc(s.res.title)}</h3><div class="cm">
     <button data-act="rsshare">${ico('share')} Поділитись</button>
     <button data-act="rslib" data-lp="gpick" data-m="lib">${ico('plus')} Додати в Бібліотеку</button>
     <button data-act="rsseen" data-lp="gpick" data-m="seen">${ico('check')} Додати в Переглянуте</button></div>
     <p class="hint" style="margin:8px 4px 0;font-size:12px">Затисніть «Бібліотеку» чи «Переглянуте», щоб додати до наявної групи.</p>`;
  else if (s.kind === 'gpick') inner = gpickHTML();
  else if (s.kind === 'cmenu' && s.gid) { const m = groupMembers(s.gid), nm = S.view.gtitle === false ? '' : gname(m); inner = `<h3>${esc(nm || 'Група')}</h3><div class="cm">
     <button data-act="gmshare" data-g="${esc(s.gid)}">${ico('share')} Поділитись</button>
     <button data-act="gmrename" data-g="${esc(s.gid)}">${ico('edit')} Перейменувати</button>
     <button data-act="gmungroup" data-g="${esc(s.gid)}">${ico('refresh')} Розгрупувати</button>
     <button class="dng" data-act="gmdel" data-g="${esc(s.gid)}">${ico('trash')} Видалити</button></div>`; }
  else if (s.kind === 'cmenu') { const it = item(s.id) || {}; inner = `<h3>${esc(lname(it))}</h3><div class="cm">
     <button data-act="cmshare" data-id="${s.id}">${ico('share')} Поділитись</button>
     <button data-act="cmrename" data-id="${s.id}">${ico('edit')} Перейменувати</button>
     <button data-act="cmarc" data-lp="gpick" data-id="${s.id}">${ico(it.archived ? 'refresh' : 'check')} ${it.archived ? 'У бібліотеку' : 'У переглянуте'}</button>
     ${it.group ? '' : `<button data-act="cmgroup" data-id="${s.id}">${ico('plus')} Створити групу</button>`}
     <button class="dng" data-act="cmdel" data-id="${s.id}">${ico('trash')} Видалити</button></div>`; }
  else if (s.kind === 'group') inner = groupSheetHTML(s.gid);
  else if (s.kind === 'text') inner = `<h3>${esc(s.title)}</h3><p style="color:var(--mu);font-size:13px">${esc(s.text || '')}</p>
     <input class="in" id="txtin" type="text" maxlength="120" autocomplete="off" value="${esc(s.value ?? '')}">
     <div class="row2"><button class="btn" data-act="dlg-no" style="flex:1">Скасувати</button>
     <button class="btn pri" data-act="dlg-yes" style="flex:1">Готово</button></div>`;
  else if (s.kind === 'opening') inner = `<h3>${esc(s.title || 'Відкриваю…')}</h3><div class="findbox"><span class="spin">${ico('spinner')}</span><div class="tx"><b id="opstage">${esc(s.stage || 'Підключаюсь до сайту…')}</b></div>
     <button class="btn sm" data-act="opencancel">${ico('close')} Скасувати</button></div>`;
  else if (s.kind === 'confirm') inner = `<h3>${esc(s.title)}</h3><p style="color:var(--mu);white-space:pre-line">${esc(s.text || '')}</p>
     <div class="row2"><button class="btn" data-act="dlg-no" style="flex:1">Скасувати</button>
     <button class="btn ${s.danger ? 'dng' : 'pri'}" data-act="dlg-yes" style="flex:1">${esc(s.ok || 'Так')}</button></div>`;
  else if (s.kind === 'number') inner = `<h3>${esc(s.title)}</h3>
     <input class="in" id="numin" type="number" inputmode="numeric" min="0" value="${esc(s.value ?? '')}">
     <div class="row2"><button class="btn" data-act="dlg-no" style="flex:1">Скасувати</button>
     <button class="btn pri" data-act="dlg-yes" style="flex:1">Готово</button></div>`;
  const keepVal = $('#addurl') ? $('#addurl').value : null;
  const sh = $('#layer .sheet');
  if (!sh || sh.dataset.kind !== s.kind || $('#layer .back-d.out')) {
    $('#layer').innerHTML = `<div class="back-d${s.kind === 'cmenu' ? ' pop' : ''}" data-act="backdrop"><div class="sheet" data-sheet data-kind="${s.kind}"><div class="grab"></div><div id="sb">${inner}</div></div></div>`;
    document.body.style.overflow = 'hidden';
    if (s.kind === 'cmenu') placePop(s.rect);
  } else {
    $('#sb').innerHTML = inner;  // тільки вміст — шторка не «перезапускається»
  }
  if (keepVal !== null && $('#addurl')) $('#addurl').value = keepVal;
  if (s.kind === 'jobs') { updateJobs(); pollJobs(); }
}
async function checkInterrupted() {  // додаток закрився посеред завантаження: питаємо, чи продовжувати (недокачане збережено)
  try {
    const j = await api('/api/jobs');
    const it = j.interrupted || [];
    if (!it.length) return;
    if (j.paused) { toast('Завантаження призупинено (' + it.reduce((a, x) => a + x.n.length, 0) + ' сер.) — продовжити можна в списку завантажень', false, 'pause'); return; }
    const total = it.reduce((a, x) => a + x.n.length, 0);
    const list = it.slice(0, 4).map(x => `${x.title}: серії ${x.n.join(', ')}`).join('\n') + (it.length > 4 ? '\n…' : '');
    const yes = await askConfirm('Завантаження зупинилось', `Ви вийшли з додатка, і завантаження перервалось (серій: ${total}). Уже скачане збережено.\n${list}\nПродовжити?`, 'Продовжити', false);
    if (yes) { const r = await api('/api/jobs_resume', {}); startPoll(); if (r.errors && r.errors.length) toast((r.queued ? 'Частково: ' : 'Не вдалось продовжити: ') + r.errors[0], true); else toast('Завантаження продовжено' + (r.queued ? ': ' + r.queued : ''), false, 'download'); }
    else await api('/api/jobs_discard', {});
  } catch (e) { /* не критично */ }
}
function askConfirm(title, text, ok, danger) {
  return new Promise(res => { S.dlg = res; openSheet('confirm', {title, text, ok, danger}); });
}
function askText(title, value, text) {
  return new Promise(res => { S.dlg = res; openSheet('text', {title, value, text}); });
}
function askNumber(title, value) {
  return new Promise(res => { S.dlg = res; openSheet('number', {title, value}); });
}

function addHTML() {
  const L = S.lookup;
  let res = '';
  if (L) {
    const dubs = L.dubbings || [];
    res = `<div class="res"><div class="pic"${L.meta.poster ? ` style="background-image:url('/img?u=${encodeURIComponent(L.meta.poster)}')"` : ''}></div>
      <div class="tx"><b>${esc(L.title)}</b>${siteIcon(L.site)} ${esc((L.site || {}).name || '')} · серій: ${esc(L.count)}${L.meta.year ? ' · ' + esc(L.meta.year) : ''}
      ${L.meta.description ? `<p>${esc(L.meta.description)}</p>` : ''}</div></div>
      ${dubs.length ? `<div class="row2"><select class="in" id="adddub">${dubs.map(d => `<option>${esc(d)}</option>`).join('')}</select></div>` : ''}
      ${L.can_add ? `<button class="btn pri" style="width:100%" data-act="doadd" ${S.busy.has('add') ? 'disabled' : ''}>${ico('book')} Додати в бібліотеку</button>
        <button class="btn" style="width:100%;margin-top:8px" data-act="addview">${ico('play')} Переглянути вкладку</button>`
                  : `<p class="err">${esc(L.hint)}</p>`}`;
  }
  return `<h3>Додати аніме</h3>
    <input class="in" id="addurl" type="url" inputmode="url" autocapitalize="off" autocorrect="off" placeholder="Посилання на сторінку аніме" value="">
    <div class="row2"><button class="btn" data-act="paste">${ico('clip')} Вставити</button>
      <button class="btn pri" data-act="find" style="flex:1" ${S.find ? 'disabled' : ''}>${ico('search')} Знайти</button></div>
    ${S.find ? `<div class="findbox"><span class="spin">${ico('spinner')}</span><div class="tx"><b id="findstage">${esc(S.find.stage)}</b><small id="findtime">0 с</small></div>
      <button class="btn sm" data-act="findcancel">${ico('close')} Скасувати</button></div>` : ''}
    <p style="color:var(--mu);font-size:12.5px;margin:6px 0 0">Підтримуються: AniLiberty, anim-ru.net, YummyAnime (старе й нове дзеркало), AnimeVost.</p>
    ${S.addErr ? `<p class="err">${esc(S.addErr)}</p>` : ''}${res}`;
}

function jobsHTML() {
  return `<h3>Завантаження</h3><div class="jsum" id="jobsum"></div><div id="joblist"></div><p id="jobempty" style="color:var(--mu)">Завантажую…</p>
    <div class="row2" id="jobacts"></div>`;
}
function jobStatus(j) {
  const pct = j.total ? Math.min(100, Math.round(j.done * 100 / j.total)) : 0;
  if (j.cancelling) return {t: ico('spinner') + 'Скасовую…', cls: '', idle: true};
  if (j.kpaused && (j.status === 'running' || j.status === 'queued')) return {t: ico('pause') + 'Призупинено' + (j.pk_total && j.pk != null ? ` · ${j.pk}/${pkW(j.pk_total)}` : ''), cls: '', idle: true, pct: j.total ? Math.min(100, Math.round(j.done * 100 / j.total)) : 0};
  if (S.dlPaused && (j.status === 'running' || j.status === 'queued')) return {t: ico('pause') + 'Призупинено' + (j.pk_total && j.pk != null ? ` · ${j.pk}/${pkW(j.pk_total)}` : ''), cls: '', idle: true, pct: j.total ? Math.min(100, Math.round(j.done * 100 / j.total)) : undefined};
  if (j.status === 'queued' && j.retry_in > 0) return {t: ico('alert') + `Збій мережі — повтор через ${fmtDur(j.retry_in)} (спроба ${j.rounds + 1}) — завантажене збережено`, cls: '', idle: true};
  if (j.status === 'queued') {
    const p = [j.q, j.pk_total ? pkW(j.pk_total) + (j.bytes_total ? ` (≈${fmtSize(j.bytes_total)})` : '') : (j.bytes_total ? '≈' + fmtSize(j.bytes_total) : null), j.eta != null ? '~' + fmtDur(j.eta) : null].filter(Boolean);
    return {t: 'У черзі' + (p.length ? ' · ' + esc(p.join(' · ')) : ''), cls: '', idle: true};
  }
  if (j.status === 'running') {
    if (j.phase === 'resolve' || !j.phase) return {t: ico('spinner') + esc(j.stage || 'Отримую посилання…'), cls: '', idle: true, pct: 0};
    if (j.phase === 'playlist') return {t: ico('spinner') + 'Читаю плейлист…', cls: '', idle: true};
    if (j.phase === 'merge') return {t: ico('spinner') + 'Склеюю частини…', cls: '', pct: 100};
    if (j.phase === 'convert') return {t: ico('spinner') + 'Збираю MP4…', cls: '', pct: 100, idle: true};
    const parts = [];
    const mb = j.bytes ? ` (${fmtSize(j.bytes)})` : '';
    if (j.pk_total) parts.push(`${j.pk || 0}/${pkW(j.pk_total)}${mb}`);
    else if (j.bytes) parts.push(fmtSize(j.bytes));
    if (j.speed) parts.push(fmtSize(j.speed) + '/с');
    if (j.eta != null) parts.push('ще ~' + fmtDur(j.eta));
    return {t: `Завантаження ${pct}%` + (parts.length ? ' · ' + parts.join(' · ') : ''), cls: '', pct};
  }
  if (j.status === 'done') {
    const p = ['Готово', j.q, j.bytes_total ? fmtSize(j.bytes_total) : null, j.secs ? fmtDur(j.secs) : null, j.message].filter(Boolean);
    return {t: ico('ok') + '<span class="tx">' + esc(p.join(' · ')) + '</span>', cls: 'd'};
  }
  if (j.status === 'cancelled') return {t: ico('close') + 'Скасовано', cls: ''};
  return {t: ico('error') + '<span class="tx">' + esc(j.message || 'Помилка') + '</span>', cls: 'e'};
}
const JG_OPEN = {};  // що користувач відкрив/закрив вручну
function jgOpen(kid, arr) { return kid in JG_OPEN ? JG_OPEN[kid] : arr.some(j => j.status === 'running'); }  // за замовчуванням відкрите лише те, що качається зараз
const JG_CONF = {};
const JG_SK = `<div class="jgh" data-act="jgtog"><div class="jgp" data-act="jgopen"></div><div class="jgt"><b></b><span></span></div><button class="btn sm jgpb" data-act="jgpause" hidden></button><span class="jga">${ico('chev')}</span></div><div class="jgb"><div><div class="jgi"></div><div class="jgf"></div></div></div>`;
const JG_IMGS = {};  // обкладинки черги живуть між відкриттями шторки: уже завантажене й розкодоване зображення не вантажиться знову
function jgImg(p) {
  let im = JG_IMGS[p];
  if (!im) {
    im = new Image(); im.className = 'pi'; im.decoding = 'async'; im.alt = ''; im.dataset.o = p;
    im.onload = () => im.classList.add('ld');
    if (p.startsWith('/')) im.src = p;  // meta.poster уже адреса нашого сервера (/poster/… чи /img?u=…): раніше її загортали в /img вдруге — звідси довгі «порожні» обкладинки в черзі
    else { im.onerror = () => imgErr(im); im.src = '/img?u=' + encodeURIComponent(p); }
    JG_IMGS[p] = im;
  }
  return im;
}
function jgPreload() { try { new Set((S.jobs || []).map(j => j.kid)).forEach(k => { const it = item(k), p = it && it.meta && it.meta.poster; if (p) jgImg(p); }); } catch (e) {} }
function jgHead(g, kid, arr, cx) {
  const it = item(kid), live = arr.filter(j => j.status === 'running' || j.status === 'queued'), run = arr.find(j => j.status === 'running');
  const done = arr.filter(j => j.status === 'done').length, kp = (arr.some(j => j.kpaused) || !!S.dlPaused) && live.length > 0;
  const p = it && it.meta && it.meta.poster ? it.meta.poster : '';
  const pct = run && run.total ? Math.min(100, Math.round(run.done * 100 / run.total)) : 0;
  const bytesLeft = live.reduce((a, j) => a + Math.max(0, (j.bytes_total || 0) - (j.status === 'running' ? (j.bytes || 0) : 0)), 0);
  const eta = live.reduce((a, j) => a + (j.eta || 0), 0);
  const parts = cx ? [`скасовано ${arr.length}`] : [live.length ? `у черзі ${live.length}` : null, done ? `готово ${done}` : null, bytesLeft ? `≈${fmtSize(bytesLeft)}` : null, live.length && eta ? `~${fmtDur(eta)}` : null, kp ? 'призупинено' : (run ? `${pct}%` : null)].filter(Boolean);
  // каркас створюється один раз; оновлюється лише те, що справді змінилось — нічого не блимає і кнопки не підміняються під пальцем
  const pe = g.querySelector('.jgp');
  if (pe.dataset.p !== p) { pe.dataset.p = p; pe.textContent = ''; if (p) pe.appendChild(jgImg(p)); }
  const tb = g.querySelector('.jgt b'), tt = it ? lname(it) : arr[0].title;
  if (tb.textContent !== tt) tb.textContent = tt;
  const sp = g.querySelector('.jgt span'), st = parts.join(' · ');
  if (sp.textContent !== st) sp.textContent = st;
  const pb = g.querySelector('.jgpb'), show = cx || live.length > 0;
  if (pb.hidden !== !show) pb.hidden = !show;
  const ps = cx ? 'x' : (kp ? 'p' : 'r');
  if (show && pb.dataset.s !== ps) { pb.dataset.s = ps; pb.dataset.k = kid; pb.dataset.act = cx ? 'jgresume' : 'jgpause'; pb.innerHTML = ico(cx ? 'refresh' : (kp ? 'play' : 'pause')); pb.setAttribute('aria-label', cx ? 'Перезапустити завантаження' : (kp ? 'Продовжити' : 'Пауза')); }
  const ft = g.querySelector('.jgf'), cf = !!JG_CONF[kid];
  const fs = (cx ? 'x' : (live.length ? 'l' : '')) + (cf ? 'c' : '');
  if (ft.dataset.s !== fs) {
    ft.dataset.s = fs;
    ft.innerHTML = cx ? `<button class="jgbtn res${cf ? ' conf' : ''}" data-act="jgresume" data-k="${esc(kid)}">Перезапустити завантаження</button>`
      : (live.length ? `<button class="jgbtn${cf ? ' conf' : ''}" data-act="jgcancel" data-k="${esc(kid)}">${cf ? 'Натисніть ще раз — скасувати' : 'Скасувати завантаження'}</button>` : '');
  }
  g.classList.toggle('open', jgOpen(g.dataset.k, arr));
}
function updateJobs() {
  const list = $('#joblist'); if (!list) return;  // шторка закрита
  const js = [...S.jobs].reverse();
  const groups = new Map();  // скасоване — окремі групи під рискою
  js.forEach(j => { const cx = j.status === 'cancelled' || !!j.cancelling, key = j.kid + (cx ? '~x' : ''); if (!groups.has(key)) groups.set(key, {key, kid: j.kid, cx, arr: []}); groups.get(key).arr.push(j); });
  [...list.querySelectorAll('.jg')].forEach(g => { if (!groups.has(g.dataset.k)) g.remove(); });
  const rank = G => G.cx ? 3 : G.arr.some(j => j.status === 'running') ? 0 : G.arr.some(j => j.status === 'queued') ? 1 : 2;  // зверху те, що качається зараз
  const order = [...groups.values()].map(G => (G.arr.reverse(), G)).sort((x, y) => rank(x) - rank(y));
  const hasCx = order.some(G => G.cx);
  let div = list.querySelector('.jdiv');
  if (hasCx && !div) { div = document.createElement('div'); div.className = 'jdiv'; div.innerHTML = '<span>Скасовані</span>'; }
  if (!hasCx && div) { div.remove(); div = null; }
  let prevG = null, divDone = false;
  order.forEach(G => {
    if (G.cx && div && !divDone) { divDone = true; if (div.previousElementSibling !== prevG || div.parentNode !== list) list.insertBefore(div, prevG ? prevG.nextSibling : list.firstChild); prevG = div; }
    let g = [...list.querySelectorAll('.jg')].find(x => x.dataset.k === G.key);
    if (!g) { g = document.createElement('div'); g.className = 'jg' + (G.cx ? ' cx' : ''); g.dataset.k = G.key; g.innerHTML = JG_SK; }
    if (g.previousElementSibling !== prevG || g.parentNode !== list) list.insertBefore(g, prevG ? prevG.nextSibling : list.firstChild);
    prevG = g;
    jgHead(g, G.kid, G.arr, G.cx);
    const body = g.querySelector('.jgi');
    const present = new Set(G.arr.map(j => String(j.id)));
    [...body.children].forEach(el => { if (!present.has(el.dataset.j) && !el.classList.contains('gone')) { el.classList.add('gone'); setTimeout(() => el.remove(), 210); } });
    let prev = null;
    G.arr.forEach(j => {
      let el = list.querySelector(`[data-j="${j.id}"]`);
      if (!el) {
        el = document.createElement('div'); el.className = 'job'; el.dataset.j = j.id;
        el.innerHTML = `<div class="h"><div class="t"></div></div><div class="st"></div><div class="prog"><i></i></div>`;
      }
      if (el.parentNode !== body || el.previousElementSibling !== prev) body.insertBefore(el, prev ? prev.nextSibling : body.firstChild);
      prev = el;
      const t = el.querySelector('.t'); const tt = `${j.title} · серія ${j.n}`;
      const tsig = tt + '|' + (j.dub || '');
      if (t.dataset.sig !== tsig) { t.dataset.sig = tsig; t.innerHTML = esc(tt) + (j.dub ? ` <span class="dub">${esc(j.dub)}</span>` : ''); }
      const st = jobStatus(j); const live = j.status === 'running' || j.status === 'queued';
      const sig = st.t + '|' + st.cls;
      const se = el.querySelector('.st');
      if (se.dataset.sig !== sig) { se.dataset.sig = sig; se.className = 'st ' + st.cls; se.innerHTML = st.t; }
      const pr = el.querySelector('.prog'); pr.style.display = j.status === 'running' ? '' : 'none';
      pr.classList.toggle('idle', !!st.idle);
      pr.firstChild.style.width = (st.pct ?? 0) + '%';
      let b = el.querySelector('.h .btn');
      if (live && !j.cancelling) {
        if (!b) { b = document.createElement('button'); b.className = 'btn sm jx'; b.dataset.act = 'jobcancel'; b.dataset.job = j.id; b.setAttribute('aria-label', 'Скасувати серію'); b.innerHTML = ico('close'); el.querySelector('.h').appendChild(b); }
      } else if (b) b.remove();
    });
  });
  const je = $('#jobempty');
  je.textContent = !S.jobsT ? 'Завантажую…' : (S.dlPaused && S.dlIntr > 0) ? `Призупинено: серій у черзі — ${S.dlIntr}. Натисніть «Продовжити».` : 'Черга порожня.';
  je.style.display = js.length ? 'none' : '';
  const sm = S.jobsum, se2 = $('#jobsum');
  if (se2) {
    let t = '';
    if (sm && sm.count) {
      t = `У черзі: <b>${sm.count}</b>` + (sm.pk_total ? ` · <b>${pkW(sm.pk_left != null ? sm.pk_left : sm.pk_total)}</b>` + (sm.bytes_left ? ` (≈${fmtSize(sm.bytes_left)})` : '') : (sm.bytes_total ? ` · ≈<b>${fmtSize(sm.bytes_left != null ? sm.bytes_left : sm.bytes_total)}</b>` : ''))
        + (sm.speed ? ` · <b>${fmtSize(sm.speed)}/с</b>` : '')
        + (sm.eta != null ? ` · залишилось ~<b>${fmtDur(sm.eta)}</b>` : '')
        + (sm.unknown && sm.bytes_total ? ` · розмір ще ${sm.unknown} невідомий` : '');
    }
    if (se2.dataset.sig !== t) { se2.dataset.sig = t; se2.innerHTML = t; se2.style.display = t ? '' : 'none'; }
  }
  const active = js.some(j => j.status === 'running' || j.status === 'queued');
  const finished = js.some(j => j.status !== 'running' && j.status !== 'queued');
  const pz = !!S.dlPaused, hasIntr = pz && S.dlIntr > 0, cA = !!JG_CONF['*'];
  const asig = (active ? 'a' : '') + (finished ? 'f' : '') + (pz ? 'p' : '') + (hasIntr ? 'i' : '') + (cA ? 'c' : '');
  const ac = $('#jobacts');
  if (ac.dataset.sig !== asig) {
    ac.dataset.sig = asig;
    ac.innerHTML = ((active || hasIntr) ? `<button class="btn" data-act="dlpause">${ico(pz ? 'play' : 'pause')} ${pz ? 'Продовжити' : 'Призупинити'}</button>` : '')
      + (finished && !active ? '<button class="btn" data-act="clearjobs">Очистити список</button>' : '')
      + '<button class="btn" data-act="dlg-no">Закрити</button>'
      + (active ? `<button class="jgbtn wide${cA ? ' conf' : ''}" data-act="canceljobs">${cA ? 'Натисніть ще раз — скасувати все' : 'Скасувати всі завантаження'}</button>` : '');
  }
}

/* ---------- збереження: експорт / імпорт ---------- */
let bkList = null, bkPrev = '';
function bkSize(n) { return fmtSize(n); }
function bkHash(t) { let h = 0; for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) | 0; return h; }
const BK_MODES = [
  ['all', 'Усе з відео', 'Бібліотека, налаштування і всі скачані серії. Файл великий.'],
  ['links', 'Підкачати після відновлення', 'Без відео, але список скачаного запам\'ятовується. Після відновлення програма спитає, чи підкачати ці серії.'],
  ['lib', 'Бібліотека й налаштування', 'Без відео і без списку скачаного.'],
  ['settings', 'Лише налаштування', 'Тільки вигляд і налаштування — наприклад, щоб надіслати другу.'],
];
const BK_LBL = {all: 'з відео', links: 'підкачати', lib: 'бібліотека', settings: 'налаштування'};
let bkShared = '', bkAsked = '', bkApplied = '', S_bkMode = 'lib';
async function applyImported() {  // після відновлення одразу показуємо нову бібліотеку й налаштування (без перезапуску програми)
  try {
    const j = await api('/api/settings');
    S.set = j; syncAuto();
    const c = j.client || {};  // уподобання, що живуть лише в браузері (вкладка бібліотеки, вигляд серій, сайт новинок, гучність)
    try {
      if (c.libTab) S.libTab = c.libTab;
      if (c.epv) { S.epView = c.epv; localStorage.setItem('epv', c.epv); }
      if (c.nwsite) { S.nw.site = c.nwsite; localStorage.setItem('nwsite', c.nwsite); }
      if (c.pvol != null) localStorage.setItem('pvol', String(c.pvol));
    } catch (e) {}
    if (j.view && Object.keys(j.view).length) { S.view = {...VIEW_DEF, ...j.view}; try { localStorage.setItem('libview', JSON.stringify(S.view)); } catch (e) {} }
    S._libSig = null;
    await loadLib();
    render();
  } catch (e) {}
}
function backupBox() { return `<h2 class="sth">Збереження програми</h2><div id="bkbox" data-h="${bkHash(S.bkHtml || '')}">${S.bkHtml || ''}</div>`; }
function bkInner(st) {
  let h = '';
  if (st.state === 'run') {
    const pct = st.total ? Math.min(100, Math.round(st.done * 100 / st.total)) : 0;
    return `<div class="bkc"><b>${st.kind === 'import' ? 'Відновлення' : 'Збереження'}: ${pct}%</b>${st.total ? `<span class="bkd">${bkSize(st.done)} з ${bkSize(st.total)}</span>` : ''}
      <div class="prog" style="margin-top:8px"><i style="width:${pct}%"></i></div><span class="bkd" style="margin-top:6px">${esc(st.msg || '')}</span>
      <button class="btn" data-act="bkcancel">Скасувати</button></div>`;
  }
  if (st.state === 'error') h += `<div class="bkc" style="border:1px solid var(--err)"><b style="color:var(--err)">Помилка</b><span class="bkd">${esc(st.err)}</span></div>`;
  if (st.state === 'done' && st.kind === 'import' && st.result) {
    const r = st.result;
    h += `<div class="bkc"><b>Відновлення завершено</b><span class="bkd">${r.mode === 'settings' ? 'Налаштування застосовано.' : `Додано аніме: ${r.added}.${r.mode === 'all' ? ' Відео відновлено.' : ''}${r.sett ? ' Налаштування застосовано.' : ''}`}${r.pending ? ` Можна підкачати серій: ${r.pending}.` : ''}${r.queued ? ` Поставлено в чергу: ${r.queued}.` : ''}</span>
      ${r.pending ? `<button class="btn pri" data-act="bkdl">Підкачати ${r.pending} сер.</button>` : ''}</div>`;
  }
  h += `<div class="bkc"><b>Що зберегти</b><div class="bkopts">${BK_MODES.map(([m, t, d]) => `<button class="bko${S_bkMode === m ? ' on' : ''}" data-act="bkmode" data-m="${m}"><i></i><span><b>${t}</b><em>${d}</em></span></button>`).join('')}</div>
    <button class="btn pri" data-act="bkexport">${ico('share')} Зберегти</button></div>`;
  h += `<div class="bkc"><b>Відновити</b><span class="bkd">Оберіть файл збереження — програма сама визначить, що в ньому: відео, список для підкачки чи лише налаштування.</span>
    <label class="btn" style="position:relative;overflow:hidden;display:flex;justify-content:center">Обрати файл…<input type="file" id="bkfile" accept=".zip,application/zip,application/x-zip-compressed" style="position:absolute;inset:0;opacity:0;width:100%;height:100%;font-size:100px"></label></div>`;
  if (bkList && bkList.length) h += `<div class="bkc"><b>Файли збережень</b>` + bkList.map(f => `<div class="strow"><div class="tx"><b style="overflow-wrap:anywhere">${esc(f.name)}</b><span>${bkSize(f.size)}${f.mode ? ' · ' + (BK_LBL[f.mode] || '') : ''}${f.eps ? ` · серій: ${f.eps}` : ''}</span></div>
      <button class="btn sm" data-act="bkshare" data-r="${esc(f.rel)}" aria-label="Поділитись">${ico('share')}</button><button class="btn sm" data-act="bkimport" data-r="${esc(f.rel)}">Відновити</button><button class="btn sm" data-act="bkdel" data-r="${esc(f.rel)}" aria-label="Видалити">${ico('trash')}</button></div>`).join('') + `</div>`;
  return h;
}
async function bkTick(reload) {
  const box = $('#bkbox');
  if (!box) return;
  let st; try { st = await api('/api/backup_status', {}); } catch (e) { return; }
  if (bkPrev === 'run' && st.state !== 'run') reload = true;
  bkPrev = st.state;
  if (st.state === 'done' && st.kind === 'export' && st.result && bkList && !bkList.some(x => x.name === st.result.name)) reload = true;
  if (reload || !bkList) { try { bkList = (await api('/api/backup_list', {})).files; } catch (e) { bkList = bkList || []; } }
  if (st.state === 'done' && st.kind === 'export' && st.result && bkShared !== st.result.name) {  // готовий файл одразу пропонуємо зберегти у «Файли»; лишається і в списку з кнопкою «Поділитись»
    bkShared = st.result.name;
    const f = (bkList || []).find(x => x.name === st.result.name);
    if (f) api('/api/backup_share', {rel: f.rel}).catch(() => {});
  }
  if (st.state === 'done' && st.kind === 'import' && st.result && bkApplied !== st.msg + st.done + st.total) {
    bkApplied = st.msg + st.done + st.total;
    applyImported();
  }
  if (st.state === 'done' && st.kind === 'import' && st.result && st.result.pending && bkAsked !== st.result.pending + ':' + st.msg + st.done) {
    bkAsked = st.result.pending + ':' + st.msg + st.done;
    askConfirm('Підкачати відео?', `У збереженні були скачані серії: ${st.result.pending}. Почати їх скачування зараз?`, 'Підкачати').then(async yes => {
      if (!yes) return;
      try { const r = await api('/api/import_dl', {}); startPoll(); toast('Поставлено в чергу: ' + r.queued, false, 'download'); } catch (e) { toast(e.message, true); }
      bkTick(true);
    });
  }
  const b2 = $('#bkbox'); if (!b2) return;
  const h = bkInner(st), hh = bkHash(h);
  S.bkHtml = h;
  if (b2.dataset.h !== String(hh)) { b2.dataset.h = String(hh); b2.innerHTML = h; const f = $('#bkfile'); if (f) f.onchange = () => bkUpload(f.files && f.files[0]); }
  else { const f = $('#bkfile'); if (f && !f.onchange) f.onchange = () => bkUpload(f.files && f.files[0]); }
}
function bkUpload(file) {
  if (!file) return;
  const box = $('#bkbox');
  if (box) { box.dataset.h = ''; box.innerHTML = `<div class="bkc"><b>Передаю файл: <span id="bkup">0%</span></b><div class="prog" style="margin-top:8px"><i id="bkupb" style="width:0%"></i></div><span class="bkd" style="margin-top:6px">${esc(file.name)} · ${bkSize(file.size)}</span></div>`; }
  const x = new XMLHttpRequest();
  x.open('POST', '/api/backup_upload');
  x.upload.onprogress = e => { if (!e.lengthComputable) return; const p = Math.round(e.loaded * 100 / e.total); const a = $('#bkup'), b = $('#bkupb'); if (a) a.textContent = p + '%'; if (b) b.style.width = p + '%'; };
  x.onload = () => {
    let r = {}; try { r = JSON.parse(x.responseText); } catch (e) {}
    if (x.status >= 400 || r.error) toast(r.error || 'Не вдалося передати файл', true);
    bkPrev = 'run'; bkTick(true);
  };
  x.onerror = () => { toast('Не вдалося передати файл', true); bkTick(true); };
  x.send(file);
}
setInterval(() => { if ($('#bkbox') && !document.hidden) bkTick(false); }, 1000);

/* ---------- опитування черги ---------- */
let pollTimer = null, lastDone = -1;
async function pollJobs() {
  try {
    const j = await api('/api/jobs');
    S.jobsT = Date.now(); S.jobs = j.jobs; jgPreload(); S.activeJobs = j.active; S.jobsum = j.summary; S.dlPaused = !!j.paused; S.dlIntr = (j.interrupted || []).reduce((a, x) => a + x.n.length, 0);
    const done = j.jobs.filter(x => x.status === 'done').length;
    if (lastDone >= 0 && done !== lastDone) await loadLib();
    lastDone = done;
    if (S.sheet && S.sheet.kind === 'jobs') updateJobs();
    else if (!S.sheet && route().name === 'home') render();
    else if (route().name === 'detail') {  // лічильник завантажень у шапці — на місці, без перемальовування
      const n = S.jobs.filter(x => x.status === 'running' || x.status === 'queued').length;
      const b = $('#root .bar .ib[data-act=jobs]');
      if (b) { let d = b.querySelector('.dot'); if (n) { if (!d) { d = document.createElement('span'); d.className = 'dot'; b.appendChild(d); } if (d.textContent !== String(n)) d.textContent = n; } else if (d) d.remove(); }
    }
    if (!j.active && !(S.sheet && S.sheet.kind === 'jobs')) { stopPoll(); pollNotifs(); }
  } catch (e) { /* сервер недоступний */ }
}
function startPoll() { if (!pollTimer) { pollTimer = setInterval(pollJobs, 800); } pollJobs(); }
function stopPoll() { clearInterval(pollTimer); pollTimer = null; }

/* ---------- дії ---------- */
async function guard(key, fn) {
  S.busy.add(key); render();
  try { await fn(); } catch (e) { toast(e.message, true); }
  finally { S.busy.delete(key); render(); }
}

function showOpening(title) {  // повноцінна сторінка завантаження — така сама за виглядом, як сторінка аніме
  hideOpening();
  const d = document.createElement('div');
  d.id = 'oppage';
  d.innerHTML = `<div class="bar"><button class="back" data-act="opencancel">‹ Назад</button><h1></h1></div>
    <div class="hero"><div class="bg"></div><div class="row"><div class="pic sk"></div><div style="min-width:0;flex:1">
      <h2>${esc(title || 'Відкриваю…')}</h2>
      <div class="opst"><span class="spin">${ico('spinner')}</span><b id="opstage">Підключаюсь до сайту…</b></div>
      <div class="sk" style="height:12px;width:70%;margin-top:14px"></div><div class="sk" style="height:12px;width:45%;margin-top:8px"></div>
      <div style="display:flex;gap:8px;margin-top:14px"><div class="sk" style="height:26px;width:64px;border-radius:13px"></div><div class="sk" style="height:26px;width:78px;border-radius:13px"></div><div class="sk" style="height:26px;width:56px;border-radius:13px"></div></div>
    </div></div></div>
    <div style="padding:0 16px"><div style="display:flex;gap:8px;margin-bottom:16px"><div class="sk" style="height:44px;flex:1;border-radius:14px"></div><div class="sk" style="height:44px;width:60px;border-radius:14px"></div></div>
    ${[0, 1, 2, 3, 4, 5].map(() => `<div style="display:flex;gap:12px;align-items:center;margin-bottom:12px"><div class="sk" style="width:104px;height:60px;border-radius:10px;flex:none"></div><div style="flex:1"><div class="sk" style="height:13px;width:60%"></div><div class="sk" style="height:11px;width:35%;margin-top:8px"></div></div></div>`).join('')}</div>`;
  document.body.appendChild(d);
  document.body.style.overflow = 'hidden';
}
function hideOpening() { const d = $('#oppage'); if (d) d.remove(); if (!S.cur && !S.sheet) document.body.style.overflow = ''; }
async function openAnime(url, title, explicit, sources) {
  const O = S.opening = {off: false, task: null};
  showOpening(title);
  try {
    const r = await api('/api/open', {url, explicit, sources});
    O.task = r.task;
    let id = r.id;
    while (!id) {
      if (O.off) return;
      const st = await api('/api/lookup_status?task=' + O.task);
      if (O.off) return;
      const e1 = $('#opstage'); if (e1 && st.stage) e1.textContent = st.stage;
      if (st.done) { if (st.error) throw new Error(st.error); id = st.result.id; break; }
      await new Promise(r2 => setTimeout(r2, 350));
    }
    await loadLib(true);  // без перемальовування фону: одразу переходимо на сторінку аніме (без мерехтіння)
    S.opening = null; hideOpening();
    location.hash = '#/a/' + id;
  } catch (e) { if (!O.off) { S.opening = null; hideOpening(); toast(e.message, true); } }
}

function gpickHTML() {
  const gs = [], seen = new Set();
  S.items.forEach(x => { if (x.group && !seen.has(x.group)) { seen.add(x.group); gs.push(x.group); } });
  const c = (S.sheet && S.sheet.ctx) || {};
  const what = c.arch ? 'у переглянуте' : 'у бібліотеку';
  return `<h3>Додати до групи</h3><p style="color:var(--mu);font-size:13px;margin:0 0 6px">Аніме буде додано ${what} і в обрану групу.</p>` + (gs.length
    ? `<div class="cm">${gs.map(g => { const m = groupMembers(g), nm = gname(m) || m.slice(0, 2).map(lname).join(' · '); return `<button data-act="gpickgo" data-g="${esc(g)}">${ico('plus')}<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(nm || 'Група')}</span><span style="color:var(--mu);font-weight:500">${m.length}</span></button>`; }).join('')}</div>`
    : `<p style="color:var(--mu)">У бібліотеці ще немає груп. Групу можна створити в меню аніме в бібліотеці.</p>`)
    + `<div class="row2" style="margin-top:10px"><button class="btn" data-act="dlg-no" style="flex:1">Скасувати</button></div>`;
}
async function resolveAnime(r0) {  // знаходимо/створюємо запис аніме без переходу на його сторінку
  const r = await api('/api/open', {url: r0.srcs[0].url, explicit: r0.explicit, sources: r0.srcs});
  let id = r.id; const t0 = Date.now();
  while (!id) {
    if (Date.now() - t0 > 90000) throw new Error('Сайт довго відповідає — спробуйте ще раз');
    const st = await api('/api/lookup_status?task=' + r.task);
    if (st.done) { if (st.error) throw new Error(st.error); id = st.result.id; break; }
    await new Promise(r2 => setTimeout(r2, 350));
  }
  return id;
}
async function addTo(ctx, gid) {  // ctx: {type: res|tmp|item, ...}; arch — додати в «Переглянуте»; gid — ще й у наявну групу
  try {
    let id;
    if (ctx.type === 'res') { toast('Додаю…', false, 'spinner'); id = await resolveAnime(ctx.res); await loadLib(true); } else id = ctx.id;
    const it = item(id);
    if (it && it.temp) await api('/api/keep', {id, archived: !!ctx.arch});
    else if (it && !!it.archived !== !!ctx.arch) await api('/api/archive', {id, value: !!ctx.arch});
    if (gid) await api('/api/group', {ids: [id], gid});
    await loadLib();
    toast((ctx.arch ? 'У переглянутих' : 'У бібліотеці') + (gid ? ' · у групі' : ''), false, 'check');
  } catch (e) { toast(e.message, true); try { await loadLib(); } catch (e2) {} }
}
async function copyText(t) {
  try { await navigator.clipboard.writeText(t); return true; } catch (e) {
    try { const x = document.createElement('textarea'); x.value = t; x.style.cssText = 'position:fixed;opacity:0'; document.body.appendChild(x); x.select(); const ok = document.execCommand('copy'); x.remove(); return ok; } catch (e2) { return false; }
  }
}
const A = {
  gpick(el) {
    const m = el.dataset.m;
    let ctx;
    if (S.sheet && S.sheet.res) ctx = {type: 'res', res: S.sheet.res, arch: m === 'seen'};
    else if (el.dataset.cx === 'tmp') ctx = {type: 'tmp', id: route().id, arch: m === 'seen'};
    else { const it = item(el.dataset.id); if (!it) return; ctx = {type: 'item', id: it.id, arch: !it.archived}; }
    openSheet('gpick', {ctx});
  },
  async gpickgo(el) { const c = S.sheet && S.sheet.ctx; closeSheet(); if (c) await addTo(c, el.dataset.g); },
  async rsshare() { const r = S.sheet && S.sheet.res; closeSheet(); if (!r) return; const ok = await copyText(r.srcs[0].url); toast(ok ? 'Посилання скопійовано' : 'Не вдалося скопіювати: ' + r.srcs[0].url, !ok, ok ? 'check' : null); },
  async rslib() { const r = S.sheet && S.sheet.res; closeSheet(); if (r) await addTo({type: 'res', res: r, arch: false}); },
  async rsseen() { const r = S.sheet && S.sheet.res; closeSheet(); if (r) await addTo({type: 'res', res: r, arch: true}); },
  async bellall() { try { S.notifs = await api('/api/notifs_read', {}); renderSheet(); render(); toast('Усі повідомлення прочитано', false, 'check'); } catch (e) { toast(e.message, true); } },
  async cmgroup(el) {
    const id = el.dataset.id; closeSheet();
    try { await api('/api/group', {ids: [id], name: ''}); await loadLib(); toast('Групу створено — додайте до неї інші аніме перетягуванням', false, 'check'); } catch (e) { toast(e.message, true); }
  },
  async gmshare(el) {
    const g = el.dataset.g, m = groupMembers(g); closeSheet();
    const ok = await copyText(m.map(x => x.url).join('\n'));
    toast(ok ? 'Посилання скопійовано: ' + m.length : 'Не вдалося скопіювати', !ok, ok ? 'check' : null);
  },
  async gmrename(el) { closeSheet(); await new Promise(r => setTimeout(r, 220)); await A.grename(el); },
  async gmungroup(el) { closeSheet(); await new Promise(r => setTimeout(r, 220)); await A.gungroup(el); },
  async gmdel(el) {
    const g = el.dataset.g, m = groupMembers(g); closeSheet(); await new Promise(r => setTimeout(r, 220));
    const names = m.map(x => '• ' + lname(x)).slice(0, 6).join('\n');
    if (!(await askConfirm('Видалити групу з аніме?', `Буде видалено всю групу: ${m.length} аніме, їхні обкладинки й усі завантажені серії з пристрою.\n${names}${m.length > 6 ? '\n…' : ''}`, 'Видалити', true))) return;
    try { for (const x of m) await api('/api/remove', {id: x.id}); await loadLib(); if (route().name === 'group') location.hash = '#/'; render(); toast('Видалено: ' + m.length); } catch (e) { toast(e.message, true); }
  },
  async site(el) {  // один тап — відкрити сайт, подвійний — скопіювати посилання
    const it = item(el.dataset.id); if (!it) return;
    if (S.siteT) {
      clearTimeout(S.siteT); S.siteT = null;
      let ok = false;
      try { await navigator.clipboard.writeText(it.url); ok = true; } catch (e) {
        try { const t = document.createElement('textarea'); t.value = it.url; t.style.cssText = 'position:fixed;opacity:0'; document.body.appendChild(t); t.select(); ok = document.execCommand('copy'); t.remove(); } catch (e2) {}
      }
      toast(ok ? 'Посилання скопійовано' : 'Не вдалося скопіювати: ' + it.url, !ok, ok ? 'check' : null);
      return;
    }
    S.siteT = setTimeout(() => { S.siteT = null; window.open(it.url, '_blank', 'noopener'); }, 300);
  },
  gopen(el) { const g = el.dataset.gid; if (g) { S.reorder = false; S.libSel.clear(); location.hash = '#/g/' + g; } },
  gback() { S.reorder = false; S.libSel.clear(); location.hash = '#/'; },
  gmopen(el) { const id = el.dataset.id; closeSheet(); setTimeout(() => { location.hash = '#/a/' + id; }, 60); },
  async grename(el) {
    const g = el.dataset.g, m = groupMembers(g);
    const v = await askText('Назва групи', gname(m), 'Порожньо — група без назви.'); if (v === null || v === undefined) return;
    try { await api('/api/group_rename', {gid: g, name: v}); await loadLib(); render(); } catch (e) { toast(e.message, true); }
  },
  async gungroup(el) {
    const g = el.dataset.g, ids = groupMembers(g).map(x => x.id);
    if (!(await askConfirm('Розгрупувати?', 'Аніме лишаться в бібліотеці, зникне лише група.', 'Розгрупувати', true))) return;
    try { await api('/api/ungroup', {ids}); S.reorder = false; S.libSel.clear(); await loadLib(); location.hash = '#/'; toast('Групу розформовано', false, 'check'); } catch (e) { toast(e.message, true); }
  },
  async libgrp() {
    const ids = [...S.libSel].filter(id => item(id)); if (ids.length < 2) return;
    const v = await askText('Назва групи', '', 'Можна залишити порожнім — тоді група буде без назви.'); if (v === null || v === undefined) return;
    try { await api('/api/group', {ids, name: v}); S.libSel.clear(); await loadLib(); toast('Групу створено', false, 'check'); } catch (e) { toast(e.message, true); }
  },
  async libungrp() {
    const ids = [...S.libSel].filter(id => item(id) && item(id).group); if (!ids.length) return;
    try { await api('/api/ungroup', {ids}); S.libSel.clear(); await loadLib(); toast('Розгруповано', false, 'check'); } catch (e) { toast(e.message, true); }
  },
  stgo() { const e = document.getElementById('stanime'); if (e) { const y = e.getBoundingClientRect().top + window.scrollY - 12; window.scrollTo({top: y, behavior: 'smooth'}); e.classList.remove('flash'); void e.offsetWidth; e.classList.add('flash'); } },
  stback() { location.hash = '#/settings'; },
  async stclear(el) {
    const w = el.dataset.w;
    if (!(await askConfirm(el.dataset.t || 'Очистити?', el.dataset.d || '', 'Видалити', true))) return;
    try {
      const r = await api('/api/storage_clear', {what: w, id: el.dataset.id, name: el.dataset.name});
      S.st.data = r.storage; S.st.t = Date.now();
      const y = window.scrollY; render(); window.scrollTo(0, y);
      toast('Звільнено ' + fmtS(r.freed), false, 'trash');
      loadLib && loadLib(true);
    } catch (e) { toast(e.message, true); }
  },
  home() { location.hash = '#/'; },
  back() { if (history.length > 1) history.back(); else location.hash = '#/search'; },
  sclear() { const i = $('#sq'); if (i) { i.value = ''; i.focus(); } S.search.q = ''; },
  sfil(el) {
    const Q = S.search, id = el.dataset.s;
    if (!id) Q.sel = new Set();
    else if (!Q.sel.size) Q.sel = new Set([id]);
    else { Q.sel.has(id) ? Q.sel.delete(id) : Q.sel.add(id); }
    paintSearch();
  },
  smore() { S.search.limit = (S.search.limit || 36) + 36; paintSearch(true); },
  async dosearch() {
    const Q = S.search, inp = $('#sq'), q = (inp ? inp.value : Q.q).trim();
    if (q.length < 2) { toast('Введіть хоча б 2 символи', true); return; }
    if (inp) inp.blur();
    Q.q = q; Q.err = ''; Q.res = null; Q.loading = true; Q.limit = 36; const my = ++Q.seq; paintSearch(true);
    try {
      const r = await api('/api/search', {q});
      for (;;) {
        if (Q.seq !== my) return;
        const st = await api('/api/search_status?task=' + r.task);
        if (Q.seq !== my) return;
        Q.res = st; paintSearch();
        if (st.done) break;
        await new Promise(r2 => setTimeout(r2, 450));
      }
    } catch (e) { if (Q.seq === my) Q.err = e.message; }
    if (Q.seq === my) { Q.loading = false; paintSearch(); }
  },
  async openres(el) {
    const Q = S.search, v = Q.vis[Number(el.dataset.g)];
    if (!v || S.opening) return;
    await openAnime(v.its[0].url, v.its[0].title, Q.sel.size > 0, v.g.items.map(x => ({url: x.url, title: x.title})));
  },
  async openrel(el) {
    if (S.opening) return;
    if (el.classList.contains('cur')) return;
    if (el.dataset.lib) { location.hash = '#/a/' + el.dataset.lib; return; }
    await openAnime(el.dataset.url, el.dataset.t, false, [{url: el.dataset.url, title: el.dataset.t}]);
  },
  opencancel() { if (S.opening) { const o = S.opening; S.opening = null; o.off = true; if (o.task) api('/api/lookup_cancel', {task: o.task}).catch(() => {}); } hideOpening(); toast('Скасовано', false, 'close'); },
  openlib(el) { S.skipOut = true; location.hash = '#/a/' + el.dataset.id; },
  async keep() {
    const id = route().id;
    await guard('keep', async () => { await api('/api/keep', {id}); await loadLib(); toast('Додано в бібліотеку', false, 'book'); });
  },
  async keepseen() {
    const id = route().id;
    await guard('keep', async () => { await api('/api/keep', {id, archived: true}); await loadLib(); const k = item(id); if (k && !k.archived) { k.archived = true; render(); } toast('Додано в переглянуті', false, 'check'); });
  },
  async setsite(el) {
    const id = route().id, it = item(id), url = el.dataset.url;
    if (!it || el.classList.contains('on') || S.busy.has('sw')) return;
    S.busy.add('sw');
    document.querySelectorAll('.srcs, .dubs').forEach(c => c.classList.add('dis'));
    document.querySelectorAll('.srcs .dchip').forEach(c => c.style.opacity = c === el ? 1 : .5);
    try {
      const r = await api('/api/switch_site', {id, url});
      await waitTask(r.task);
      S.sel.clear(); S.selMode = false; fx(); S.chipFix = true; await loadLib();
      const n = item(id); toast('Сайт: ' + ((n && n.site && n.site.name) || ''), false, 'check');
    } catch (e) { toast(e.message, true); }
    finally { S.busy.delete('sw'); S.chipFix = true; render(); }
  },
  async findalts() {
    const id = route().id;
    if (S.busy.has('alts')) return;
    S.busy.add('alts'); render();
    try {
      const r = await api('/api/find_alts', {id});
      for (;;) {
        const st = await api('/api/lookup_status?task=' + r.task);
        if (st.done) { if (st.error) throw new Error(st.error); await loadLib(); toast(st.result.found ? 'Знайдено на інших сайтах: ' + st.result.found : 'На інших сайтах не знайдено', false, st.result.found ? 'check' : null); break; }
        await new Promise(r2 => setTimeout(r2, 500));
      }
    } catch (e) { toast(e.message, true); }
    S.busy.delete('alts'); render();
  },
  dtab(el) { S.dtab = el.dataset.t; S.dtabMap[route().id] = S.dtab; if (S.dtab !== 'eps') { S.sel.clear(); S.selMode = false; } render(); },
  notifs() { openSheet('notifs'); pollNotifs(); },
  async nclear() { try { S.notifs = await api('/api/notifs_clear', {}); renderSheet(); render(); toast('Прочитані повідомлення очищено', false, 'check'); } catch (e) { toast(e.message, true); } },
  async nreadall() { try { S.notifs = await api('/api/notifs_read', {}); renderSheet(); render(); } catch (e) { toast(e.message, true); } },
  async nopen(el) {
    const id = el.dataset.id, kid = el.dataset.kid, ep = el.dataset.ep;
    try { S.notifs = await api('/api/notifs_read', {id}); } catch (e) {}
    if (!kid) { renderSheet(); return; }
    closeSheet();
    if (!item(kid)) { try { await loadLib(); } catch (e) {} }
    if (!item(kid)) { toast('Аніме вже немає в бібліотеці', true); return; }
    S.scrollEp = {kid, n: ep};
    if (location.hash === '#/a/' + kid) { S.dtab = 'eps'; render(); } else location.hash = '#/a/' + kid;
  },
  nsite(el) { S.nw.site = el.dataset.s; try { localStorage.setItem('nwsite', S.nw.site); } catch (e) {} S.nw.limit = 24; S.nw.tries = 0; paintNew(); nwEnsure(); },
  nfilt() { S.nw.open = !S.nw.open; paintNew(); },
  nhide() { S.nw.f.hide = !S.nw.f.hide; S.nw.limit = 24; S.nw.tries = 0; paintNew(); const D = nwD(); if (D.more && nwVisible(D).length < 12) nwLoad(true); },
  nreset() { Object.assign(S.nw.f, {year: '', type: '', genre: '', sort: 'def'}); S.nw.limit = 24; paintNew(); },
  nrefresh() { S.nw.tries = 0; nwLoad(false, true); },
  nmore() { S.nw.tries = 0; nwLoad(true); },
  nlim() { S.nw.limit = (S.nw.limit || 24) + 24; paintNew(); },
  async bopen(el) {
    const x = nwD().items[Number(el.dataset.i)]; if (!x) return;
    const srcs = x.srcs || [{url: x.url, title: x.title}];
    await openAnime(srcs[0].url, x.title, nwSite() !== 'mix', srcs);  // обрано сайт — дивимось саме його, запис бібліотеки не змінюємо
  },
  libtab(el) { S.libTab = el.dataset.t; render(); },
  vsort(el) { S.view.sort = el.dataset.t; saveView(); render(); },
  vongoing() { S.view.ongoing = !S.view.ongoing; saveView(); render(); },
  async archive() {
    const id = route().id, it = item(id); if (!it) return;
    try { await api('/api/archive', {id, value: !it.archived}); await loadLib(); toast(it.archived ? 'Повернуто в бібліотеку' : 'Додано до переглянутих', false, 'check'); if (!it.archived) {} } catch (e) { toast(e.message, true); }
  },
  lvtog() { S.lvOpen = !S.lvOpen; const g = $('#lvgrp'); if (g) g.classList.toggle('open', S.lvOpen); paintLvPrev(); },
  lgv(el) { S.view[el.dataset.k] = el.dataset.v; saveView(); paintLvPrev(); el.parentNode.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === el)); },
  lvcols(el) { S.view.cols = Number(el.dataset.v); saveView(); paintLvPrev(); document.querySelectorAll('#lvgrp .pch button').forEach(b => b.classList.toggle('on', Number(b.dataset.v) === S.view.cols)); },
  selmode() { S.selMode = !S.selMode; if (!S.selMode) S.sel.clear(); applySelMode(); },
  selall() { const it = item(route().id); S.sel = new Set(it.episodes.map(e => String(e.n))); paintSel(); },
  async delsel() {
    const id = route().id, it = item(id);
    const ns = it.episodes.filter(e => S.sel.has(String(e.n)) && e.downloaded).map(e => e.n);
    if (!ns.length) return;
    const list = ns.length > 8 ? ns.slice(0, 8).join(', ') + '…' : ns.join(', ');
    if (!(await askConfirm(`Видалити файли: ${ns.length}?`, `Серії ${list}. Відео буде видалено з телефону; самі серії лишаться в списку, їх можна дивитись онлайн чи скачати знову.`, 'Видалити ' + ns.length, true))) return;
    try {
      const j = await api('/api/delete_files', {id, episodes: ns});
      S.sel.clear(); S.selMode = false; await loadLib(); toast('Видалено файлів: ' + j.deleted);
    } catch (e) { toast(e.message, true); }
  },
  async watchsel() {
    const id = route().id, it = item(id);
    const w = it.episodes.filter(e => S.sel.has(String(e.n)));
    const v = !w.every(e => e.watched);
    try { await api('/api/watched_many', {id, episodes: w.map(e => e.n), value: v}); S.sel.clear(); S.selMode = false; await loadLib(); toast(v ? 'Позначено переглянутими' : 'Позначку знято', false, 'check'); }
    catch (e) { toast(e.message, true); }
  },
  sitetog() { S.siteOpen = !S.siteOpen; const g = $('#sitegrp'); if (g) g.classList.toggle('open', S.siteOpen); },
  sitemv(el) {
    const ord = siteOrder(), i = ord.indexOf(el.dataset.s), j = i + Number(el.dataset.d);
    if (i < 0 || j < 0 || j >= ord.length) return;
    [ord[i], ord[j]] = [ord[j], ord[i]]; S.set.site_order = ord; S.siteOpen = true;
    api('/api/settings', {site_order: ord}).catch(e => toast(e.message, true));
    render();
  },
  add() { S.lookup = null; S.addErr = ''; openSheet('add'); setTimeout(() => $('#addurl') && $('#addurl').focus(), 250); },
  async paste() {
    try { $('#addurl').value = await navigator.clipboard.readText(); }
    catch (e) { toast('Вставте посилання вручну: затисніть поле й оберіть «Вставити»'); }
  },
  async find() {
    if (S.find) return;
    const url = $('#addurl').value.trim();
    if (!url) { S.addErr = 'Вставте посилання.'; renderSheet(); return; }
    S.addErr = ''; S.lookup = null;
    const F = S.find = {t0: Date.now(), stage: 'Підключаюсь до сайту…', task: null, off: false};
    renderSheet(); $('#addurl').value = url;
    const tick = setInterval(() => { const el = $('#findtime'); if (el) el.textContent = Math.round((Date.now() - F.t0) / 1000) + ' с'; }, 500);
    try {
      const r = await api('/api/lookup', {url});
      F.task = r.task;
      for (;;) {
        if (F.off) return;
        const st = await api('/api/lookup_status?task=' + F.task);
        if (F.off) return;
        if (st.stage !== F.stage) { F.stage = st.stage; const el = $('#findstage'); if (el) el.textContent = st.stage; }
        if (st.done) {
          if (st.error) throw new Error(st.error);
          S.lookup = st.result; S.lookup.srcurl = url; break;
        }
        await new Promise(r => setTimeout(r, 350));
      }
    } catch (e) { if (!F.off) S.addErr = e.message; }
    clearInterval(tick);
    if (F.off) return;
    S.find = null; renderSheet(); $('#addurl').value = url;
  },
  findcancel() { cancelFind(false); },
  async addview() {  // переглянути аніме без додавання в бібліотеку (тимчасовий запис)
    const L = S.lookup; if (!L) return;
    const u = L.srcurl || ($('#addurl') && $('#addurl').value.trim()); if (!u) return;
    S.lookup = null;
    await openAnime(u, L.title, true, [{url: u, title: L.title}]);
  },
  async doadd() {
    const dub = $('#adddub') ? $('#adddub').value : null;
    S.busy.add('add'); renderSheet();
    try {
      const r = await api('/api/add', {token: S.lookup.token, dubbing: dub});
      closeSheet(); S.lookup = null;
      await loadLib(); toast('Додано в бібліотеку', false, 'book');
      location.hash = '#/a/' + r.id;
    } catch (e) { S.addErr = e.message; }
    S.busy.delete('add'); if (S.sheet) renderSheet();
  },
  async check() {
    const r = route(); const it = item(r.id);
    await guard('check:' + r.id, async () => {
      const j = await api('/api/check', {id: r.id});
      await loadLib();
      toast(j.new.length ? 'Нові серії: ' + j.new.join(', ') : 'Нових серій немає', false, j.new.length ? 'spark' : 'ok');
    });
  },
  async checkall() {
    if (S.checking || !S.items.length) return;
    S.checking = true; render();
    const found = [];
    for (const it of S.items.filter(x => !x.archived)) {
      try { const j = await api('/api/check', {id: it.id}); if (j.new.length) found.push(`${it.title}: ${j.new.join(', ')}`); }
      catch (e) { toast(it.title + ': ' + e.message, true); }
    }
    S.checking = false; await loadLib();
    toast(found.length ? found.join(' · ') : 'Нових серій немає', false, found.length ? 'spark' : 'ok');
  },
  more() { S.descOpen = !S.descOpen; render(); },
  poster(el) { openPoster(el.dataset.src); },
  cmmore() { cmLoad(route().id, true); },
  cmreload() { const id = route().id; CM[id] = null; cmLoad(id, false); },
  cmrep(el) { cmLoad(route().id, !!el.dataset.more, el.dataset.cid); },
  cmrepx(el) { const c = CM[route().id]; if (c && c.rep[el.dataset.cid]) { c.rep[el.dataset.cid].open = false; render(); } },
  sel(el) {
    const n = el.closest('[data-n]').dataset.n;
    S.sel.has(n) ? S.sel.delete(n) : S.sel.add(n);
    if (!S.sel.size) { S.selMode = false; applySelMode(); return; }  // нічого не вибрано — виходимо з режиму
    paintSel();
    bump(`[data-n="${CSS.escape(n)}"] .ck`);
  },
  selnone() { S.sel.clear(); S.selMode = false; applySelMode(); },  // «Зняти вибір» — заодно вимикає режим вибору
  selmissing() {
    const it = item(route().id);
    S.sel = new Set(it.episodes.filter(e => !e.downloaded).map(e => String(e.n))); S.selMode = S.sel.size > 0;
    applySelMode();
    if (!S.sel.size) toast('Усе вже скачано');
  },
  async download() {
    const id = route().id;
    try {
      const j = await api('/api/download', {id, episodes: [...S.sel].map(Number), quality: S.q});
      S.sel.clear(); S.selMode = false; render();
      toast(j.queued ? `У черзі: ${j.queued}` : 'Нічого нового для завантаження', false, j.queued ? 'download' : null);
      startPoll();
    } catch (e) { toast(e.message, true); }
  },
  jobs() { openSheet('jobs'); startPoll(); },
  async jobcancel(el) {
    if (!el.classList.contains('conf')) {  // перше натискання — лише просимо підтвердити
      el.classList.add('conf'); el.innerHTML = 'Скасувати?';
      setTimeout(() => { if (el.isConnected) { el.classList.remove('conf'); el.innerHTML = ico('close'); } }, 3000);
      return;
    }
    const row = el.closest('.job'); el.remove();
    if (row) { const se = row.querySelector('.st'); se.dataset.sig = ''; se.innerHTML = ico('spinner') + 'Скасовую…'; }
    try { await api('/api/cancel', {job: Number(el.dataset.job)}); } catch (e) { toast(e.message, true); }
    pollJobs();
  },
  bkmode(el) { S_bkMode = el.dataset.m; bkTick(false); },
  async bkexport() { try { await api('/api/settings', {view: S.view, client: {libTab: S.libTab, epv: S.epView, nwsite: nwSite(), pvol: volGet()}}); await api('/api/export', {mode: S_bkMode}); } catch (e) { toast(e.message, true); } bkTick(false); },
  async bkcancel() { try { await api('/api/backup_cancel', {}); } catch (e) {} bkTick(true); },
  async bkimport(el) {
    if (!(await askConfirm('Відновити зі збереження?', 'Бібліотека буде доповнена, налаштування з файлу застосуються.', 'Відновити'))) return;
    try { await api('/api/import', {rel: el.dataset.r}); } catch (e) { toast(e.message, true); } bkTick(false);
  },
  async bkdel(el) {
    if (!(await askConfirm('Видалити файл збереження?', 'Файл буде видалено з пристрою. Анімe й налаштування в програмі лишаться.', 'Видалити', true))) return;
    try { await api('/api/backup_delete', {rel: el.dataset.r}); } catch (e) { toast(e.message, true); } bkTick(true);
  },
  async bkdl() {
    try { const r = await api('/api/import_dl', {}); startPoll(); toast('Поставлено в чергу: ' + r.queued, false, 'download'); } catch (e) { toast(e.message, true); } bkTick(true);
  },
  async bkshare(el) {
    try { const r = await api('/api/backup_share', {rel: el.dataset.r}); if (!r.shared) toast('Не вдалося відкрити «Поділитись»', true); } catch (e) { toast(e.message, true); }
  },
  skmode(el) { setOpt('skip_mode', el.dataset.m); render(); },
  volmax(el) { setOpt('vol_max', Number(el.dataset.m)); if (volMax() < 200 && volGet() > 100) volApply(100); render(); volPaint(); },
  jgtog(el, ev) {
    if (ev && ev.target.closest('.btn, .jgp')) return;
    const g = el.closest('.jg'), k = g.dataset.k, now = g.classList.contains('open');
    JG_OPEN[k] = !now; g.classList.toggle('open', !now);
  },
  jgopen(el) {  // обкладинка в черзі відкриває аніме
    const k = el.closest('.jg').dataset.k.replace(/~x$/, '');
    if (!item(k)) return;
    S.skipOut = true; closeSheet(); location.hash = '#/a/' + k;
  },
  async jgresume(el) {
    const k = el.dataset.k;
    if (!(await askConfirm('Перезапустити завантаження?', 'Скасовані серії цього аніме буде поставлено в чергу знову.', 'Перезапустити'))) return;
    try { const r = await api('/api/jobs_retry', {kid: k}); startPoll(); toast('Поставлено в чергу: ' + r.queued, false, 'download'); } catch (e) { toast(e.message, true); }
    pollJobs();
  },
  async jgpause(el) {
    const k = el.dataset.k, paused = S.jobs.some(j => j.kid === k && j.kpaused);
    try { await api('/api/dl_pause_kid', {kid: k, on: !paused}); await pollJobs(); toast(paused ? 'Продовжено' : 'Призупинено', false, paused ? 'check' : 'pause'); } catch (e) { toast(e.message, true); }
  },
  async jgcancel(el) {
    const k = el.dataset.k;
    if (!JG_CONF[k]) { JG_CONF[k] = setTimeout(() => { delete JG_CONF[k]; updateJobs(); }, 3500); updateJobs(); return; }  // перше натискання — просимо підтвердити
    clearTimeout(JG_CONF[k]); delete JG_CONF[k];
    try { await api('/api/cancel', {kid: k}); } catch (e) { toast(e.message, true); }
    pollJobs();
  },
  async dlpause() { try { await api('/api/dl_pause', {on: !S.dlPaused}); S.dlPaused = !S.dlPaused; await pollJobs(); toast(S.dlPaused ? 'Завантаження призупинено' : 'Завантаження продовжено', false, S.dlPaused ? 'pause' : 'check'); } catch (e) { toast(e.message, true); } },
  async canceljobs() {
    if (!JG_CONF['*']) { JG_CONF['*'] = setTimeout(() => { delete JG_CONF['*']; updateJobs(); }, 3500); updateJobs(); return; }
    clearTimeout(JG_CONF['*']); delete JG_CONF['*'];
    document.querySelectorAll('#joblist .job .h .btn').forEach(b => b.remove()); try { await api('/api/cancel', {}); } catch (e) { toast(e.message, true); } pollJobs(); },
  async clearjobs() { await api('/api/jobs_clear', {}); pollJobs(); },
  async watch(el) {
    const id = route().id, it = item(id), n = Number(el.closest('[data-n]').dataset.n);
    const e = it.episodes.find(x => x.n === n); const v = !e.watched;
    e.watched = v; if (v) e.new = false; recount(it); render();
    bump(`[data-n="${CSS.escape(String(n))}"] .eye[data-act=watch]`);
    try { await api('/api/watched', {id, n, value: v}); } catch (er) { toast(er.message, true); loadLib(); }
  },
  async upto() {
    const id = route().id, it = item(id);
    const nx = nextEp(it);
    const v = await askNumber('Подивився до серії (включно)', nx ? Number(nx.n) - 1 : '');
    if (v === null || v === '' || isNaN(Number(v))) return;
    try { await api('/api/watched', {id, upto: Number(v)}); await loadLib(); } catch (e) { toast(e.message, true); }
  },
  async delfile(el) {
    const id = route().id, n = Number(el.closest('[data-n]').dataset.n);
    if (!(await askConfirm(`Видалити файл серії ${n}?`, 'Відео буде видалено з телефону. Серія лишиться в списку (її можна дивитись онлайн чи завантажити знову).', 'Видалити', true))) return;
    try { await api('/api/delete_file', {id, n}); await loadLib(); toast('Файл видалено'); } catch (e) { toast(e.message, true); }
  },
  async setdub(el) {
    const id = route().id, dub = el.dataset.dub, it = item(id);
    if (!it || dub === it.dubbing || S.busy.has('sw')) return;
    S.busy.add('sw');
    document.querySelectorAll('.srcs, .dubs').forEach(c => c.classList.add('dis'));
    document.querySelectorAll('.dubs .dchip').forEach(c => c.style.opacity = c === el ? 1 : .5);
    try {
      const r = await api('/api/set_dub', {id, dubbing: dub});
      if (r.task) await waitTask(r.task);
      S.sel.clear(); S.selMode = false; fx(); S.chipFix = true; await loadLib();
      const n = item(id);
      toast('Озвучка: ' + dub + (n && n.episodes.length ? '' : ' — серій поки немає'), false, 'check');
    } catch (e) { toast(e.message, true); }
    finally { S.busy.delete('sw'); S.chipFix = true; render(); }
  },
  csel(el) { const c = el.closest('.card[data-id]'), ids = tileIds(c), all = ids.every(id => S.libSel.has(id)); ids.forEach(id => all ? S.libSel.delete(id) : S.libSel.add(id)); render(); },
  libclr() { S.libSel.clear(); render(); },
  async libarc() {
    const ids = [...S.libSel].filter(id => item(id)); if (!ids.length) return;
    const arc = ids.some(id => !item(id).archived) && !arcTab();
    try { for (const id of ids) { await api('/api/archive', {id, value: arc}); item(id).archived = arc; } S.libSel.clear(); render(); toast(arc ? 'У переглянутих: ' + ids.length : 'Повернуто в бібліотеку: ' + ids.length, false, 'check'); loadLib().catch(() => {}); } catch (e) { toast(e.message, true); }
  },
  async libdel() {
    const ids = [...S.libSel].filter(id => item(id)); if (!ids.length) return;
    const names = ids.map(id => '• ' + lname(item(id))).slice(0, 5).join('\n');
    if (!(await askConfirm('Видалити з бібліотеки?', `Буде видалено записи, обкладинки й усі завантажені серії з пристрою (${ids.length}). Це не можна скасувати.\n${names}`, 'Видалити ' + ids.length, true))) return;
    try { for (const id of ids) await api('/api/remove', {id}); S.libSel.clear(); await loadLib(); if (!S.items.length) S.reorder = false; render(); toast('Видалено: ' + ids.length); } catch (e) { toast(e.message, true); await loadLib(); }
  },
  async cmshare(el) {
    const it = item(el.dataset.id); closeSheet(); if (!it) return;
    let ok = false;
    try { await navigator.clipboard.writeText(it.url); ok = true; } catch (e) {
      try { const t = document.createElement('textarea'); t.value = it.url; t.style.cssText = 'position:fixed;opacity:0'; document.body.appendChild(t); t.select(); ok = document.execCommand('copy'); t.remove(); } catch (e2) {}
    }
    toast(ok ? 'Посилання скопійовано' : 'Не вдалося скопіювати: ' + it.url, !ok, ok ? 'check' : null);
  },
  async cmrename(el) {
    const id = el.dataset.id, it = item(id); closeSheet(); if (!it) return;
    const v = await askText('Перейменувати', lname(it), 'Назва зміниться лише в бібліотеці — в самому аніме лишиться оригінальна. Порожнє поле поверне оригінальну назву.');
    if (v === null) return;
    try { const r = await api('/api/rename', {id, name: v}); it.alias = r.alias || ''; render(); toast('Назву змінено', false, 'check'); loadLib().catch(() => {}); } catch (e) { toast(e.message, true); }
  },
  async cmdel(el) {
    const id = el.dataset.id, it = item(id); closeSheet(); if (!it) return;
    if (!(await askConfirm('Видалити з бібліотеки?', `«${lname(it)}»: буде видалено запис, обкладинку й усі завантажені серії з пристрою. Це не можна скасувати.`, 'Видалити', true))) return;
    try { await api('/api/remove', {id}); S.libSel.delete(id); await loadLib(); if (!S.items.length) S.reorder = false; render(); toast('Видалено'); } catch (e) { toast(e.message, true); await loadLib(); }
  },
  async cmdel(el) {
    const id = el.dataset.id, it = item(id); closeSheet(); if (!it) return;
    await new Promise(r => setTimeout(r, 220));
    if (!(await askConfirm('Видалити з бібліотеки?', `«${lname(it)}» буде видалено разом з обкладинкою й усіма завантаженими серіями на пристрої. Це не можна скасувати.`, 'Видалити', true))) return;
    try { await api('/api/remove', {id}); S.libSel.delete(id); await loadLib(); if (!S.items.length) S.reorder = false; render(); toast('Видалено', false, 'check'); } catch (e) { toast(e.message, true); await loadLib(); }
  },
  async cmarc(el) {
    const id = el.dataset.id, it = item(id); closeSheet(); if (!it) return;
    try { await api('/api/archive', {id, value: !it.archived}); it.archived = !it.archived; render(); toast(it.archived ? 'У переглянутих' : 'Повернуто в бібліотеку', false, 'check'); loadLib().catch(() => {}); } catch (e) { toast(e.message, true); }
  },
  async rmarc(el) {
    const id = el.dataset.id, it = item(id); if (!it) return;
    try { await api('/api/archive', {id, value: !it.archived}); it.archived = !it.archived; render(); toast(it.archived ? 'У переглянутих' : 'Повернуто в бібліотеку', false, 'check'); loadLib(); } catch (e) { toast(e.message, true); }
  },
  async rmitem(el) {
    const id = el.dataset.id, it = item(id);
    if (!(await askConfirm('Видалити з бібліотеки?', `«${it ? it.title : ''}»: буде видалено запис, обкладинку й усі завантажені серії (усіх озвучок) з пристрою. Це не можна скасувати.`, 'Видалити', true))) return;
    try { await api('/api/remove', {id}); await loadLib(); if (!S.items.length) S.reorder = false; render(); toast('Видалено'); } catch (e) { toast(e.message, true); }
  },
  async remove() {
    const id = route().id;
    if (!(await askConfirm('Видалити з бібліотеки?', 'Буде видалено запис, обкладинку й усі завантажені серії (усіх озвучок) з пристрою, разом з недокачаним. Це не можна скасувати.', 'Видалити', true))) return;
    try { await api('/api/remove', {id}); await loadLib(); location.hash = '#/'; toast('Видалено'); } catch (e) { toast(e.message, true); }
  },
  'dlg-yes'() {
    const s = S.sheet; const v = s && s.kind === 'number' ? $('#numin').value : s && s.kind === 'text' ? $('#txtin').value : true;
    const f = S.dlg; S.dlg = null; closeSheet(); if (f) f(v);
  },
  'dlg-no'() { const num = S.sheet && (S.sheet.kind === 'number' || S.sheet.kind === 'text'); const f = S.dlg; S.dlg = null; closeSheet(); if (f) f(num ? null : false); },
  backdrop(el, ev) { if (ev.target === el) A['dlg-no'](); },
  play(el) {
    const id = route().id; const n = Number(el.closest('[data-n]').dataset.n);
    play(id, n);
  },
  playnext() {
    const id = route().id, it = item(id); const nx = nextEp(it) || it.episodes[0];
    if (nx) play(id, nx.n);
  },
  pclose() { closePlayer(); },
  epview(el) { S.epView = el.dataset.v; try { localStorage.setItem('epv', S.epView); } catch (e) {} render(); },
  reorder() { keepAnchor(() => { S.reorder = !S.reorder; if (!S.reorder) S.libSel.clear(); render(); }); },
  mv(el) { moveItem(el.dataset.id, el.dataset.d); },
  actog() { S.acOpen = !S.acOpen; const g = $('#acgrp'); if (g) g.classList.toggle('open', S.acOpen); },
  achours(el) {
    const h = Number(el.dataset.h); setOpt('auto_check_hours', h);
    document.querySelectorAll('.pch button').forEach(b => b.classList.toggle('on', Number(b.dataset.h) === h));
    const sb = $('#acsub'); if (sb) sb.textContent = acSub(); setTimeout(pollAC, 600);
  },
  grptog(el) { const g = document.getElementById(el.dataset.id); if (!g) return; S[el.dataset.g] = !g.classList.contains('open'); g.classList.toggle('open', S[el.dataset.g]); },
  istog(el, ev) { if (ev && ev.target.closest('.sw')) return; S.isOpen = !$('#isg').classList.contains('open'); $('#isg').classList.toggle('open', S.isOpen); },
  sktog(el, ev) { if (ev && ev.target.closest('.sw')) return; S.skOpen = !$('#skg').classList.contains('open'); $('#skg').classList.toggle('open', S.skOpen); },
  ptog() { S.pOpen = !S.pOpen; const g = $('#pgrp'); if (g) g.classList.toggle('open', S.pOpen); },
  pprev() { stepPlayer(-1); },
  pnxnow() { nxNow(); },
  pnxcancel() { nxCancel(); },
  pnext() { stepPlayer(1); },
};
function recount(it) {
  it.stats.watched = it.episodes.filter(e => e.watched).length;
  it.stats.new = it.episodes.filter(e => e.new).length;
}

const LP = {t: 0, timer: null, x: 0, y: 0};
const DR = {lock: 0, el: null, on: false, timer: null, sx: 0, sy: 0, gx: 0, gy: 0, px: 0, py: 0, auto: 0, id: '', pid: null, stack: [], clones: [], aux: null, corner: false, w: 0};
function drZones(upd) {
  const ic = ico(drToArc() ? 'check' : 'refresh');
  let r = $('#dzr'), l = $('#dzl');
  if (!r) { r = document.createElement('div'); r.id = 'dzr'; r.className = 'dz r'; document.body.appendChild(r); }
  if (!l) { l = document.createElement('div'); l.id = 'dzl'; l.className = 'dz l'; l.innerHTML = `<i class="ring"></i>${ico('trash')}`; document.body.appendChild(l); }
  r.innerHTML = `<i class="ring"></i>${ic}`;
}
function zoneC(side) { const e = $(side === 'l' ? '#dzl' : '#dzr'); if (!e) return null; const b = e.getBoundingClientRect(); return {x: b.left + b.width / 2, y: b.top + b.height / 2, e}; }
function inCorner() {
  let best = false;
  for (const sd of ['r', 'l']) {
    const z = zoneC(sd); if (!z) continue;
    const d = Math.hypot(z.x - DR.px, z.y - DR.py);
    z.e.style.setProperty('--p', Math.max(0, Math.min(1, 1 - (d - 50) / 170)).toFixed(2));
    if (d < 62) best = sd;
  }
  return best;
}
function drStart(c) {
  if (!c) return;
  DR.el = c; DR.on = true; DR.stack = []; DR.clones = []; DR.aux = null; DR.corner = false;
  c.classList.add('drag'); document.body.classList.add('dragging');
  const r = c.getBoundingClientRect(); DR.gx = DR.px - r.left; DR.gy = DR.py - r.top; DR.w = r.width;
  drZones();
  c.style.transition = 'none'; drMove();
  if (tileSel(c)) document.querySelectorAll('#root .card[data-id]').forEach(o => { if (o !== c && tileSel(o)) drToggleStack(o); });  // вибрані їдуть разом
  clearInterval(DR.auto);
  DR.auto = setInterval(() => {
    if (!DR.on || DR.corner) return;
    const y = DR.py, H = window.innerHeight;
    if (y < 110) { window.scrollBy(0, -(110 - y) / 5 - 3); drMove(); }
    else if (y > H - 130) { window.scrollBy(0, (y - (H - 130)) / 5 + 3); drMove(); }
  }, 16);
}
function drToArc() {  // у куті «переглянуті»; у вкладці переглянутих — навпаки повертаємо в бібліотеку
  if (arcTab()) return false;
  const ids = [DR.el, ...DR.stack].flatMap(tileIds);
  return !ids.every(id => { const it = item(id); return it && it.archived; });
}
function drToggleStack(card) {  // другий палець торкнувся картки: додаємо її в стос (повторний дотик — прибирає)
  if (!DR.on || !card || card === DR.el) return;
  const k = DR.stack.indexOf(card);
  if (k >= 0) {
    DR.stack.splice(k, 1); const cl = DR.clones.splice(k, 1)[0]; if (cl) cl.remove();
    card.classList.remove('stacked');
  } else {
    const cl = card.cloneNode(true); cl.classList.remove('drag'); cl.classList.add('stk'); cl.style.width = DR.w + 'px';
    cl.querySelectorAll('[data-act]').forEach(b => b.remove());
    document.body.appendChild(cl); card.classList.add('stacked');
    DR.stack.push(card); DR.clones.push(cl);
    if (navigator.vibrate) try { navigator.vibrate(10); } catch (e) {}
  }
  DR.el.dataset.cnt = DR.stack.length ? String(DR.stack.length + 1) : '';
  if (!DR.stack.length) DR.el.removeAttribute('data-cnt');
  drZones(true);
  drMove();
}
function drClearMerge() { clearTimeout(DR.mt); DR.mc = DR.mg = null; document.querySelectorAll('.card.mrg').forEach(x => x.classList.remove('mrg')); if (DR.el) DR.el.classList.remove('mrgd'); }
async function mergeDrop(tgt, ids) {
  const t = item(tgt.dataset.id); if (!t) return;
  try {
    if (tgt.dataset.gid) await api('/api/group', {ids, gid: tgt.dataset.gid});
    else await api('/api/group', {ids: [...ids, t.id], name: ''});
    ids.forEach(id => S.libSel.delete(id));
    toast('Згруповано', false, 'check');
  } catch (e) { toast(e.message, true); }
  render(); loadLib().catch(() => {});
}
function drMove() {
  const c = DR.el; if (!c) return;
  const cards = [...document.querySelectorAll('#root .card[data-id]')].filter(o => !o.classList.contains('stacked'));
  const over = Date.now() < DR.lock || DR.corner ? null : cards.find(o => { if (o === c) return false; const r = o.getBoundingClientRect(); return DR.px >= r.left && DR.px <= r.right && DR.py >= r.top && DR.py <= r.bottom; });
  let inner = false;
  const canMerge = route().name !== 'group';
  const mdw = over && over.dataset.gid ? 220 : 380;
  if (over && canMerge) { const r = over.getBoundingClientRect(), cx = (DR.px - r.left) / r.width, cy = (DR.py - r.top) / r.height, z = over.dataset.gid ? .1 : .22, zy = over.dataset.gid ? .08 : .18; inner = cx > z && cx < 1 - z && cy > zy && cy < 1 - zy; }
  if (over && inner) {  // середина чужої картки: затримка → «згрупувати» (звичайне переміщення — по краях)
    if (DR.mc !== over) { drClearMerge(); DR.mc = over; DR.mtime = Date.now(); clearTimeout(DR.mt); DR.mt = setTimeout(drMove, mdw + 40); }
    else if (!DR.mg && Date.now() - DR.mtime >= mdw) { DR.mg = over; over.classList.add('mrg'); c.classList.add('mrgd'); if (navigator.vibrate) try { navigator.vibrate(14); } catch (e) {} }
  } else if (DR.mc && !(over && DR.mc === over)) drClearMerge();
  if (over !== DR.sw) { DR.sw = over; DR.stime = Date.now(); clearTimeout(DR.st); if (over) DR.st = setTimeout(drMove, 280); }
  const hit = over && !inner && Date.now() - DR.stime >= 260 ? over : null;
  if (hit) DR.sw = null;
  if (hit) {
    const rects = new Map(cards.map(o => [o, o.getBoundingClientRect()]));
    const after = c.compareDocumentPosition(hit) & Node.DOCUMENT_POSITION_FOLLOWING;
    if (after) hit.after(c); else hit.before(c);
    DR.lock = Date.now() + 280;
    cards.forEach(o => {
      if (o === c) return;
      const a = rects.get(o), b = o.getBoundingClientRect(), dx = a.left - b.left, dy = a.top - b.top;
      if (dx || dy) { o.style.transition = 'none'; o.style.transform = `translate(${dx}px,${dy}px)`;
        requestAnimationFrame(() => requestAnimationFrame(() => { o.style.transition = 'transform .25s cubic-bezier(.2,.8,.2,1)'; o.style.transform = ''; })); }
    });
  }
  c.style.transform = ''; const n = c.getBoundingClientRect();
  c.style.transform = `translate(${DR.px - DR.gx - n.left}px,${DR.py - DR.gy - n.top}px) scale(1.05)`;
  const n2 = c.getBoundingClientRect();
  DR.clones.forEach((cl, i) => { cl.style.transform = `translate(${n2.left + (i + 1) * 7}px,${n2.top + (i + 1) * 7}px) rotate(${i % 2 ? -3 : 3}deg)`; });
  const inC = inCorner();
  if (inC !== DR.corner) { DR.corner = inC; ['r', 'l'].forEach(sd => { const z = $(sd === 'l' ? '#dzl' : '#dzr'); if (z) z.classList.toggle('on', inC === sd); }); if (inC && navigator.vibrate) try { navigator.vibrate(8); } catch (e) {} }
}
async function cornerDrop(ids, side, c) {
  const back = () => { if (c) { c.style.transition = 'transform .25s, opacity .25s'; c.style.transform = ''; c.style.opacity = ''; setTimeout(() => { c.classList.remove('drag'); c.style.transition = ''; }, 260); } };
  if (side === 'l') {
    const its = ids.filter(id => item(id)), names = its.map(id => '• ' + lname(item(id)));
    const ok = its.length && await askConfirm('Видалити з бібліотеки?', `Буде видалено записи, обкладинки й усі завантажені серії з пристрою (${its.length}). Це не можна скасувати.\n` + names.slice(0, 5).join('\n') + (names.length > 5 ? '\n…' : ''), 'Видалити' + (its.length > 1 ? ' ' + its.length : ''), true);
    if (!ok) { back(); return; }
    try { for (const id of its) { await api('/api/remove', {id}); S.libSel.delete(id); } await loadLib(); if (!S.items.length) S.reorder = false; render(); back(); toast('Видалено: ' + its.length, false, 'check'); }
    catch (e) { toast(e.message, true); await loadLib(); render(); }
    return;
  }
  const arc = ids.some(id => { const it = item(id); return it && !it.archived; }) && !arcTab();
  const names = ids.map(id => item(id) ? lname(item(id)) : '').filter(Boolean);
  const list = names.slice(0, 4).map(t => '• ' + t).join('\n') + (names.length > 4 ? '\n…' : '');
  const ok = await askConfirm(arc ? 'Перенести у переглянуті?' : 'Повернути в бібліотеку?',
    (arc ? 'Ці аніме зникнуть з бібліотеки й перестануть перевірятись на нові серії (нічого не видаляється):\n' : 'Ці аніме знову з\'являться в бібліотеці:\n') + list, arc ? 'Перенести' : 'Повернути', false);
  if (ok) {
    try {
      for (const id of ids) { await api('/api/archive', {id, value: arc}); const it = item(id); if (it) it.archived = arc; }
      ids.forEach(id => S.libSel.delete(id));
      toast(arc ? 'У переглянутих: ' + ids.length : 'Повернуто в бібліотеку: ' + ids.length, false, 'check');
    } catch (e) { toast(e.message, true); }
  }
  render(); back(); loadLib().catch(() => {});
}
function drEnd() {
  clearInterval(DR.auto); clearTimeout(DR.timer); DR.timer = null;
  const c = DR.el, stack = DR.stack, corner = DR.corner, mg = DR.mg;
  clearTimeout(DR.mt); clearTimeout(DR.st); DR.sw = null;
  DR.on = false; DR.el = null; DR.stack = []; DR.aux = null; DR.corner = false; DR.pid = null;
  document.body.classList.remove('dragging'); LP.t = Date.now();
  DR.clones.forEach(x => x.remove()); DR.clones = [];
  ['#dzr', '#dzl'].forEach(sel => { const z = $(sel); if (!z) return; z.removeAttribute('id'); if (corner && z.classList.contains('on')) { z.classList.add('sink'); setTimeout(() => z.remove(), 450); } else z.remove(); });
  if (!c) return;
  c.removeAttribute('data-cnt');
  let ref = c; stack.forEach(x => { ref.after(x); x.classList.remove('stacked'); ref = x; });  // стос лягає поруч, у тому ж порядку
  if (corner) {  // картка «залітає» в кут і зменшується
    const zz = zoneC(corner), rr = c.getBoundingClientRect();
    if (zz) {
      const m = /translate\(([-\d.]+)px,\s*([-\d.]+)px\)/.exec(c.style.transform || '');
      const a = m ? +m[1] : 0, b = m ? +m[2] : 0;
      c.style.transition = 'transform .3s cubic-bezier(.5,0,.8,.4), opacity .3s';
      c.style.transform = `translate(${a + zz.x - (rr.left + rr.width / 2)}px,${b + zz.y - (rr.top + rr.height / 2)}px) scale(.1)`; c.style.opacity = '0';
    }
  } else {
    c.style.transition = 'transform .2s'; c.style.transform = '';
    setTimeout(() => { c.classList.remove('drag'); c.style.transition = ''; }, 220);
  }
  document.querySelectorAll('.card.mrg').forEach(x => x.classList.remove('mrg')); c.classList.remove('mrgd'); DR.mc = DR.mg = null;
  if (mg && !corner) { mergeDrop(mg, [c, ...stack].flatMap(tileIds)); return; }
  if (corner) { cornerDrop([c, ...stack].flatMap(tileIds), corner, c); return; }
  const domIds = []; document.querySelectorAll('#root .card[data-id]').forEach(x => { if (x.dataset.gid) groupMembers(x.dataset.gid).forEach(y => domIds.push(y.id)); else domIds.push(x.dataset.id); });
  const slots = S.items.map((x, i) => domIds.includes(x.id) ? i : -1).filter(i => i >= 0), byId = new Map(S.items.map(x => [x.id, x]));
  domIds.forEach((id, k) => { S.items[slots[k]] = byId.get(id); });
  clearTimeout(moveItem.t);
  moveItem.t = setTimeout(() => api('/api/reorder', {ids: S.items.map(x => x.id)}).catch(e => toast(e.message, true)), 300);
}
document.addEventListener('pointerdown', ev => {
  if (['home', 'group'].includes(route().name) && !S.cur && !S.sheet && !(ev.target.closest && ev.target.closest('input,textarea'))) document.body.classList.add('pressing');
  if (!['home', 'group'].includes(route().name) || S.cur || S.sheet) return;
  const c = ev.target.closest && ev.target.closest('.card[data-id]');
  if (DR.on) {  // другий палець: дотик по картці додає її в стос, рух — прокручує сторінку
    if (ev.pointerId !== DR.pid && !DR.aux) DR.aux = {pid: ev.pointerId, y: ev.clientY, y0: ev.clientY, card: c, moved: false};
    return;
  }
  const gap = !c && ev.target.classList && (ev.target.classList.contains('grid') || ev.target.id === 'root' || ev.target.classList.contains('empty'));  // порожнє місце між картками
  if ((!c && !gap) || (c && ev.target.closest('.arw'))) return;
  DR.pid = ev.pointerId;
  DR.sx = DR.px = ev.clientX; DR.sy = DR.py = ev.clientY; DR.id = c ? c.dataset.id : ''; clearTimeout(DR.timer);
  if (gap) {
    if (S.reorder) return;
    DR.timer = setTimeout(() => {  // довге натискання між картками — режим редагування
      DR.timer = null; S.reorder = true; LP.t = Date.now(); try { getSelection().removeAllRanges(); } catch (e) {} if (navigator.vibrate) try { navigator.vibrate(15); } catch (e) {}
      keepAnchor(render);
    }, 450);
    return;
  }
  DR.timer = setTimeout(() => {
    DR.timer = null;
    if (S.reorder) drStart(c);  // у режимі редагування — перетягування
    else if (c.dataset.gid) { LP.t = Date.now(); try { getSelection().removeAllRanges(); } catch (e) {} if (navigator.vibrate) try { navigator.vibrate(15); } catch (e) {} openSheet('cmenu', {gid: c.dataset.gid, rect: c.getBoundingClientRect()}); }
    else { LP.t = Date.now(); try { getSelection().removeAllRanges(); } catch (e) {} if (navigator.vibrate) try { navigator.vibrate(15); } catch (e) {} openSheet('cmenu', {id: c.dataset.id, rect: c.getBoundingClientRect()}); }  // довге натискання на картку — меню
  }, S.reorder ? 200 : 480);
});
document.addEventListener('pointermove', ev => {
  if (DR.aux && ev.pointerId === DR.aux.pid) {
    if (Math.abs(ev.clientY - DR.aux.y0) > 9) DR.aux.moved = true;
    if (DR.aux.moved) { window.scrollBy(0, DR.aux.y - ev.clientY); DR.aux.y = ev.clientY; drMove(); }
    return;
  }
  if (DR.pid !== null && ev.pointerId !== DR.pid) return;
  DR.px = ev.clientX; DR.py = ev.clientY;
  if (DR.timer && Math.hypot(ev.clientX - DR.sx, ev.clientY - DR.sy) > 9) { clearTimeout(DR.timer); DR.timer = null; }
  if (DR.on) drMove();
});
['pointerup', 'pointercancel'].forEach(t => document.addEventListener(t, ev => {
  if (DR.aux && ev.pointerId === DR.aux.pid) { const a = DR.aux; DR.aux = null; if (!a.moved && a.card && DR.on && t === 'pointerup') drToggleStack(a.card); return; }
  if (DR.pid !== null && ev.pointerId !== DR.pid) return;
  if (DR.on) drEnd(); else { clearTimeout(DR.timer); DR.timer = null; DR.pid = null; }
}, true));
document.addEventListener('touchmove', ev => { if (DR.on) ev.preventDefault(); }, {passive: false});
document.addEventListener('contextmenu', ev => { if (ev.target.closest && ev.target.closest('.card[data-id]')) ev.preventDefault(); });
document.addEventListener('submit', ev => { if (ev.target.dataset && ev.target.dataset.form === 'search') { ev.preventDefault(); A.dosearch(); } });
const unpress = () => setTimeout(() => document.body.classList.remove('pressing'), 60);
document.addEventListener('pointerup', unpress, true); document.addEventListener('pointercancel', unpress, true); document.addEventListener('touchend', unpress, true);
document.addEventListener('selectstart', ev => { if (document.body.classList.contains('pressing')) ev.preventDefault(); }, true);
document.addEventListener('contextmenu', ev => { if (document.body.classList.contains('pressing')) ev.preventDefault(); }, true);
/* довге натискання: на кнопках з data-lp (дія з A) і на картках Пошуку/Новинок (меню картки) */
const GP = {t: 0, timer: null, fired: false, x: 0, y: 0};
function gpHaptic() { try { getSelection().removeAllRanges(); } catch (e) {} if (navigator.vibrate) try { navigator.vibrate(15); } catch (e) {} }
function cardRes(c) {
  if (c.dataset.act === 'openres') { const v = S.search.vis && S.search.vis[Number(c.dataset.g)]; if (!v) return null; return {title: S.search.sel.size ? v.its[0].title : v.g.title, srcs: v.g.items.map(x => ({url: x.url, title: x.title})), explicit: S.search.sel.size > 0}; }
  if (c.dataset.act === 'bopen') { const x = nwD().items[Number(c.dataset.i)]; if (!x) return null; return {title: x.title, srcs: x.srcs || [{url: x.url, title: x.title}], explicit: nwSite() !== 'mix'}; }
  return null;
}
document.addEventListener('pointerdown', ev => {
  const t = ev.target; if (!t || !t.closest) return;
  const rn = route().name;
  const card = (rn === 'search' || rn === 'new') && !S.sheet && !S.cur ? t.closest('.card.sr') : null;
  const el = card ? null : t.closest('[data-lp]');
  if (!card && !el) return;
  clearTimeout(GP.timer); GP.x = ev.clientX; GP.y = ev.clientY; GP.fired = false;
  if (card) document.body.classList.add('pressing');
  GP.timer = setTimeout(() => {
    GP.timer = null;
    if (card) { const r = cardRes(card); if (!r) return; GP.fired = true; GP.t = Date.now(); LP.t = GP.t; gpHaptic(); openSheet('cmenu', {res: r, rect: card.getBoundingClientRect()}); }
    else { const f = A[el.dataset.lp]; if (!f) return; GP.fired = true; GP.t = Date.now(); gpHaptic(); f(el); }
  }, card ? 480 : 520);
}, true);
document.addEventListener('pointermove', ev => { if (GP.timer && Math.hypot(ev.clientX - GP.x, ev.clientY - GP.y) > 9) { clearTimeout(GP.timer); GP.timer = null; } }, true);
['pointerup', 'pointercancel'].forEach(t => document.addEventListener(t, () => { clearTimeout(GP.timer); GP.timer = null; if (GP.fired) { GP.t = Date.now(); GP.fired = false; } }, true));
addEventListener('scroll', () => { clearTimeout(GP.timer); GP.timer = null; }, true);
addEventListener('click', ev => { if (Date.now() - GP.t < 400) { ev.preventDefault(); ev.stopPropagation(); ev.stopImmediatePropagation(); } }, true);  // відпускання після довгого натискання не вважаємо кліком
function lpCancel() { clearTimeout(LP.timer); LP.timer = null; }
document.addEventListener('pointerdown', ev => {
  if (route().name !== 'detail' || S.cur) return;
  const row = ev.target.closest('.ep,.ec'); if (!row || ev.target.closest('button,a,select')) return;
  LP.x = ev.clientX; LP.y = ev.clientY; lpCancel();
  LP.timer = setTimeout(() => {
    LP.timer = null; LP.t = Date.now();
    const n = row.dataset.n; if (n == null) return;
    S.selMode = true; S.sel.add(n); if (navigator.vibrate) try { navigator.vibrate(15); } catch (e) {}
    applySelMode();
  }, 480);
});
document.addEventListener('pointermove', ev => { if (LP.timer && Math.hypot(ev.clientX - LP.x, ev.clientY - LP.y) > 9) lpCancel(); });
['pointerup', 'pointercancel', 'scroll'].forEach(t => document.addEventListener(t, lpCancel, true));
document.addEventListener('contextmenu', ev => { if (ev.target.closest('.ep,.ec')) ev.preventDefault(); });
document.addEventListener('click', ev => {
  if (Date.now() - LP.t < 600 && !(ev.target.closest && ev.target.closest('.sheet button, .sheet input'))) { ev.preventDefault(); ev.stopPropagation(); return; }
  const el = ev.target.closest('[data-act]');
  if (!el) return;
  const f = A[el.dataset.act];
  if (!f) return;
  if (el.tagName === 'A') return;
  if ((el.dataset.act === 'sktog' || el.dataset.act === 'istog') && ev.target.closest('.sw')) return;
  ev.preventDefault();
  f(el, ev);
});
document.addEventListener('change', ev => {
  if (ev.target.dataset && ev.target.dataset.nf) {
    S.nw.f[ev.target.dataset.nf] = ev.target.value; S.nw.limit = 24; S.nw.tries = 0; const D = nwD(); paintNew();
    if (D.more && nwVisible(D).length < 12 && ev.target.dataset.nf !== 'sort') nwLoad(true);
    return;
  }
  if (ev.target.id === 'qsel') { S.q = ev.target.value; ls.set('q', S.q); }
  if (ev.target.id === 'mobsw') {
    const v = ev.target.checked; S.set.mobile_download = v;
    api('/api/settings', {mobile_download: v}).catch(e => { toast(e.message, true); });
  }
  if (ev.target.id === 'pauto') setOpt('auto_next', ev.target.checked);
  if (ev.target.id === 'skmaster') {
    const v = ev.target.checked; setOpt('auto_skip', v);
    const g = $('#skg'); if (g) { g.classList.toggle('open', v); S.skOpen = v; g.querySelectorAll('.row.sub').forEach(r => r.classList.toggle('dis', !v)); }
    return;
  }
  if (ev.target.id === 'ismaster') {
    const v = ev.target.checked; setOpt('island', v);
    const g = $('#isg'); if (g) { g.classList.toggle('open', v); S.isOpen = v; g.querySelectorAll('.row.sub').forEach(r => r.classList.toggle('dis', !v)); }
    return;
  }
  if (ev.target.id === 'vsel') { S.view.sort = ev.target.value; saveView(); render(); return; }
  if (ev.target.dataset && ev.target.dataset.lv) { S.view[ev.target.dataset.lv] = ev.target.checked; saveView(); paintLvPrev(); if (ev.target.dataset.lv === 'logo') document.querySelectorAll('.lgopt').forEach(e => e.classList.toggle('dis', !ev.target.checked)); return; }
  if (ev.target.dataset && ev.target.dataset.set) {
    setOpt(ev.target.dataset.set, ev.target.checked);
    if (ev.target.dataset.set === 'auto_check') { const sb = $('#acsub'); if (sb) sb.textContent = acSub(); setTimeout(pollAC, 600); }
    if (ev.target.dataset.set === 'blur_thumbs') document.querySelectorAll('.ec .th img').forEach(i => i.classList.toggle('bl', ev.target.checked));
    if (ev.target.dataset.set === 'auto_next') { const c = $('#pauto'); if (c) c.checked = ev.target.checked; }
    if (ev.target.dataset.set === 'ios_player') applyPlayerMode();
  }
});

/* ---------- плеєр ---------- */
const V = $('#vid');
let pSaveT = 0, pMarked = false;
const nativeHls = !!V.canPlayType('application/vnd.apple.mpegurl');
/* ---------- власна гучність (окрема від системної; до 200%) ---------- */
let AUD = null;  // Web Audio створюємо лише коли користувач змінив гучність (за 100% звук іде як є, найнадійніше для фону й PiP)
function volMax() { return S.set && S.set.vol_max >= 200 ? 200 : 100; }
function volGet() { let v = 100; try { v = parseInt(localStorage.getItem('pvol') || '100', 10); } catch (e) {} return Math.max(0, Math.min(volMax(), isNaN(v) ? 100 : v)); }
function volApply(p) {
  p = Math.max(0, Math.min(volMax(), p));
  try { localStorage.setItem('pvol', String(p)); } catch (e) {}
  if (!AUD && p === 100) return;
  try {
    if (!AUD) {
      const C = window.AudioContext || window.webkitAudioContext; if (!C) return;
      const ctx = new C(), src = ctx.createMediaElementSource(V), g = ctx.createGain();
      src.connect(g); g.connect(ctx.destination); AUD = {ctx, g};
    }
    AUD.g.gain.value = p / 100;
    if (AUD.ctx.state !== 'running') AUD.ctx.resume().catch(() => {});
  } catch (e) { /* без Web Audio лишається системна гучність */ }
}
function volPaint() {
  const m = volMax(), p = volGet(), r = $('#vvr'); if (!r) return;
  r.max = m; r.value = p;
  const sl = $('#vsl'); sl.style.setProperty('--p', (p / m * 100) + '%'); sl.style.setProperty('--m', m); sl.style.setProperty('--bs', (p > 100 ? 100 / p * 100 : 100) + '%');
  const mk = $('#v100'); mk.style.display = m > 100 ? 'block' : 'none'; mk.style.left = 'calc(' + (100 / m * 100) + '% - 1px)';
  $('#vvn').textContent = p + '%'; $('#vvp').classList.toggle('boost', p > 100);
}
$('#vvol').addEventListener('click', ev => { ev.stopPropagation(); const v = $('#vvp'); v.classList.toggle('hide'); if (!v.classList.contains('hide')) volPaint(); vcShow(); });
$('#vvr').addEventListener('input', () => { volApply(parseInt($('#vvr').value, 10)); volPaint(); vcShow(); });
$('#vvp').addEventListener('click', ev => ev.stopPropagation());
V.addEventListener('playing', () => { if (AUD && AUD.ctx.state !== 'running') AUD.ctx.resume().catch(() => {}); });
function volRestore() { const p = volGet(); if (p !== 100 || AUD) volApply(p); }

async function play(id, n) {
  const it = item(id); if (!it) return;
  S.cur = {id, n};
  const e = it.episodes.find(x => x.n === n);
  $('#player').classList.remove('hide'); vcIcon(); VC.classList.remove('off');
  document.body.style.overflow = 'hidden';
  applyPlayerMode(); setNowPlaying(it, e, n);
  $('#ptitle').innerHTML = `Серія ${esc(n)}<small>${esc(it.title)}</small>`;
  syncAuto(); skips = {}; skDone = {}; skSoft = null; skEnded = false; pReset(); paintMarks(); skipBtn(null);
  const msg = $('#pmsg'); msg.classList.remove('hide'); msg.innerHTML = ico('hourglass') + 'Готую відео…';
  V.pause(); V.removeAttribute('src'); V.load();
  pMarked = !!(e && e.watched); pSaveT = 0;
  try {
    const s = await api(`/api/stream?id=${id}&n=${encodeURIComponent(n)}&q=${S.q}`);
    if (!S.cur || S.cur.n !== n || S.cur.id !== id) return;
    if (s.type === 'hls' && !nativeHls) {
      msg.innerHTML = 'Цей браузер не вміє грати онлайн-потік. На iPhone це працює; або <a href="' + esc(it.url) + '" target="_blank">відкрийте сторінку сайту</a>.';
      return;
    }
    if (s.type === 'file' && (s.quality || s.size)) $('#ptitle').innerHTML = `Серія ${esc(n)}<small>${esc(it.title)} · ${esc([s.quality, s.size ? fmtSize(s.size) : '', 'на пристрої'].filter(Boolean).join(' · '))}</small>`;
    if (s.type !== 'file') $('#ptitle').innerHTML = `Серія ${esc(n)}<small>${esc(it.title)} · ${esc(s.quality || '')}${s.player ? ' · ' + esc(s.player) : ''}</small>`;
    skips = s.skips || {}; skDone = {}; paintMarks();
    if (s.skips_pending) pollSkips(id, n);
    V.src = s.url; volRestore();
    const nx2 = it.episodes[it.episodes.findIndex(x => x.n === n) + 1]; if (nx2 && !nx2.downloaded) warm(id, nx2.n, it.dubbing);
    const resume = e && e.pos > 5 && !e.watched ? e.pos : 0;
    V.onloadedmetadata = () => { paintMarks(); if (resume && resume < (V.duration || 1e9) - 10) { P.prog = true; setTimeout(() => { P.prog = false; }, 800); P.credit += resume; P.extra += resume; P.good = P.lastT = resume; V.currentTime = resume; } msg.classList.add('hide'); };
    let rec = 0;
    V.onerror = async () => {
      if (s.type === 'file' && rec < 4) {  // файл віддає наш сервер; якщо його вбила iOS — чекаємо, поки програма підніме, і продовжуємо з того ж місця
        rec++; const keep = V.currentTime || P.good || resume || 0;
        for (let i = 0; i < 12; i++) { try { await api('/api/settings'); break; } catch (e) { await new Promise(r => setTimeout(r, 700)); } }
        if (!S.cur || S.cur.id !== id || S.cur.n !== n) return;
        V.src = s.url; V.addEventListener('loadedmetadata', () => { if (keep > 1) V.currentTime = keep; V.play().catch(() => {}); }, {once: true});
        return;
      }
      msg.classList.remove('hide'); msg.innerHTML = ico('error') + esc('Не вдалося відтворити відео.' + (s.type !== 'file' ? ' Спробуйте іншу якість або завантажте серію.' : '')); };
    const p = V.play(); if (p && p.catch) p.catch(() => {});
  } catch (er) { msg.classList.remove('hide'); msg.innerHTML = ico('error') + esc(er.message); }
}
async function pollSkips(id, n) {  // скачана серія без таймінгів інтро/аутро: сервер довантажує їх із сайту у фоні
  for (const d of [2500, 4000, 6000, 9000, 14000]) {
    await new Promise(r => setTimeout(r, d));
    if (!S.cur || S.cur.id !== id || S.cur.n !== n) return;
    try {
      const r = await api(`/api/skips?id=${id}&n=${encodeURIComponent(n)}`);
      if (r.skips && (r.skips.opening || r.skips.ending)) { skips = r.skips; paintMarks(); skipTick(); return; }
    } catch (e) {}
  }
}
/* ---------- «справжній» перегляд: відрізняємо перегляд від випадкової перемотки ---------- */
const P = {credit: 0, extra: 0, good: 0, lastT: 0, sinceSeek: 99, prog: false};  // credit — секунди, реально переглянуті (плюс пропущені інтро/аутро й місце, звідки продовжили)
function pReset() { Object.assign(P, {credit: 0, extra: 0, good: 0, lastT: 0, sinceSeek: 99, prog: false}); nxCancel(); }
function playedSecs() { let s = 0; try { const r = V.played; for (let i = 0; i < r.length; i++) s += r.end(i) - r.start(i); } catch (e) {} return s; }
function genuine() { const d = V.duration; return !!d && isFinite(d) && Math.max(P.credit, playedSecs() + (P.extra || 0)) >= Math.min(d * 0.1, 90); }  // «серію дивились», якщо побачено хоч ~10% (не більше 90 с)
function posToSave() {  // різкий стрибок уперед (в кінець) і одразу вихід — не зберігаємо «фейкову» позицію, лишаємо останню справжню
  const t = V.currentTime;
  if (P.sinceSeek >= 8 || t <= P.good + 20) return t;
  return P.good >= 5 ? P.good : null;
}
function savePos(force) {
  if (!S.cur || !V.duration) return;
  const now = Date.now();
  if (!force && now - pSaveT < 10000) return;
  const t = posToSave(); if (t === null) return;
  pSaveT = now;
  const it = item(S.cur.id); const e = it && it.episodes.find(x => x.n === S.cur.n);
  if (e) e.pos = t;
  sendPos({id: S.cur.id, n: S.cur.n, t});
}
function sendPos(p) {  // якщо сервер тимчасово недоступний — позицію не губимо, а відправляємо, щойно він повернеться
  try { localStorage.setItem('pendPos', JSON.stringify(p)); } catch (e) {}
  api('/api/progress', p).then(() => { try { localStorage.removeItem('pendPos'); } catch (e) {} }).catch(() => {});
}
let beatT = 0;
function beat(closed) { const n = Date.now(); if (!closed && n - beatT < 7000) return; beatT = n; fetch('/api/beat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: closed === true ? '{"closed":true}' : '{}'}).catch(() => {}); }
setInterval(() => { if (S.cur) beat(); }, 10000);  // поки плеєр відкритий (і на паузі, і в PiP) програму не присипляємо  // Python дізнається, що відео грає, і не дає iOS приспати програму
V.addEventListener('playing', () => beat()); V.addEventListener('timeupdate', () => { if (!V.paused) beat(); }); V.addEventListener('pause', () => beat());
function flushPos() { try { const p = JSON.parse(localStorage.getItem('pendPos') || 'null'); if (p && p.id) sendPos(p); } catch (e) {} }
window.__srv = () => { try { pollAC(true); } catch (e) {} loadLib(true).catch(() => {}); flushPos(); };
async function markWatched() {
  if (pMarked || !S.cur) return; pMarked = true;
  const {id, n} = S.cur; const it = item(id); const e = it && it.episodes.find(x => x.n === n);
  if (e) { e.watched = true; e.new = false; recount(it); }
  try { await api('/api/watched', {id, n, value: true}); } catch (er) {}
}
V.addEventListener('timeupdate', () => {
  const t = V.currentTime, dt = t - P.lastT;
  if (!V.paused && !V.seeking && !seeking && dt > 0 && dt < 2.5) { P.credit += dt; P.sinceSeek += dt; P.good = t; }  // природне відтворення
  P.lastT = t;
  savePos(false);
  if (V.duration && t / V.duration > 0.9 && genuine()) markWatched();
});
V.addEventListener('pause', () => savePos(true));
V.addEventListener('ended', async () => {
  try { if (V.webkitDisplayingFullscreen && V.webkitExitFullscreen) V.webkitExitFullscreen(); } catch (e) {}  // у системному плеєрі відлік не видно — виходимо, щоб показати
  if (genuine()) await markWatched(); else toast('Серію майже не дивились — не позначаю переглянутою', false, 'alert');
  const quick = skEnded; skEnded = false;
  if (autoNext()) nxStart(quick ? 3 : 6);  // відлік працює завжди, його можна скасувати кнопкою «Скасувати»
});
/* пауза перед автопереходом на наступну серію: можна скасувати або перейти одразу */
let nxT = 0;
function nxCancel() { clearInterval(nxT); nxT = 0; const b = $('#vnx'); if (b) b.classList.add('hide'); }
function nxStart(secs) {
  if (nxT || !S.cur) return;
  const it = item(S.cur.id), i = it ? it.episodes.findIndex(x => x.n === S.cur.n) : -1, nx = it && it.episodes[i + 1];
  if (!nx) { closePlayer(); return; }
  const tot = secs || 6; let left = tot;
  const paint = () => { $('#vnxt').textContent = `Наступна серія ${nx.n} — через ${left} с`; $('#vnxbar').style.width = ((tot - left) / tot * 100) + '%'; };
  $('#vnx').classList.remove('hide'); paint();
  nxT = setInterval(() => { left--; if (left <= 0) nxNow(); else paint(); }, 1000);
}
function nxNow() { nxCancel(); stepPlayer(1, true); }
function moveItem(id, d) {
  const tl = visibleTiles(), key = t => t.g ? t.m[0].id : t.it.id, of = t => t.g ? t.m : [t.it];
  const i = tl.findIndex(t => key(t) === id); if (i < 0) return;
  const j = d === 'top' ? 0 : Math.max(0, Math.min(tl.length - 1, i + Number(d)));
  if (j === i) return;
  const rects = {};
  document.querySelectorAll('.card[data-id]').forEach(c => { rects[c.dataset.id] = c.getBoundingClientRect(); });
  const xs = of(tl[i]), tg = of(tl[j]);
  const rest = S.items.filter(x => !xs.includes(x));
  const ti = j > i ? rest.indexOf(tg[tg.length - 1]) + 1 : rest.indexOf(tg[0]);
  rest.splice(ti, 0, ...xs); S.items.splice(0, S.items.length, ...rest);
  render();
  document.querySelectorAll('.card[data-id]').forEach(c => {
    const r = rects[c.dataset.id]; if (!r) return;
    const n = c.getBoundingClientRect(), dx = r.left - n.left, dy = r.top - n.top;
    if (dx || dy) { c.style.transition = 'none'; c.style.transform = `translate(${dx}px,${dy}px)`;
      requestAnimationFrame(() => requestAnimationFrame(() => { c.style.transition = 'transform .3s cubic-bezier(.2,.8,.2,1)'; c.style.transform = ''; })); }
  });
  clearTimeout(moveItem.t);
  moveItem.t = setTimeout(() => api('/api/reorder', {ids: S.items.map(x => x.id)}).catch(e => toast(e.message, true)), 400);
}
function stepPlayer(d, auto) {
  if (!S.cur) return;
  const it = item(S.cur.id); const i = it.episodes.findIndex(x => x.n === S.cur.n);
  const nx = it.episodes[i + d];
  if (!nx) { if (auto) closePlayer(); else toast(d > 0 ? 'Це остання серія' : 'Це перша серія'); return; }
  savePos(true); play(S.cur.id, nx.n);
}
/* ---------- інтро / аутро ---------- */
let skips = {}, skDone = {}, skShown = null;
const TAIL_MIN = 25; /* якщо після аутро лишається більше — це сцена, не кінець */
function autoNext() { return S.set.auto_next !== false; }
function syncAuto() { const c = $('#pauto'); if (c) c.checked = autoNext(); }
function setOpt(k, v) { S.set[k] = v; api('/api/settings', {[k]: v}).catch(er => toast(er.message, true)); }
function win(k) { const w = skips && skips[k]; return w && w.length === 2 && w[1] > w[0] ? w : null; }
function paintMarks() {
  const tr = $('#vtr'); if (!tr) return;
  tr.querySelectorAll('.mk').forEach(x => x.remove());
  const d = V.duration; if (!d || !isFinite(d)) return;
  [['opening', 'op'], ['ending', 'ed']].forEach(([k, c]) => {
    const w = win(k); if (!w) return;
    const a = Math.max(0, w[0]), b = Math.min(d, w[1]); if (b <= a) return;
    const m = document.createElement('div'); m.className = 'mk ' + c;
    m.style.left = (a / d * 100) + '%'; m.style.width = ((b - a) / d * 100) + '%';
    tr.insertBefore(m, $('#vknob'));
  });
}
function endTail(w) { const d = V.duration; return d && isFinite(d) ? d - w[1] : 0; }
function skipBtn(kind, w) {
  const b = $('#vskip'); if (!b) return;
  if (!kind) { b.classList.add('hide'); skShown = null; return; }
  if (skShown === kind) return; skShown = kind;
  b.dataset.k = kind;
  b.innerHTML = kind === 'opening' ? 'Пропустити інтро' : 'Пропустити аутро';
  b.classList.remove('hide');
}
let skSoft = null, skEnded = false;
function doSkip(kind, auto) {
  const w = win(kind); if (!w) return;
  skDone[kind] = true; skSoft = null;
  if (kind === 'ending' && endTail(w) <= TAIL_MIN) skEnded = true;  // аутро в кінці серії: спершу пропускаємо його, а на наступну серію переходимо через пару секунд (після кінця відео)
  const to = Math.min(w[1], (V.duration || 1e9) - 0.3);
  P.extra += Math.max(0, to - V.currentTime); P.credit += Math.max(0, to - V.currentTime); P.good = to; P.prog = true; setTimeout(() => { P.prog = false; }, 800);
  V.currentTime = to;
}
function skipTick() {
  const t = V.currentTime; if (!S.cur || !skips) return;
  let hit = null;
  for (const k of ['opening', 'ending']) {
    const w = win(k); if (w && t >= w[0] - 0.2 && t < w[1] - 1) { hit = [k, w]; break; }
  }
  if (!hit) { skipBtn(null); skSoft = null; return; }
  const [k, w] = hit; skipBtn(k, w);
  const on = S.set.auto_skip && (k === 'opening' ? S.set.auto_skip_opening : S.set.auto_skip_ending);
  const soft = S.set.skip_mode === 'soft';
  if (on && !skDone[k] && !seeking && !V.seeking && t < w[0] + 3 + (soft ? 4 : 0)) {  // лише коли доходимо до вікна природним відтворенням, а не потрапили в нього перемоткою
    if (!soft) { doSkip(k, true); return; }
    if (!skSoft || skSoft.k !== k) skSoft = {k, at: Date.now() + 2500};  // лагідний режим: пара секунд на роздуми, кнопку можна натиснути й раніше
    const left = Math.ceil((skSoft.at - Date.now()) / 1000);
    if (left <= 0) { doSkip(k, true); return; }
    const b = $('#vskip'); if (b) b.innerHTML = (k === 'opening' ? 'Пропустити інтро' : 'Пропустити аутро') + ` <span class="cnt">· ${left}</span>`;
  }
}
$('#vskip').addEventListener('click', ev => { ev.stopPropagation(); const k = $('#vskip').dataset.k; skDone[k] = true; doSkip(k, false); skipBtn(null); });
V.addEventListener('seeking', () => {
  const t = V.currentTime;
  if (P.prog) { P.prog = false; P.lastT = t; return; }  // програмний пропуск інтро/аутро або продовження з місця
  const a = Math.min(t, P.lastT), b = Math.max(t, P.lastT);  // ручна перемотка: вікна, куди потрапили або через які повернулись назад, не пропускаємо автоматично
  for (const k of ['opening', 'ending']) { const w = win(k); if (w && ((t >= w[0] - 0.2 && t < w[1]) || (t < P.lastT && w[1] > a && w[0] < b))) skDone[k] = true; }
  P.sinceSeek = 0; P.lastT = t; nxCancel(); skSoft = null;
});
/* ---------- режим плеєра (iOS), Now Playing, повний екран, PiP ---------- */
function applyPlayerMode() {  // без playsinline iPhone сам відкриває стандартний плеєр iOS (PiP, AirPlay, керування з екрана блокування)
  const native = IOS_APP && S.set.ios_player !== false;
  ['playsinline', 'webkit-playsinline'].forEach(a => native ? V.removeAttribute(a) : V.setAttribute(a, ''));
}
function setNowPlaying(it, e, n) {
  const title = `${n} Серія • ${it.title || ''}`, sub = (e && e.title) ? String(e.title) : '';
  document.title = title;
  let poster = '';
  try {
    if (S.set.np_ep_cover !== false && e && e.th) poster = new URL(`/thumb/${it.id}/${encodeURIComponent(e.n)}?v=${e.downloaded ? 1 : 0}`, location.href).href;  // обкладинка самої серії
    else if (it.meta && it.meta.poster) poster = new URL(it.meta.poster, location.href).href;
  } catch (er) {}
  if (poster) V.poster = poster; else V.removeAttribute('poster');
  try {
    if (!('mediaSession' in navigator) || !window.MediaMetadata) return;
    const ms = navigator.mediaSession;
    ms.metadata = new MediaMetadata({title, artist: sub, album: '', artwork: poster ? [{src: poster, sizes: '512x512'}] : []});
    const h = (k, f) => { try { ms.setActionHandler(k, f); } catch (er) {} };
    h('play', () => V.play().catch(() => {})); h('pause', () => V.pause());
    h('previoustrack', () => stepPlayer(-1)); h('nexttrack', () => stepPlayer(1));
    h('seekbackward', () => { V.currentTime = Math.max(0, V.currentTime - 10); });
    h('seekforward', () => { V.currentTime = Math.min(V.duration || 1e9, V.currentTime + 10); });
  } catch (er) {}
}
$('#vfs').addEventListener('click', () => {
  try { if (V.webkitEnterFullscreen) V.webkitEnterFullscreen(); else if (V.requestFullscreen) V.requestFullscreen(); } catch (e) { toast('Не вдалося відкрити на весь екран', true); }
});
(function () {
  const ok = (V.webkitSupportsPresentationMode && V.webkitSupportsPresentationMode('picture-in-picture')) || document.pictureInPictureEnabled;
  if (!ok) $('#vpip').style.display = 'none';
})();
$('#vpip').addEventListener('click', async () => {
  try {
    if (V.webkitSetPresentationMode) V.webkitSetPresentationMode(V.webkitPresentationMode === 'picture-in-picture' ? 'inline' : 'picture-in-picture');
    else if (document.pictureInPictureElement) await document.exitPictureInPicture();
    else await V.requestPictureInPicture();
  } catch (e) { toast('Картинка в картинці недоступна для цього відео', true); }
});
V.addEventListener('webkitendfullscreen', () => { vcShow(); });
/* ---------- власні елементи керування ---------- */
const VC = $('#vc'), VS = $('#vseek'); let vcT = 0, seeking = false;
function vcShow() {
  VC.classList.remove('off'); clearTimeout(vcT);
  if (!V.paused && !seeking) vcT = setTimeout(() => VC.classList.add('off'), 3000);
}
function vcSync() {
  const d = V.duration || 0, t = V.currentTime || 0;
  if (!seeking) VS.value = d ? Math.round(t / d * 1000) : 0;
  $('#vcur').textContent = fmtT(seeking ? VS.value / 1000 * d : t);
  const pc = d ? (seeking ? VS.value / 10 : t / d * 100) : 0;
  $('#vfill').style.width = pc + '%'; $('#vknob').style.left = pc + '%';
  $('#vrem').textContent = d ? '-' + fmtT(Math.max(0, d - (seeking ? VS.value / 1000 * d : t))) : '0:00';
}
function vcIcon() { $('#vpp').innerHTML = ico(V.paused ? 'play' : 'pause'); }
$('#stage').addEventListener('click', ev => {
  if (ev.target.closest('.skipbtn')) return;
  if (ev.target.closest('.cb, .sk, .top')) { vcShow(); return; }
  if (VC.classList.contains('off')) vcShow(); else { VC.classList.add('off'); clearTimeout(vcT); }
});
$('#vpp').addEventListener('click', () => { V.paused ? V.play().catch(() => {}) : V.pause(); });
$('#vback').addEventListener('click', () => { V.currentTime = Math.max(0, V.currentTime - 10); vcSync(); });
$('#vfwd').addEventListener('click', () => { V.currentTime = Math.min(V.duration || 1e9, V.currentTime + 10); vcSync(); });
VS.addEventListener('input', () => { seeking = true; vcSync(); clearTimeout(vcT); });
VS.addEventListener('change', () => { if (V.duration) V.currentTime = VS.value / 1000 * V.duration; seeking = false; vcSync(); vcShow(); });
['play', 'pause', 'playing'].forEach(e => V.addEventListener(e, () => { vcIcon(); vcShow(); }));
V.addEventListener('timeupdate', () => { vcSync(); skipTick(); });
['playing', 'seeked', 'loadeddata', 'canplay'].forEach(e => V.addEventListener(e, skipTick));
setInterval(() => { if (S.cur && !V.paused) skipTick(); }, 400);
V.addEventListener('loadedmetadata', vcSync);
function closePlayer() {
  nxCancel(); savePos(true);
  try { if (V.webkitDisplayingFullscreen && V.webkitExitFullscreen) V.webkitExitFullscreen(); } catch (e) {}
  document.title = 'Мої аніме'; V.removeAttribute('poster');
  try { if ('mediaSession' in navigator) navigator.mediaSession.metadata = null; } catch (e) {}
  V.pause(); V.removeAttribute('src'); V.load();
  $('#player').classList.add('hide'); document.body.style.overflow = '';
  S.cur = null; beat(true); loadLib().catch(() => {});
}


/* ---------- шторка не віддає прокрутку сторінці під нею ---------- */
(function () {
  let y0 = 0, x0 = 0;
  const L = document.getElementById('layer');
  L.addEventListener('touchstart', e => { y0 = e.touches[0].clientY; x0 = e.touches[0].clientX; }, {passive: true});
  L.addEventListener('touchmove', e => {
    const sc = e.target.closest('#joblist, #sb');
    if (!sc) { e.preventDefault(); return; }
    if (e.target.closest('textarea, input')) return;
    const dy = e.touches[0].clientY - y0, dx = e.touches[0].clientX - x0;
    if (Math.abs(dx) > Math.abs(dy)) return;  // горизонтальні жести не чіпаємо
    const atTop = sc.scrollTop <= 0, atBot = sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 1;
    if ((dy > 0 && atTop) || (dy < 0 && atBot)) e.preventDefault();
  }, {passive: false});
})();

/* ---------- панель фільтрів: ховається при прокрутці вниз, з'являється при прокрутці вгору ---------- */
(function () {
  let ly = 0, acc = 0, busy = false, hideT = 0, lastChg = 0;
  const tog = document.createElement('button');
  tog.id = 'hdrtog'; tog.setAttribute('aria-label', 'Панель фільтрів');
  document.body.appendChild(tog);
  const fit = () => { const b = document.querySelector('.bar'); if (b) { const h = b.offsetHeight + 'px'; if (document.documentElement.style.getPropertyValue('--barh') !== h) document.documentElement.style.setProperty('--barh', h); } };
  const sync = () => {  // кнопка-ручка: показуємо лише коли панель сховалась (щоб розкрити) або закріплена відкритою (щоб сховати)
    const h = document.querySelector('.hdr2');
    const show = !!h && (S.hdrPin || h.classList.contains('hide'));
    tog.classList.toggle('on', show);
    if (show) { const k = S.hdrPin ? 'up' : 'down'; if (tog.dataset.k !== k) { tog.dataset.k = k; tog.innerHTML = `<svg viewBox="0 0 24 24"><path d="${k === 'up' ? 'M6 15l6-6 6 6' : 'M6 9l6 6 6-6'}"/></svg>`; tog.setAttribute('aria-label', k === 'up' ? 'Сховати панель' : 'Показати панель'); } }
  };
  const apply = (h, hide, instant) => {
    if (h.classList.contains('hide') !== hide) {
      if (instant) { h.style.transition = 'none'; h.classList.toggle('hide', hide); void h.offsetHeight; h.style.transition = ''; } else h.classList.toggle('hide', hide);
    }
    sync();
  };
  tog.addEventListener('click', () => {
    const h = document.querySelector('.hdr2'); if (!h) return;
    lastChg = Date.now();
    if (S.hdrPin) { S.hdrPin = false; S.hdrHide = window.scrollY > 150; apply(h, !!S.hdrHide); }
    else { S.hdrPin = true; S.hdrHide = false; apply(h, false); }
  });
  window.__fitBar = () => { fit(); const h = document.querySelector('.hdr2'); if (h) apply(h, !!S.hdrHide && !S.hdrPin && window.scrollY > 30, true); else sync(); };  // після перемальовування сторінки стан панелі не скидається
  addEventListener('scroll', () => {
    if (busy) return; busy = true;
    requestAnimationFrame(() => {
      busy = false;
      const h = document.querySelector('.hdr2');
      const max = document.documentElement.scrollHeight - window.innerHeight;
      const y = window.scrollY;
      if (y < 0 || y > max + 1) return;  // «гумова» відскока на краях — ігноруємо
      const d = y - ly; ly = y;
      if (!h) { acc = 0; return; }
      fit();
      if (S.hdrPin) { apply(h, false); return; }  // закріплена вручну — не чіпаємо
      if (y < 30) { acc = 0; clearTimeout(hideT); hideT = 0; if (S.hdrHide) { S.hdrHide = false; apply(h, false); } return; }  // лише біля самого верху (між 30 і 150 стан не змінюється — без дрижання)
      if (d <= 0) { acc = 0; clearTimeout(hideT); hideT = 0; return; }  // вгору сама не з'являється — лише ручка
      acc += d;
      if (acc > 90 && y > 150 && !S.hdrHide && !hideT && Date.now() - lastChg > 500) {  // ховаємо з невеликою затримкою: коротка прокрутка панель не чіпає
        hideT = setTimeout(() => { hideT = 0; if (S.hdrPin || S.hdrHide || window.scrollY <= 150) return; S.hdrHide = true; lastChg = Date.now(); const h2 = document.querySelector('.hdr2'); if (h2) apply(h2, true); }, 180);
      }
    });
  }, {passive: true});
})();

/* ---------- свайп шторки вниз ---------- */
(function () {
  let sw = null;
  const layer = $('#layer');
  layer.addEventListener('touchstart', e => {
    const sh = e.target.closest('.sheet'); if (!sh || e.touches.length !== 1) { sw = null; return; }
    const sb = $('#sb');
    sw = {y: e.touches[0].clientY, t: Date.now(), sh, drag: false, grab: !!e.target.closest('.grab'), atTop: !sb || sb.scrollTop <= 0, last: 0};
  }, {passive: true});
  layer.addEventListener('touchmove', e => {
    if (!sw) return;
    const dy = e.touches[0].clientY - sw.y, sb = $('#sb');
    if (!sw.drag) {
      if (dy > 8 && (sw.grab || (sw.atTop && sb.scrollTop <= 0))) { sw.drag = true; sw.sh.style.transition = 'none'; sw.sh.style.animation = 'none'; }
      else { if (Math.abs(dy) > 8) sw = null; return; }
    }
    e.preventDefault();
    sw.last = Math.max(0, dy);
    sw.sh.style.transform = `translateY(${sw.last}px)`;
    const bd = sw.sh.parentNode; bd.style.background = `rgba(0,0,0,${Math.max(0, .63 - sw.last / 700)})`;
  }, {passive: false});
  const end = () => {
    if (!sw || !sw.drag) { sw = null; return; }
    const {sh, last, t} = sw; sw = null;
    const v = last / Math.max(1, Date.now() - t);
    if (last > 110 || (v > 0.55 && last > 40)) {
      sh.style.transition = 'transform .18s ease-in'; sh.style.transform = 'translateY(105%)';
      sh.parentNode.style.transition = 'background .18s'; sh.parentNode.style.background = 'rgba(0,0,0,0)';
      setTimeout(() => { S.skipOut = true; A['dlg-no'](); }, 170);
    } else {
      sh.style.transition = 'transform .28s cubic-bezier(.2,.9,.3,1.2)'; sh.style.transform = '';
      sh.parentNode.style.transition = 'background .2s'; sh.parentNode.style.background = '';
      setTimeout(() => { sh.style.transition = ''; }, 300);  // animation лишається 'none': інакше шторка повторно програє появу й блимає
    }
  };
  layer.addEventListener('touchend', end); layer.addEventListener('touchcancel', end);
})();

/* ---------- меню вибору: власна шторка замість системного списку (у WKWebView він не відкривається) ---------- */
(function () {
  if (document.documentElement.dataset.ios !== '1') return;
  let openAt = 0;
  function openSel(sel) {
    if (Date.now() - openAt < 500) return; openAt = Date.now();
    document.querySelectorAll('.selpop').forEach(e => e.remove());
    const opts = [...sel.options], r = sel.getBoundingClientRect();
    const lay = document.createElement('div'); lay.className = 'selpop-b';
    const pop = document.createElement('div'); pop.className = 'selpop';
    pop.innerHTML = opts.map((o, i) => `<button class="selopt${i === sel.selectedIndex ? ' on' : ''}" data-i="${i}"><span>${esc(o.textContent)}</span>${i === sel.selectedIndex ? ico('check') : ''}</button>`).join('');
    lay.appendChild(pop); document.body.appendChild(lay);
    const vw = window.innerWidth, vh = window.innerHeight, m = 10;
    const w = Math.min(vw - 2 * m, Math.max(r.width, 190));
    let left = r.left; if (left + w > vw - m) left = Math.max(m, r.right - w);  // не вилазимо за край — притискаємо до правого краю блока
    pop.style.width = w + 'px'; pop.style.left = left + 'px';
    const full = pop.scrollHeight, below = vh - r.bottom - m - 4, above = r.top - m - 4;
    const up = full > below && above > below;  // знизу не вміщається — показуємо над блоком
    const room = Math.max(120, up ? above : below);
    pop.style.maxHeight = Math.min(full, room) + 'px';
    if (up) { pop.style.bottom = (vh - r.top + 4) + 'px'; pop.style.transformOrigin = 'bottom'; } else { pop.style.top = (r.bottom + 4) + 'px'; pop.style.transformOrigin = 'top'; }
    const close = () => { window.removeEventListener('scroll', close, true); lay.remove(); };
    lay.addEventListener('click', ev => {
      const b = ev.target.closest('.selopt');
      if (b) { sel.selectedIndex = +b.dataset.i; sel.dispatchEvent(new Event('change', {bubbles: true})); }
      if (b || !ev.target.closest('.selpop')) close();
    });
    lay.addEventListener('touchmove', ev => { if (!ev.target.closest('.selpop')) ev.preventDefault(); }, {passive: false});
    const on = pop.querySelector('.selopt.on'); if (on) pop.scrollTop = Math.max(0, on.offsetTop - pop.clientHeight / 2 + on.offsetHeight / 2);
  }
  let tap = null;
  document.addEventListener('touchstart', ev => {
    const s = ev.target.closest && ev.target.closest('select'); if (!s) return;
    ev.preventDefault(); const t = ev.touches[0]; tap = {s, x: t.clientX, y: t.clientY, moved: false};
  }, {capture: true, passive: false});
  document.addEventListener('touchmove', ev => { if (tap) { const t = ev.touches[0]; if (Math.hypot(t.clientX - tap.x, t.clientY - tap.y) > 10) tap.moved = true; } }, {capture: true, passive: true});
  document.addEventListener('touchend', ev => { if (tap && !tap.moved) { ev.preventDefault(); openSel(tap.s); } tap = null; }, {capture: true, passive: false});
  document.addEventListener('mousedown', ev => { const s = ev.target.closest && ev.target.closest('select'); if (s) ev.preventDefault(); }, true);
  document.addEventListener('click', ev => { const s = ev.target.closest && ev.target.closest('select'); if (s) { ev.preventDefault(); openSel(s); } }, true);
})();

/* ---------- старт ---------- */
document.addEventListener('visibilitychange', () => { if (!document.hidden) { pollAC(true); flushPos(); if (!S.cur) loadLib('vis').catch(() => {}); } });
flushPos();
api('/api/settings').then(j => {
  S.set = j; syncAuto();
  if (j.view && Object.keys(j.view).length) { S.view = {...VIEW_DEF, ...j.view}; try { localStorage.setItem('libview', JSON.stringify(S.view)); } catch (e) {} if (['home', 'group'].includes(route().name)) render(); }
  else if (S.view) api('/api/settings', {view: S.view}).catch(() => {});
}).catch(() => {});
pollAC(true); setInterval(() => pollAC(), 45000);
pollNet();
fx();
loadLib().then(() => { pollJobs(); pollNotifs(); checkInterrupted(); }).catch(e => {
  $('#root').innerHTML = `<div class="empty"><div class="big">${ico('plug')}</div><h2>Зв'язок з Python втрачено</h2><p>${esc(e.message)}</p></div>`;
});
</script>
</body>
</html>
"""


if __name__ == '__main__':
  start()
