# AniSyns для ПК
Використовує ту саму `src/anisyns/anime_app.py`, що й iOS (одна копія).
Запуск: `pip install requests`, потім `python pc/anisyns_pc.py`.
Дані: Windows %APPDATA%\AniSyns, macOS ~/Library/Application Support/AniSyns, Linux ~/.local/share/AniSyns.

## Вимикачі збірок (економія лімітів)
GitHub → Settings → Secrets and variables → Actions → Variables:
- BUILD_IOS = false — не збирати IPA після push
- BUILD_PC = false — не збирати ПК після push
Ручний запуск (Actions → Run workflow) працює завжди. Після push ПК збирається лише для Windows; macOS/Linux — галочками при ручному запуску.
