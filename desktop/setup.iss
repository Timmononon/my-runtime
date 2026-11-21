; The compiled installer contains the app and the entire Python runtime.
[Setup]
AppId=Aster.Desktop.Windows
SetupMutex=Aster.Desktop.Windows.Setup
AppName=Aster
AppVersion={#AsterVersion}
AppPublisher=Aster
VersionInfoVersion={#AsterVersion}.0
VersionInfoProductName=Aster
VersionInfoDescription=Aster Desktop Setup
DefaultDirName={localappdata}\Programs\Aster
DefaultGroupName=Aster
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableProgramGroupPage=auto
AllowNoIcons=yes
WizardStyle=modern
SetupIconFile={#DesktopSource}\assets\aster.ico
UninstallDisplayIcon={app}\Aster.exe
OutputBaseFilename=Aster-Setup-{#AsterVersion}-win-x64
Compression=lzma2/normal
SolidCompression=yes
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinese"; MessagesFile: "{#ChineseMessages}"

[CustomMessages]
english.CloseAster=In Aster, choose "Quit and stop backend" from the Aster menu. Then try again.
chinese.CloseAster=请先在 Aster 菜单选择「退出并停止后台」，然后重试。
english.EmptyFolder=Please choose an empty folder for Aster.
chinese.EmptyFolder=请选择一个空文件夹安装 Aster。
english.PreviousLocation=Aster is already installed. Use the original installation folder, or uninstall it first.
chinese.PreviousLocation=Aster 已安装。请选择原安装目录，或先卸载原版本。

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#ElectronApp}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RuntimeSource}\*"; DestDir: "{app}\resources\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Aster"; Filename: "{app}\Aster.exe"; AppUserModelID: "Aster.Desktop"; Check: LinkAvailable('{group}\Aster.lnk'); Flags: uninsneveruninstall
Name: "{autodesktop}\Aster"; Filename: "{app}\Aster.exe"; AppUserModelID: "Aster.Desktop"; Tasks: desktopicon; Check: LinkAvailable('{autodesktop}\Aster.lnk'); Flags: uninsneveruninstall

[Run]
Filename: "{app}\Aster.exe"; Description: "{cm:LaunchProgram,Aster}"; Flags: postinstall nowait skipifsilent

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\Aster.Desktop.Windows_is1';

function SameFolder(A, B: String): Boolean;
begin
  Result := CompareText(RemoveBackslashUnlessRoot(ExpandFileName(A)),
    RemoveBackslashUnlessRoot(ExpandFileName(B))) = 0;
end;

function FolderHasFiles(Dir: String): Boolean;
var
  Entry: TFindRec;
begin
  Result := False;
  if FindFirst(AddBackslash(Dir) + '*', Entry) then begin
    try
      repeat
        if (Entry.Name <> '.') and (Entry.Name <> '..') then begin
          Result := True;
          Break;
        end;
      until not FindNext(Entry);
    finally
      FindClose(Entry);
    end;
  end;
end;

function StopInstalledRuntime(): Boolean;
var
  Python, Launcher: String;
  ExitCode: Integer;
begin
  Python := ExpandConstant('{app}\resources\runtime\python.exe');
  Launcher := ExpandConstant('{app}\resources\launch.py');
  Result := Exec(Python, '-B -X utf8 "' + Launcher + '" --stop-installed',
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Previous, Target: String;
begin
  Result := '';
  Target := ExpandConstant('{app}');
  if RegQueryStringValue(HKCU, UninstallKey, 'InstallLocation', Previous) and
      (Previous <> '') and DirExists(Previous) then begin
    if not SameFolder(Previous, Target) then
      Result := CustomMessage('PreviousLocation')
    else if not StopInstalledRuntime() then
      Result := CustomMessage('CloseAster');
  end else if FolderHasFiles(Target) then
    Result := CustomMessage('EmptyFolder');
end;

function IsOurLink(Path: String): Boolean;
var
  Shell, Link: Variant;
begin
  Result := False;
  if FileExists(Path) then begin
    try
      Shell := CreateOleObject('WScript.Shell');
      Link := Shell.CreateShortcut(Path);
      Result := CompareText(Link.TargetPath, ExpandConstant('{app}\Aster.exe')) = 0;
    except
      Log('Unable to inspect shortcut; leaving it in place.');
    end;
  end;
end;

function LinkAvailable(Location: String): Boolean;
var
  Path: String;
begin
  Path := ExpandConstant(Location);
  Result := not FileExists(Path) or IsOurLink(Path);
end;

procedure RemoveOurLink(Location: String; RemoveEmptyGroup: Boolean);
var
  Path: String;
begin
  Path := ExpandConstant(Location);
  if not IsOurLink(Path) then Exit;
  if DeleteFile(Path) and RemoveEmptyGroup then
    RemoveDir(ExtractFileDir(Path));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then begin
    if not StopInstalledRuntime() then begin
      SuppressibleMsgBox(CustomMessage('CloseAster'), mbError, MB_OK, IDOK);
      Abort;
    end;
    RemoveOurLink('{autodesktop}\Aster.lnk', False);
    RemoveOurLink('{group}\Aster.lnk', True);
  end;
end;
