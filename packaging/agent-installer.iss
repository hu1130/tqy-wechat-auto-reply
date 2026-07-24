#ifndef AgentVersion
  #error AgentVersion must be supplied by build_agent.ps1
#endif
#ifndef ReleaseRoot
  #error ReleaseRoot must be supplied by build_agent.ps1
#endif
#ifndef InstallerOutput
  #error InstallerOutput must be supplied by build_agent.ps1
#endif

#define AppName "深度觉醒个人微信客服 Agent"
#define AppPublisher "深度觉醒"
#define TaskName "DeepAwake WeChat Agent"

[Setup]
AppId={{BC28E129-7341-4B3B-9B7E-94A9FD56661D}
AppName={#AppName}
AppVersion={#AgentVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\DeepAwake\WeChatAgent
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#InstallerOutput}
OutputBaseFilename=微信客服Agent安装程序-{#AgentVersion}-x64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
UninstallDisplayIcon={app}\WeChatAIAgentLauncher.exe
CloseApplications=no
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "{#SourcePath}\ChineseSimplified.isl"

[Dirs]
Name: "{localappdata}\DeepAwake\WeChatAgent"
Name: "{localappdata}\DeepAwake\WeChatAgent\data"
Name: "{localappdata}\DeepAwake\WeChatAgent\logs"
Name: "{localappdata}\DeepAwake\WeChatAgent\reports"

[Files]
Source: "{#ReleaseRoot}\WeChatAIAgentLauncher.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ReleaseRoot}\versions\{#AgentVersion}\*"; DestDir: "{app}\versions\{#AgentVersion}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\查看 Agent 日志"; Filename: "{localappdata}\DeepAwake\WeChatAgent\logs"

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/End /TN ""{#TaskName}"""; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopAgentTask"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM WeChatAIAgentLauncher.exe"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopAgentLauncher"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM WeChatAIAgent.exe"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopAgentProcess"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""{#TaskName}"" /F"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "DeleteAgentTask"

[UninstallDelete]
Type: files; Name: "{localappdata}\DeepAwake\WeChatAgent\profile.json"
Type: files; Name: "{localappdata}\DeepAwake\WeChatAgent\agent.runtime"

[Code]
var
  ActivationPage: TInputQueryWizardPage;
  DeleteRuntimeDataCheck: TNewCheckBox;
  ActivatedThisRun: Boolean;

function ProfileDir(): String;
begin
  Result := ExpandConstant('{localappdata}\DeepAwake\WeChatAgent');
end;

function ProfileFile(): String;
begin
  Result := AddBackslash(ProfileDir()) + 'profile.json';
end;

function InitialAgentExe(): String;
begin
  Result := ExpandConstant('{app}\versions\{#AgentVersion}\WeChatAIAgent.exe');
end;

procedure InitializeWizard();
begin
  ActivationPage := CreateInputQueryPage(
    wpSelectDir,
    '激活个人微信客服 Agent',
    '请输入平台生成的一次性激活码',
    '激活码只用于本次绑定。中心地址已由发布版本固化，客户无需填写。'
  );
  ActivationPage.Add('一次性激活码：', False);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = ActivationPage.ID) and FileExists(ProfileFile());
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = ActivationPage.ID) and (Trim(ActivationPage.Values[0]) = '') then
  begin
    MsgBox('请输入一次性激活码。', mbError, MB_OK);
    Result := False;
  end;
end;

procedure StopAndRemoveLogonTask();
var
  ResultCode: Integer;
begin
  Exec(
    ExpandConstant('{sys}\schtasks.exe'),
    '/End /TN "{#TaskName}"',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    '/F /T /IM WeChatAIAgentLauncher.exe',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    '/F /T /IM WeChatAIAgent.exe',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  Sleep(500);
  Exec(
    ExpandConstant('{sys}\schtasks.exe'),
    '/Delete /TN "{#TaskName}" /F',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopAndRemoveLogonTask();
  Result := '';
end;

procedure ActivateAgent();
var
  ActivationFile: String;
  Parameters: String;
  ResultCode: Integer;
begin
  if FileExists(ProfileFile()) then
    exit;
  ActivationFile := ExpandConstant('{tmp}\wechat-agent-activation.txt');
  if not SaveStringToFile(ActivationFile, Trim(ActivationPage.Values[0]), False) then
    RaiseException('无法准备激活信息，安装已停止。');
  Parameters := 'activate --activation-file "' + ActivationFile +
    '" --profile "' + ProfileDir() + '"';
  if (not Exec(InitialAgentExe(), Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or
     (ResultCode <> 0) then
  begin
    DeleteFile(ActivationFile);
    RaiseException('Agent 激活失败。未创建自启动任务，请检查激活码和网络后重试。');
  end;
  DeleteFile(ActivationFile);
  ActivatedThisRun := True;
end;

procedure InstallLogonTask();
var
  Parameters: String;
  ResultCode: Integer;
begin
  Parameters := 'install-task --launcher "' + ExpandConstant('{app}\WeChatAIAgentLauncher.exe') +
    '" --profile "' + ProfileDir() + '" --task-name "{#TaskName}"';
  if (not Exec(InitialAgentExe(), Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or
     (ResultCode <> 0) then
  begin
    StopAndRemoveLogonTask();
    if ActivatedThisRun then
    begin
      DeleteFile(ProfileFile());
      DeleteFile(AddBackslash(ProfileDir()) + 'agent.runtime');
    end;
    RaiseException('无法创建当前用户登录任务。安装未启用自动运行。');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    StopAndRemoveLogonTask();
    ActivateAgent();
    InstallLogonTask();
    if not Exec(
      ExpandConstant('{sys}\schtasks.exe'),
      '/Run /TN "{#TaskName}"',
      '',
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    ) then
      Log('安装完成，但无法立即启动计划任务；下次登录时会自动启动。');
  end;
end;

procedure InitializeUninstallProgressForm();
begin
  DeleteRuntimeDataCheck := TNewCheckBox.Create(UninstallProgressForm);
  DeleteRuntimeDataCheck.Parent := UninstallProgressForm;
  DeleteRuntimeDataCheck.Left := UninstallProgressForm.StatusLabel.Left;
  DeleteRuntimeDataCheck.Top := UninstallProgressForm.ProgressBar.Top +
    UninstallProgressForm.ProgressBar.Height + ScaleY(16);
  DeleteRuntimeDataCheck.Width := UninstallProgressForm.StatusLabel.Width;
  DeleteRuntimeDataCheck.Caption := '同时删除本地消息队列、日志和报表（不可恢复）';
  DeleteRuntimeDataCheck.Checked := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DeleteFile(ProfileFile());
    DeleteFile(AddBackslash(ProfileDir()) + 'agent.runtime');
    if DeleteRuntimeDataCheck.Checked then
      DelTree(ProfileDir(), True, True, True);
  end;
end;
