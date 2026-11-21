; Інсталятор AniSyns (Inno Setup 6.4+). Збирається в CI: iscc /DAppVersion=1.0.N AniSyns.iss
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{8F3C2B1E-5A47-4C9D-9B6E-A71D0C5E2F64}
AppName=AniSyns
AppVersion={#AppVersion}
AppPublisher=AniSyns
DefaultDirName={autopf}\AniSyns
DefaultGroupName=AniSyns
UsePreviousAppDir=yes
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=out
OutputBaseFilename=AniSyns-Setup
SetupIconFile=..\host\app.ico
UninstallDisplayIcon={app}\AniSyns.exe
WizardStyle=modern dynamic
WizardImageFile=wizard.bmp,wizard2x.bmp
WizardSmallImageFile=small.bmp,small2x.bmp
Compression=lzma2/max
SolidCompression=yes
AppMutex=AniSyns.Host
CloseApplications=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
ShowLanguageDialog=no

[Languages]
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[Tasks]
Name: "desktopicon"; Description: "Створити ярлик на робочому столі"; Flags: unchecked

[Files]
Source: "..\host\publish\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\server-dist\AniSynsServer\*"; DestDir: "{app}\server"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\AniSyns"; Filename: "{app}\AniSyns.exe"; IconFilename: "{app}\AniSyns.exe"
Name: "{autodesktop}\AniSyns"; Filename: "{app}\AniSyns.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AniSyns.exe"; Description: "Запустити AniSyns"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\server"

[Code]
var
  DataPage: TInputDirWizardPage;
  ModePage: TInputOptionWizardPage;
  Existing, SameVer: Boolean;

function CfgDir: String;
begin
  Result := ExpandConstant('{userappdata}\AniSyns');
end;

function OldDataDir: String;
var s: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(CfgDir + '\location.txt', s) then
    Result := Trim(String(s));
end;

function PrevVersion: String;
begin
  Result := '';
  if not RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F3C2B1E-5A47-4C9D-9B6E-A71D0C5E2F64}_is1', 'DisplayVersion', Result) then
    if not RegQueryStringValue(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F3C2B1E-5A47-4C9D-9B6E-A71D0C5E2F64}_is1', 'DisplayVersion', Result) then
      Result := '';
end;

function HasWebView2: Boolean;
var v: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) then Result := (v <> '') and (v <> '0.0.0.0');
  if (not Result) and RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) then Result := (v <> '') and (v <> '0.0.0.0');
end;

function UpdateMode: Boolean;
begin
  Result := Existing and (ModePage <> nil) and ModePage.Values[0];
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if UpdateMode and ((PageID = wpSelectDir) or (PageID = DataPage.ID)) then Result := True;
end;

procedure InitializeWizard;
var d, pv: String;
begin
  pv := PrevVersion;
  Existing := pv <> '';
  SameVer := Existing and (pv = '{#AppVersion}');
  DataPage := CreateInputDirPage(wpSelectDir, 'Папка даних', 'Де зберігати бібліотеку, відео та налаштування?',
    'Тут житимуть завантажені серії та база програми. Її можна розмістити на іншому диску. Оновлення програми цю папку не змінює.', False, '');
  DataPage.Add('');
  d := OldDataDir;
  if d = '' then d := CfgDir;
  DataPage.Values[0] := d;
  if Existing then
  begin
    if SameVer then
      ModePage := CreateInputOptionPage(wpWelcome, 'AniSyns уже встановлено', 'У вас уже встановлена ця версія (' + pv + ').',
        'Перевстановити файли програми? Бібліотека, завантажені серії та налаштування не зачіпаються.', True, False)
    else
      ModePage := CreateInputOptionPage(wpWelcome, 'Знайдено встановлену версію ' + pv, 'Оновити AniSyns до версії {#AppVersion}?',
        'Бібліотека, завантажені серії та налаштування залишаться на місці.', True, False);
    if SameVer then ModePage.Add('Перевстановити файли програми (рекомендовано)') else ModePage.Add('Оновити програму (рекомендовано)');
    ModePage.Add('Нове встановлення: обрати папку програми та папку даних');
    ModePage.Values[0] := True;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var rc: Integer; tmp: String;
begin
  if CurStep = ssPostInstall then
  begin
    ForceDirectories(CfgDir);
    if not UpdateMode then ForceDirectories(DataPage.Values[0]);
    if (not UpdateMode) or (OldDataDir = '') then SaveStringToFile(CfgDir + '\location.txt', DataPage.Values[0], False);
    if not HasWebView2 then
    begin
      // компонент Windows для відображення інтерфейсу; на Windows 11 та свіжій Windows 10 він уже є
      try
        DownloadTemporaryFile('https://go.microsoft.com/fwlink/p/?LinkId=2124703', 'MicrosoftEdgeWebview2Setup.exe', '', nil);
        tmp := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');
        Exec(tmp, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, rc);
      except
        MsgBox('Не вдалося автоматично встановити компонент WebView2. Встановіть його вручну з microsoft.com/edge/webview2 — без нього вікно програми не відкриється.', mbInformation, MB_OK);
      end;
    end;
  end;
end;
