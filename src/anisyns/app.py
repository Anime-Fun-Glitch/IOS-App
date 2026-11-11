import os
import socket
import traceback

import toga
from toga.style import Pack

# SIGPIPE: вбудований Python не ігнорує його сам, і запис у сокет, який закрив плеєр, мовчки вбиває застосунок (без звіту про збій)
try:
    import signal as _sg
    _sg.signal(_sg.SIGPIPE, _sg.SIG_IGN)
except Exception:
    pass
try:
    import ctypes as _ct
    _ct.CDLL(None).signal(13, _ct.c_void_p(1))
except Exception:
    pass


def _documents_dir(app):
    """Папка Documents застосунку (видна в додатку «Файли»); запасний варіант — службова папка Toga."""
    try:
        from rubicon.objc import ObjCClass
        urls = ObjCClass('NSFileManager').defaultManager.URLsForDirectory(9, inDomains=1)  # NSDocumentDirectory
        return str(urls[0].path)
    except Exception:
        try:
            return str(app.paths.data)
        except Exception:
            return os.path.expanduser('~/Documents')


def _free_port(start=8765):
    for p in range(start, start + 30):
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return start


def _hide_nav_bar(window):
    """Прибирає верхню панель із назвою вікна (UINavigationBar), щоб сторінка займала весь екран."""
    ok = False
    try:
        impl = window._impl
        roots = [getattr(impl, 'controller', None), getattr(impl.native, 'rootViewController', None)]
        for c in roots:
            if c is None:
                continue
            for t in (c, getattr(c, 'navigationController', None)):
                if t is None:
                    continue
                try:
                    t.setNavigationBarHidden(True, animated=False)
                    ok = True
                except Exception:
                    pass
    except Exception:
        pass
    return ok


def _audio_session():
    """Категорія «Playback»: звук і Picture-in-Picture працюють при вимкненому дзвінку й у фоні."""
    try:
        from rubicon.objc import ObjCClass
        from rubicon.objc.runtime import load_library
        load_library('AVFoundation')
        sess = ObjCClass('AVAudioSession').sharedInstance()
        sess.setCategory('AVAudioSessionCategoryPlayback', error=None)
        sess.setActive(True, error=None)
    except Exception:
        pass


def _setup_native_info(anime_app):
    """Реальні дані iOS для «Пам'яті»: розмір програми й вільне місце так, як їх показують Налаштування."""
    try:
        from rubicon.objc import ObjCClass
        anime_app.APP_BUNDLE['path'] = str(ObjCClass('NSBundle').mainBundle.bundlePath)
    except Exception:
        pass

    def disk():
        from rubicon.objc import ObjCClass
        u = ObjCClass('NSURL').fileURLWithPath(str(anime_app.BASE_DIR))
        v = u.resourceValuesForKeys(['NSURLVolumeAvailableCapacityForImportantUsageKey', 'NSURLVolumeTotalCapacityKey'], error=None)
        free = v.objectForKey('NSURLVolumeAvailableCapacityForImportantUsageKey')
        total = v.objectForKey('NSURLVolumeTotalCapacityKey')
        return int(total.longLongValue), int(free.longLongValue)
    anime_app.DISK_PROVIDER['fn'] = disk

    def share(path):
        """Системне вікно «Поділитись» (Зберегти у Файли, AirDrop…) — виконується в головному потоці UI."""
        import asyncio

        def show():
            try:
                from rubicon.objc import ObjCClass
                url = ObjCClass('NSURL').fileURLWithPath(str(path))
                root = ObjCClass('UIApplication').sharedApplication.keyWindow.rootViewController
                top = root
                while top.presentedViewController is not None:
                    top = top.presentedViewController
                try:
                    # «Зберегти у Файли»: системний вибір папки на весь екран (модально) — не залежить від popover-вікна «Поділитись»,
                    # яке в режимі iPad/LiveContainer з'їжджало в куток
                    pk = ObjCClass('UIDocumentPickerViewController').alloc().initForExportingURLs([url], asCopy=True)
                    top.presentViewController(pk, animated=True, completion=None)
                    return
                except Exception:
                    traceback.print_exc()
                avc = ObjCClass('UIActivityViewController').alloc().initWithActivityItems([url], applicationActivities=None)
                pop = avc.popoverPresentationController
                if pop is not None:
                    # popover (регулярний розмір вікна, зокрема в LiveContainer): прив'язуємо до центру ВІКНА (координати екрана),
                    # а не до виду Toga, який може мати інший розмір/зсув; без стрілки, щоб вікно не з'їжджало в кут
                    win = ObjCClass('UIApplication').sharedApplication.keyWindow
                    try:
                        from rubicon.objc.types import CGPoint, CGRect, CGSize
                        wb = win.bounds
                        pop.sourceView = win
                        pop.sourceRect = CGRect(CGPoint(wb.size.width / 2, wb.size.height / 2), CGSize(1, 1))
                        pop.permittedArrowDirections = 0
                    except Exception:
                        traceback.print_exc()
                        pop.sourceView = win
                    try:
                        avc.preferredContentSize = CGSize(min(wb.size.width - 40, 420), min(wb.size.height - 120, 560))
                    except Exception:
                        pass
                top.presentViewController(avc, animated=True, completion=None)
            except Exception:
                traceback.print_exc()
        loop = getattr(anime_app, 'UI_LOOP', None)
        if loop is not None:
            loop.call_soon_threadsafe(show)
        else:
            show()
    anime_app.SHARE_PROVIDER['fn'] = share

    def net():
        """Wi-Fi / мобільні дані / немає мережі — через SCNetworkReachability (так само, як це бачить iOS)."""
        import ctypes
        import ctypes.util
        import threading
        if not hasattr(net, '_lock'):
            net._lock = threading.Lock()
        with net._lock:
            return _net_locked(net)

    def _net_locked(net):
        import ctypes
        import ctypes.util
        lib = getattr(net, '_lib', None)
        if lib is None:
            lib = ctypes.CDLL(ctypes.util.find_library('SystemConfiguration') or '/System/Library/Frameworks/SystemConfiguration.framework/SystemConfiguration')
            lib.SCNetworkReachabilityCreateWithName.restype = ctypes.c_void_p
            lib.SCNetworkReachabilityCreateWithName.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            lib.SCNetworkReachabilityGetFlags.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            lib.SCNetworkReachabilityGetFlags.restype = ctypes.c_bool
            net._lib = lib
            net._ref = lib.SCNetworkReachabilityCreateWithName(None, b'1.1.1.1')
        fl = ctypes.c_uint32(0)
        if not lib.SCNetworkReachabilityGetFlags(net._ref, ctypes.byref(fl)):
            return None
        f = fl.value
        if not (f & 0x2) or (f & 0x4):
            return 'offline'  # 0x2 = Reachable, 0x4 = ConnectionRequired (мережі фактично немає)
        return 'cell' if f & 0x40000 else 'wifi'  # 0x40000 = IsWWAN
    anime_app.NET_PROVIDER['fn'] = net

    def captured():
        from rubicon.objc import ObjCClass
        return bool(ObjCClass('UIScreen').mainScreen.isCaptured)
    anime_app.SCREEN_PROVIDER['fn'] = captured


class _KeepAlive:
    """Тихе аудіо по колу: поки воно «грає», iOS не присипляє програму (з UIBackgroundModes=audio). Вмикаємо лише коли треба."""

    def __init__(self):
        self.player = None
        self.on = False
        self.mix = False  # сесію вже налаштовано на старті (_audio_session) — не чіпаємо її під час відео
        self.path = None

    def _wav(self):
        import struct
        import tempfile
        p = os.path.join(tempfile.gettempdir(), 'keepalive.wav')
        if not os.path.exists(p):
            n = 8000
            with open(p, 'wb') as f:
                f.write(b'RIFF' + struct.pack('<I', 36 + n * 2) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 8000, 16000, 2, 16))
                f.write(b'data' + struct.pack('<I', n * 2) + b'\x00' * (n * 2))
        return p

    def _category(self, mix):
        if mix == self.mix:
            return
        self.mix = mix
        try:
            from rubicon.objc import ObjCClass
            sess = ObjCClass('AVAudioSession').sharedInstance()
            sess.setCategory('AVAudioSessionCategoryPlayback', withOptions=(1 if mix else 0), error=None)  # 1 = MixWithOthers
            sess.setActive(True, error=None)
        except Exception:
            pass

    def set(self, want, video_playing=False):
        """want — чи треба тримати програму; video_playing — тоді не міксуємо (потрібні елементи керування на екрані блокування)."""
        try:
            from rubicon.objc import ObjCClass
            if want:
                if video_playing and not self.on:
                    return  # відео вже саме тримає програму живою; запуск «тихого» плеєра в момент старту відео перебиває його (пауза через секунду на першому запуску)
                if self.player is None:
                    NSURL = ObjCClass('NSURL')
                    self.path = self._wav()
                    pl = ObjCClass('AVAudioPlayer').alloc().initWithContentsOfURL(NSURL.fileURLWithPath(self.path), error=None)
                    pl.numberOfLoops = -1
                    pl.volume = 1.0
                    pl.prepareToPlay()
                    self.player = pl
                if not self.on:
                    self.player.play()
                    self.on = True
                elif not bool(self.player.isPlaying):
                    self.player.play()  # iOS могла перервати (дзвінок тощо) — запускаємо знову
            elif self.on and self.player is not None:
                self.player.stop()
                self.on = False
        except Exception:
            pass


def _island(app_state):
    """Передає стан у Swift-міст Dynamic Island (якщо збірка з ним). Повертає рядок-статус або None."""
    try:
        from rubicon.objc import ObjCClass
        cls = ObjCClass('AnimeLive')
    except Exception:
        return None
    try:
        import json
        return str(cls.apply(json.dumps(app_state) if app_state else ''))
    except Exception:
        return None


def _pin(view, to, safe=False):
    """Прикріплює 4 краї view до країв to (або до їхньої безпечної зони)."""
    from rubicon.objc import ObjCClass
    NSLayoutConstraint = ObjCClass('NSLayoutConstraint')
    g = to.safeAreaLayoutGuide if safe else to
    NSLayoutConstraint.activateConstraints([
        view.topAnchor.constraintEqualToAnchor(g.topAnchor),
        view.bottomAnchor.constraintEqualToAnchor(g.bottomAnchor),
        view.leadingAnchor.constraintEqualToAnchor(g.leadingAnchor),
        view.trailingAnchor.constraintEqualToAnchor(g.trailingAnchor),
    ])


def _native_webview(app, url):
    """Свій WKWebView на ВЕСЬ екран у власному UIViewController (щоб працювали системні вікна на кшталт списків вибору).
    Безпечні зони (виріз, Dynamic Island, індикатор «Home») передаємо сторінці як CSS-змінні — вона малює під ними
    напівпрозорі панелі з розмиттям."""
    from rubicon.objc import CGPoint, CGRect, CGSize, ObjCClass
    WKWebView = ObjCClass('WKWebView')
    WKWebViewConfiguration = ObjCClass('WKWebViewConfiguration')
    NSURL = ObjCClass('NSURL')
    NSURLRequest = ObjCClass('NSURLRequest')
    UIColor = ObjCClass('UIColor')
    UIViewController = ObjCClass('UIViewController')

    win = app.main_window._impl.native  # UIWindow
    dark = UIColor.colorWithRed(0.051, green=0.055, blue=0.075, alpha=1.0)  # #0d0e13
    cfg = WKWebViewConfiguration.alloc().init()
    cfg.allowsInlineMediaPlayback = True
    cfg.mediaTypesRequiringUserActionForPlayback = 0
    try:
        cfg.allowsPictureInPictureMediaPlayback = True
    except Exception:
        pass

    child = UIViewController.alloc().init()
    child.view.backgroundColor = dark
    child.view.translatesAutoresizingMaskIntoConstraints = False
    win.backgroundColor = dark

    root = win.rootViewController
    host = root
    try:  # якщо корінь — UINavigationController, чіпляємось до його поточного контролера
        tv = root.topViewController
        if tv is not None:
            host = tv
    except Exception:
        pass
    host.addChildViewController(child)
    host.view.addSubview(child.view)
    _pin(child.view, host.view)
    child.didMoveToParentViewController(host)

    wv = WKWebView.alloc().initWithFrame(CGRect(CGPoint(0, 0), CGSize(100, 100)), configuration=cfg)
    wv.translatesAutoresizingMaskIntoConstraints = False
    wv.backgroundColor = dark
    wv.opaque = True
    try:  # колір під сторінкою (при гумовому прокручуванні / поки вміст перемальовується) — темний, а не білий
        wv.underPageBackgroundColor = dark
    except Exception:
        pass
    wv.scrollView.backgroundColor = dark
    wv.scrollView.contentInsetAdjustmentBehavior = 2  # Never: відступи робить сторінка за даними нижче
    try:  # без щипка-зуму: сторінка не має «роз'їжджатись»
        wv.scrollView.pinchGestureRecognizer.enabled = False
        wv.scrollView.minimumZoomScale = 1.0
        wv.scrollView.maximumZoomScale = 1.0
    except Exception:
        pass
    child.view.addSubview(wv)
    _pin(wv, child.view)  # на весь екран, під статус-баром та індикатором «Home»
    wv.loadRequest(NSURLRequest.requestWithURL(NSURL.URLWithString(url)))
    app._native_child = child
    return wv, child.view


async def _inset_loop(app, wv, view):
    """Фоновий цикл (раз на 0,4 с): safe-area → сторінка; раз на ~1,2 с перевіряє Python-сервер і піднімає, якщо iOS його зупинила;
    тримає програму активною (тихе аудіо), коли треба; оновлює Dynamic Island."""
    import asyncio
    import json
    from anisyns import anime_app
    last, tick, ka = None, 0, _KeepAlive()
    isl_last, isl_t, isl_port = None, 0.0, None
    while True:
        try:
            ins = view.safeAreaInsets
            cur = (round(float(ins.top)), round(float(ins.left)), round(float(ins.bottom)), round(float(ins.right)))
            tick += 1
            if ins.top is not None and float(ins.top) > 0:
                # Dynamic Island є лише в iPhone 14 Pro і новіших (верхній відступ ≥ 51 pt) і лише якщо в збірку додано Swift-міст
                if not getattr(_inset_loop, '_isl_checked', False) or float(ins.top) >= 51:
                    _inset_loop._isl_checked = True
                    if not hasattr(_inset_loop, '_bridge'):
                        try:
                            from rubicon.objc import ObjCClass as _OC
                            _OC('AnimeLive')
                            _inset_loop._bridge = True
                        except Exception:
                            _inset_loop._bridge = False
                    anime_app.ISLAND_OK['v'] = bool(_inset_loop._bridge and float(ins.top) >= 51)
            if cur != last or tick % 10 == 0:
                last = cur
                wv.evaluateJavaScript('window.__insets&&window.__insets(%d,%d,%d,%d)' % cur, completionHandler=None)
        except Exception:
            pass
        try:
            if tick % 3 == 0 and not anime_app.server_alive():
                anime_app.restart_server()
                wv.evaluateJavaScript('window.__srv&&window.__srv()', completionHandler=None)
        except Exception:
            pass
        try:
            if tick % 2 == 0:
                ka.set(anime_app.bg_needed(), anime_app.video_active())
        except Exception:
            pass
        try:
            if tick % 2 == 1:
                st = anime_app.island_state()
                key = json.dumps(st, sort_keys=True) if st is None else json.dumps({k: (round(v, 2) if isinstance(v, float) and k == 'progress' else v) for k, v in st.items()}, sort_keys=True)
                import time as _t
                if isl_port != anime_app.PORT:
                    isl_port = anime_app.PORT
                    try:
                        from rubicon.objc import ObjCClass
                        ObjCClass('AnimeLive').setPort(int(anime_app.PORT))
                    except Exception:
                        pass
                if key != isl_last or _t.time() - isl_t > 20:  # те саме перешлемо раз на 20 с: Activity могла не стартувати, поки програма була у фоні
                    isl_last, isl_t = key, _t.time()
                    _island(st)
        except Exception:
            pass
        await asyncio.sleep(0.4)


def _darken(window):
    """Темне тло вікна й усіх його корінних в'ю одразу: без білого спалаху між екраном запуску та сторінкою."""
    try:
        from rubicon.objc import ObjCClass
        c = ObjCClass('UIColor').colorWithRed(0.051, green=0.055, blue=0.075, alpha=1.0)
        impl = window._impl
        win = impl.native
        win.backgroundColor = c
        views = []
        for r in (getattr(win, 'rootViewController', None), getattr(impl, 'controller', None)):
            if r is None:
                continue
            views.append(r)
            for attr in ('topViewController', 'visibleViewController'):
                try:
                    v = getattr(r, attr)
                    if v is not None:
                        views.append(v)
                except Exception:
                    pass
        for r in views:
            try:
                r.view.backgroundColor = c
            except Exception:
                pass
    except Exception:
        pass


def _cover_add(window):
    """Темне «покривало» поверх усього вікна: ховає біле мерехтіння, поки WebKit запускає свій процес і малює першу сторінку."""
    try:
        from rubicon.objc import ObjCClass
        win = window._impl.native
        try:
            win.overrideUserInterfaceStyle = 2  # завжди темне оформлення системних елементів
        except Exception:
            pass
        c = ObjCClass('UIColor').colorWithRed(0.051, green=0.055, blue=0.075, alpha=1.0)
        v = ObjCClass('UIView').alloc().initWithFrame(win.bounds)
        v.backgroundColor = c
        v.autoresizingMask = 18  # на всю ширину й висоту
        v.userInteractionEnabled = False
        win.addSubview(v)
        return v
    except Exception:
        return None


def _boot_server(app_obj):
    """Важка частина запуску (імпорт великого модуля й старт сервера) — поза головним потоком, щоб екран не «замерзав»."""
    from anisyns import anime_app
    anime_app.BASE_DIR = os.path.join(_documents_dir(app_obj), 'AniSyns')
    anime_app.PORT = _free_port(8765)
    anime_app.start()
    return f'http://127.0.0.1:{anime_app.PORT}/'


class AnimeApp(toga.App):
    def startup(self):
        self.main_window = toga.MainWindow(title=self.formal_name)
        # одразу показуємо темне вікно (а не біле) — і лише потім вантажимо сервер
        self.main_window.content = toga.Box(style=Pack(flex=1, background_color='#0d0e13'))
        _darken(self.main_window)
        self.main_window.show()
        self._cover = _cover_add(self.main_window)
        _hide_nav_bar(self.main_window)
        _darken(self.main_window)
        try:
            self.add_background_task(self._boot)
        except Exception:
            import asyncio
            asyncio.ensure_future(self._boot(self))

    async def _boot(self, app=None, **kwargs):
        import asyncio
        loop = asyncio.get_event_loop()
        url = None
        err = None
        try:
            url = await loop.run_in_executor(None, _boot_server, self)
        except Exception:
            err = traceback.format_exc()
        if url is None:
            self.main_window.content = toga.MultilineTextInput(
                readonly=True, value='Не вдалося запустити:\n\n' + err, style=Pack(flex=1))
            try:
                if getattr(self, '_cover', None) is not None:
                    self._cover.removeFromSuperview()
            except Exception:
                pass
            return
        self._attach(url)

    def _attach(self, url):
        from anisyns import anime_app
        try:
            import asyncio
            anime_app.UI_LOOP = asyncio.get_event_loop()
            _setup_native_info(anime_app)
            anime_app.IOS_APP = True  # сторінка знає, що вона в iPhone-застосунку (меню, плеєр iOS, відступи)
            _audio_session()
            if anime_app.SETTINGS.get('plain_wv'):
                raise RuntimeError('plain_wv')  # режим діагностики: штатний WebView Toga без власних надбудов
            self._native_wv, view = _native_webview(self, url)
            wv = self._native_wv

            async def _drop_cover(app, **kwargs):
                import asyncio, time
                t0 = time.time()
                # знімаємо покривало, щойно сторінка САМА повідомила, що намалювала перший кадр (тоді ж стартує анімація заставки)
                while anime_app.READY[0] < t0 and time.time() - t0 < 6:
                    await asyncio.sleep(0.03)
                try:
                    if getattr(self, '_cover', None) is not None:
                        self._cover.removeFromSuperview()
                        self._cover = None
                except Exception:
                    pass
            try:
                self.add_background_task(_drop_cover)
            except Exception:
                import asyncio
                asyncio.ensure_future(_drop_cover(self))

            async def _bg(app, **kwargs):
                await _inset_loop(app, wv, view)
            try:
                self.add_background_task(_bg)
            except Exception:
                import asyncio
                asyncio.ensure_future(_inset_loop(self, wv, view))
        except Exception:
            # запасний варіант: вбудований WebView Toga + розрахунок відступів у самій сторінці
            anime_app.IOS_APP = True
            self.webview = toga.WebView(url=url, style=Pack(flex=1))
            try:
                cfg = self.webview._impl.native.configuration
                cfg.allowsInlineMediaPlayback = True
                cfg.mediaTypesRequiringUserActionForPlayback = 0
            except Exception:
                pass
            self.main_window.content = self.webview
            try:
                if getattr(self, '_cover', None) is not None:
                    self._cover.removeFromSuperview()
            except Exception:
                pass


def main():
    return AnimeApp('AniSyns', 'com.watch.anisyns')
