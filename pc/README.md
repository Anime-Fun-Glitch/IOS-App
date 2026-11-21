# AniSyns для ПК (Windows)
Складається з трьох частин (збираються в GitHub Actions → «Build PC», результат — `AniSyns-Setup.exe`):
- `host/` — вікно на C# + WebView2 (без браузера Edge, з іконкою, у стилі програми);
- `server_main.py` — той самий `src/anisyns/anime_app.py`, що й на iOS, як фоновий сервер;
- `installer/` — інсталятор Inno Setup (оновлення поверх, ярлик у меню «Пуск»/пошуку, вибір папки даних).

Папка даних обирається в інсталяторі (за замовчуванням `%APPDATA%\AniSyns`), зберігається в `%APPDATA%\AniSyns\location.txt`.
Вимикачі збірок: Variables `BUILD_IOS` / `BUILD_PC` = false (ручний запуск працює завжди).
