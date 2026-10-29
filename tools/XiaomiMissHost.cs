using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

// 32-bit bridge: Xiaomi libmiss (official PC viewer) -> MPEG-TS on stdout for go2rtc.
// Loads proprietary DLLs from --dll-dir at runtime; they are never bundled here.
internal static class XiaomiMissHost
{
    private const int CmdVideoStart = 0x102;
    private const int CmdVideoStop = 0x103;
    private const int CodecH264 = 4;
    private const int CodecH265 = 5;
    private const int MaxFrame = 8 * 1024 * 1024;

    private static string _did, _model, _rpc, _tokenFile, _dllDir, _ffmpegPath, _logPath;
    private static int _parentPid;
    private static string _token;
    private static StreamWriter _log;
    private static readonly object LogLock = new object();

    private static IntPtr _lib;
    private static IntPtr _session;
    private static RegisterDelegate _register;
    private static SessionOpenDelegate _sessionOpen;
    private static SessionCloseDelegate _sessionClose;
    private static CmdSendDelegate _cmdSend;
    private static InvokeRpcDelegate _invokeRpc;
    private static LogSetPathDelegate _logSetPath;
    private static LogSetLevelDelegate _logSetLevel;

    private static readonly CallbackDelegate[] Callbacks = new CallbackDelegate[9];
    private static readonly IntPtr[] CallbackPtrs = new IntPtr[9];

    private static readonly object FfmpegLock = new object();
    private static Process _ffmpeg;
    private static Stream _ffmpegIn;
    private static IntPtr _job;
    private static volatile bool _firstFrame;
    private static volatile bool _stopped;
    private static int _stopping;
    private static long _frames;
    private static long _bytes;
    private static int _videoCodec;

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int CallbackDelegate(IntPtr a1, IntPtr a2, IntPtr a3, IntPtr a4,
                                          IntPtr a5, IntPtr a6, IntPtr a7);

    [StructLayout(LayoutKind.Sequential)]
    private struct DevInfo
    {
        public IntPtr Did;
        public int DidLen;
        public IntPtr Model;
        public int ModelLen;
        public IntPtr Platform;
        public int PlatformLen;
    }

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate IntPtr SessionOpenDelegate(ref DevInfo dev, IntPtr reserved);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int SessionCloseDelegate(IntPtr session);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int CmdSendDelegate(IntPtr session, int cmd, IntPtr data, int len);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int InvokeRpcDelegate(IntPtr ctx, IntPtr payload, int len);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int RegisterDelegate(IntPtr cb1, IntPtr cb2, IntPtr cb3, IntPtr cb4,
                                          IntPtr cb5, IntPtr cb6, IntPtr cb7, IntPtr cb8,
                                          IntPtr cb9);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
    private delegate int LogSetPathDelegate(string path);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int LogSetLevelDelegate(int level);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetDllDirectory(string path);

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr LoadLibrary(string path);

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attrs, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass,
                                                       IntPtr info, int len);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    private static void Log(string msg)
    {
        string line = DateTime.Now.ToString("HH:mm:ss.fff") + " " + msg;
        lock (LogLock)
        {
            try
            {
                if (_log != null)
                {
                    _log.WriteLine(line);
                    _log.Flush();
                }
            }
            catch
            {
            }
        }
    }

    private static int Main(string[] args)
    {
        try
        {
            if (!ParseArgs(args))
            {
                return 2;
            }
            try
            {
                _log = new StreamWriter(new FileStream(_logPath, FileMode.Create, FileAccess.Write,
                                                       FileShare.ReadWrite), new UTF8Encoding(false));
                _log.AutoFlush = true;
            }
            catch
            {
                _log = null;
            }
            Log("host start did=" + _did + " model=" + _model + " dll=" + _dllDir);
            if (_parentPid > 0)
            {
                Thread watcher = new Thread(ParentWatcher);
                watcher.IsBackground = true;
                watcher.Start();
            }
            _token = File.ReadAllText(_tokenFile).Trim();
            if (_token.Length == 0)
            {
                Log("empty rpc token");
                return 3;
            }
            if (!LoadLibmiss())
            {
                return 2;
            }
            _job = CreateJob(_job);
            if (!RegisterCallbacks())
            {
                return 3;
            }
            DevInfo dev = new DevInfo();
            IntPtr pDid = Marshal.StringToHGlobalAnsi(_did);
            IntPtr pModel = Marshal.StringToHGlobalAnsi(_model);
            IntPtr pPlatform = Marshal.StringToHGlobalAnsi("pc");
            try
            {
                dev.Did = pDid;
                dev.DidLen = _did.Length;
                dev.Model = pModel;
                dev.ModelLen = _model.Length;
                dev.Platform = pPlatform;
                dev.PlatformLen = 2;
                _session = _sessionOpen(ref dev, IntPtr.Zero);
            }
            finally
            {
                Marshal.FreeHGlobal(pDid);
                Marshal.FreeHGlobal(pModel);
                Marshal.FreeHGlobal(pPlatform);
            }
            if (_session == IntPtr.Zero)
            {
                Log("session open failed");
                return 3;
            }
            Log("session=" + _session);
            Thread starter = new Thread(VideoStarter);
            starter.IsBackground = true;
            starter.Start();
            while (!_stopped)
            {
                Thread.Sleep(500);
            }
            return 0;
        }
        catch (Exception ex)
        {
            Log("fatal: " + ex);
            Stop(9);
            return 9;
        }
    }

    private static bool ParseArgs(string[] args)
    {
        Dictionary<string, string> map = new Dictionary<string, string>();
        for (int i = 0; i + 1 < args.Length; i += 2)
        {
            map[args[i]] = args[i + 1];
        }
        string[] keys = { "--did", "--model", "--rpc", "--token-file", "--dll-dir", "--ffmpeg", "--log" };
        string[] vals = new string[keys.Length];
        for (int i = 0; i < keys.Length; i++)
        {
            if (!map.TryGetValue(keys[i], out vals[i]) || string.IsNullOrEmpty(vals[i]))
            {
                return false;
            }
        }
        _did = vals[0];
        _model = vals[1];
        _rpc = vals[2];
        _tokenFile = vals[3];
        _dllDir = vals[4];
        _ffmpegPath = vals[5];
        _logPath = vals[6];
        string parentText;
        if (map.TryGetValue("--parent", out parentText))
        {
            int pid;
            if (int.TryParse(parentText, out pid) && pid > 0)
            {
                _parentPid = pid;
            }
        }
        return true;
    }

    private static bool LoadLibmiss()
    {
        try
        {
            Environment.CurrentDirectory = _dllDir;
            SetDllDirectory(_dllDir);
            _lib = LoadLibrary(Path.Combine(_dllDir, "libmiss.dll"));
            if (_lib == IntPtr.Zero)
            {
                Log("LoadLibrary libmiss failed err=" + Marshal.GetLastWin32Error());
                return false;
            }
            _register = (RegisterDelegate)Bind("register_miss_client_callback", typeof(RegisterDelegate));
            _sessionOpen = (SessionOpenDelegate)Bind("miss_client_session_open", typeof(SessionOpenDelegate));
            _sessionClose = (SessionCloseDelegate)Bind("miss_client_session_close", typeof(SessionCloseDelegate));
            _cmdSend = (CmdSendDelegate)Bind("miss_cmd_send", typeof(CmdSendDelegate));
            _invokeRpc = (InvokeRpcDelegate)Bind("invoke_miss_rpc_process", typeof(InvokeRpcDelegate));
            _logSetPath = (LogSetPathDelegate)Bind("miss_log_set_path", typeof(LogSetPathDelegate));
            _logSetLevel = (LogSetLevelDelegate)Bind("miss_log_set_level", typeof(LogSetLevelDelegate));
            if (_logSetPath != null)
            {
                try
                {
                    string dir = Path.GetDirectoryName(_logPath);
                    string liblog = Path.Combine(dir, "libmiss-" + _did + ".log");
                    using (File.Create(liblog))
                    {
                    }
                    _logSetPath(liblog);
                    if (_logSetLevel != null)
                    {
                        _logSetLevel(4);
                    }
                    Log("libmiss log -> " + liblog);
                }
                catch (Exception ex)
                {
                    Log("libmiss log setup failed: " + ex.Message);
                }
            }
            return _register != null && _sessionOpen != null && _sessionClose != null
                   && _cmdSend != null && _invokeRpc != null;
        }
        catch (Exception ex)
        {
            Log("load error: " + ex.Message);
            return false;
        }
    }

    private static Delegate Bind(string name, Type type)
    {
        IntPtr fn = GetProcAddress(_lib, name);
        if (fn == IntPtr.Zero)
        {
            Log("missing export " + name);
            return null;
        }
        return Marshal.GetDelegateForFunctionPointer(fn, type);
    }

    private static bool RegisterCallbacks()
    {
        for (int i = 0; i < 9; i++)
        {
            int index = i;
            Callbacks[i] = delegate(IntPtr a1, IntPtr a2, IntPtr a3, IntPtr a4,
                                    IntPtr a5, IntPtr a6, IntPtr a7)
            {
                return OnCallback(index, a1, a2, a3, a4, a5, a6, a7);
            };
            CallbackPtrs[i] = Marshal.GetFunctionPointerForDelegate(Callbacks[i]);
        }
        int rc = _register(CallbackPtrs[0], CallbackPtrs[1], CallbackPtrs[2], CallbackPtrs[3],
                           CallbackPtrs[4], CallbackPtrs[5], CallbackPtrs[6], CallbackPtrs[7],
                           CallbackPtrs[8]);
        Log("register callbacks rc=" + rc);
        return true;
    }

    private static int OnCallback(int index, IntPtr a1, IntPtr a2, IntPtr a3, IntPtr a4,
                                  IntPtr a5, IntPtr a6, IntPtr a7)
    {
        try
        {
            if (index == 0)
            {
                return OnRpc(a1, a2, a3, a4);
            }
            if (index == 3)
            {
                OnVideo(a2, a3);
            }
        }
        catch (Exception ex)
        {
            Log("callback " + (index + 1) + " error: " + ex.Message);
        }
        return 0;
    }

    private static int OnRpc(IntPtr ctx, IntPtr pathPtr, IntPtr bodyPtr, IntPtr bodyLenPtr)
    {
        int bodyLen = bodyLenPtr.ToInt32();
        string path = Marshal.PtrToStringAnsi(pathPtr);
        byte[] bodyBytes = new byte[bodyLen > 0 ? bodyLen : 0];
        if (bodyLen > 0)
        {
            Marshal.Copy(bodyPtr, bodyBytes, 0, bodyLen);
        }
        string body = Encoding.UTF8.GetString(bodyBytes);
        Log("rpc " + path + " body=" + bodyLen);
        byte[] payload = null;
        try
        {
            string json = "{\"path\":\"" + JsonEscape(path) + "\",\"body\":\"" + JsonEscape(body) + "\"}";
            payload = HttpPost(json);
        }
        catch (Exception ex)
        {
            Log("rpc http error: " + ex.Message);
        }
        if (payload == null || payload.Length == 0)
        {
            _invokeRpc(ctx, IntPtr.Zero, 0);
            return 0;
        }
        IntPtr buf = Marshal.AllocHGlobal(payload.Length);
        try
        {
            Marshal.Copy(payload, 0, buf, payload.Length);
            int rc = _invokeRpc(ctx, buf, payload.Length);
            Log("rpc result " + payload.Length + " bytes invoke=" + rc);
        }
        finally
        {
            Marshal.FreeHGlobal(buf);
        }
        return 0;
    }

    private static byte[] HttpPost(string json)
    {
        HttpWebRequest req = (HttpWebRequest)WebRequest.Create(_rpc);
        req.Method = "POST";
        req.Proxy = null;
        req.ContentType = "application/json; charset=utf-8";
        req.Headers["X-Miss-Token"] = _token;
        req.Timeout = 30000;
        req.ReadWriteTimeout = 30000;
        byte[] data = Encoding.UTF8.GetBytes(json);
        req.ContentLength = data.Length;
        using (Stream s = req.GetRequestStream())
        {
            s.Write(data, 0, data.Length);
        }
        try
        {
            using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
            using (Stream rs = resp.GetResponseStream())
            {
                if (resp.StatusCode != HttpStatusCode.OK)
                {
                    Log("rpc status " + (int)resp.StatusCode);
                    return null;
                }
                MemoryStream ms = new MemoryStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = rs.Read(buf, 0, buf.Length)) > 0)
                {
                    ms.Write(buf, 0, n);
                }
                return ms.ToArray();
            }
        }
        catch (WebException ex)
        {
            if (ex.Response != null)
            {
                using (Stream es = ex.Response.GetResponseStream())
                using (StreamReader er = new StreamReader(es, Encoding.UTF8))
                {
                    Log("rpc fail: " + er.ReadToEnd());
                }
            }
            else
            {
                Log("rpc fail: " + ex.Message);
            }
            return null;
        }
    }

    private static void OnVideo(IntPtr frame, IntPtr payload)
    {
        if (frame == IntPtr.Zero || _stopped)
        {
            return;
        }
        int len = Marshal.ReadInt32(frame, 0);
        int codec = Marshal.ReadInt32(frame, 4);
        if (len <= 0 || len > MaxFrame)
        {
            return;
        }
        if (codec != CodecH264 && codec != CodecH265)
        {
            return;
        }
        IntPtr data = payload != IntPtr.Zero ? payload : new IntPtr(frame.ToInt64() + 40);
        lock (FfmpegLock)
        {
            if (_ffmpeg == null)
            {
                if (!StartFfmpeg(codec))
                {
                    Stop(5);
                    return;
                }
            }
            if (_ffmpegIn == null)
            {
                return;
            }
            byte[] buf = new byte[len];
            Marshal.Copy(data, buf, 0, len);
            try
            {
                _ffmpegIn.Write(buf, 0, len);
                _ffmpegIn.Flush();
                _frames++;
                _bytes += len;
                if (!_firstFrame)
                {
                    _firstFrame = true;
                    _videoCodec = codec;
                    Log("first frame codec=" + codec + " len=" + len);
                }
            }
            catch (Exception ex)
            {
                Log("write ffmpeg failed: " + ex.Message);
                Stop(0);
            }
        }
    }

    private static bool StartFfmpeg(int codec)
    {
        string fmt = codec == CodecH265 ? "hevc" : "h264";
        string args = "-hide_banner -loglevel error -use_wallclock_as_timestamps 1 -f " + fmt +
                      " -i pipe:0 -c copy -an -flush_packets 1 -f mpegts pipe:1";
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = _ffmpegPath;
            psi.Arguments = args;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardInput = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            Process p = new Process();
            p.StartInfo = psi;
            p.Start();
            _ffmpeg = p;
            _ffmpegIn = p.StandardInput.BaseStream;
            AssignToJob(p);
            Thread pump = new Thread(PumpStdout);
            pump.IsBackground = true;
            pump.Start(p);
            Thread err = new Thread(PumpStderr);
            err.IsBackground = true;
            err.Start(p);
            Log("ffmpeg started fmt=" + fmt + " pid=" + p.Id);
            return true;
        }
        catch (Exception ex)
        {
            Log("ffmpeg start failed: " + ex.Message);
            _ffmpeg = null;
            _ffmpegIn = null;
            return false;
        }
    }

    private static void PumpStdout(object state)
    {
        Process p = (Process)state;
        Stream stdout = Console.OpenStandardOutput();
        byte[] buf = new byte[65536];
        try
        {
            Stream src = p.StandardOutput.BaseStream;
            int n;
            while ((n = src.Read(buf, 0, buf.Length)) > 0)
            {
                stdout.Write(buf, 0, n);
                stdout.Flush();
            }
            Log("ffmpeg stdout closed");
        }
        catch (Exception ex)
        {
            Log("stdout pump: " + ex.Message);
        }
        Stop(0);
    }

    private static void PumpStderr(object state)
    {
        Process p = (Process)state;
        try
        {
            string line;
            while ((line = p.StandardError.ReadLine()) != null)
            {
                if (line.Trim().Length > 0)
                {
                    Log("ffmpeg: " + line);
                }
            }
        }
        catch
        {
        }
    }

    private static void ParentWatcher()
    {
        while (!_stopped)
        {
            Thread.Sleep(3000);
            if (_stopped || _parentPid <= 0)
            {
                return;
            }
            try
            {
                using (Process parent = Process.GetProcessById(_parentPid))
                {
                    if (parent.HasExited)
                    {
                        throw new InvalidOperationException();
                    }
                }
            }
            catch
            {
                Log("panel parent gone");
                Stop(0);
                return;
            }
        }
    }

    private static void VideoStarter()
    {
        int[] delays = { 1800, 5000, 10000, 15000 };
        foreach (int delay in delays)
        {
            Thread.Sleep(delay);
            if (_firstFrame || _stopped)
            {
                return;
            }
            try
            {
                int rc = _cmdSend(_session, CmdVideoStart, IntPtr.Zero, 0);
                Log("video start rc=" + rc);
            }
            catch (Exception ex)
            {
                Log("video start error: " + ex.Message);
            }
        }
        if (!_firstFrame)
        {
            Log("no video frame");
            Stop(4);
        }
    }

    private static IntPtr CreateJob(IntPtr unused)
    {
        try
        {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero)
            {
                return IntPtr.Zero;
            }
            IntPtr info = Marshal.AllocHGlobal(160);
            try
            {
                for (int i = 0; i < 160; i++)
                {
                    Marshal.WriteByte(info, i, 0);
                }
                // JOBOBJECT_EXTENDED_LIMIT_INFORMATION.BasicLimitInformation.LimitFlags
                Marshal.WriteInt32(info, 16, 0x2000); // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                SetInformationJobObject(job, 9, info, 144);
            }
            finally
            {
                Marshal.FreeHGlobal(info);
            }
            return job;
        }
        catch
        {
            return IntPtr.Zero;
        }
    }

    private static void AssignToJob(Process p)
    {
        try
        {
            if (_job != IntPtr.Zero)
            {
                AssignProcessToJobObject(_job, p.Handle);
            }
        }
        catch
        {
        }
    }

    private static void Stop(int code)
    {
        if (Interlocked.Exchange(ref _stopping, 1) == 1)
        {
            return;
        }
        _stopped = true;
        Log("stop code=" + code + " frames=" + _frames + " bytes=" + _bytes + " codec=" + _videoCodec);
        try
        {
            if (_session != IntPtr.Zero && _cmdSend != null)
            {
                _cmdSend(_session, CmdVideoStop, IntPtr.Zero, 0);
            }
        }
        catch
        {
        }
        try
        {
            if (_session != IntPtr.Zero && _sessionClose != null)
            {
                _sessionClose(_session);
            }
        }
        catch
        {
        }
        try
        {
            if (_ffmpeg != null && !_ffmpeg.HasExited)
            {
                _ffmpeg.Kill();
            }
        }
        catch
        {
        }
        try
        {
            if (_job != IntPtr.Zero)
            {
                CloseHandle(_job);
                _job = IntPtr.Zero;
            }
        }
        catch
        {
        }
        Environment.Exit(code);
    }

    private static string JsonEscape(string s)
    {
        StringBuilder sb = new StringBuilder(s.Length + 16);
        foreach (char c in s)
        {
            switch (c)
            {
                case '\\': sb.Append("\\\\"); break;
                case '"': sb.Append("\\\""); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20)
                    {
                        sb.Append("\\u").Append(((int)c).ToString("x4"));
                    }
                    else
                    {
                        sb.Append(c);
                    }
                    break;
            }
        }
        return sb.ToString();
    }
}
