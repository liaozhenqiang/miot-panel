using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

// 32-bit bridge: Xiaomi libmiss (official PC viewer) -> MPEG-TS on stdout for go2rtc.
// Loads proprietary DLLs from --dll-dir at runtime; they are never bundled here.
// Video is copied through; the mic stream (Opus) is re-framed and fed to ffmpeg as a
// second input over loopback TCP, then encoded to AAC in the same TS.
internal static class XiaomiMissHost
{
    private const int CmdVideoStart = 0x102;
    private const int CmdVideoStop = 0x103;
    private const int CmdAudioStart = 0x104;
    private const int CmdAudioStop = 0x105;
    private const int CodecH264 = 4;
    private const int CodecH265 = 5;
    private const int CodecAudio = 1032;   // 发 0x104 后设备在视频通道里送的麦克风帧
    private const int MaxFrame = 8 * 1024 * 1024;

    private static string _did, _model, _rpc, _ctrl, _tokenFile, _dllDir, _ffmpegPath, _logPath;
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
    private static OggOpusStream _audio;
    private static string _audioPortFile;
    private static bool _noAudio;
    private static long _audioPackets;
    private static long _audioSkipped;
    private static long _audioFill;
    private static int _ffmpegTick;
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
            StartAudioStream();
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
            Thread ctrl = new Thread(ControlPoller);
            ctrl.IsBackground = true;
            ctrl.Start();
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

    private static string AudioOffPath
    {
        get
        {
            string dir = Path.GetDirectoryName(_logPath);
            return Path.Combine(dir, "audio-off-" + _did + ".flag");
        }
    }

    private static void StartAudioStream()
    {
        if (_noAudio)
        {
            Log("audio off (panel flag)");
            return;
        }
        try
        {
            string off = AudioOffPath;
            if (File.Exists(off))
            {
                DateTime written = File.GetLastWriteTime(off);
                if ((DateTime.Now - written).TotalMinutes < 30)
                {
                    Log("audio paused after an earlier ffmpeg failure; retry " +
                        written.AddMinutes(30).ToString("HH:mm"));
                    return;
                }
                File.Delete(off);
            }
            _audio = new OggOpusStream();
            if (!string.IsNullOrEmpty(_audioPortFile))
            {
                File.WriteAllText(_audioPortFile, _audio.Port.ToString());
            }
            Log("audio stream port=" + _audio.Port);
        }
        catch (Exception ex)
        {
            _audio = null;
            Log("audio stream failed: " + ex.Message);
        }
    }

    // 音频是 ffmpeg 的第二路输入，它出问题会连累画面；一旦起流后很快退出就暂时关掉音频
    private static void MarkAudioOff(string reason)
    {
        try
        {
            File.WriteAllText(AudioOffPath, reason ?? "");
            Log("audio paused: " + (reason ?? ""));
        }
        catch (Exception)
        {
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
        int apiCut = _rpc.IndexOf("/api/");
        _ctrl = apiCut > 0 ? _rpc.Substring(0, apiCut) + "/api/camera/bridge_ctrl" : "";
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
        string audioFile;
        if (map.TryGetValue("--audio-port-file", out audioFile))
        {
            _audioPortFile = audioFile;
        }
        string noAudio;
        if (map.TryGetValue("--no-audio", out noAudio))
        {
            _noAudio = noAudio != "0";
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
                    // 每进程一个文件：两个桥接同时存在时，共享同一个 libmiss 日志会打开失败，
                    // 而 libmiss 打不开文件时会把日志直接写到 stdout，把 TS 流污染掉。
                    string liblog = Path.Combine(dir, "libmiss-" + _did + "-" +
                                                 Process.GetCurrentProcess().Id + ".log");
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
                    if (_logSetLevel != null)
                    {
                        try
                        {
                            _logSetLevel(5);
                        }
                        catch (Exception)
                        {
                        }
                    }
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

    private static void ControlPoller()
    {
        if (string.IsNullOrEmpty(_ctrl))
        {
            return;
        }
        int fails = 0;
        while (!_stopped)
        {
            if (_session == IntPtr.Zero)
            {
                Thread.Sleep(300);
                continue;
            }
            byte[] resp = null;
            try
            {
                resp = HttpPostTo(_ctrl, "{\"did\":\"" + _did + "\"}", 2500);
            }
            catch (Exception ex)
            {
                Log("ctrl poll error: " + ex.Message);
            }
            if (resp == null)
            {
                // 面板不支持该接口时彻底停轮询，避免刷日志
                if (++fails > 30)
                {
                    Log("ctrl poll disabled");
                    return;
                }
                Thread.Sleep(600);
                continue;
            }
            fails = 0;
            string text = Encoding.UTF8.GetString(resp);
            if (text.Length > 0)
            {
                SendCommands(text);
            }
            Thread.Sleep(120);
        }
    }

    private static void SendCommands(string text)
    {
        string[] lines = text.Replace("\r", "").Split('\n');
        foreach (string line in lines)
        {
            if (line.Length == 0)
            {
                continue;
            }
            string[] parts = line.Split(' ');
            if (parts.Length != 3)
            {
                continue;
            }
            int cmd, len;
            if (!int.TryParse(parts[0], out cmd) || !int.TryParse(parts[1], out len))
            {
                continue;
            }
            if (len <= 0 || len > 4096 || _session == IntPtr.Zero)
            {
                continue;
            }
            byte[] payload;
            try
            {
                payload = Convert.FromBase64String(parts[2]);
            }
            catch
            {
                continue;
            }
            if (payload.Length != len)
            {
                continue;
            }
            IntPtr buf = Marshal.AllocHGlobal(len);
            try
            {
                Marshal.Copy(payload, 0, buf, len);
                int rc = _cmdSend(_session, cmd, buf, len);
                Log("ctrl cmd=0x" + cmd.ToString("X") + " len=" + len + " rc=" + rc);
            }
            catch (Exception ex)
            {
                Log("ctrl send error: " + ex.Message);
            }
            finally
            {
                Marshal.FreeHGlobal(buf);
            }
        }
    }

    private static byte[] HttpPost(string json)
    {
        return HttpPostTo(_rpc, json, 30000);
    }

    private static byte[] HttpPostTo(string url, string json, int timeoutMs)
    {
        HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
        req.Method = "POST";
        req.Proxy = null;
        req.ContentType = "application/json; charset=utf-8";
        req.Headers["X-Miss-Token"] = _token;
        req.Timeout = timeoutMs;
        req.ReadWriteTimeout = timeoutMs;
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
            if (codec == CodecAudio && _audio != null && len >= 4 && len <= 4096)
            {
                IntPtr src = payload != IntPtr.Zero ? payload : new IntPtr(frame.ToInt64() + 40);
                byte[] buf = new byte[len];
                try
                {
                    Marshal.Copy(src, buf, 0, len);
                    _audio.WriteFrame(buf);
                    _audioPackets++;
                }
                catch (Exception)
                {
                    _audioSkipped++;
                }
            }
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
        bool audio = _audio != null;
        string args = "-hide_banner -loglevel error -use_wallclock_as_timestamps 1 -f " + fmt +
                      " -i pipe:0";
        if (audio)
        {
            // 音频输入读不到数据就会把整条 TS 一起带走，超时给到 15 秒才不至于因为
            // 设备短暂停推就前功尽弃（心跳补包正常时根本用不到这么久）
            args += " -use_wallclock_as_timestamps 1 -f ogg -i tcp://127.0.0.1:" + _audio.Port +
                    "?timeout=15000000";
        }
        args += audio ? " -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 24k" : " -c copy -an";
        args += " -flush_packets 1 -f mpegts pipe:1";
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
            _ffmpegTick = Environment.TickCount;
            _ffmpegIn = p.StandardInput.BaseStream;
            AssignToJob(p);
            Thread pump = new Thread(PumpStdout);
            pump.IsBackground = true;
            pump.Start(p);
            Thread err = new Thread(PumpStderr);
            err.IsBackground = true;
            err.Start(p);
            Log("ffmpeg started fmt=" + fmt + " audio=" + (audio ? _audio.Port.ToString() : "off") +
                " pid=" + p.Id);
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

    // 设备音频帧 = [Opus TOC][1 字节设备字段][Opus 载荷]，去掉中间那个字节就是合法 Opus 包。
    // libmiss 不带解码器，所以这里把包封成 Ogg-Opus，用回环 TCP 交给 ffmpeg 的第二路输入。
    private sealed class OggOpusStream : IDisposable
    {
        private const uint Serial = 0x4D494F54;
        private static readonly uint[] CrcTable = BuildCrcTable();

        private readonly TcpListener _listener;
        private readonly object _gate = new object();
        private readonly Queue<byte[]> _queue = new Queue<byte[]>();
        private readonly byte[] _headers;
        private NetworkStream _client;
        private int _seq = 2;
        private long _granule;
        private int _startedTick;
        private byte _toc = 0x50;              // 实测：SILK-WB / 40ms / VBR 单帧
        private long _lastPacket;
        private bool _closed;

        public OggOpusStream()
        {
            _listener = new TcpListener(IPAddress.Loopback, 0);
            _listener.Start();
            _headers = Concat(Page(OpusHead(), 0, 2), Page(OpusTags(), 0, 0));
            Thread accept = new Thread(AcceptLoop);
            accept.IsBackground = true;
            accept.Start();
            Thread beat = new Thread(Heartbeat);
            beat.IsBackground = true;
            beat.Start();
        }

        public int Port
        {
            get { return ((IPEndPoint)_listener.LocalEndpoint).Port; }
        }

        private static uint[] BuildCrcTable()
        {
            uint[] table = new uint[256];
            for (uint i = 0; i < 256; i++)
            {
                uint r = i << 24;
                for (int k = 0; k < 8; k++)
                {
                    r = ((r & 0x80000000u) != 0) ? ((r << 1) ^ 0x04C11DB7u) : (r << 1);
                }
                table[i] = r;
            }
            return table;
        }

        private static uint Crc(byte[] data)
        {
            uint crc = 0;
            for (int i = 0; i < data.Length; i++)
            {
                crc = (crc << 8) ^ CrcTable[((crc >> 24) & 0xFF) ^ data[i]];
            }
            return crc;
        }

        private static byte[] Concat(byte[] a, byte[] b)
        {
            byte[] outBuf = new byte[a.Length + b.Length];
            Buffer.BlockCopy(a, 0, outBuf, 0, a.Length);
            Buffer.BlockCopy(b, 0, outBuf, a.Length, b.Length);
            return outBuf;
        }

        private static byte[] OpusHead()
        {
            byte[] head = new byte[19];
            Buffer.BlockCopy(Encoding.ASCII.GetBytes("OpusHead"), 0, head, 0, 8);
            head[8] = 1;                       // version
            head[9] = 1;                       // channels
            head[10] = 0; head[11] = 0;        // pre-skip
            WriteLE(head, 12, 48000);          // input sample rate
            head[16] = 0; head[17] = 0;        // output gain
            head[18] = 0;                      // channel mapping family
            return head;
        }

        private static byte[] OpusTags()
        {
            byte[] tags = new byte[12];
            Buffer.BlockCopy(Encoding.ASCII.GetBytes("OpusTags"), 0, tags, 0, 8);
            return tags;
        }

        private static void WriteLE(byte[] buf, int off, int value)
        {
            buf[off] = (byte)(value & 0xFF);
            buf[off + 1] = (byte)((value >> 8) & 0xFF);
            buf[off + 2] = (byte)((value >> 16) & 0xFF);
            buf[off + 3] = (byte)((value >> 24) & 0xFF);
        }

        private byte[] Page(byte[] payload, long granule, int type)
        {
            int segs = payload.Length / 255 + 1;
            byte[] page = new byte[27 + segs + payload.Length];
            Buffer.BlockCopy(Encoding.ASCII.GetBytes("OggS"), 0, page, 0, 4);
            page[4] = 0;
            page[5] = (byte)type;
            for (int i = 0; i < 8; i++)
            {
                page[6 + i] = (byte)((granule >> (8 * i)) & 0xFF);
            }
            WriteLE(page, 14, unchecked((int)Serial));
            WriteLE(page, 18, _seq);
            WriteLE(page, 22, 0);
            page[26] = (byte)segs;
            int left = payload.Length;
            for (int i = 0; i < segs; i++)
            {
                int take = left >= 255 ? 255 : left;
                page[27 + i] = (byte)take;
                left -= take;
            }
            Buffer.BlockCopy(payload, 0, page, 27 + segs, payload.Length);
            WriteLE(page, 22, unchecked((int)Crc(page)));
            _seq++;
            return page;
        }

        // TOC 里 5 bit config 决定一帧时长，granule 按 48kHz 累加（RFC 6716 表）
        private static int FrameSamples(byte toc)
        {
            int[] wide = new int[] { 100, 200, 400, 600 };
            int config = (toc >> 3) & 0x1F;
            int ms10;
            if (config < 12)
            {
                ms10 = wide[config % 4];
            }
            else if (config < 16)
            {
                ms10 = (config % 2) == 0 ? 100 : 200;
            }
            else if (config < 24)
            {
                ms10 = (config % 2) == 0 ? 25 : 50;
            }
            else
            {
                ms10 = wide[(config - 24) % 4];
            }
            return 48 * ms10 / 10;
        }

        public void WriteFrame(byte[] frame)
        {
            if (frame.Length < 4)
            {
                return;
            }
            _toc = frame[0];
            byte[] packet = new byte[frame.Length - 1];
            packet[0] = frame[0];
            Buffer.BlockCopy(frame, 2, packet, 1, frame.Length - 3);
            WritePacket(packet);
        }

        private void WritePacket(byte[] packet)
        {
            lock (_gate)
            {
                WritePacketLocked(packet);
            }
        }

        private void WritePacketLocked(byte[] packet)
        {
            _granule += FrameSamples(_toc);
            byte[] page = Page(packet, _granule, 0);
            _lastPacket = Environment.TickCount;
            if (_client != null)
            {
                try
                {
                    _client.Write(page, 0, page.Length);
                    return;
                }
                catch (Exception)
                {
                    CloseClient();
                }
            }
            if (_queue.Count > 400)
            {
                _queue.Dequeue();
            }
            _queue.Enqueue(page);
        }

        private void AcceptLoop()
        {
            while (!_closed)
            {
                Socket socket;
                try
                {
                    socket = _listener.AcceptSocket();
                }
                catch (Exception)
                {
                    return;
                }
                try
                {
                    NetworkStream stream = new NetworkStream(socket, true);
                    lock (_gate)
                    {
                        _client = stream;
                        _seq = 2;
                        _granule = 0;
                        _startedTick = Environment.TickCount;
                        stream.Write(_headers, 0, _headers.Length);
                        while (_queue.Count > 0)
                        {
                            byte[] page = _queue.Dequeue();
                            stream.Write(page, 0, page.Length);
                        }
                        _lastPacket = Environment.TickCount;
                    }
                    Log("audio client attached port=" + Port);
                }
                catch (Exception ex)
                {
                    Log("audio client failed: " + ex.Message);
                }
            }
        }

        // ffmpeg 的解码循环会阻塞在输入上，没声音时也要按 DTX 空包续着，否则整条 TS 卡住
        private void Heartbeat()
        {
            int tick = 0;
            while (!_closed)
            {
                Thread.Sleep(100);
                lock (_gate)
                {
                    if (_client == null)
                    {
                        continue;
                    }
                    // 对齐墙钟：设备静音时补 DTX 空包，ffmpeg 读不到数据会把整条流卡住
                    // granule 是 48kHz 采样数，除以 48 才是毫秒（乘 1000 除 48 会把它放大 1000 倍）
                    long want = Environment.TickCount - _startedTick;
                    long have = _granule / 48;
                    int guard = 0;
                    while (have + 100 < want && guard++ < 30)
                    {
                        WritePacketLocked(new byte[] { _toc });
                        _audioFill++;
                        have += 40;
                    }
                    // 墙钟还领先也要保持链路有数据：ffmpeg 的音频输入读不到包超时会把整条 TS 带走
                    if (Environment.TickCount - _lastPacket > 500)
                    {
                        WritePacketLocked(new byte[] { _toc });
                        _audioFill++;
                    }
                    // 每 10 秒报一次状态，现场判断“到底是谁没喂数据”全靠这行
                    if (++tick >= 100)
                    {
                        tick = 0;
                        Log("audio state packets=" + _audioPackets + " fill=" + _audioFill +
                            " queued=" + _queue.Count + " stream=" + (have / 1000) + "s wall=" +
                            (want / 1000) + "s");
                    }
                }
            }
        }

        private void CloseClient()
        {
            if (_client != null)
            {
                try
                {
                    _client.Close();
                }
                catch (Exception)
                {
                }
                _client = null;
                Log("audio client dropped packets=" + _audioPackets + " queued=" + _queue.Count);
            }
        }

        public void Dispose()
        {
            _closed = true;
            lock (_gate)
            {
                CloseClient();
            }
            try
            {
                _listener.Stop();
            }
            catch (Exception)
            {
            }
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
            if (_audio != null && Environment.TickCount - _ffmpegTick < 20000)
            {
                MarkAudioOff("ffmpeg died " + ((Environment.TickCount - _ffmpegTick) / 1000) +
                             "s after an audio start");
            }
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

    private static DateTime _parentStart;

    private static void ParentWatcher()
    {
        try
        {
            using (Process parent = Process.GetProcessById(_parentPid))
            {
                _parentStart = parent.StartTime;
            }
        }
        catch (Exception)
        {
            Log("panel parent already gone");
            Stop(0);
            return;
        }
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
                    // 只判断"还在不在"会被 PID 复用骗过：面板重启后新进程可能拿到同一个 pid
                    if (parent.HasExited || parent.StartTime != _parentStart)
                    {
                        throw new InvalidOperationException();
                    }
                }
            }
            catch (Exception)
            {
                Log("panel parent gone");
                Stop(0);
                return;
            }
        }
    }

    private static void VideoStarter()
    {
        int[] delays = { 1200, 3000, 6000, 10000 };
        int refused = 0;
        int start = Environment.TickCount;
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
                if (rc != 0)
                {
                    // 刚建立会话时被拒是常见现象；6 秒后还连续拒绝才快速放弃
                    if (Environment.TickCount - start > 6000 && ++refused >= 3)
                    {
                        Log("video start refused, giving up");
                        break;
                    }
                    continue;
                }
                if (_audio != null)
                {
                    int arc = _cmdSend(_session, CmdAudioStart, IntPtr.Zero, 0);
                    Log("audio start rc=" + arc);
                }
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
        Log("stop code=" + code + " frames=" + _frames + " bytes=" + _bytes +
            " codec=" + _videoCodec + " audio=" + _audioPackets + "/" + _audioSkipped +
            " fill=" + _audioFill);
        try
        {
            if (_session != IntPtr.Zero && _cmdSend != null)
            {
                _cmdSend(_session, CmdVideoStop, IntPtr.Zero, 0);
                if (_audio != null)
                {
                    _cmdSend(_session, CmdAudioStop, IntPtr.Zero, 0);
                }
            }
        }
        catch
        {
        }
        if (_audio != null)
        {
            _audio.Dispose();
            _audio = null;
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
