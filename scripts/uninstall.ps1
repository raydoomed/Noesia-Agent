# 一键卸载：双击 uninstall.bat
#
# 为什么要分档：这个程序的全部状态都在项目目录里（data/ 有会话、记忆、密钥；.venv 是依赖）。
# 「卸载」对每个人含义不同——有人只想把命令从 PATH 去掉，有人要连数据一起清。
# 所以这里给出三档，默认什么都不删；第三档（连数据一起删）必须手打 DELETE 才继续。
# 不可逆的事不做默认、不猜。

param([ValidateSet("path", "venv", "all")] [string]$Mode = "")

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Say($m) { Write-Host "  $m" }
function Warn($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host ""; Write-Host "  [X] $m" -ForegroundColor Red; Write-Host ""; exit 1 }
function Ask($m) {
    # 提示语自己打印、Read-Host 不带提示：Read-Host 会自作主张补一个 ": "，
    # 提示语里再写「：」就成了「）：: 」（用户实测截图里就是这个）。自己打印还能被重定向抓到，好测。
    Write-Host "  ${m}: " -NoNewline
    return (Read-Host)
}

Write-Host ""
Say "项目目录：$root"

# ---- 1. 程序还开着就别删：占着 .venv 的文件删不掉，硬杀进程也不是卸载该干的事 ----
# 判据是可执行文件路径，不是进程名：机器上可能还有别处的 noesia（另一个部署），它不占本项目的
# .venv，没理由拦住这次卸载。报错时连命令行一起印出来——用户得能看出哪一个是命令行里的会话、
# 哪一个是界面拉起的引擎子进程（踩过：只印「引擎进程 (pid …)」，根本不知道是谁）。
$busy = @()
foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
    if (-not $p.ExecutablePath) { continue }
    try {
        if (-not $p.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { continue }
    } catch { continue }
    if ($p.Name -in @("noesia-gui.exe", "noesia.exe")) {
        $label = "界面 / 命令行"
    } elseif ($p.Name -like "python*") {
        $label = "引擎进程"
    } else {
        $label = $p.Name
    }
    $cmd = ($p.CommandLine + "") -replace "\s+", " "
    if ($cmd.Length -gt 100) { $cmd = $cmd.Substring(0, 100) + "…" }
    $busy += "        $label (pid $($p.ProcessId))：$cmd"
}
if ($busy.Count -gt 0) {
    $hint = "        （命令行里聊过天的 noesia，Ctrl+C 退出后它拉起的后台进程还会活着："
    $hint += "结束上面这些进程，再卸载）"
    Fail ("这些进程还在跑，先把窗口关掉再卸载：" + [Environment]::NewLine +
          ($busy -join [Environment]::NewLine) + [Environment]::NewLine + $hint)
}

# ---- 2. 从当前用户的 PATH 里摘掉本项目那一条（只摘完全相同的，别动别的条目）----
$scriptsDir = Join-Path $root ".venv\Scripts"

# Windows 上 `[Environment]::SetEnvironmentVariable(..., "User")` 会同步广播 WM_SETTINGCHANGE，
# 再逐个等每个窗口回应：实测 30.2 秒（用户看到的是「手打 DELETE 之后干等半分钟，一个字都不出」）。
# 直接写 HKCU\Environment 只要 10 毫秒；广播换成不等回复的 PostMessage，对新开的程序效果一样。
function Set-UserPath($value) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)
    if ($null -eq $key) { throw "打不开 HKCU\Environment（可能被策略锁了）" }
    try {
        $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
        try {
            # 值的类型要照原样写回：用户 Path 有的是 REG_SZ，有的是 REG_EXPAND_SZ，改了类型等于改语义
            if ($key.GetValueKind("Path") -eq [Microsoft.Win32.RegistryValueKind]::String) {
                $kind = [Microsoft.Win32.RegistryValueKind]::String
            }
        } catch { }
        $key.SetValue("Path", $value, $kind)
    } finally { $key.Close() }
    if (-not ("EnvBroadcast" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class EnvBroadcast {
    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    public static extern bool PostMessage(IntPtr hWnd, int msg, IntPtr wParam, string lParam);
}
"@
    }
    # 告诉已经开着的程序「环境变量变了」（HWND_BROADCAST + WM_SETTINGCHANGE），发完就走
    [EnvBroadcast]::PostMessage([IntPtr]0xffff, 0x001A, [IntPtr]::Zero, "Environment") | Out-Null
}

function Remove-PathEntry {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $userPath) { Say "当前用户 PATH 是空的，没有要摘的。"; return }
    $parts = @($userPath -split ';' | Where-Object { $_.Trim() })
    $keep = @($parts | Where-Object { $_.TrimEnd('\') -ine $scriptsDir.TrimEnd('\') })
    if ($keep.Count -eq $parts.Count) {
        Say "PATH 里没有 $scriptsDir（可能装的时候跳过了），不处理。"
        return
    }
    Set-UserPath ($keep -join ';')
    $after = [Environment]::GetEnvironmentVariable("Path", "User")
    $still = @($after -split ';' | Where-Object { $_.Trim() -and ($_.TrimEnd('\') -ieq $scriptsDir.TrimEnd('\')) })
    if ($still.Count -gt 0) {
        Warn "写回去了但读出来还在：$scriptsDir —— 请手动删掉这条环境变量"
        return
    }
    Say "已从当前用户 PATH 摘掉：$scriptsDir"
    Say "（已经开着的终端里还留着，重开就没了）"
}

function Get-SizeMB($path) {
    # 踩过：`Get-ChildItem -Recurse | Measure-Object -Property Length` 在「目录里只有子目录、
    # 一个文件都没有」时直接报红——目录没有 Length 这个属性，空输入连一个对象都没有，
    # 用户看到的就是一大片 InvalidArgument 错误。所以只累加文件（-File），不用 Measure-Object。
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    try {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if (-not $item.PSIsContainer) { return [math]::Round($item.Length / 1MB, 1) }
        $sum = 0
        Get-ChildItem -LiteralPath $path -Recurse -Force -File -ErrorAction SilentlyContinue |
            ForEach-Object { $sum += $_.Length }
        return [math]::Round($sum / 1MB, 1)
    } catch { return 0 }
}

# 删 .venv / data 要几十秒（几万个文件），中间不给动静，用户会以为卡住了。
# 所以：先报「正在删除 X（N MB）」，再一边删一边刷新已用秒数，删完报用时。
function Remove-Tree($path, $label) {
    $mb = Get-SizeMB $path
    $isDir = Test-Path -LiteralPath $path -PathType Container
    $sizeText = if (-not $isDir) { "文件" } elseif ($mb -ge 0.1) { "$mb MB" } elseif ($mb -gt 0) { "不足 0.1 MB" } else { "空目录" }
    $t0 = Get-Date
    Say "正在删除 $label（$sizeText）…"

    $job = $null
    try {
        $job = Start-Job -ScriptBlock {
            param($p)
            Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction Stop
        } -ArgumentList $path
    } catch { $job = $null }  # 没有 Job 基础设施就退化成阻塞删除，别为了进度卡住卸载

    if ($null -eq $job) {
        try { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop }
        catch {
            Warn "删不掉 ${label}：$($_.Exception.Message)（多半是还有进程占着，关掉窗口再试）"
            return
        }
    } else {
        $spin = @('|', '/', '-', '\')
        $i = 0
        while ($job.State -eq 'Running') {
            Start-Sleep -Milliseconds 400
            $sec = [int]((Get-Date) - $t0).TotalSeconds
            Write-Host ("`r      {0} 已用 {1} 秒…" -f $spin[$i % 4], $sec) -NoNewline
            $i++
        }
        Write-Host "`r                                   `r" -NoNewline  # 擦掉进度那行
        $errors = @($job.ChildJobs[0].Error)
        $message = if ($errors.Count -gt 0 -and $errors[0]) { $errors[0].ToString().Trim() } else { "" }
        Remove-Job $job -Force -ErrorAction SilentlyContinue
        if ($message) {
            Warn "删不掉 ${label}：$message（多半是还有进程占着，关掉窗口再试）"
            return
        }
    }

    if (Test-Path -LiteralPath $path) {
        Warn "删不掉 ${label}：目录还在（多半是还有进程占着，关掉窗口再试）"
        return
    }
    Say "已删除 $label（用时 $([int]((Get-Date) - $t0).TotalSeconds) 秒）"
}

# ---- 3. 问清楚要卸到什么程度 ----
Say "正在统计占用（.venv 文件多，稍等几秒）…"
$venvSize = Get-SizeMB (Join-Path $root ".venv")
$dataPath = Join-Path $root "data"
$dataSize = Get-SizeMB $dataPath
$hasEnv = (Test-Path (Join-Path $root ".env")) -or (Test-Path (Join-Path $dataPath "NoesiaHome\.env"))
# 后面要拼进中文句子，先算好：PowerShell 字符串里再嵌双引号会把字符串截断（踩过）
$keyNote = if ($hasEnv) { "、密钥" } else { "" }
if (-not $Mode) {
    Write-Host ""
    Write-Host "  卸载分三档，选一个（回车 = 只做第 1 档）：" -ForegroundColor Cyan
    Say "1) 只把 noesia 从 PATH 里摘掉（程序和 data 全留着；以后想用还能用）"
    Say "2) 再加上删除 .venv（依赖，$venvSize MB，重装 setup.bat 就能回来）——data 全留"
    Say "3) 全清：PATH + .venv + data（$dataSize MB：会话、记忆、配置${keyNote}，还有 workspace 里你自己放的文件）——不可恢复"
    Write-Host ""
    $choice = Ask "输入 1 / 2 / 3（直接回车 = 1）"
    switch ($choice.Trim()) {
        "2" { $Mode = "venv" }
        "3" { $Mode = "all" }
        default { $Mode = "path" }
    }
}

if ($Mode -eq "all") {
    Write-Host ""
    Warn "第 3 档会永久删掉 data/：里面的会话记录、记忆、配置${keyNote}都找不回来，"
    Warn "data\workspace 里你自己放的文件也会一起删。想留就先拷出去。"
    $confirm = Ask "确认请手打 DELETE（其它输入 = 取消）"
    if ($confirm -cne "DELETE") {
        Say "已取消，什么都没删。"
        Write-Host ""
        exit 0
    }
}

# ---- 4. 执行 ----
Remove-PathEntry

if ($Mode -in @("venv", "all")) {
    Write-Host ""
    Say "清理依赖（.venv 里是几万个文件，通常十几秒到一分钟，中途别关窗口）"
    foreach ($dir in @(".venv", ".pytest_cache", "build")) {
        $full = Join-Path $root $dir
        if (Test-Path $full) { Remove-Tree $full $dir }
    }
    # pip install -e 会在仓库根留下 egg-info
    Get-ChildItem -LiteralPath $root -Directory -Filter "*.egg-info" -ErrorAction SilentlyContinue | ForEach-Object {
        Remove-Tree $_.FullName $_.Name
    }
    # 跑过 Python 的地方全是 __pycache__：不删的话"回到刚克隆的样子"只是看着像，
    # 源码没变而时间戳对不上时，旧的 .pyc 还会被当成新字节码用。
    $pycDirs = @(Get-ChildItem -LiteralPath $root -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue)
    if ($pycDirs.Count -gt 0) {
        Say "清理 Python 缓存（__pycache__，$($pycDirs.Count) 处）"
        foreach ($d in $pycDirs) {
            Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($Mode -eq "all") {
    Write-Host ""
    Say "清理数据（data 里是会话记录、记忆、配置${keyNote}）"
    if (Test-Path $dataPath) { Remove-Tree $dataPath "data" }
    $envFile = Join-Path $root ".env"
    if (Test-Path $envFile) { Remove-Tree $envFile ".env" }
}

# ---- 5. 收尾：目录本身不删 ----
Write-Host ""
if ($Mode -eq "path") {
    Write-Host "  [√] 命令已摘掉，程序和 data 原样留着（想用直接双击 Noesia.vbs）。" -ForegroundColor Green
} elseif ($Mode -eq "venv") {
    Write-Host "  [√] 命令已摘掉，依赖已删，data 原样留着。" -ForegroundColor Green
    Write-Host "      以后想用，双击 setup.bat 装回来就行——data 里的会话和记忆不会丢。"
} else {
    Write-Host "  [√] 命令、依赖、数据都清掉了。" -ForegroundColor Green
}

if ($Mode -ne "path") {
    # 卸载不动 vendor：那里面是程序自己跑要用的东西，不是用户数据。
    # 但也不能不说——用户看到「都清掉了」，一开文件夹发现还占着几百 MB，会觉得没卸干净。
    $vendorItems = @(
        @{ Name = "python314"; Desc = "程序用的 Python（没有它，setup 要先联网下一份）" },
        @{ Name = "ffmpeg";    Desc = "语音朗读要的解码器" },
        @{ Name = "alirtc";    Desc = "语音互动（画面）要的运行库" },
        @{ Name = "wheels";    Desc = "离线安装包，装依赖时不用联网" }
    )
    $vendorLines = @()
    $vendorTotal = 0
    foreach ($it in $vendorItems) {
        $mb = Get-SizeMB (Join-Path $root ("vendor\" + $it.Name))
        if ($mb -ge 0.1) {
            $vendorLines += ("      {0,-10} {1,7} MB  {2}" -f $it.Name, $mb, $it.Desc)
            $vendorTotal += $mb
        }
    }
    if ($vendorLines.Count -gt 0) {
        Write-Host ""
        Write-Host "  另外 vendor 文件夹里还有 $vendorTotal MB 随包文件，这次没删。"
        Write-Host "  它们不是你的数据，是程序跑起来要靠的东西："
        $vendorLines | ForEach-Object { Write-Host $_ }
        Write-Host ""
        Write-Host "  重装时 setup.bat 靠它们离线装，所以默认留着。"
        Write-Host "  想省掉这块空间 —— 打开项目文件夹，只删 vendor 这一个文件夹（别删别的），"
        Write-Host "  重装时 setup.bat 会自己重新下回来。"
    }
}

Write-Host ""
Write-Host "  项目文件夹本身没删（脚本删不掉自己所在的目录）。"
if ($Mode -eq "path") {
    Write-Host "  以后整个不要了，删这个文件夹即可（里面的 data 会一起没）："
} else {
    Write-Host "  想连程序带源码一起清掉，删这个文件夹即可（删了就什么都没了）："
}
Write-Host "      $root"
Write-Host ""
exit 0
