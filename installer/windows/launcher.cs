// InstrumentalStudio.exe - the program you pin to the taskbar (after Studio Lab's launcher). It starts Instrumental
// Studio with the private Python the installer put next to it (runtime\python\pythonw.exe, no console window), passes
// on any arguments, and exits. Built by .github/workflows/installers.yml with the C# compiler that ships with Windows.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

[assembly: System.Reflection.AssemblyTitle("Instrumental Studio")]
[assembly: System.Reflection.AssemblyProduct("Instrumental Studio")]

static class Launcher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr h, string text, string caption, uint type);

    [STAThread]
    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string py = Path.Combine(dir, "runtime", "python", "pythonw.exe");
        string script = Path.Combine(dir, "app.py");
        if (!File.Exists(py) || !File.Exists(script))
        {
            MessageBoxW(IntPtr.Zero, "Instrumental Studio's files are missing next to InstrumentalStudio.exe.\nPlease reinstall Instrumental Studio.",
                        "Instrumental Studio", 0x10);
            return 1;
        }
        string rest = "";
        foreach (string a in args)
            rest += " \"" + a.Replace("\"", "\\\"") + "\"";
        var psi = new ProcessStartInfo(py, "\"" + script + "\"" + rest);
        psi.WorkingDirectory = dir;
        psi.UseShellExecute = false;
        psi.EnvironmentVariables["INSTRUMENTAL_STUDIO_LAUNCHER"] = "1";
        Process.Start(psi);
        return 0;
    }
}
