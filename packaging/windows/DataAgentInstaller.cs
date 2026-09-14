using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Text;

public static class DataAgentInstaller
{
    private const string AppName = "DataAgent";
    private static readonly byte[] Marker = Encoding.ASCII.GetBytes("__DATAAGENT_APP_ZIP_V1__");

    public static int Main(string[] args)
    {
        try
        {
            string installDir = null;
            bool noShortcuts = false;
            bool noLaunch = false;
            for (int i = 0; i < args.Length; i++)
            {
                string arg = args[i] ?? "";
                if (arg.Equals("--install-dir", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    installDir = args[++i];
                }
                else if (arg.Equals("--no-shortcuts", StringComparison.OrdinalIgnoreCase))
                {
                    noShortcuts = true;
                }
                else if (arg.Equals("--no-launch", StringComparison.OrdinalIgnoreCase))
                {
                    noLaunch = true;
                }
            }

            if (String.IsNullOrWhiteSpace(installDir))
            {
                string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                installDir = Path.Combine(localAppData, "Programs", AppName);
            }

            string ownPath = Process.GetCurrentProcess().MainModule.FileName;
            string tempZip = Path.Combine(Path.GetTempPath(), "DataAgent-app-" + Guid.NewGuid().ToString("N") + ".zip");
            ExtractAppZip(ownPath, tempZip);

            KillExistingApp();
            if (Directory.Exists(installDir))
            {
                Directory.Delete(installDir, true);
            }
            Directory.CreateDirectory(installDir);
            ZipFile.ExtractToDirectory(tempZip, installDir);
            try { File.Delete(tempZip); } catch { }

            string target = Path.Combine(installDir, "DataAgent.exe");
            if (!File.Exists(target))
            {
                throw new FileNotFoundException("安装后未找到 DataAgent.exe", target);
            }

            WriteUninstaller(installDir);
            if (!noShortcuts)
            {
                CreateShortcuts(target);
            }

            Console.WriteLine(AppName + " 已安装到：" + installDir);
            if (!noShortcuts)
            {
                Console.WriteLine("已创建桌面和开始菜单快捷方式。");
            }
            if (!noLaunch)
            {
                Process.Start(new ProcessStartInfo { FileName = target, WorkingDirectory = installDir });
            }
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("DataAgent 安装失败：" + ex.Message);
            Console.Error.WriteLine(ex.ToString());
            return 1;
        }
    }

    private static void ExtractAppZip(string installerPath, string zipPath)
    {
        byte[] payload = File.ReadAllBytes(installerPath);
        long markerIndex = LastIndexOf(payload, Marker);
        if (markerIndex < 0)
        {
            throw new InvalidDataException("安装包缺少应用数据。");
        }
        long start = markerIndex + Marker.Length;
        using (FileStream output = File.Create(zipPath))
        {
            output.Write(payload, (int)start, payload.Length - (int)start);
        }
    }

    private static long LastIndexOf(byte[] source, byte[] pattern)
    {
        for (long i = source.LongLength - pattern.LongLength; i >= 0; i--)
        {
            bool matched = true;
            for (long j = 0; j < pattern.LongLength; j++)
            {
                if (source[i + j] != pattern[j])
                {
                    matched = false;
                    break;
                }
            }
            if (matched) return i;
        }
        return -1;
    }

    private static void KillExistingApp()
    {
        try
        {
            foreach (Process process in Process.GetProcessesByName("DataAgent"))
            {
                try { process.Kill(); process.WaitForExit(3000); } catch { }
            }
        }
        catch { }
    }

    private static void WriteUninstaller(string installDir)
    {
        string uninstallPath = Path.Combine(installDir, "Uninstall DataAgent.cmd");
        string content =
            "@echo off\r\n" +
            "setlocal\r\n" +
            "taskkill /IM DataAgent.exe /F >nul 2>nul\r\n" +
            "del \"%USERPROFILE%\\Desktop\\DataAgent.lnk\" >nul 2>nul\r\n" +
            "del \"%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\DataAgent.lnk\" >nul 2>nul\r\n" +
            "cd /d \"%TEMP%\"\r\n" +
            "rmdir /S /Q \"%LOCALAPPDATA%\\Programs\\DataAgent\"\r\n" +
            "endlocal\r\n";
        File.WriteAllText(uninstallPath, content, Encoding.UTF8);
    }

    private static void CreateShortcuts(string target)
    {
        string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
        string startMenu = Environment.GetFolderPath(Environment.SpecialFolder.StartMenu);
        CreateShortcut(Path.Combine(desktop, "DataAgent.lnk"), target);
        CreateShortcut(Path.Combine(startMenu, "Programs", "DataAgent.lnk"), target);
    }

    private static void CreateShortcut(string linkPath, string target)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(linkPath));
        Type shellType = Type.GetTypeFromProgID("WScript.Shell");
        dynamic shell = Activator.CreateInstance(shellType);
        dynamic shortcut = shell.CreateShortcut(linkPath);
        shortcut.TargetPath = target;
        shortcut.WorkingDirectory = Path.GetDirectoryName(target);
        shortcut.IconLocation = target;
        shortcut.Description = "DataAgent 本地智能分析平台";
        shortcut.Save();
    }
}
