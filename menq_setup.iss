; MenQ — Windows installer script (Inno Setup)
; Produces MenQ-Setup.exe: a single install file to hand to any shop.
; Per-user install (no admin needed), creates Start Menu + optional desktop shortcut.

[Setup]
AppId={{7E1C0E64-8A2B-4E2E-9C3A-2B1D9F4C0A11}
AppName=MenQ
AppVersion=1.0
AppVerName=MenQ 1.0
AppPublisher=MenQ
DefaultDirName={localappdata}\Programs\MenQ
DefaultGroupName=MenQ
DisableProgramGroupPage=yes
UninstallDisplayName=MenQ
UninstallDisplayIcon={app}\MenQ.exe
SetupIconFile=C:\Users\Admin\Desktop\MenQ\menq.ico
PrivilegesRequired=lowest
AppMutex=MenQ_SingleInstance_Mutex
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=C:\Users\Admin\Desktop\MenQ\installer
OutputBaseFilename=MenQ-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "C:\Users\Admin\Desktop\MenQ\dist\MenQ.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "C:\Users\Admin\Desktop\MenQ\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\MenQ"; Filename: "{app}\MenQ.exe"
Name: "{group}\MenQ (LAN mode)"; Filename: "{app}\MenQ.exe"; Parameters: "lan"; Comment: "Serve to other devices on the same Wi-Fi (change the admin password first)"
Name: "{group}\Uninstall MenQ"; Filename: "{uninstallexe}"
Name: "{userdesktop}\MenQ"; Filename: "{app}\MenQ.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MenQ.exe"; Description: "Launch MenQ now"; Flags: nowait postinstall skipifsilent

[Code]
function OldUninstaller(): String;
var s: String;
begin
  Result := '';
  if RegQueryStringValue(HKCU,
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E1C0E64-8A2B-4E2E-9C3A-2B1D9F4C0A11}_is1',
    'UninstallString', s) then
    Result := RemoveQuotes(s);
end;

function InitializeSetup(): Boolean;
var un: String; rc, ec, i: Integer;
begin
  Result := True;
  if WizardSilent() then Exit;   // unattended/silent = just update, never prompt
  un := OldUninstaller();
  if un <> '' then
  begin
    rc := MsgBox('MenQ is already installed on this computer.' + #13#10#13#10 +
      'Yes  =  Update (install over the existing one)' + #13#10 +
      'No   =  Remove the old version first, then install clean' + #13#10 +
      'Cancel  =  Do nothing',
      mbConfirmation, MB_YESNOCANCEL);
    if rc = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
    if rc = IDNO then
    begin
      if Exec(un, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '',
              SW_HIDE, ewWaitUntilTerminated, ec) then
      begin
        { unins000.exe relaunches itself from a temp copy and returns early, so }
        { ewWaitUntilTerminated is NOT enough — wait until the old uninstaller  }
        { file is truly gone before we install over it (max ~20s). }
        i := 0;
        while (i < 100) and FileExists(un) do
        begin
          Sleep(200);
          i := i + 1;
        end;
      end;
    end;
  end;
end;
