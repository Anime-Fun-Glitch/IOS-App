using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace AniSyns
{
    // Заокруглення вікна: Windows 11 — рідні кути DWM (з відновленням після кожної зміни стану; якщо DWM відмовився — запасна маска),
    // Windows 10 — як раніше (прямі кути, нічого не чіпаємо).
    static class Corners
    {
        [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int val, int size);
        [DllImport("gdi32.dll")] static extern IntPtr CreateRoundRectRgn(int l, int t, int r, int b, int w, int h);
        [DllImport("user32.dll")] static extern int SetWindowRgn(IntPtr hwnd, IntPtr rgn, bool redraw);
        public static readonly bool Win11 = Environment.OSVersion.Version.Build >= 22000;

        static readonly System.Runtime.CompilerServices.ConditionalWeakTable<Form, string> last = new System.Runtime.CompilerServices.ConditionalWeakTable<Form, string>();
        public static void Apply(Form f, bool square)
        {
            try
            {
                if (!f.IsHandleCreated) return;
                // не чіпаємо DWM, якщо нічого не змінилось (зайві виклики викликають мерехтіння)
                string key = (square ? "s" : "r") + f.DeviceDpi;
                string old; if (last.TryGetValue(f, out old) && old == key) return;
                last.Remove(f); last.Add(f, key);
                int one = 1, none = unchecked((int)0xFFFFFFFE);
                DwmSetWindowAttribute(f.Handle, 19, ref one, 4);
                DwmSetWindowAttribute(f.Handle, 20, ref one, 4);
                if (!Win11) return;  // Windows 10: старий вигляд
                int pref = square ? 1 : 2;  // DWMWCP_DONOTROUND / DWMWCP_ROUND
                int hr = DwmSetWindowAttribute(f.Handle, 33, ref pref, 4);
                DwmSetWindowAttribute(f.Handle, 34, ref none, 4);  // без кольорової рамки
                if (hr != 0 && !square)
                {
                    int d = (int)(16 * f.DeviceDpi / 96.0);
                    SetWindowRgn(f.Handle, CreateRoundRectRgn(0, 0, f.Width + 1, f.Height + 1, d, d), true);
                }
                else if (hr != 0) SetWindowRgn(f.Handle, IntPtr.Zero, true);
            }
            catch { }
        }
    }

    static class Wv
    {
        // WebView2 не завжди дізнається, що батьківське вікно змінило місце/розмір (розгорнути → повернути): повідомляємо явно
        public static void Sync(Microsoft.Web.WebView2.WinForms.WebView2 w)
        {
            try
            {
                foreach (var f in typeof(Microsoft.Web.WebView2.WinForms.WebView2).GetFields(System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance))
                {
                    if (f.FieldType != typeof(CoreWebView2Controller)) continue;
                    var ctl = f.GetValue(w) as CoreWebView2Controller;
                    if (ctl == null) continue;
                    var sz = w.ClientSize;
                    var nb = new Rectangle(0, 0, Math.Max(1, sz.Width), Math.Max(1, sz.Height));
                    if (ctl.Bounds != nb) ctl.Bounds = nb;
                    ctl.NotifyParentWindowPositionChanged();
                    break;
                }
            }
            catch { }
        }
    }

    static class Program
    {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode)] static extern int SetCurrentProcessExplicitAppUserModelID(string id);

        [STAThread]
        static void Main()
        {
            try { SetCurrentProcessExplicitAppUserModelID("AniSyns.App"); } catch { }  // один «додаток» для Windows: група на панелі завдань і в диспетчері
            bool created;
            using (var mx = new Mutex(true, "AniSyns.Host", out created))
            {
                if (!created) return;  // вже запущено
                Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new MainForm());
            }
        }
    }

    class MainForm : Form
    {
        const string Url = "http://127.0.0.1:8765/";
        const int Edge = 0;  // системної смуги немає: розмір міняється невидимими краями сторінки (rz:<HT>)
        static readonly Color Bg = Color.FromArgb(13, 14, 19);
        readonly WebView2 wv = new WebView2();
        Process server;
        PipForm pip;
        CoreWebView2Environment env;
        bool full;
        FormWindowState prevState;
        Rectangle prevBounds;
        bool startMax;

        [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int val, int size);
        [DllImport("dwmapi.dll")] static extern int DwmExtendFrameIntoClientArea(IntPtr hwnd, ref MARGINS m);
        [DllImport("user32.dll")] static extern bool ReleaseCapture();
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hwnd, int cmd);
        [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr hwnd, int msg, IntPtr w, IntPtr l);

        [StructLayout(LayoutKind.Sequential)] struct MARGINS { public int l, r, t, b; }
        [StructLayout(LayoutKind.Sequential)] struct RECT { public int left, top, right, bottom; }
        [StructLayout(LayoutKind.Sequential)] struct MINMAXINFO { public int resX, resY, maxSizeX, maxSizeY, maxPosX, maxPosY, minTX, minTY, maxTX, maxTY; }
        [StructLayout(LayoutKind.Sequential)] struct NCCALCSIZE_PARAMS { public RECT r0, r1, r2; public IntPtr pos; }

        static string StateFile()
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AniSyns", "window.txt");
        }

        public MainForm()
        {
            Text = "AniSyns";
            FormBorderStyle = FormBorderStyle.None;
            BackColor = Bg;
            MinimumSize = new Size(560, 640);
            try { Icon = new Icon(Path.Combine(AppContext.BaseDirectory, "app.ico")); }
            catch { try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { } }
            wv.Dock = DockStyle.Fill;
            wv.DefaultBackgroundColor = Bg;
            wv.AllowExternalDrop = true; // файл можна кинути на картку «Своя обкладинка»; поза нею сторінка скидання блокує
            Controls.Add(wv);
            Padding = new Padding(Edge);
            SetStyle(ControlStyles.ResizeRedraw | ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint, true);

            // розмір і місце: з минулого разу, інакше 82% робочої області екрана по центру
            var wa = Screen.PrimaryScreen.WorkingArea;
            int bw = Math.Max(900, wa.Width * 63 / 100), bh = Math.Max(640, wa.Height * 70 / 100);
            Rectangle b = new Rectangle(wa.Left + (wa.Width - bw) / 2, wa.Top + (wa.Height - bh) / 2, bw, bh);
            try
            {
                var p = File.ReadAllText(StateFile()).Split(',');
                var r = new Rectangle(int.Parse(p[0]), int.Parse(p[1]), int.Parse(p[2]), int.Parse(p[3]));
                bool visible = false;
                foreach (var sc in Screen.AllScreens) if (sc.WorkingArea.IntersectsWith(r) && r.Width >= 560 && r.Height >= 640) visible = true;
                if (visible) b = r;
                startMax = p.Length > 4 && p[4] == "1";
            }
            catch { }
            StartPosition = FormStartPosition.Manual;
            Bounds = b;

            Shown += async (s, e) => { if (startMax) WindowState = FormWindowState.Maximized; await Start(); };
            FormClosing += (s, e) => { try { if (pip != null && !pip.IsDisposed) { pip.Silent = true; pip.Close(); } } catch { } SaveState(); };
            FormClosed += (s, e) => StopServer();
            Resize += (s, e) => OnState();
        }

        void SaveState()
        {
            try
            {
                var r = full ? prevBounds : (WindowState == FormWindowState.Normal ? Bounds : RestoreBounds);
                bool mx = full ? prevState == FormWindowState.Maximized : WindowState == FormWindowState.Maximized;
                Directory.CreateDirectory(Path.GetDirectoryName(StateFile()));
                File.WriteAllText(StateFile(), r.X + "," + r.Y + "," + r.Width + "," + r.Height + "," + (mx ? "1" : "0"));
            }
            catch { }
        }

        // Без системного заголовка: клієнтська область = усе вікно (зберігаємо тінь, заокруглення, прилипання до країв)
        protected override CreateParams CreateParams
        {
            get
            {
                var cp = base.CreateParams;
                cp.Style |= 0x40000 | 0x20000 | 0x10000;  // WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
                return cp;
            }
        }

        protected override void WndProc(ref Message m)
        {
            const int WM_NCCALCSIZE = 0x83, WM_NCHITTEST = 0x84, WM_NCACTIVATE = 0x86, WM_NCPAINT = 0x85;
            if (m.Msg == WM_NCACTIVATE) { m.LParam = (IntPtr)(-1); base.WndProc(ref m); return; }  // не перемальовувати системну рамку при втраті фокуса
            if (m.Msg == WM_NCPAINT) { m.Result = IntPtr.Zero; return; }
            if (m.Msg == WM_NCCALCSIZE && m.WParam != IntPtr.Zero)
            {
                // Клієнтська область = усе вікно. РАНІШЕ тут підставлялась робоча область, коли WindowState ще «Maximized»:
                // під час повернення з розгорнутого стану це давало клієнтську область розміром на весь екран у вікні нормального
                // розміру — звідси «інтерфейс лишається розгорнутим, вміст обрізаний». Розмір при розгортанні задає WM_GETMINMAXINFO.
                m.Result = IntPtr.Zero;
                return;
            }
            if (m.Msg == 0x24)  // WM_GETMINMAXINFO: розгортання рівно на робочу область монітора (без заходу під панель завдань)
            {
                base.WndProc(ref m);
                try
                {
                    var mi = (MINMAXINFO)Marshal.PtrToStructure(m.LParam, typeof(MINMAXINFO));
                    var sc = Screen.FromHandle(Handle);
                    var wa = sc.WorkingArea; var mb = sc.Bounds;
                    mi.maxPosX = wa.Left - mb.Left; mi.maxPosY = wa.Top - mb.Top; mi.maxSizeX = wa.Width; mi.maxSizeY = wa.Height;
                    Marshal.StructureToPtr(mi, m.LParam, false);
                }
                catch { }
                return;
            }
            if (m.Msg == WM_NCHITTEST && WindowState == FormWindowState.Normal && !full)
            {
                long l = m.LParam.ToInt64();
                int x = (short)(l & 0xFFFF), y = (short)((l >> 16) & 0xFFFF);
                var r = Bounds;
                bool L = x < r.Left + Edge, R = x >= r.Right - Edge, T = y < r.Top + Edge, B = y >= r.Bottom - Edge;
                int ht = 0;
                if (T && L) ht = 13; else if (T && R) ht = 14; else if (B && L) ht = 16; else if (B && R) ht = 17;
                else if (L) ht = 10; else if (R) ht = 11; else if (T) ht = 12; else if (B) ht = 15;
                if (ht != 0) { m.Result = (IntPtr)ht; return; }
            }
            base.WndProc(ref m);
        }

        void Style()
        {
            Corners.Apply(this, WindowState == FormWindowState.Maximized || full);
        }

        protected override void OnLocationChanged(EventArgs e) { base.OnLocationChanged(e); if (WindowState != FormWindowState.Minimized) Wv.Sync(wv); }
        protected override void OnSizeChanged(EventArgs e) { base.OnSizeChanged(e); if (WindowState != FormWindowState.Minimized) Wv.Sync(wv); }

        protected override void OnHandleCreated(EventArgs e) { base.OnHandleCreated(e); Style(); }

        // Без лінії обводки: смуга по краях (для зміни розміру) має колір програми, тож зливається з нею;
        // заокруглення дає сама Windows 11 (DWM), системну кольорову рамку вимкнено.

        // Поки вікно згорнуте, не міняємо розмір сторінки — інакше вона «перебудовується» при відкритті
        protected override void OnLayout(LayoutEventArgs levent)
        {
            if (WindowState == FormWindowState.Minimized) return;
            base.OnLayout(levent);
        }

        FormWindowState lastWs = FormWindowState.Normal;

        // Повна перебудова після зміни стану вікна (розгорнути/повернути): розмір WebView, відступи, кути, стан сторінки
        void Reconcile()
        {
            try
            {
                if (WindowState == FormWindowState.Minimized) return;
                Wv.Sync(wv);   // без перемикання Dock/Padding і без зайвих перемальовувань — саме вони давали мерехтіння
                Style();
                SyncUi();
            }
            catch { }
        }

        System.Windows.Forms.Timer rcT;
        void ReconcileSoon()
        {
            // один відкладений прохід (після завершення анімації розгортання) замість серії
            if (rcT == null) { rcT = new System.Windows.Forms.Timer { Interval = 120 }; rcT.Tick += (s, e) => { rcT.Stop(); Reconcile(); }; }
            rcT.Stop(); rcT.Start();
        }

        void OnState()
        {
            if (WindowState == FormWindowState.Minimized) return;
            if (WindowState != lastWs) { lastWs = WindowState; Style(); ReconcileSoon(); }
        }

        // Єдине джерело правди для сторінки: стан «на весь екран» і «розгорнуто» беремо з вікна, а не з пам'яті сторінки
        void SyncUi()
        {
            try
            {
                if (wv.CoreWebView2 == null) return;
                bool mx = WindowState == FormWindowState.Maximized;
                wv.CoreWebView2.ExecuteScriptAsync("window.__wmax&&window.__wmax(" + (mx ? "true" : "false") + ");window.__fs&&window.__fs(" + (full ? "true" : "false") + ");window.dispatchEvent(new Event('resize'))");
            }
            catch { }
        }

        static string DataDir()
        {
            string cfg = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "AniSyns");
            try { var v = File.ReadAllText(Path.Combine(cfg, "location.txt")).Trim(); if (v.Length > 0) return v; } catch { }
            return cfg;
        }

        static async Task<bool> Ping()
        {
            try
            {
                using (var h = new HttpClient { Timeout = TimeSpan.FromSeconds(1.5) })
                { var r = await h.GetAsync(Url); return r.IsSuccessStatusCode; }
            }
            catch { return false; }
        }

        async Task Start()
        {
            try
            {
                var opts = new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required --disable-features=msSmartScreenProtection --ignore-gpu-blocklist --enable-gpu-rasterization --renderer-process-limit=1");
                string ud = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AniSyns", "webview");
                env = await CoreWebView2Environment.CreateAsync(null, ud, opts);
                await wv.EnsureCoreWebView2Async(env);
                var c = wv.CoreWebView2;
                var st = c.Settings;
                st.AreDevToolsEnabled = false;
                st.IsStatusBarEnabled = false;
                st.IsZoomControlEnabled = false;
                st.IsPasswordAutosaveEnabled = false;
                st.IsGeneralAutofillEnabled = false;
                st.IsSwipeNavigationEnabled = false;
                st.AreBrowserAcceleratorKeysEnabled = false;
                c.NewWindowRequested += (s, e) =>
                {
                    e.Handled = true;
                    try { Process.Start(new ProcessStartInfo(e.Uri) { UseShellExecute = true }); } catch { }
                };
                c.ContainsFullScreenElementChanged += (s, e) => SetFull(c.ContainsFullScreenElement);
                c.PermissionRequested += (s, e) => e.State = CoreWebView2PermissionState.Allow;
                // меню браузера не потрібне: лишаємо тільки вирізати/копіювати/вставити в полях введення
                c.ContextMenuRequested += (s, e) =>
                {
                    if (!e.ContextMenuTarget.IsEditable) { e.Handled = true; return; }
                    for (int i = e.MenuItems.Count - 1; i >= 0; i--)
                    {
                        string n = e.MenuItems[i].Name;
                        if (n != "cut" && n != "copy" && n != "paste" && n != "selectAll" && n != "undo" && n != "redo") e.MenuItems.RemoveAt(i);
                    }
                };
                // кнопки вікна та перетягування шапки приходять зі сторінки
                c.WebMessageReceived += (s, e) =>
                {
                    string msg = e.TryGetWebMessageAsString();
                    if (msg == "min") WindowState = FormWindowState.Minimized;
                    else if (msg == "max") { if (!full) WindowState = WindowState == FormWindowState.Maximized ? FormWindowState.Normal : FormWindowState.Maximized; }
                    else if (msg == "close") Close();
                    else if (msg == "fs") SetFull(!full);
                    else if (msg == "fsoff") { if (full) SetFull(false); }
                    else if (msg == "fson") { if (!full) SetFull(true); }
                    else if (msg == "pipopen") OpenPip();
                    else if (msg == "pipclose") { if (pip != null && !pip.IsDisposed) { pip.Silent = true; pip.Close(); } }
                    else if (msg == "front") { if (WindowState == FormWindowState.Minimized) ShowWindow(Handle, 9); Activate(); }
                    else if (msg != null && msg.StartsWith("pip>")) { try { if (pip != null && !pip.IsDisposed && pip.wv.CoreWebView2 != null) pip.wv.CoreWebView2.ExecuteScriptAsync("window.__pipIn&&window.__pipIn(" + msg.Substring(4) + ")"); } catch { } }
                    else if (msg != null && msg.StartsWith("rz:") && !full && WindowState == FormWindowState.Normal) { int ht; if (int.TryParse(msg.Substring(3), out ht) && ht >= 10 && ht <= 17) { ReleaseCapture(); SendMessage(Handle, 0xA1, (IntPtr)ht, IntPtr.Zero); } }
                    else if (msg == "drag" && !full) { ReleaseCapture(); SendMessage(Handle, 0xA1, (IntPtr)2, IntPtr.Zero); }  // WM_NCLBUTTONDOWN, HTCAPTION
                };
                c.NavigationCompleted += (s, e) =>
                {
                    OnState();
                };
                // Одразу показуємо ту саму заставку, що й на iOS, поки стартує сервер
                var t0 = DateTime.UtcNow;
                try { c.NavigateToString(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "splash.html"))); } catch { }
                if (!await Ping())
                {
                    string exe = Path.Combine(AppContext.BaseDirectory, "server", "AniSynsServer.exe");
                    server = Process.Start(new ProcessStartInfo(exe, "--parent " + Process.GetCurrentProcess().Id)
                    { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Path.GetDirectoryName(exe) });
                    bool ok = false;
                    for (int i = 0; i < 80 && !ok; i++)
                    {
                        ok = await Ping();
                        if (!ok) await Task.Delay(300);
                    }
                    if (!ok) throw new Exception("Сервер не відповів. Журнал: " + Path.Combine(DataDir(), "server.log"));
                }
                var left = 1900 - (int)(DateTime.UtcNow - t0).TotalMilliseconds;  // даємо заставці програти вступ
                if (left > 0) await Task.Delay(left);
                await c.AddScriptToExecuteOnDocumentCreatedAsync("window.__hostsplash=1");
                c.Navigate(Url);
                StartWatchdog();
            }
            catch (Exception ex)
            {
                string m = ex.Message;
                if (ex is WebView2RuntimeNotFoundException)
                    m = "Не знайдено WebView2 Runtime (компонент Windows). Встановіть його з microsoft.com/edge/webview2 або перевстановіть AniSyns.";
                MessageBox.Show(m, "AniSyns", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Close();
            }
        }

        // Сторож сервера: якщо Python-процес помер або не відповідає ~15 с — перезапускаємо його й оновлюємо сторінку
        System.Windows.Forms.Timer wd;
        int wdFails;
        bool wdBusy;

        static async Task<bool> PingApi()
        {
            try
            {
                using (var h = new HttpClient { Timeout = TimeSpan.FromSeconds(2.5) })
                { var r = await h.GetAsync(Url + "api/ping"); return r.IsSuccessStatusCode; }
            }
            catch { return false; }
        }

        void StartWatchdog()
        {
            if (wd != null) return;
            wd = new System.Windows.Forms.Timer { Interval = 5000 };
            wd.Tick += async (s, e) =>
            {
                if (wdBusy) return;
                wdBusy = true;
                try
                {
                    if (await PingApi()) { wdFails = 0; return; }
                    if (++wdFails < 3) return;
                    wdFails = 0;
                    await RestartServer();
                }
                catch { }
                finally { wdBusy = false; }
            };
            wd.Start();
        }

        async Task RestartServer()
        {
            try { if (server != null && !server.HasExited) server.Kill(true); } catch { }
            string exe = Path.Combine(AppContext.BaseDirectory, "server", "AniSynsServer.exe");
            server = Process.Start(new ProcessStartInfo(exe, "--parent " + Process.GetCurrentProcess().Id)
            { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Path.GetDirectoryName(exe) });
            for (int i = 0; i < 100; i++)
            {
                if (await Ping()) { try { wv.CoreWebView2.Reload(); } catch { } return; }
                await Task.Delay(300);
            }
        }

        // Власне вікно «картинка в картинці» у стилі програми (без адреси та іконки сайту)
        void OpenPip()
        {
            if (pip != null && !pip.IsDisposed) return;
            var p = new PipForm(env, Screen.FromControl(this).WorkingArea, Url + "pip");
            pip = p;
            p.Msg = m =>
            {
                try { if (wv.CoreWebView2 != null) wv.CoreWebView2.ExecuteScriptAsync("window.__pipMsg&&window.__pipMsg(" + m + ")"); } catch { }
            };
            p.FormClosed += (s, e) =>
            {
                if (pip == p) pip = null;
                if (!p.Silent) { try { if (wv.CoreWebView2 != null) wv.CoreWebView2.ExecuteScriptAsync("window.__pipMsg&&window.__pipMsg({\"t\":\"closed\"})"); } catch { } }
            };
            p.Show();
        }

        // Повний екран відео (наш плеєр): вікно на весь екран без відступів, і навпаки
        void SetFull(bool on)
        {
            if (on == full) return;
            if (on)
            {
                prevState = WindowState; prevBounds = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
                full = true;
                Padding = Padding.Empty;
                WindowState = FormWindowState.Normal;
                Bounds = Screen.FromControl(this).Bounds;
                Tell(true);
                Nudge();
            }
            else
            {
                full = false;
                Bounds = prevBounds;
                WindowState = prevState;
                OnState();
                Tell(false);
                SyncUi();
                Nudge();
            }
        }

        void Tell(bool on)
        {
            try { if (wv.CoreWebView2 != null) wv.CoreWebView2.ExecuteScriptAsync("window.__fs&&window.__fs(" + (on ? "true" : "false") + ")"); } catch { }
        }

        // WebView2 після виходу з повного екрана може лишити старий розмір сторінки — підштовхуємо розмір
        void Nudge() { ReconcileSoon(); }

        void StopServer()
        {
            try { if (server != null && !server.HasExited) server.Kill(true); } catch { }
        }
    }

    // Вікно PiP: безрамкове, поверх усіх вікон, зі збереженням пропорцій відео
    class PipForm : Form
    {
        const int Edge = 0;
        public readonly WebView2 wv = new WebView2();
        public Action<string> Msg;
        public bool Silent;
        double ratio = 16.0 / 9;
        readonly CoreWebView2Environment env;
        readonly string url;
        static readonly string Cfg = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AniSyns", "pip.txt");

        [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int val, int size);
        [DllImport("user32.dll")] static extern bool ReleaseCapture();
        [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr hwnd, int msg, IntPtr w, IntPtr l);
        [StructLayout(LayoutKind.Sequential)] struct RECT { public int left, top, right, bottom; }

        public PipForm(CoreWebView2Environment env, Rectangle area, string url)
        {
            this.env = env; this.url = url;
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.Manual;
            ShowInTaskbar = false;
            TopMost = true;
            BackColor = Color.Black;
            Padding = new Padding(Edge);
            Text = "AniSyns";
            MinimumSize = new Size(280, 170);
            int w = 480 + 2 * Edge, h = 270 + 2 * Edge;
            var r = new Rectangle(area.Right - w - 24, area.Bottom - h - 24, w, h);
            try
            {
                var p = File.ReadAllText(Cfg).Trim().Split(',');
                var s = new Rectangle(int.Parse(p[0]), int.Parse(p[1]), int.Parse(p[2]), int.Parse(p[3]));
                foreach (var sc in Screen.AllScreens) if (sc.WorkingArea.IntersectsWith(s)) { r = s; break; }
            }
            catch { }
            Bounds = r;
            wv.Dock = DockStyle.Fill;
            wv.DefaultBackgroundColor = Color.Black;
            Controls.Add(wv);
            FormClosing += (s, e) => { try { Directory.CreateDirectory(Path.GetDirectoryName(Cfg)); File.WriteAllText(Cfg, Bounds.X + "," + Bounds.Y + "," + Bounds.Width + "," + Bounds.Height); } catch { } };
            Shown += async (s, e) => await Init();
        }

        protected override bool ShowWithoutActivation { get { return true; } }

        protected override CreateParams CreateParams
        {
            get
            {
                var cp = base.CreateParams;
                cp.Style |= 0x40000;  // WS_THICKFRAME
                cp.ExStyle |= 0x80;   // WS_EX_TOOLWINDOW: без запису в Alt+Tab
                return cp;
            }
        }

        async Task Init()
        {
            try
            {
                await wv.EnsureCoreWebView2Async(env);
                var c = wv.CoreWebView2;
                c.Settings.AreDevToolsEnabled = false;
                c.Settings.IsStatusBarEnabled = false;
                c.Settings.IsZoomControlEnabled = false;
                c.Settings.AreBrowserAcceleratorKeysEnabled = false;
                c.PermissionRequested += (s, e) => e.State = CoreWebView2PermissionState.Allow;
                c.NewWindowRequested += (s, e) => e.Handled = true;
                c.ContextMenuRequested += (s, e) => e.Handled = true;
                c.WebMessageReceived += (s, e) =>
                {
                    string m = e.TryGetWebMessageAsString();
                    if (m == null) return;
                    if (m == "pipdrag") { ReleaseCapture(); SendMessage(Handle, 0xA1, (IntPtr)2, IntPtr.Zero); }
                    else if (m.StartsWith("rz:")) { int ht; if (int.TryParse(m.Substring(3), out ht) && ht >= 10 && ht <= 17) { ReleaseCapture(); SendMessage(Handle, 0xA1, (IntPtr)ht, IntPtr.Zero); } }
                    else if (m.StartsWith("ar:"))
                    {
                        double r;
                        if (double.TryParse(m.Substring(3), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out r) && r > 0.3 && r < 4)
                        {
                            ratio = r;
                            Height = (int)((Width - 2 * Edge) / ratio) + 2 * Edge;
                        }
                    }
                    else if (m.StartsWith("main>") && Msg != null) Msg(m.Substring(5));
                };
                c.Navigate(url);
            }
            catch { Close(); }
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            Corners.Apply(this, false);
        }

        protected override void OnResize(EventArgs e) { base.OnResize(e); Corners.Apply(this, false); Wv.Sync(wv); }
        protected override void OnLocationChanged(EventArgs e) { base.OnLocationChanged(e); Wv.Sync(wv); }

        protected override void WndProc(ref Message m)
        {
            const int WM_NCCALCSIZE = 0x83, WM_NCHITTEST = 0x84, WM_NCACTIVATE = 0x86, WM_NCPAINT = 0x85, WM_SIZING = 0x214;
            if (m.Msg == WM_NCACTIVATE) { m.LParam = (IntPtr)(-1); base.WndProc(ref m); return; }
            if (m.Msg == WM_NCPAINT) { m.Result = IntPtr.Zero; return; }
            if (m.Msg == WM_NCCALCSIZE && m.WParam != IntPtr.Zero) { m.Result = IntPtr.Zero; return; }
            if (m.Msg == WM_SIZING)
            {
                var r = (RECT)Marshal.PtrToStructure(m.LParam, typeof(RECT));
                int edge = m.WParam.ToInt32();
                int cw = r.right - r.left - 2 * Edge, ch = r.bottom - r.top - 2 * Edge;
                if (edge == 3 || edge == 6) { cw = (int)(ch * ratio); r.right = r.left + cw + 2 * Edge; }
                else
                {
                    ch = (int)(cw / ratio);
                    if (edge == 4 || edge == 5) r.top = r.bottom - ch - 2 * Edge; else r.bottom = r.top + ch + 2 * Edge;
                }
                Marshal.StructureToPtr(r, m.LParam, false);
                m.Result = (IntPtr)1;
                return;
            }
            if (m.Msg == WM_NCHITTEST)
            {
                long l = m.LParam.ToInt64();
                int x = (short)(l & 0xFFFF), y = (short)((l >> 16) & 0xFFFF);
                var b = Bounds;
                bool L = x < b.Left + Edge, R = x >= b.Right - Edge, T = y < b.Top + Edge, B = y >= b.Bottom - Edge;
                int ht = 0;
                if (T && L) ht = 13; else if (T && R) ht = 14; else if (B && L) ht = 16; else if (B && R) ht = 17;
                else if (L) ht = 10; else if (R) ht = 11; else if (T) ht = 12; else if (B) ht = 15;
                if (ht != 0) { m.Result = (IntPtr)ht; return; }
            }
            base.WndProc(ref m);
        }
    }
}
