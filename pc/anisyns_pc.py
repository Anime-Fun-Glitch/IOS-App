"""AniSyns для ПК (Windows / macOS / Linux): запускає той самий сервер і відкриває його у власному вікні.
Вікно: pywebview (якщо встановлено), інакше — браузер за замовчуванням. Дані (бібліотека, відео) — у папці користувача."""
import os, sys, time, threading, webbrowser, urllib.request

HERE = os.path.dirname(os.path.abspath(getattr(sys, '_MEIPASS', __file__)))
for _p in (HERE, os.path.join(HERE, '..', 'src', 'anisyns')):  # у збірці файл лежить поруч, у репозиторії — в src/anisyns (єдина копія для iOS і ПК)
    if os.path.exists(os.path.join(_p, 'anime_app.py')):
        sys.path.insert(0, _p)
        break
import anime_app  # noqa: E402


def data_dir():
    if os.environ.get('ANISYNS_DIR'):
        return os.environ['ANISYNS_DIR']
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
    elif sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(base, 'AniSyns')


def wait_ready(url, t=20):
    end = time.time() + t
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1).read(1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def main():
    anime_app.BASE_DIR = data_dir()
    anime_app.start()
    url = f'http://{anime_app.HOST}:{anime_app.PORT}/'
    wait_ready(url)
    if '--no-window' in sys.argv:
        threading.Event().wait()
        return
    try:
        import webview  # pip install pywebview
        webview.create_window('AniSyns', url, width=1280, height=820, min_size=(420, 600), background_color='#0d0e13')
        webview.start()  # блокує, доки вікно відкрите
    except ImportError:
        webbrowser.open(url)
        print('Вікно закрити не можна (відкрито в браузері). Ctrl+C — вимкнути.')
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
