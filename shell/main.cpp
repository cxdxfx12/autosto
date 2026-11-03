// 运单自动采集自动揽收助手 —— Windows 原生外壳
// Win32 主窗口 + 内嵌 CEF（Chromium 内核）浏览器。
//
// 结构：
//   本程序（C++）  负责：窗口、图标、单实例、浏览器内核、拉起后端
//   core\miao-core.exe（Python）负责：微信采集、Excel 解析、SQLite、HTTP 接口
//   浏览器打开 http://127.0.0.1:8766/ 渲染界面
//
// 为什么要 CEF 而不是 WebView2：WebView2 走 DirectComposition 合成，在部分
// 机器（虚拟显卡/安全管控环境）上会整块黑屏，实测这台上就是黑的；CEF 用的是
// 和 Chrome 同一套渲染路径，Chrome 能显示它就能显示。

// winsock2.h 必须先于 windows.h，否则 ws2tcpip.h 里的类型会缺定义
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <shellapi.h>

#include <algorithm>
#include <string>

#include "include/cef_app.h"
#include "include/cef_browser.h"
#include "include/cef_client.h"
#include "include/cef_command_line.h"
#include "include/wrapper/cef_helpers.h"
#include "resource.h"

#pragma comment(lib, "libcef.lib")
#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "gdi32.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#pragma comment(lib, "uuid.lib")
#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "comdlg32.lib")
#pragma comment(lib, "winspool.lib")
#pragma comment(lib, "comctl32.lib")
#pragma comment(lib, "version.lib")

namespace {

const wchar_t kAppTitle[] = L"运单自动采集自动揽收助手";
const wchar_t kMutexName[] = L"MiaoScanShell_SingleInstance";
const wchar_t kClassName[] = L"MiaoScanShellWindow";
const int kBackendPort = 8766;
const int kWinWidth = 1180;
const int kWinHeight = 820;

HWND g_hwnd = nullptr;
CefRefPtr<CefBrowser> g_browser;
HANDLE g_backend_process = nullptr;

// 启动过程写文件日志：windowed 程序没有控制台，卡在哪一步只能靠这个看
std::wstring ExeDir();  // 定义在下面

void Trace(const char* msg) {
  const std::wstring dir = ExeDir() + L"\\data\\logs";
  CreateDirectoryW((ExeDir() + L"\\data").c_str(), nullptr);
  CreateDirectoryW(dir.c_str(), nullptr);
  const std::wstring path = dir + L"\\shell.log";

  HANDLE f = CreateFileW(path.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ, nullptr,
                         OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (f == INVALID_HANDLE_VALUE) return;
  SYSTEMTIME st;
  GetLocalTime(&st);
  char line[512];
  const int n = wsprintfA(line, "[%02d:%02d:%02d.%03d] %s\r\n", st.wHour, st.wMinute,
                          st.wSecond, st.wMilliseconds, msg);
  DWORD written = 0;
  WriteFile(f, line, static_cast<DWORD>(n), &written, nullptr);
  CloseHandle(f);
}

// ---------------------------------------------------------------- 路径工具

std::wstring ExeDir() {
  wchar_t buf[MAX_PATH] = {0};
  GetModuleFileNameW(nullptr, buf, MAX_PATH);
  std::wstring path(buf);
  const size_t pos = path.find_last_of(L"\\/");
  return pos == std::wstring::npos ? path : path.substr(0, pos);
}

bool FileExists(const std::wstring& path) {
  return GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES;
}

// ---------------------------------------------------------------- 后端进程

bool PortAlive(int port) {
  SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (s == INVALID_SOCKET) return false;
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(static_cast<u_short>(port));
  inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

  // 非阻塞 + 短超时，避免后端没起来时卡住界面
  u_long nonblocking = 1;
  ioctlsocket(s, FIONBIO, &nonblocking);
  connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));

  fd_set write_set, err_set;
  FD_ZERO(&write_set);
  FD_ZERO(&err_set);
  FD_SET(s, &write_set);
  FD_SET(s, &err_set);
  timeval tv{0, 200 * 1000};  // 200ms
  const int ready = select(0, nullptr, &write_set, &err_set, &tv);
  closesocket(s);
  return ready > 0 && FD_ISSET(s, &write_set);
}

bool StartBackend() {
  if (PortAlive(kBackendPort)) return true;  // 已经在跑（比如手动开的）

  const std::wstring exe = ExeDir() + L"\\core\\miao-core.exe";
  if (!FileExists(exe)) return false;

  STARTUPINFOW si{};
  si.cb = sizeof(si);
  si.dwFlags = STARTF_USESHOWWINDOW;
  si.wShowWindow = SW_HIDE;
  PROCESS_INFORMATION pi{};
  std::wstring cmd = L"\"" + exe + L"\"";

  const BOOL ok = CreateProcessW(exe.c_str(), &cmd[0], nullptr, nullptr, FALSE,
                                 CREATE_NO_WINDOW, nullptr, ExeDir().c_str(),
                                 &si, &pi);
  if (!ok) return false;

  g_backend_process = pi.hProcess;
  CloseHandle(pi.hThread);
  return true;
}

void StopBackend() {
  if (g_backend_process) {
    TerminateProcess(g_backend_process, 0);
    CloseHandle(g_backend_process);
    g_backend_process = nullptr;
  }
}

// 等后端端口就绪，最多等 timeout_ms；期间处理消息让窗口能画出来
bool WaitBackend(int timeout_ms) {
  for (int waited = 0; waited < timeout_ms; waited += 200) {
    if (PortAlive(kBackendPort)) return true;

    // 后端起不来就别傻等 60 秒：进程退了直接放弃，界面走错误提示页
    if (g_backend_process &&
        WaitForSingleObject(g_backend_process, 0) == WAIT_OBJECT_0) {
      Trace("backend exited early");
      return false;
    }

    MSG msg;
    while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
      TranslateMessage(&msg);
      DispatchMessageW(&msg);
    }
    Sleep(200);
  }
  return PortAlive(kBackendPort);
}

// ---------------------------------------------------------------- 浏览器回调

class AppClient : public CefClient,
                  public CefLifeSpanHandler,
                  public CefDisplayHandler,
                  public CefLoadHandler {
 public:
  AppClient() = default;

  CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
  CefRefPtr<CefDisplayHandler> GetDisplayHandler() override { return this; }
  CefRefPtr<CefLoadHandler> GetLoadHandler() override { return this; }

  void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
    CEF_REQUIRE_UI_THREAD();
    g_browser = browser;
  }

  bool DoClose(CefRefPtr<CefBrowser> browser) override {
    CEF_REQUIRE_UI_THREAD();
    return false;  // 让 CEF 走默认关闭流程
  }

  void OnBeforeClose(CefRefPtr<CefBrowser> browser) override {
    CEF_REQUIRE_UI_THREAD();
    g_browser = nullptr;
    if (g_hwnd) DestroyWindow(g_hwnd);  // 浏览器没了，窗口跟着收
  }

  void OnTitleChange(CefRefPtr<CefBrowser> browser,
                     const CefString& title) override {
    CEF_REQUIRE_UI_THREAD();
    std::wstring text = title.ToWString();
    if (text.empty()) text = kAppTitle;
    SetWindowTextW(g_hwnd, text.c_str());
  }

  void OnLoadError(CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame> frame,
                   ErrorCode errorCode, const CefString& errorText,
                   const CefString& failedUrl) override {
    CEF_REQUIRE_UI_THREAD();
    if (errorCode == ERR_ABORTED || !frame->IsMain()) return;
    // 后端没起来时给一个可点的提示页，而不是白屏
    const std::string html =
        "<!doctype html><meta charset='utf-8'>"
        "<body style=\"font-family:'Microsoft YaHei',sans-serif;background:#f6f7f9;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0\">"
        "<div style='text-align:center'><div style='font-size:44px'>😿</div>"
        "<h2 style='color:#333'>界面没连上后台服务</h2>"
        "<p style='color:#777'>8766 端口没有响应。后台进程可能还在启动，或被杀毒软件拦了。</p>"
        "<button onclick='location.href=\"http://127.0.0.1:8766/\"' "
        "style='margin-top:12px;padding:10px 22px;border:0;border-radius:8px;"
        "background:#f0742c;color:#fff;font-size:15px;cursor:pointer'>重新连接</button></div></body>";
    frame->LoadURL("data:text/html;charset=utf-8," + html);
  }

 private:
  IMPLEMENT_REFCOUNTING(AppClient);
};

// 空 App：只为把命令行处理留给默认实现，子进程也靠它
class ShellApp : public CefApp {
 public:
  ShellApp() = default;

  // 必须在 CefInitialize 内部执行 —— CefCommandLine::GetGlobalCommandLine()
  // 在 CefInitialize 之前调用会拿到空指针，进程直接挂掉（实测踩过：
  // shell.log 停在 "window class ok"，连 "CefInitialize begin" 都打不出来）。
  void OnBeforeCommandLineProcessing(
      const CefString& process_type,
      CefRefPtr<CefCommandLine> command_line) override {
    // 只对浏览器进程生效，子进程继承即可
    if (!process_type.empty()) return;

    // 默认禁用 GPU 加速。
    //
    // 为什么默认关：这台机器装了 5 个显示适配器（NVIDIA GT1030、Intel UHD730、
    // 向日葵虚拟显示器 OrayIddDriver、MuMu 虚拟显卡、另一个 Virtual Display）。
    // CEF 的 GPU 进程在多个虚拟适配器间挑设备会反复崩：
    //   GPU process exited unexpectedly: exit_code=-2147483645
    //   Failed to create shared context for virtualization
    // 连崩 3 次后 libcef.dll 访问冲突（0xc0000005），整个外壳跟着退出。
    // 界面只是本地一个轻量网页，软件渲染完全够用，换稳定更划算。
    //
    // 想要回硬件加速：设 MIAO_ENABLE_GPU=1。
    wchar_t gpu_flag[8] = {0};
    if (GetEnvironmentVariableW(L"MIAO_ENABLE_GPU", gpu_flag, 8) > 0) {
      Trace("gpu enabled by env");
      return;
    }
    // 只关硬件加速，保留软件光栅化 —— 否则界面会整块白屏
    command_line->AppendSwitch("disable-gpu");
    command_line->AppendSwitch("disable-gpu-compositing");
    Trace("gpu disabled by default");
  }

 private:
  IMPLEMENT_REFCOUNTING(ShellApp);
};

// ---------------------------------------------------------------- 主窗口

void ResizeBrowser() {
  if (!g_browser) return;
  RECT rc;
  GetClientRect(g_hwnd, &rc);
  g_browser->GetHost()->WasResized();
  // 子窗口跟着父窗口走（CEF 自己也会处理，这里双保险）
  HWND child = g_browser->GetHost()->GetWindowHandle();
  if (child) {
    SetWindowPos(child, nullptr, 0, 0, rc.right - rc.left, rc.bottom - rc.top,
                 SWP_NOZORDER | SWP_NOACTIVATE);
  }
}

LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
  switch (msg) {
    case WM_SIZE:
      ResizeBrowser();
      return 0;
    case WM_SETFOCUS:
      if (g_browser) g_browser->GetHost()->SetFocus(true);
      return 0;
    case WM_ERASEBKGND:
      return 1;  // 浏览器自己会画，别闪白
    case WM_CLOSE:
      if (g_browser) {
        g_browser->GetHost()->CloseBrowser(true);
        return 0;  // 等 OnBeforeClose 再真正销毁
      }
      DestroyWindow(hwnd);
      return 0;
    case WM_DESTROY:
      PostQuitMessage(0);
      return 0;
    case WM_KEYDOWN:
      if (wparam == VK_F5 && g_browser) {
        g_browser->Reload();
        return 0;
      }
      if (wparam == VK_F12 && g_browser) {
        CefWindowInfo dev_info;
        CefBrowserSettings dev_settings;
        g_browser->GetHost()->ShowDevTools(dev_info, nullptr, dev_settings, CefPoint());
        return 0;
      }
      break;
    default:
      break;
  }
  return DefWindowProcW(hwnd, msg, wparam, lparam);
}

bool RegisterWindowClass(HINSTANCE instance) {
  WNDCLASSEXW wc{};
  wc.cbSize = sizeof(wc);
  wc.style = CS_HREDRAW | CS_VREDRAW;
  wc.lpfnWndProc = WndProc;
  wc.hInstance = instance;
  wc.hIcon = LoadIconW(instance, MAKEINTRESOURCEW(IDI_APPICON));
  wc.hIconSm = wc.hIcon;
  wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
  wc.hbrBackground = CreateSolidBrush(RGB(246, 247, 249));
  wc.lpszClassName = kClassName;
  return RegisterClassExW(&wc) != 0;
}

HWND CreateMainWindow(HINSTANCE instance) {
  RECT work{};
  SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0);
  // 屏幕尺寸是 LONG，先转 int，否则 std::min 模板推导不出来
  const int work_w = static_cast<int>(work.right - work.left);
  const int work_h = static_cast<int>(work.bottom - work.top);
  const int width = (std::min)(kWinWidth, work_w - 80);
  const int height = (std::min)(kWinHeight, work_h - 80);
  const int x = work.left + (work_w - width) / 2;
  const int y = work.top + (work_h - height) / 2;

  HWND hwnd = CreateWindowExW(
      0, kClassName, kAppTitle, WS_OVERLAPPEDWINDOW,
      x, y, width, height,
      nullptr, nullptr, instance, nullptr);
  if (!hwnd) return nullptr;

  ShowWindow(hwnd, SW_SHOW);
  UpdateWindow(hwnd);
  return hwnd;
}

// 首次运行：后端把 token 写在文件里，这里弹一次提示
void ShowFirstRunTokenIfAny() {
  const std::wstring path = ExeDir() + L"\\data\\first-run-token.txt";
  if (!FileExists(path)) return;

  HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (file == INVALID_HANDLE_VALUE) return;
  char buf[256] = {0};
  DWORD read = 0;
  ReadFile(file, buf, sizeof(buf) - 1, &read, nullptr);
  CloseHandle(file);
  DeleteFileW(path.c_str());

  std::wstring token;
  for (DWORD i = 0; i < read; ++i) token.push_back(static_cast<wchar_t>(buf[i]));

  const std::wstring msg =
      L"首次启动，已在程序目录生成 config.yaml。\n\n手机端 token：" + token +
      L"\n\n把这个 token 填到手机 App 里，用于拉取扫描任务。\n（文件里也能看到，别外传）";
  MessageBoxW(nullptr, msg.c_str(), kAppTitle, MB_ICONINFORMATION | MB_OK);
}

// 已有实例在跑时，把它的窗口叫到前面来
void ActivateExistingInstance() {
  HWND existing = FindWindowW(kClassName, nullptr);
  if (existing) {
    ShowWindow(existing, SW_RESTORE);
    SetForegroundWindow(existing);
  }
}

}  // namespace

// ---------------------------------------------------------------- 入口

int APIENTRY wWinMain(HINSTANCE instance, HINSTANCE, LPWSTR, int) {
  SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
  Trace("---- start ----");

  CefMainArgs main_args(instance);
  CefRefPtr<ShellApp> app(new ShellApp());

  // 子进程（renderer/gpu/utility）走这里直接退出
  const int child_exit = CefExecuteProcess(main_args, app, nullptr);
  Trace("CefExecuteProcess done");
  if (child_exit >= 0) return child_exit;

  // 单实例
  HANDLE mutex = CreateMutexW(nullptr, FALSE, kMutexName);
  const DWORD mutex_err = GetLastError();
  if (mutex && mutex_err == ERROR_ALREADY_EXISTS) {
    Trace("another instance running, activate it");
    ActivateExistingInstance();
    return 0;
  }
  Trace("mutex ok");

  WSADATA wsa{};
  WSAStartup(MAKEWORD(2, 2), &wsa);

  if (!RegisterWindowClass(instance)) {
    Trace("RegisterWindowClass failed");
    return 1;
  }
  Trace("window class ok");

  // CEF 配置
  CefSettings settings;
  settings.no_sandbox = true;                // 免管理员权限，也避开部分杀软拦沙箱
  settings.windowless_rendering_enabled = false;
  settings.multi_threaded_message_loop = false;
  settings.log_severity = LOGSEVERITY_WARNING;
  settings.background_color = CefColorSetARGB(255, 246, 247, 249);
  CefString(&settings.locale) = "zh-CN";

  const std::wstring cache_dir = ExeDir() + L"\\data\\cef_cache";
  CefString(&settings.root_cache_path) = cache_dir.c_str();
  CefString(&settings.cache_path) = cache_dir.c_str();
  CefString(&settings.log_file) = (ExeDir() + L"\\data\\logs\\cef.log").c_str();

  // GPU 开关在 ShellApp::OnBeforeCommandLineProcessing 里处理：
  // CefCommandLine::GetGlobalCommandLine() 必须等 CefInitialize 内部才可用，
  // 在这里调会拿到空指针直接崩（实测踩过）。

  Trace("CefInitialize begin");
  if (!CefInitialize(main_args, settings, app, nullptr)) {
    Trace("CefInitialize FAILED");
    MessageBoxW(nullptr, L"浏览器内核初始化失败，请把程序目录完整解压后重试。",
                kAppTitle, MB_ICONERROR | MB_OK);
    return 1;
  }
  Trace("CefInitialize ok");

  StartBackend();          // 起后端（可能已经在跑）
  Trace("StartBackend done");
  WaitBackend(60000);      // 首启要建库，给足 60 秒
  Trace("WaitBackend done");

  g_hwnd = CreateMainWindow(instance);
  if (!g_hwnd) {
    Trace("CreateMainWindow FAILED");
    CefShutdown();
    return 1;
  }
  Trace("main window created");

  ShowFirstRunTokenIfAny();

  // 内嵌浏览器
  RECT rc;
  GetClientRect(g_hwnd, &rc);
  CefWindowInfo window_info;
  window_info.SetAsChild(g_hwnd, CefRect(0, 0, rc.right - rc.left, rc.bottom - rc.top));

  CefBrowserSettings browser_settings;
  browser_settings.background_color = CefColorSetARGB(255, 246, 247, 249);

  CefRefPtr<AppClient> client(new AppClient());
  const bool created = CefBrowserHost::CreateBrowser(
      window_info, client, "http://127.0.0.1:8766/", browser_settings, nullptr, nullptr);
  Trace(created ? "CreateBrowser ok" : "CreateBrowser FAILED");

  CefRunMessageLoop();
  Trace("message loop exit");

  CefShutdown();
  StopBackend();
  if (mutex) CloseHandle(mutex);
  WSACleanup();
  return 0;
}
