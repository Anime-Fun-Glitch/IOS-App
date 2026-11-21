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
        [STAThread]
        static void Main()
        {
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
        static readonly Color Bg = Color.FromArgb(13, 14, 19);
        readonly WebView2 wv = new WebView2();
        Process server;
        bool full;
        FormBorderStyle prevStyle;
        FormWindowState prevState;
        Rectangle prevBounds;

        [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int val, int size);

        public MainForm()
        {
            Text = "AniSyns";
            BackColor = Bg;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(1280, 800);
            MinimumSize = new Size(480, 640);
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
            wv.Dock = DockStyle.Fill;
            wv.DefaultBackgroundColor = Bg;
            Controls.Add(wv);
            Shown += async (s, e) => await Start();
            FormClosed += (s, e) => StopServer();
        }

        // Заголовок вікна у кольори програми, без світлої рамки (Windows 11; на Windows 10 — хоча б темний заголовок)
        void Style()
        {
            try
            {
                int one = 1, bg = 0x130E0D, txt = 0xF0EAE6;  // COLORREF = 0x00BBGGRR
                DwmSetWindowAttribute(Handle, 19, ref one, 4);
                DwmSetWindowAttribute(Handle, 20, ref one, 4);
                DwmSetWindowAttribute(Handle, 35, ref bg, 4);   // колір заголовка
                DwmSetWindowAttribute(Handle, 34, ref bg, 4);   // колір рамки
                DwmSetWindowAttribute(Handle, 36, ref txt, 4);  // колір тексту
            }
            catch { }
        }

        protected override void OnHandleCreated(EventArgs e) { base.OnHandleCreated(e); Style(); }

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
                var opts = new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required --disable-features=msSmartScreenProtection");
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
                c.DocumentTitleChanged += (s, e) => { };
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

        // Повний екран відео: ховаємо рамку вікна, і навпаки
        void SetFull(bool on)
        {
            if (on == full) return;
            full = on;
            if (on)
            {
                prevStyle = FormBorderStyle; prevState = WindowState; prevBounds = Bounds;
                FormBorderStyle = FormBorderStyle.None;
                WindowState = FormWindowState.Normal;
                Bounds = Screen.FromControl(this).Bounds;
            }
            else
            {
                FormBorderStyle = prevStyle;
                Bounds = prevBounds;
                WindowState = prevState;
                Style();
            }
        }

        void StopServer()
        {
            try { if (server != null && !server.HasExited) server.Kill(true); } catch { }
        }
    }
}
