// Noesia 启动器：双击这个 exe 等同于双击 Noesia.vbs。
// 之所以要一层 exe：vbs 文件带不了图标，资源管理器里只能显示系统给的卷轴图标。
// 干的事只有一件——把 .venv 里的 pythonw 叫起来跑 `-m app`，参数与 Noesia.vbs 完全一致。
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Noesia")]
[assembly: AssemblyProduct("Noesia")]
[assembly: AssemblyDescription("Noesia 桌面数字人")]
[assembly: AssemblyCompany("Noesia")]
[assembly: AssemblyVersion("0.1.0.0")]
[assembly: AssemblyFileVersion("0.1.0.0")]

internal static class Launcher
{
    [STAThread]
    private static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        string pythonw = Path.Combine(root, Path.Combine(".venv", Path.Combine("Scripts", "pythonw.exe")));
        if (!File.Exists(pythonw))
        {
            MessageBox.Show(
                "还没装好环境。\n\n先双击同一目录下的 setup.bat 装一次，再打开 Noesia。",
                "Noesia",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
            return;
        }

        ProcessStartInfo start = new ProcessStartInfo(pythonw);
        start.WorkingDirectory = Path.Combine(root, "src");
        start.Arguments = "-E -m app";
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        Process.Start(start);
    }
}
