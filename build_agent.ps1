[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [string]$CenterUrl,

    [Parameter(Mandatory = $true)]
    [string]$UpdateManifestUrl,

    [Parameter(Mandatory = $true)]
    [string]$PackageUrl,

    [Parameter(Mandatory = $true)]
    [string]$Ed25519PrivateKeyPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9A-Fa-f]{40}$')]
    [string]$CertificateThumbprint,

    [Parameter(Mandatory = $true)]
    [string]$ISCCPath,

    [Parameter(Mandatory = $true)]
    [string]$SignToolPath,

    [string]$TimestampUrl = 'http://timestamp.digicert.com',
    [string]$PythonPath = 'python'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-HttpsUrl([string]$Value, [string]$Name) {
    $parsed = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$parsed) -or
        $parsed.Scheme -ne 'https' -or
        -not $parsed.Host -or
        $parsed.UserInfo -or
        $parsed.Fragment) {
        throw "$Name must be an absolute HTTPS URL without credentials or a fragment."
    }
}

function Assert-TimestampUrl([string]$Value) {
    $parsed = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$parsed) -or
        $parsed.Scheme -notin @('http', 'https') -or
        -not $parsed.Host -or
        $parsed.UserInfo -or
        $parsed.Fragment) {
        throw 'TimestampUrl must be an absolute HTTP(S) URL without credentials or a fragment.'
    }
}

function Invoke-Checked([string]$FilePath, [string[]]$Arguments) {
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

function Invoke-PackagedAgentCheck(
    [string]$FilePath,
    [string]$Command,
    [string]$LogRoot,
    [int]$TimeoutSeconds = 180
) {
    $stdoutPath = Join-Path $LogRoot "agent-$Command.stdout.log"
    $stderrPath = Join-Path $LogRoot "agent-$Command.stderr.log"
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = $Command
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "Packaged Agent $Command did not start."
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
        if ($timedOut) {
            $process.Kill()
        }
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        $exitCode = $process.ExitCode
    }
    finally {
        $process.Dispose()
    }
    [IO.File]::WriteAllText($stdoutPath, $stdout, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($stderrPath, $stderr, [Text.UTF8Encoding]::new($false))
    if ($timedOut) {
        throw "Packaged Agent $Command timed out after $TimeoutSeconds seconds."
    }
    if ($exitCode -ne 0) {
        throw "Packaged Agent $Command failed ($exitCode): $($stderr.Trim())"
    }
}

Assert-HttpsUrl $CenterUrl 'CenterUrl'
Assert-HttpsUrl $UpdateManifestUrl 'UpdateManifestUrl'
Assert-HttpsUrl $PackageUrl 'PackageUrl'
Assert-TimestampUrl $TimestampUrl
if (-not (Test-Path -LiteralPath $Ed25519PrivateKeyPath -PathType Leaf)) {
    throw "Ed25519 private key was not found: $Ed25519PrivateKeyPath"
}
if (-not (Test-Path -LiteralPath $ISCCPath -PathType Leaf)) {
    throw "ISCC.exe was not found: $ISCCPath"
}
if (-not (Test-Path -LiteralPath $SignToolPath -PathType Leaf)) {
    throw "signtool.exe was not found: $SignToolPath"
}

$ProjectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$BuildRoot = Join-Path $ProjectRoot 'build\windows-agent'
$DistRoot = Join-Path $BuildRoot 'pyinstaller-dist'
$WorkRoot = Join-Path $BuildRoot 'pyinstaller-work'
$GeneratedRoot = Join-Path $BuildRoot 'generated'
$ReleaseRoot = Join-Path $BuildRoot 'release'
$InstallerOutput = Join-Path $BuildRoot 'installer'

foreach ($target in @($BuildRoot)) {
    $fullTarget = [IO.Path]::GetFullPath($target)
    $allowedRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot 'build')) + [IO.Path]::DirectorySeparatorChar
    if (-not $fullTarget.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove build path outside the project build directory: $fullTarget"
    }
    if (Test-Path -LiteralPath $fullTarget) {
        Remove-Item -LiteralPath $fullTarget -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path $GeneratedRoot, $ReleaseRoot, $InstallerOutput | Out-Null

$DependencyCheck = @'
import importlib
import importlib.metadata
import importlib.util
import platform
import struct
import sys

def native_machine():
    if sys.platform != "win32":
        return platform.machine().lower()
    with open(sys.executable, "rb") as handle:
        if handle.read(2) != b"MZ":
            raise SystemExit("Python runtime is not a Windows PE executable.")
        handle.seek(0x3C)
        pe_offset = struct.unpack("<I", handle.read(4))[0]
        handle.seek(pe_offset)
        if handle.read(4) != b"PE\0\0":
            raise SystemExit("Python runtime has an invalid PE header.")
        return {0x8664: "amd64", 0xAA64: "arm64"}.get(
            struct.unpack("<H", handle.read(2))[0],
            "unsupported",
        )

if struct.calcsize("P") * 8 != 64 or native_machine() not in {"amd64", "x86_64"}:
    raise SystemExit("Python runtime must be native x64 (AMD64).")
required = {
    "wxauto4": "41.1.2",
    "uiautomation": "2.0.29",
}
for distribution, expected in required.items():
    actual = importlib.metadata.version(distribution)
    if actual != expected:
        raise SystemExit(f"{distribution} must be exactly {expected}; found {actual}")
if importlib.util.find_spec("wxauto4") is None:
    raise SystemExit("wxauto4 is not installed")
for module in ("uiautomation", "cryptography", "httpx", "sqlalchemy", "waitress", "PyInstaller"):
    importlib.import_module(module)
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required.")
'@
$DependencyCheckPath = Join-Path $GeneratedRoot 'dependency-check.py'
[IO.File]::WriteAllText(
    $DependencyCheckPath,
    $DependencyCheck,
    [Text.UTF8Encoding]::new($false)
)
Invoke-Checked $PythonPath @($DependencyCheckPath)

$PublicKeyPath = Join-Path $GeneratedRoot 'ed25519-public-key.txt'
Invoke-Checked $PythonPath @(
    (Join-Path $ProjectRoot 'packaging\sign_release.py'),
    'export-public',
    '--private-key', (Resolve-Path -LiteralPath $Ed25519PrivateKeyPath).Path,
    '--output', $PublicKeyPath
)
$PublicKey = (Get-Content -LiteralPath $PublicKeyPath -Raw).Trim()
$BuildConfigPath = Join-Path $GeneratedRoot 'agent-build.json'
$BuildConfigJson = @{
    version = $Version
    center_url = $CenterUrl.TrimEnd('/')
    update_manifest_url = $UpdateManifestUrl
    update_public_key = $PublicKey
} | ConvertTo-Json -Compress
[IO.File]::WriteAllText(
    $BuildConfigPath,
    $BuildConfigJson,
    [Text.UTF8Encoding]::new($false)
)

$AgentArguments = @(
    '-m', 'PyInstaller', '--clean', '--noconfirm', '--onedir', '--noconsole',
    '--name', 'WeChatAIAgent',
    '--distpath', $DistRoot,
    '--workpath', $WorkRoot,
    '--specpath', $BuildRoot,
    '--paths', $ProjectRoot,
    '--add-data', "$BuildConfigPath;.",
    '--collect-all', 'wxauto4',
    '--collect-all', 'uiautomation',
    '--hidden-import', 'main',
    (Join-Path $ProjectRoot 'packaging\agent_entry.py')
)
Invoke-Checked $PythonPath $AgentArguments

$LauncherArguments = @(
    '-m', 'PyInstaller', '--clean', '--noconfirm', '--onefile', '--noconsole',
    '--name', 'WeChatAIAgentLauncher',
    '--distpath', $DistRoot,
    '--workpath', (Join-Path $WorkRoot 'launcher'),
    '--specpath', $BuildRoot,
    '--paths', $ProjectRoot,
    (Join-Path $ProjectRoot 'packaging\launcher_entry.py')
)
Invoke-Checked $PythonPath $LauncherArguments

$AgentOutput = Join-Path $DistRoot 'WeChatAIAgent'
$AgentExe = Join-Path $AgentOutput 'WeChatAIAgent.exe'
$LauncherExe = Join-Path $DistRoot 'WeChatAIAgentLauncher.exe'
if (-not (Test-Path -LiteralPath $AgentExe -PathType Leaf) -or
    -not (Test-Path -LiteralPath $LauncherExe -PathType Leaf)) {
    throw 'PyInstaller did not produce the expected Agent and launcher executables.'
}

$SignArguments = @('sign', '/sha1', $CertificateThumbprint, '/fd', 'SHA256', '/tr', $TimestampUrl, '/td', 'SHA256')
$AgentNativeFiles = @(
    Get-ChildItem -LiteralPath $AgentOutput -Recurse -File |
        Where-Object { $_.Extension.ToLowerInvariant() -in @('.exe', '.dll', '.pyd') }
)
if ($AgentNativeFiles.Count -eq 0) {
    throw 'The packaged Agent contains no native executable files.'
}
foreach ($NativeFile in $AgentNativeFiles) {
    $ExistingSignature = Get-AuthenticodeSignature -LiteralPath $NativeFile.FullName
    if ($ExistingSignature.Status -ne 'Valid') {
        Invoke-Checked $SignToolPath ($SignArguments + @($NativeFile.FullName))
    }
    Invoke-Checked $SignToolPath @('verify', '/pa', '/q', $NativeFile.FullName)
}
Invoke-Checked $SignToolPath ($SignArguments + @($LauncherExe))
Invoke-Checked $SignToolPath @('verify', '/pa', '/v', $LauncherExe)
Invoke-PackagedAgentCheck $AgentExe 'version' $GeneratedRoot
Invoke-PackagedAgentCheck $AgentExe 'self-test' $GeneratedRoot

$VersionRoot = Join-Path $ReleaseRoot "versions\$Version"
New-Item -ItemType Directory -Force -Path $VersionRoot | Out-Null
Copy-Item -Path (Join-Path $AgentOutput '*') -Destination $VersionRoot -Recurse -Force
Copy-Item -LiteralPath $LauncherExe -Destination (Join-Path $ReleaseRoot 'WeChatAIAgentLauncher.exe') -Force

$ArchivePath = Join-Path $BuildRoot "WeChatAIAgent-$Version-x64.zip"
Compress-Archive -Path (Join-Path $VersionRoot '*') -DestinationPath $ArchivePath -CompressionLevel Optimal
$ManifestPath = Join-Path $BuildRoot "agent-update-$Version.json"
Invoke-Checked $PythonPath @(
    (Join-Path $ProjectRoot 'packaging\sign_release.py'),
    'manifest',
    '--private-key', (Resolve-Path -LiteralPath $Ed25519PrivateKeyPath).Path,
    '--archive', $ArchivePath,
    '--version', $Version,
    '--package-url', $PackageUrl,
    '--output', $ManifestPath
)

$ISCCArguments = @(
    "/DAgentVersion=$Version",
    "/DReleaseRoot=$ReleaseRoot",
    "/DInstallerOutput=$InstallerOutput",
    (Join-Path $ProjectRoot 'packaging\agent-installer.iss')
)
Invoke-Checked $ISCCPath $ISCCArguments
$InstallerCandidates = @(
    Get-ChildItem -LiteralPath $InstallerOutput -Filter "*-$Version-x64.exe" -File
)
if ($InstallerCandidates.Count -ne 1) {
    throw "Inno Setup must produce exactly one versioned x64 installer; found $($InstallerCandidates.Count)."
}
$InstallerPath = $InstallerCandidates[0].FullName
Invoke-Checked $SignToolPath ($SignArguments + @($InstallerPath))
Invoke-Checked $SignToolPath @('verify', '/pa', '/v', $InstallerPath)

Write-Host "Installer: $InstallerPath"
Write-Host "Update archive: $ArchivePath"
Write-Host "Signed manifest: $ManifestPath"
