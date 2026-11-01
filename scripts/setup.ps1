# Noesia 环境安装（真正干活的在这里；setup.bat 只是个 ASCII 外壳）
#
# 为什么不在 .bat 里直接写中文：cmd 按 512 字节一块读批处理，UTF-8 中文被从中间切开后
# 会把后半截当命令执行（报 '。所以改成解析版本号。' 这种错）。PowerShell 不会。
# 注意：这里不能用 "Stop"。PowerShell 5.1 里原生程序（pip）往 stderr 写一行，
# 就会被当成 NativeCommandError 终止整个脚本，换源逻辑根本没机会跑。
$ErrorActionPreference = "Continue"
# 清掉外部塞进来的 PYTHONPATH：否则 pip 会认为依赖"已装好"（其实在别人的 venv 里），
# 程序也会导入到别人的 site-packages 去。
$env:PYTHONPATH = ""
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Say($msg) { Write-Host "  $msg" }
function Fail($msg) {
    Write-Host ""
    Write-Host "  [x] $msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "  Noesia 环境安装"
Write-Host "  ----------------"
Write-Host "  第一次装几分钟；包里带了离线轮子就不用联网，装过一次以后就不用再点了。"
Write-Host ""

# ---- 1. 找 Python：不看退出码，只认真正打印出 "Python 3.x" 的那个 ----
# 老一点的 py 启动器找不到指定版本时返回负数退出码，`if errorlevel` 会误判成成功，所以只能解析 --version 的输出。
function Get-PythonVersion([string]$exe, [string[]]$preArgs = @()) {
    try { $out = & $exe @preArgs --version 2>$null } catch { return $null }
    foreach ($line in @($out)) {
        if ("$line" -match '^Python\s+3\.(\d+)\.') { return [int]$matches[1] }
    }
    return $null
}

# 只认 3.14，不退版本：两处硬约束都在 3.14 上。
#   1. `src\engine\deps.txt` 每一行都带 `python_version >= '3.14'`（按 3.14 环境导出的清单），
#      在 3.10 / 3.13 上 pip 会把整份清单当「不适用」整行 Ignoring 掉，一个包都不装，引擎起不来；
#   2. `src\app\_alirtc.pyd` 是按 CPython 3.14 编的（只认 python314.dll），语音互动档也要 3.14。
# 但「没有 3.14」不该让用户自己去官网下：这里从 NuGet 下官方 python 包补进 vendor\python314
# （见下面 Install-BundledPython），补不上才退回手动提示。不用 exe 安装包，原因写在那段注释里。
# 候选写成参数数组而不是一整条命令：走启动器是 ("py","-3.14")，走磁盘路径是 ("<安装目录>\python.exe")，
# 带空格的路径只有在数组形式下才不会解析错（`cmd /c "$py ..."` 那种拼字符串必踩）。
function Find-Python314 {
    # 0. 随包带的这份（vendor\python314，就是 python.org 官方 NuGet 包解开后的 tools\）：
    #    用户不用装 Python、不用联网，先认它
    $bundled = Join-Path $root "vendor\python314\python.exe"
    if ((Test-Path $bundled) -and ((Get-PythonVersion $bundled) -eq 14)) { return , @($bundled) }

    $tries = @(
        @("py", "-3.14"),
        @("py"),      # 不指定版本时 py 挑最高的那个；机器上真有 3.14 也会走到
        @("python")
    )
    foreach ($t in $tries) {
        if ($t.Count -gt 1) {
            $minor = Get-PythonVersion $t[0] @($t[1..($t.Count - 1)])
        } else {
            $minor = Get-PythonVersion $t[0]
        }
        if ($minor -eq 14) { return , $t }
    }
    # 安装时没勾 PATH、或 py 启动器没注册：直接看标准安装位置
    $paths = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"),
        (Join-Path $env:ProgramFiles "Python314\python.exe")
    )
    foreach ($p in $paths) {
        if ((Test-Path $p) -and ((Get-PythonVersion $p) -eq 14)) { return , @($p) }
    }
    return $null
}

# 从 git clone 下来的仓库里没有 vendor\python314（vendor 整个目录不进仓库，里面 1 GB 二进制推不上去）。
# 这一步按整合包同一个来源把它补回来：python.org 在 NuGet 上发布的 `python` 包，解开 `tools\` 就是完整的
# CPython（整合包里那份就是它，见 vendor\MANIFEST.txt）。不用 python.org 的 exe 安装包：
# 它走 MSI，会写 HKCU\Software\Python\PythonCore\3.14 并覆盖机器上已有的登记；装过一次后目录被删，
# 下次 /quiet 还会变成「修复」，退出码 0 却一个文件都不装。
# 网络不通也没关系：下面 Fail 里留着手动装 Python 3.14 的路。
function Install-BundledPython {
    $target = Join-Path $root "vendor\python314"
    if ((Test-Path (Join-Path $target "python.exe")) -and ((Get-PythonVersion (Join-Path $target "python.exe")) -eq 14)) {
        return $true
    }
    $tarExe = Join-Path $env:SystemRoot "System32\tar.exe"
    if (-not (Test-Path $tarExe)) {
        Say "[WARN] 这台系统没有 tar.exe，没法自动补 Python 3.14。"
        return $false
    }
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("noesia-py-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    $oldProgress = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    $ok = $false
    try {
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        $nupkg = Join-Path $tmp "python.nupkg"
        Say "      下载 Python 3.14.6（NuGet 官方包，约 15 MB）..."
        Invoke-WebRequest -Uri "https://api.nuget.org/v3-flatcontainer/python/3.14.6/python.3.14.6.nupkg" `
            -OutFile $nupkg -UseBasicParsing -TimeoutSec 600
        & $tarExe -xf $nupkg -C $tmp "tools"
        if (Test-Path (Join-Path $tmp "tools\python.exe")) {
            New-Item -ItemType Directory -Force -Path $target | Out-Null
            Copy-Item -Path (Join-Path $tmp "tools\*") -Destination $target -Recurse -Force
            $ok = ((Test-Path (Join-Path $target "python.exe")) -and ((Get-PythonVersion (Join-Path $target "python.exe")) -eq 14))
        }
    } catch {
        Say "      [WARN] 下载失败：$($_.Exception.Message)"
    } finally {
        $ProgressPreference = $oldProgress
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($ok) { Say "      已放好：$target" }
    return $ok
}

$pyCmd = Find-Python314
if (-not $pyCmd) {
    Say "这台机器上没有 Python 3.14，先补一份随包的（vendor\python314）..."
    if (Install-BundledPython) {
        $pyCmd = Find-Python314
        if ($pyCmd) { Say "      Python 3.14 已就位。" }
    }
}
if (-not $pyCmd) {
    $have = @()
    foreach ($cand in @("py", "python")) {
        $minor = Get-PythonVersion $cand
        if ($null -ne $minor) { $have += "3.$minor"; break }
    }
    if ($have.Count -gt 0) { $where = "这台机器上是 Python $($have[0])" } else { $where = "这台机器上没有 Python" }
    $tip = "自动补也没成功（多半是网络问题）。两条路任选：① 自己装 Python 3.14（python.org 下载时勾 Add python.exe to PATH，或命令行 winget install -e --id Python.Python.3.14）；② 联网后重新双击 setup.bat，让它自己补。装好再跑一次。"
    Fail "需要 Python 3.14（这个程序只能在 3.14 上跑，两处硬约束都在这里）；$where。$tip"
}

# 所有调用都走这里：$pyCmd 是「可执行文件 + 可选版本参数」的数组，带空格的路径也不会解析错
function Run-Py314([string[]]$rest) {
    if ($pyCmd.Count -gt 1) {
        & $pyCmd[0] @($pyCmd[1..($pyCmd.Count - 1)]) @rest
    } else {
        & $pyCmd[0] @rest
    }
}

Say "用的 Python：$($pyCmd -join ' ')"
Run-Py314 @("--version")

# ---- 2. 虚拟环境 ----
# 已有的 .venv 版本不对也要重建：这台机器上常见「以前用 3.10 装过」，那时 pyproject 允许 3.10，
# 留着一个 3.10 的 venv 会让每一步都"看起来装上了"而引擎其实一个包都没有。
if (Test-Path ".venv\Scripts\python.exe") {
    $venvMinor = Get-PythonVersion (Join-Path (Get-Location).Path ".venv\Scripts\python.exe")
    if ($null -ne $venvMinor -and $venvMinor -ne 14) {
        Say "[1/6] 已有 .venv 是 Python 3.${venvMinor}，跟这个包要求的 3.14 不一致，删掉重建（.venv 里只有依赖，不含 data/ 与聊天记录）..."
        Remove-Item -LiteralPath ".venv" -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Say "[1/6] 创建虚拟环境 .venv ..."
    Run-Py314 @("-m", "venv", ".venv")
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Fail "建虚拟环境失败，上面几行是原因。"
    }
} else {
    Say "[1/6] 已有 .venv，跳过创建。"
}

# ---- 3. 装依赖 ----
Say "[2/6] 安装依赖（PySide6 约 200 MB；有离线轮子就不联网，没有就走镜像源）..."
$vpy = ".venv\Scripts\python.exe"

# uv 建的或精简过的 venv 里可能没有 pip，先补上
& cmd /c "$vpy -m pip --version >nul 2>nul || $vpy -m ensurepip --upgrade >nul"

function Test-Deps {
    $probe = @()
    try {
        # 探针必须导入外壳真实用到的模块：仓库里没有叫 noesia 的包（项目名是 noesia，
        # 但顶层模块是 app / backends / settings），写 import noesia 会永远失败，
        # 于是四个源挨个白跑一遍、最后误报「安装失败」。
        # 主依赖一个都不能少：pyproject 里声明的 7 个 + 界面，逐个 import 验证。
        # 尤其 edge-tts：有的镜像源还没收录它，装漏了 setup 以前照样报成功，
        # 等用户切到 Edge 朗读才发现「没装 edge-tts」。
        $probe = & $vpy -c "import sys; sys.path.insert(0, 'src'); import app, backends, settings, cli, engine_config_view, PySide6, numpy, yaml, pydantic, httpx, websockets, PIL, edge_tts; print('NOESIA_DEPS_OK')" 2>$null
    } catch {
        return $false
    }
    return (@($probe) -contains "NOESIA_DEPS_OK")
}

# 源顺序：阿里云 → 中科大 → 你机器上配的源 → 官方 PyPI。
# 国内用户第一条就能通，省一次白跑的 5 秒；海外用户走到官方源也能装。
# 最后一家必须是官方：镜像各有各的策略（实测 pip 26.1.2 从清华镜像拿页面会收到 403，
# 浏览器打开同一页却是 200），官方源不会因为镜像策略把 pip 挡在外面。
$sources = @()
# 随包轮子排第一：vendor\wheels 里是一整套 whl（跟仓库一起分发），断网也能装完。
# 只有一条命——装不动（轮子和 Python 版本对不上之类）就往下走网络源，不影响原来的流程。
if ((Test-Path "vendor\wheels") -and (@(Get-ChildItem "vendor\wheels" -Filter *.whl -ErrorAction SilentlyContinue).Count -gt 0)) {
    $sources += @{ Name = "随包轮子（离线）"; Args = @("--no-index", "--find-links", "vendor\wheels") }
}
$sources += @(
    @{ Name = "阿里云镜像"; Args = @("-i", "https://mirrors.aliyun.com/pypi/simple") },
    @{ Name = "中科大镜像"; Args = @("-i", "https://mirrors.ustc.edu.cn/pypi/simple") },
    @{ Name = "你机器上配的源"; Args = @() },
    @{ Name = "官方 PyPI"; Args = @("-i", "https://pypi.org/simple") }
)
foreach ($src in $sources) {
    Say "用 $($src.Name) 安装 ..."
    $extra = $src.Args
    # 先装构建后端：隔离构建设置会自己去网上取 setuptools，网络一拦就整个装不上
    & $vpy -m pip install setuptools wheel --disable-pip-version-check @extra
    & $vpy -m pip install -e ".[dev]" --no-build-isolation --disable-pip-version-check @extra
    if (Test-Deps) { break }
    Write-Host ""
    Say "这个源没装上，换下一个。"
}

if (-not (Test-Deps)) {
    Fail "安装失败：换网络或挂代理再双击 setup.bat。具体原因看上面几行。"
}

# ---- 4. 内核依赖：她干活用的工具、技能、浏览器都在 src\engine 里 ----
# 内核代码随仓库分发，依赖单独一份清单（src\engine\deps.txt），不安装内核包本身
# （它声明了一个不存在的 setuptools 版本，装包这条路走不通，也没必要）。
function Test-EngineDeps {
    $probe = @()
    try {
        # 探针要把「不装上就当场炸」的都导一遍，光 import run_agent 不够：
        # run_agent 是懒加载，语音识别那三个包（numpy / onnxruntime / kaldi-native-fbank）
        # 要等用户真的按住球说话才 import。漏装的话这里照样报「环境就绪」，
        # 等用户开口才炸——那时候他已经以为装好了。
        $probe = & $vpy -c "import sys; sys.path.insert(0, 'src/engine'); import run_agent, numpy, onnxruntime, kaldi_native_fbank; print('NOESIA_ENGINE_OK')" 2>$null
    } catch {
        return $false
    }
    return (@($probe) -contains "NOESIA_ENGINE_OK")
}

if (Test-Path "src\engine\deps.txt") {
    if (Test-EngineDeps) {
        Say "[3/6] 内核依赖已装好，跳过。"
    } else {
        Say "[3/6] 安装内核依赖（46 个直接依赖，连传递依赖约 100 个包，第一次要几分钟）..."
        foreach ($src in $sources) {
            Say "用 $($src.Name) 安装 ..."
            $extra = $src.Args
            & $vpy -m pip install -r "src\engine\deps.txt" --disable-pip-version-check @extra
            if (Test-EngineDeps) { break }
            Write-Host ""
            Say "这个源没装上，换下一个。"
        }
        if (-not (Test-EngineDeps)) {
            Fail "内核依赖没装上：她能聊天，但还不能干活。换网络或挂代理再双击 setup.bat。"
        }
    }
} else {
    Say "[3/6] 没找到 src\engine\deps.txt，跳过内核依赖（只聊天，不干活）。"
}

# ---- 5. 运行时目录与引擎起步配置（用户可能删过 data/，这里补齐，已有不覆盖）----
& $vpy scripts\init_data.py
if ($LASTEXITCODE -ne 0) {
    Say "[WARN] 初始化运行时目录失败；程序启动时还会再试一次。"
}

# ---- 4.5 随包的本地运行库：语音互动那一档要用（缺了不影响文字与纯语音）----
# 这里只判「文件在不在」；真正的导入验证放到 4.8——ARTC 的 dll 在 [5/6] 才补齐，
# 早一步验会对着还不存在的 dll 报红字，让人以为互动档坏了。
if (-not (Test-Path "src\app\_alirtc.pyd")) {
    Say "[WARN] 没找到 src\app\_alirtc.pyd（杀毒软件常把这种文件删掉）：语音互动用不了，文字和纯语音不受影响。"
}

# ---- 4.6 外部程序：ffmpeg / ffprobe（语音解码与素材体检要用）----
# 它们不是 Python 包，pip 装不了——整合包里放在 vendor\ffmpeg\bin，源码副本没有（vendor 不进仓库）。
# 这里联网补到同一个位置：engine_wire.py 启动时挂的就是 vendor\ffmpeg\bin，代码一行不用改。
$ffBin = Join-Path (Get-Location).Path "vendor\ffmpeg\bin"
if ((Test-Path (Join-Path $ffBin "ffmpeg.exe")) -and (Test-Path (Join-Path $ffBin "ffprobe.exe"))) {
    Say "[4/6] ffmpeg 已在包里，跳过。"
} elseif ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Say "[4/6] 系统里已经有 ffmpeg / ffprobe，跳过。"
} else {
    Say "[4/6] 补 ffmpeg / ffprobe（Edge 朗读要边解码边播，约 140 MB）..."
    $tarExe = Join-Path $env:SystemRoot "System32\tar.exe"
    if (-not (Test-Path $tarExe)) {
        Say "[WARN] 这台系统没有 tar.exe，跳过补 ffmpeg；Edge 语音会念不出来。"
        Say "       手动装一份：winget install Gyan.FFmpeg"
    } else {
        $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("noesia-ff-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
        $ffOk = $false
        $oldProgress = $ProgressPreference
        $ProgressPreference = "SilentlyContinue"
        try {
            New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
            New-Item -ItemType Directory -Force -Path $ffBin | Out-Null
            # 走 npmmirror（国内），两个包一共约 50 MB，几秒钟
            $packages = @(
                @{ Name = "ffmpeg"; Url = "https://registry.npmmirror.com/@ffmpeg-installer/win32-x64/-/win32-x64-4.1.0.tgz" },
                @{ Name = "ffprobe"; Url = "https://registry.npmmirror.com/@ffprobe-installer/win32-x64/-/win32-x64-5.1.0.tgz" }
            )
            foreach ($pkg in $packages) {
                $tgz = Join-Path $tmpDir "$($pkg.Name).tgz"
                Invoke-WebRequest -Uri $pkg.Url -OutFile $tgz -UseBasicParsing -TimeoutSec 300
                & $tarExe -xzf $tgz -C $tmpDir "package/$($pkg.Name).exe"
                $src = Join-Path $tmpDir "package\$($pkg.Name).exe"
                if (-not (Test-Path $src)) { throw "$($pkg.Name).tgz 里没有 $($pkg.Name).exe" }
                Move-Item -LiteralPath $src -Destination (Join-Path $ffBin "$($pkg.Name).exe") -Force
            }
            $ffOk = (Test-Path (Join-Path $ffBin "ffmpeg.exe")) -and (Test-Path (Join-Path $ffBin "ffprobe.exe"))
        } catch {
            Say "[WARN] 补 ffmpeg 失败：$($_.Exception.Message)"
        } finally {
            $ProgressPreference = $oldProgress
            Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($ffOk) {
            Say "     已放好：$ffBin"
        } else {
            Say "[WARN] 没补上 ffmpeg：Edge 语音念不出来、素材体检会跳过视频。"
            Say "       手动装一份：winget install Gyan.FFmpeg，装完重开窗口再跑一次 setup.bat"
        }
    }
}

# ---- 4.7 阿里云 ARTC：语音互动那一档要的 dll（Vidu 画面走它）----
# 也不是 Python 包：官方 Windows SDK 解出来的 dll 放 vendor\alirtc，avatar_rtc.py 用 add_dll_directory 载它。
# 直链来自阿里云文档的 SDK 下载页（Windows v7.6.0），实测 100 MB / 4.5 秒，dll 的 sha256 与整合包里的完全一致。
$rtcDir = Join-Path (Get-Location).Path "vendor\alirtc"
if (Test-Path (Join-Path $rtcDir "AliRTCSdk.dll")) {
    Say "[5/6] 阿里云 ARTC 已在包里，跳过。"
} else {
    Say "[5/6] 补阿里云 ARTC（语音互动要的 dll，约 100 MB，几秒）..."
    $tarExe = Join-Path $env:SystemRoot "System32\tar.exe"
    if (-not (Test-Path $tarExe)) {
        Say "[WARN] 这台系统没有 tar.exe，跳过 ARTC：语音互动那一档用不了（文字和纯语音不受影响）。"
    } else {
        $tmpRtc = Join-Path ([System.IO.Path]::GetTempPath()) ("noesia-rtc-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
        $rtcOk = $false
        $oldProgress = $ProgressPreference
        $ProgressPreference = "SilentlyContinue"
        try {
            New-Item -ItemType Directory -Force -Path $tmpRtc | Out-Null
            New-Item -ItemType Directory -Force -Path $rtcDir | Out-Null
            $zip = Join-Path $tmpRtc "artc.zip"
            Invoke-WebRequest -Uri "https://alivc-demo-cms.alicdn.com/versionProduct/sdk/rtc/windows/AliVCSDK_ARTC-7.6.0.zip" -OutFile $zip -UseBasicParsing -TimeoutSec 600
            & $tarExe -xf $zip -C $tmpRtc "AliVCSDK_ARTC-7.6.0/x64/Release" "AliVCSDK_ARTC-7.6.0/include"
            $rel = Join-Path $tmpRtc "AliVCSDK_ARTC-7.6.0\x64\Release"
            Copy-Item -Path (Join-Path $rel "*.dll") -Destination $rtcDir -Force
            Copy-Item -Path (Join-Path $rel "*.lib") -Destination $rtcDir -Force
            Copy-Item -Path (Join-Path $tmpRtc "AliVCSDK_ARTC-7.6.0\include") -Destination $rtcDir -Recurse -Force
            Get-ChildItem -Path $rtcDir -Recurse -Filter ".DS_Store" -Force -ErrorAction SilentlyContinue |
                Remove-Item -Force -ErrorAction SilentlyContinue
            $rtcOk = Test-Path (Join-Path $rtcDir "AliRTCSdk.dll")
        } catch {
            Say "[WARN] 补阿里云 ARTC 失败：$($_.Exception.Message)"
        } finally {
            $ProgressPreference = $oldProgress
            Remove-Item -LiteralPath $tmpRtc -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($rtcOk) {
            Say "     已放好：$rtcDir"
        } else {
            Say "[WARN] 没补上 ARTC：语音互动那一档用不了（文字和纯语音不受影响）。"
        }
    }
}

# ---- 4.8 互动档收尾确认：ARTC 的 dll 刚在 [5/6] 补齐，到这里才真导入 pyd ----
# 4.5 那一步在 dll 还没到位时只提示「等下面补完再验」，不报红字；ABI 对不上（pyd 不是 3.14 编的）也在这里露出来。
if (Test-Path "src\app\_alirtc.pyd") {
    $probe = @()
    try {
        $probe = & $vpy -c "import os, sys; sys.path.insert(0, 'src'); os.add_dll_directory(os.path.abspath('vendor/alirtc')); import app._alirtc; print('NOESIA_ALIRTC_OK')" 2>$null
    } catch { $probe = @() }
    if (@($probe) -contains "NOESIA_ALIRTC_OK") {
        Say "语音互动的本地运行库可用（ARTC 原生 SDK，src\app\_alirtc.pyd）。"
    } else {
        Say "[WARN] 语音互动的本地运行库导入失败：dll 不完整，或被杀毒软件删过。"
        Say "       文字聊天和纯语音（Edge / ElevenLabs / 豆包）都不受影响。"
    }
}

# ---- 5. PATH：装完就能在任意终端敲 noesia（当前用户级，不需要管理员）----
# 不加这一步，`noesia` 只存在于 .venv\Scripts 里，用户必须先激活 venv 才能用——
# 对「装完就该能用」的产品来说这一步不能省。
# Windows 上 `[Environment]::SetEnvironmentVariable(..., "User")` 会同步广播 WM_SETTINGCHANGE，
# 再逐个等窗口回应：实测 30.2 秒（用户看到的是「装到最后一步，屏幕上三十秒一个字都不动」）。
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

$scriptsDir = Join-Path (Get-Location).Path ".venv\Scripts"
Say "[6/6] 把 noesia 命令加进当前用户的 PATH ..."
try {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($userPath -split ';' | Where-Object { $_.Trim() })
    $same = $parts | Where-Object { $_.TrimEnd('\') -ieq $scriptsDir.TrimEnd('\') }
    if ($same) {
        Say "PATH 里已经有 $scriptsDir，跳过。"
    } elseif (($parts -join ';').Length -gt 1800) {
        Say "[WARN] 当前用户 PATH 太长（>1800 字符），写进去可能被系统截断，跳过。"
        Say "       手动加：环境变量 → 用户变量 Path → 新建 → $scriptsDir"
    } else {
        Set-UserPath ((@($parts) + $scriptsDir) -join ';')
        Say "已加进当前用户 PATH：$scriptsDir"
        Say "（新开一个终端才生效；删掉本项目时记得把这条也删掉）"
    }
} catch {
    Say "[WARN] 改 PATH 失败：$($_.Exception.Message)"
    Say "       不影响启动；想用 noesia 就手动把 $scriptsDir 加进用户 Path"
}

Write-Host ""
Write-Host "  [√] 装好了。现在双击 Noesia.vbs 就能启动。" -ForegroundColor Green
Write-Host ""
Write-Host "  还差一步才能聊天：把模型密钥写进引擎家目录的 .env，例如"
Write-Host "      DEEPSEEK_API_KEY=sk-..."
Write-Host "  文件是 $root\data\NoesiaHome\.env（不存在就自己建一个，模板见 .env.example）"
Write-Host "  更省事：启动后点聊天窗的「设置模型…」，选供应商、粘密钥、保存——程序自己写进那个 .env。"
Write-Host "  （密钥只放 .env，配置文件里不写）"
Write-Host "  启动后在聊天窗顶部的「模型」下拉框里选一个即可，选择会写回"
Write-Host "      data\NoesiaHome\config.yaml 的 model: 段。"
Write-Host ""
exit 0
