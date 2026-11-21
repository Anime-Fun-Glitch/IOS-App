"""Сервер AniSyns для ПК без вікна: його запускає AniSyns.exe (вікно на WebView2). Сам нічого не показує."""
import os, sys, time, threading, webbrowser

APPDATA = os.environ.get('APPDATA') or os.path.expanduser('~')
CFG_DIR = os.path.join(APPDATA, 'AniSyns')


def data_dir():
    """Папка даних: її вибирає інсталятор (CFG_DIR/location.txt), інакше %APPDATA%\\AniSyns."""
    try:
        v = open(os.path.join(CFG_DIR, 'location.txt'), encoding='utf-8-sig').read().strip()
        if v:
            return v
    except Exception:
        pass
    return CFG_DIR


def setup_log(d):
    try:
        os.makedirs(d, exist_ok=True)
        f = open(os.path.join(d, 'server.log'), 'a', buffering=1, encoding='utf-8')
        sys.stdout = sys.stderr = f  # у windowed-збірці stdout немає, а програма друкує повідомлення
    except Exception:
        pass


def watch_parent(pid):
    """Якщо вікно (AniSyns.exe) зникло — вимикаємось, щоб сервер не висів у пам'яті."""
    if sys.platform != 'win32' or not pid:
        return
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not h:
        return

    def run():
        k.WaitForSingleObject(h, 0xFFFFFFFF)
        os._exit(0)
    threading.Thread(target=run, daemon=True).start()


def main():
    try:  # той самий ідентифікатор додатка, що й у вікна: Windows має показувати їх разом
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('AniSyns.App')
    except Exception:
        pass
    d = data_dir()
    setup_log(d)
    try:  # залишок від ранньої версії (профіль браузера Edge, 700+ МБ) — нашій програмі більше не потрібен
        import shutil
        old = os.path.join(d, 'window')
        if os.path.isdir(old) and os.path.isdir(os.path.join(old, 'Default')):
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass
    pid = 0
    if '--parent' in sys.argv:
        try:
            pid = int(sys.argv[sys.argv.index('--parent') + 1])
        except Exception:
            pid = 0
    watch_parent(pid)
    here = os.path.dirname(os.path.abspath(getattr(sys, '_MEIPASS', __file__)))
    for p in (here, os.path.join(here, '..', 'src', 'anisyns')):
        if os.path.exists(os.path.join(p, 'anime_app.py')):
            sys.path.insert(0, p)
            break
    import anime_app
    anime_app.BASE_DIR = d
    anime_app.PC_APP = True
    anime_app.BROWSER_PROVIDER['fn'] = lambda url, ext=False: (webbrowser.open(url), {'ok': True})[1]  # зовнішні сайти — у браузері за замовчуванням
    anime_app.start()
    threading.Event().wait()


if __name__ == '__main__':
    main()
