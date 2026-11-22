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
        const int Edge = 6;  // смуга по краях для зміни розміру
        static readonly Color Bg = Color.FromArgb(13, 14, 19);
        readonly WebView2 wv = new WebView2();
        Process server;
        bool full;
        FormWindowState prevState;
        Rectangle prevBounds;
        bool startMax;

        [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int val, int size);
        [DllImport("dwmapi.dll")] static extern int DwmExtendFrameIntoClientArea(IntPtr hwnd, ref MARGINS m);
        [DllImport("user32.dll")] static extern bool ReleaseCapture();
        [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr hwnd, int msg, IntPtr w, IntPtr l);

        [StructLayout(LayoutKind.Sequential)] struct MARGINS { public int l, r, t, b; }
        [StructLayout(LayoutKind.Sequential)] struct RECT { public int left, top, right, bottom; }
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
            wv.AllowExternalDrop = false;
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
            FormClosing += (s, e) => SaveState();
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
            const int WM_NCCALCSIZE = 0x83, WM_NCHITTEST = 0x84;
            if (m.Msg == WM_NCCALCSIZE && m.WParam != IntPtr.Zero)
            {
                if (WindowState == FormWindowState.Maximized && !full)
                {
                    var p = (NCCALCSIZE_PARAMS)Marshal.PtrToStructure(m.LParam, typeof(NCCALCSIZE_PARAMS));
                    var wa = Screen.FromHandle(Handle).WorkingArea;
                    p.r0.left = wa.Left; p.r0.top = wa.Top; p.r0.right = wa.Right; p.r0.bottom = wa.Bottom;
                    Marshal.StructureToPtr(p, m.LParam, false);
                }
                m.Result = IntPtr.Zero;
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
            try
            {
                int one = 1, round = 2, none = unchecked((int)0xFFFFFFFE);
                DwmSetWindowAttribute(Handle, 19, ref one, 4);
                DwmSetWindowAttribute(Handle, 20, ref one, 4);
                DwmSetWindowAttribute(Handle, 33, ref round, 4);  // заокруглені кути (Windows 11)
                DwmSetWindowAttribute(Handle, 34, ref none, 4);  // без кольорової рамки навколо вікна
            }
            catch { }
        }

        protected override void OnHandleCreated(EventArgs e) { base.OnHandleCreated(e); Style(); }

        // Тонка напівпрозора рамка у стилі програми (замість білої системної)
        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            if (WindowState == FormWindowState.Maximized || full) return;
            using (var pen = new Pen(Color.FromArgb(255, 44, 46, 60)))
                e.Graphics.DrawRectangle(pen, 0, 0, ClientSize.Width - 1, ClientSize.Height - 1);
        }

        // Поки вікно згорнуте, не міняємо розмір сторінки — інакше вона «перебудовується» при відкритті
        protected override void OnLayout(LayoutEventArgs levent)
        {
            if (WindowState == FormWindowState.Minimized) return;
            base.OnLayout(levent);
        }

        void OnState()
        {
            if (WindowState == FormWindowState.Minimized) return;
            Invalidate();
            bool mx = WindowState == FormWindowState.Maximized;
            Padding = (mx || full) ? Padding.Empty : new Padding(Edge);
            try { if (wv.CoreWebView2 != null) wv.CoreWebView2.ExecuteScriptAsync("window.__wmax&&window.__wmax(" + (mx ? "true" : "false") + ")"); } catch { }
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
                var opts = new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required --disable-features=msSmartScreenProtection --ignore-gpu-blocklist --enable-gpu-rasterization --renderer-process-limit=1");
                string ud = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AniSyns", "webview");
                var env = await CoreWebView2Environment.CreateAsync(null, ud, opts);
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
                    else if (msg == "drag" && !full) { ReleaseCapture(); SendMessage(Handle, 0xA1, (IntPtr)2, IntPtr.Zero); }  // WM_NCLBUTTONDOWN, HTCAPTION
                };
                c.NavigationCompleted += (s, e) => OnState();
                c.Navigate(Url);
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
            }
            else
            {
                full = false;
                Bounds = prevBounds;
                WindowState = prevState;
                OnState();
                Nudge();
            }
        }

        // WebView2 після виходу з повного екрана може лишити старий розмір сторінки — підштовхуємо розмір
        async void Nudge()
        {
            foreach (int d in new[] { 120, 450 })
            {
                await Task.Delay(d);
                try
                {
                    var p0 = Padding;
                    Padding = new Padding(p0.Left + 1, p0.Top, p0.Right, p0.Bottom);
                    Padding = p0;
                }
                catch { }
            }
        }

        void StopServer()
        {
            try { if (server != null && !server.HasExited) server.Kill(true); } catch { }
        }
    }
}
