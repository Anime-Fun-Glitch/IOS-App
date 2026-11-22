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
        Splash splash;
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
            splash = new Splash { Dock = DockStyle.Fill };
            Controls.Add(splash);  // екран завантаження поверх сторінки, поки сервер і вікно не готові
            splash.BringToFront();
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
            const int WM_NCCALCSIZE = 0x83, WM_NCHITTEST = 0x84, WM_NCACTIVATE = 0x86, WM_NCPAINT = 0x85;
            if (m.Msg == WM_NCACTIVATE) { m.LParam = (IntPtr)(-1); base.WndProc(ref m); return; }  // не перемальовувати системну рамку при втраті фокуса
            if (m.Msg == WM_NCPAINT) { m.Result = IntPtr.Zero; return; }
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
                    else if (msg == "fs") SetFull(!full);
                    else if (msg == "fsoff") { if (full) SetFull(false); }
                    else if (msg == "drag" && !full) { ReleaseCapture(); SendMessage(Handle, 0xA1, (IntPtr)2, IntPtr.Zero); }  // WM_NCLBUTTONDOWN, HTCAPTION
                };
                c.NavigationCompleted += (s, e) =>
                {
                    OnState();
                    if (splash != null)
                    {
                        var sp = splash; splash = null;
                        Controls.Remove(sp); sp.Dispose();  // далі показує свою заставку сама сторінка
                    }
                };
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
                Tell(true);
            }
            else
            {
                full = false;
                Bounds = prevBounds;
                WindowState = prevState;
                OnState();
                Tell(false);
                Nudge();
            }
        }

        void Tell(bool on)
        {
            try { if (wv.CoreWebView2 != null) wv.CoreWebView2.ExecuteScriptAsync("window.__fs&&window.__fs(" + (on ? "true" : "false") + ")"); } catch { }
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

    // Екран завантаження: темний фон, логотип, назва та рухома смужка (як заставка на iOS)
    class Splash : Control
    {
        readonly System.Windows.Forms.Timer tm = new System.Windows.Forms.Timer { Interval = 16 };
        readonly Stopwatch sw = Stopwatch.StartNew();
        Image logo;

        public Splash()
        {
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint | ControlStyles.ResizeRedraw, true);
            BackColor = Color.FromArgb(13, 14, 19);
            try { logo = Image.FromFile(Path.Combine(AppContext.BaseDirectory, "splash.png")); } catch { }
            tm.Tick += (s, e) => Invalidate();
            tm.Start();
        }

        protected override void Dispose(bool d)
        {
            if (d) { tm.Stop(); tm.Dispose(); if (logo != null) logo.Dispose(); }
            base.Dispose(d);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.Clear(BackColor);
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
            g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
            float t = (float)sw.Elapsed.TotalSeconds;
            float cx = Width / 2f, cy = Height / 2f - 30;

            // м'яке фіолетове сяйво
            float gr = 230 + 10 * (float)Math.Sin(t * 2);
            using (var path = new System.Drawing.Drawing2D.GraphicsPath())
            {
                path.AddEllipse(cx - gr, cy - gr, gr * 2, gr * 2);
                using (var pb = new System.Drawing.Drawing2D.PathGradientBrush(path))
                {
                    pb.CenterColor = Color.FromArgb(70, 109, 76, 255);
                    pb.SurroundColors = new[] { Color.FromArgb(0, 109, 76, 255) };
                    g.FillPath(pb, path);
                }
            }

            // логотип: поява + легке «биття»
            float appear = Math.Min(1f, t / 0.7f);
            float k = (0.8f + 0.2f * (1 - (float)Math.Pow(1 - appear, 3))) * (1 + 0.025f * (float)Math.Sin(t * 4.2));
            float sz = 150 * k;
            if (logo != null)
            {
                var cm = new System.Drawing.Imaging.ColorMatrix { Matrix33 = appear };
                using (var ia = new System.Drawing.Imaging.ImageAttributes())
                {
                    ia.SetColorMatrix(cm);
                    g.DrawImage(logo, new Rectangle((int)(cx - sz / 2), (int)(cy - sz / 2), (int)sz, (int)sz), 0, 0, logo.Width, logo.Height, GraphicsUnit.Pixel, ia);
                }
            }

            // назва
            float na = Math.Max(0f, Math.Min(1f, (t - 0.5f) / 0.6f));
            using (var f = new Font("Segoe UI", 26f, FontStyle.Bold, GraphicsUnit.Point))
            {
                var s1 = g.MeasureString("Ani", f, 0, StringFormat.GenericTypographic);
                var s2 = g.MeasureString("Syns", f, 0, StringFormat.GenericTypographic);
                float x = cx - (s1.Width + s2.Width) / 2, y = cy + 100;
                using (var b1 = new SolidBrush(Color.FromArgb((int)(255 * na), 255, 255, 255)))
                    g.DrawString("Ani", f, b1, x, y, StringFormat.GenericTypographic);
                using (var b2 = new System.Drawing.Drawing2D.LinearGradientBrush(new RectangleF(x + s1.Width, y, s2.Width, s2.Height), Color.FromArgb((int)(255 * na), 155, 107, 255), Color.FromArgb((int)(255 * na), 95, 176, 255), 0f))
                    g.DrawString("Syns", f, b2, x + s1.Width, y, StringFormat.GenericTypographic);
            }
            using (var f = new Font("Segoe UI", 10f, FontStyle.Regular, GraphicsUnit.Point))
            using (var b = new SolidBrush(Color.FromArgb((int)(255 * na), 154, 160, 184)))
            {
                var sf = new StringFormat { Alignment = StringAlignment.Center };
                g.DrawString("The way to watch Anime with comfort!", f, b, cx, cy + 148, sf);
            }

            // рухома смужка завантаження
            float bw = 120, bh = 3, bx = cx - bw / 2, by = Height - 64;
            using (var bg = new SolidBrush(Color.FromArgb(20, 255, 255, 255)))
                g.FillRectangle(bg, bx, by, bw, bh);
            float ph = (t % 1.2f) / 1.2f, seg = bw * 0.45f;
            float sx = bx - seg + (bw + seg) * (ph * ph * (3 - 2 * ph));
            float x0 = Math.Max(bx, sx), x1 = Math.Min(bx + bw, sx + seg);
            if (x1 > x0)
                using (var lb = new System.Drawing.Drawing2D.LinearGradientBrush(new RectangleF(x0 - 1, by, x1 - x0 + 2, bh), Color.FromArgb(122, 59, 203), Color.FromArgb(102, 162, 211), 0f))
                    g.FillRectangle(lb, x0, by, x1 - x0, bh);
        }
    }
}
