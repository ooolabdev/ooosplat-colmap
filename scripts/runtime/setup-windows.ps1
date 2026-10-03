# SPDX-License-Identifier: BSD-3-Clause
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$instance = & $vswhere -version '[17.0,18.0)' -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -format json | ConvertFrom-Json
if (!$instance) { throw 'A supported VS2022 x64 toolchain is required.' }
Import-Module (Join-Path $instance.installationPath 'Common7/Tools/Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell $instance.instanceId -SkipAutomaticLocation -DevCmdArguments '-no_logo -host_arch=amd64 -arch=amd64'
if ($env:VSCMD_ARG_TGT_ARCH -ne 'x64') { throw 'Wrong MSVC target architecture.' }
foreach ($name in @('PATH','INCLUDE','LIB','LIBPATH','VCToolsInstallDir','VCToolsVersion','VSINSTALLDIR','VisualStudioVersion','WindowsSdkDir','WindowsSDKVersion','VSCMD_ARG_TGT_ARCH','VCToolsRedistDir')) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($null -ne $value -and $env:GITHUB_ENV) { "$name=$value" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append }
}
Write-Output "VS2022 toolset $env:VCToolsVersion; Windows SDK $env:WindowsSDKVersion"
