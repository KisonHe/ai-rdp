// SPDX-License-Identifier: Apache-2.0
// One owner thread for every FreeRDP call except the documented abort function.
// stdin/stdout are a private pipe, never a network-facing protocol.
#include <freerdp/freerdp.h>
#include <freerdp/settings.h>
#include <freerdp/gdi/gdi.h>
#include <freerdp/input.h>
#include <freerdp/codec/color.h>
#include <winpr/synch.h>
#include <openssl/pem.h>
#include <openssl/x509.h>
#include <openssl/rand.h>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <iostream>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#include <filesystem>
#include <fcntl.h>
#include <sys/stat.h>
#ifdef _WIN32
#include <io.h>
#else
#include <unistd.h>
#endif

static FILE* wire = nullptr;
static std::mutex output_mutex, queue_mutex;
static std::deque<std::string> commands;
static std::atomic<bool> stopped{false};
static freerdp* instance = nullptr;
static HANDLE command_event = nullptr;
static std::string frame_path, certificate_pin;
static std::string expected_host;
static UINT16 expected_port = 0;
static std::set<UINT32> pressed;
static UINT16 buttons = 0, mouse_x = 0, mouse_y = 0;
static uint64_t generation = 0;

class InputPacer {
#ifdef _WIN32
    HANDLE timer = CreateWaitableTimerExW(nullptr, nullptr, 0x00000002,
                                          TIMER_MODIFY_STATE | SYNCHRONIZE);
    std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::now();
#endif
public:
    ~InputPacer() {
#ifdef _WIN32
        if (timer) CloseHandle(timer);
#endif
    }
    void wait() {
#ifdef _WIN32
        // High-resolution per-process wait; no global timeBeginPeriod change.
        // Ordinary Sleep(3) can take 15.6ms and exceed the command timeout.
        if (timer) {
            LARGE_INTEGER due; due.QuadPart = -30000; // Relative 3ms, in 100ns units.
            if (SetWaitableTimer(timer, &due, 0, nullptr, nullptr, FALSE) &&
                WaitForSingleObject(timer, 100) == WAIT_OBJECT_0) return;
        }
        // Older Windows: bound accumulated rounding, allowing at most a small burst.
        auto now = std::chrono::steady_clock::now();
        if (now > deadline + std::chrono::milliseconds(16)) deadline = now;
        deadline += std::chrono::milliseconds(3);
        std::this_thread::sleep_until(deadline);
#else
        std::this_thread::sleep_for(std::chrono::milliseconds(3));
#endif
    }
};

static void reply(const std::string& message) {
    std::lock_guard<std::mutex> guard(output_mutex);
    std::fprintf(wire, "%s\n", message.c_str());
    std::fflush(wire);
}
static int nibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static std::string unhex(const std::string& in) {
    if (in == "-") return {};
    if (in.size() % 2) throw std::runtime_error("invalid_hex");
    std::string out;
    for (size_t i = 0; i < in.size(); i += 2) {
        int a = nibble(in[i]), b = nibble(in[i+1]);
        if (a < 0 || b < 0) throw std::runtime_error("invalid_hex");
        out.push_back(static_cast<char>((a << 4) | b));
    }
    return out;
}
static int verify_certificate(freerdp*, const BYTE* data, size_t length,
                              const char* host, UINT16 port, DWORD flags) {
    if (stopped) return 0;
    if (!host || expected_host != host || port != expected_port) {
        reply("CERT_ERROR 0 unexpected_target"); return 0;
    }
    if (flags & (VERIFY_CERT_FLAG_REDIRECT | VERIFY_CERT_FLAG_GATEWAY)) {
        reply("CERT_ERROR 0 unsupported_redirect"); return 0;
    }
    BIO* bio = BIO_new_mem_buf(data, static_cast<int>(length));
    X509* cert = bio ? PEM_read_bio_X509(bio, nullptr, nullptr, nullptr) : nullptr;
    unsigned char md[EVP_MAX_MD_SIZE];
    unsigned int md_len = 0;
    std::string fingerprint;
    if (cert && X509_digest(cert, EVP_sha256(), md, &md_len)) {
        static const char* digits = "0123456789abcdef";
        for (unsigned i = 0; i < md_len; ++i) {
            fingerprint += digits[md[i] >> 4]; fingerprint += digits[md[i] & 15];
        }
    }
    X509_free(cert); BIO_free(bio);
    // Explicit pinning, never accept-all / TOFU. No certificate is persisted by FreeRDP.
    if (fingerprint.size() == 64 && fingerprint == certificate_pin) return 2;
    if (!fingerprint.empty()) reply("CERT 0 " + fingerprint);
    reply("CERT_ERROR 0 " + std::string(fingerprint.empty() ? "certificate_invalid" :
                                       certificate_pin.empty() ? "certificate_untrusted" : "certificate_mismatch"));
    return 0;
}
static BOOL begin_paint(rdpContext* c) {
    c->gdi->primary->hdc->hwnd->invalid->null = TRUE;
    return TRUE;
}
static BOOL end_paint(rdpContext*) { ++generation; return TRUE; }
static BOOL resize(rdpContext* c) {
    ++generation;
    return gdi_resize(c->gdi, freerdp_settings_get_uint32(c->settings, FreeRDP_DesktopWidth),
                      freerdp_settings_get_uint32(c->settings, FreeRDP_DesktopHeight));
}
static BOOL post_connect(freerdp* i) {
    if (stopped) return FALSE;
    if (!gdi_init(i, PIXEL_FORMAT_BGRA32)) return FALSE;
    i->context->update->BeginPaint = begin_paint;
    i->context->update->EndPaint = end_paint;
    i->context->update->DesktopResize = resize;
    return TRUE;
}
static BOOL authenticate(freerdp*, char**, char**, char**, rdp_auth_reason) {
    // Supplied credentials only. Never prompt on the command pipe or retry another account.
    return FALSE;
}
static void release_all() {
    if (!instance || !instance->context || !instance->context->input) return;
    auto* input = instance->context->input;
    for (auto code : pressed) (void)freerdp_input_send_keyboard_event_ex(input, FALSE, FALSE, code);
    pressed.clear();
    for (UINT16 b : {PTR_FLAGS_BUTTON1, PTR_FLAGS_BUTTON2, PTR_FLAGS_BUTTON3})
        if (buttons & b) (void)freerdp_input_send_mouse_event(input, b, mouse_x, mouse_y);
    buttons = 0;
}
static FILE* temporary_frame(std::filesystem::path& name) {
#ifdef _WIN32
    unsigned char random[12];
    if (RAND_bytes(random, sizeof(random)) != 1) return nullptr;
    std::string suffix;
    for (auto b : random) { suffix += "0123456789abcdef"[b >> 4]; suffix += "0123456789abcdef"[b & 15]; }
    name = std::filesystem::u8path(frame_path + "." + suffix);
    HANDLE handle = CreateFileW(name.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                                FILE_ATTRIBUTE_TEMPORARY, nullptr);
    if (handle == INVALID_HANDLE_VALUE) return nullptr;
    int fd = _open_osfhandle(reinterpret_cast<intptr_t>(handle), _O_WRONLY | _O_BINARY);
    if (fd < 0) { CloseHandle(handle); DeleteFileW(name.c_str()); return nullptr; }
    FILE* f = _fdopen(fd, "wb");
    if (!f) { _close(fd); DeleteFileW(name.c_str()); }
    return f;
#else
    std::string tmp = frame_path + ".XXXXXX";
    std::vector<char> buffer(tmp.begin(), tmp.end()); buffer.push_back(0);
    int fd = mkstemp(buffer.data());
    if (fd < 0) return nullptr;
    name = std::filesystem::u8path(buffer.data());
    FILE* f = fdopen(fd, "wb");
    if (!f) { close(fd); unlink(buffer.data()); }
    return f;
#endif
}
static bool capture() {
    auto* g = instance->context->gdi;
    if (!g || !g->primary_buffer || g->width < 1 || g->height < 1) return false;
    std::filesystem::path name;
    FILE* f = temporary_frame(name);
    if (!f) return false;
    bool ok = std::fprintf(f, "P6\n%d %d\n255\n", g->width, g->height) > 0;
    std::vector<unsigned char> row(static_cast<size_t>(g->width) * 3);
    for (int y = 0; y < g->height && ok; ++y) {
        const BYTE* src = g->primary_buffer + y * g->stride;
        for (int x = 0; x < g->width; ++x) {
            row[x*3] = src[x*4+2]; row[x*3+1] = src[x*4+1]; row[x*3+2] = src[x*4];
        }
        ok = fwrite(row.data(), 1, row.size(), f) == row.size();
    }
    if (fclose(f)) ok = false;
    if (ok) {
#ifdef _WIN32
        auto target = std::filesystem::u8path(frame_path);
        ok = false;
        for (int attempt = 0; attempt < 5; ++attempt) {
            if (MoveFileExW(name.c_str(), target.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
                ok = true; break;
            }
            auto error = GetLastError();
            if (error != ERROR_SHARING_VIOLATION && error != ERROR_ACCESS_DENIED) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
#else
        ok = rename(name.c_str(), frame_path.c_str()) == 0;
#endif
    }
    if (!ok) { std::error_code ignored; std::filesystem::remove(name, ignored); }
    return ok;
}
static void execute(const std::string& line) {
    std::istringstream s(line);
    std::string op; unsigned long long id = 0;
    s >> op >> id;
    std::string error;
    try {
        auto* c = instance->context;
        if (stopped) throw std::runtime_error("stopped");
        if (op == "CAPTURE") {
            long long previous = -1;
            s >> previous;
            if (previous >= 0 && static_cast<uint64_t>(previous) == generation) {
                reply("OK " + std::to_string(id) + " " + std::to_string(c->gdi->width) + " " +
                      std::to_string(c->gdi->height) + " " + std::to_string(generation) + " unchanged"); return;
            }
            if (!capture()) throw std::runtime_error("capture_failed");
            reply("OK " + std::to_string(id) + " " + std::to_string(c->gdi->width) + " " +
                  std::to_string(c->gdi->height) + " " + std::to_string(generation)); return;
        } else if (op == "KEY") {
            unsigned code = 0, down = 0;
            if (!(s >> code >> down) || code > 0x1ff || down > 1) throw std::runtime_error("invalid_key");
            if (!freerdp_input_send_keyboard_event_ex(c->input, down, FALSE, code))
                throw std::runtime_error("input_failed");
            if (down) pressed.insert(code); else pressed.erase(code);
        } else if (op == "MOUSE") {
            unsigned x = 0, y = 0, flags = 0;
            if (!(s >> x >> y >> flags) || x >= static_cast<unsigned>(c->gdi->width) ||
                y >= static_cast<unsigned>(c->gdi->height) || flags > 0xffff)
                throw std::runtime_error("invalid_mouse");
            if (!freerdp_input_send_mouse_event(c->input, flags, x, y)) throw std::runtime_error("input_failed");
            mouse_x = x; mouse_y = y;
            const UINT16 mask = flags & (PTR_FLAGS_BUTTON1 | PTR_FLAGS_BUTTON2 | PTR_FLAGS_BUTTON3);
            if (flags & PTR_FLAGS_DOWN) buttons |= mask; else buttons &= ~mask;
        } else if (op == "TEXT") {
            std::string text; s >> text;
            auto raw = unhex(text);
            if (raw.size() % 2 || raw.size() > 8192) throw std::runtime_error("invalid_text");
            InputPacer pacer;
            for (size_t n = 0; n < raw.size(); n += 2) {
                if (stopped) throw std::runtime_error("stopped");
                const UINT16 u = static_cast<unsigned char>(raw[n]) |
                                 (static_cast<unsigned char>(raw[n+1]) << 8);
                if (!freerdp_input_send_unicode_keyboard_event(c->input, 0, u) ||
                    !freerdp_input_send_unicode_keyboard_event(c->input, KBD_FLAGS_RELEASE, u))
                    throw std::runtime_error("input_failed");
                // Bound bursts into Windows' input queue without making the caller type per key.
                pacer.wait();
            }
        } else if (op == "RELEASE") release_all();
        else throw std::runtime_error("unknown_command");
        reply("OK " + std::to_string(id));
    } catch (const std::exception& e) {
        release_all(); reply("ERR " + std::to_string(id) + " " + e.what());
    }
}
int main() {
#ifdef _WIN32
    WSADATA sockets;
    if (WSAStartup(MAKEWORD(2, 2), &sockets) != 0) return 1;
    wire = _fdopen(_dup(_fileno(stdout)), "w");
    _dup2(_fileno(stderr), _fileno(stdout));
#else
    umask(0077);
    wire = fdopen(dup(STDOUT_FILENO), "w");
    dup2(STDERR_FILENO, STDOUT_FILENO); // Keep all library output out of the response pipe.
#endif
    if (!wire) return 1;
    std::string init;
    if (!std::getline(std::cin, init) || init.size() > 20000) return 1;
    std::istringstream s(init);
    std::string op, host, user, domain, password, path, pin;
    unsigned port = 0, width = 0, height = 0;
    if (!(s >> op >> host >> port >> user >> domain >> password >> width >> height >> path >> pin) ||
        op != "INIT" || port < 1 || port > 65535 || width < 640 || width > 3840 || height < 480 || height > 2160)
        return 1;
    try { host=unhex(host); user=unhex(user); domain=unhex(domain); password=unhex(password);
          frame_path=unhex(path); certificate_pin=unhex(pin); }
    catch (...) { return 1; }
    instance = freerdp_new();
    expected_host = host; expected_port = static_cast<UINT16>(port);
    if (!instance || !freerdp_context_new(instance)) return 1;
    auto* settings = instance->context->settings;
    bool ok = freerdp_settings_set_string(settings, FreeRDP_ServerHostname, host.c_str()) &&
              freerdp_settings_set_string(settings, FreeRDP_Username, user.c_str()) &&
              freerdp_settings_set_string(settings, FreeRDP_Domain, domain.c_str()) &&
              freerdp_settings_set_string(settings, FreeRDP_Password, password.c_str()) &&
              freerdp_settings_set_uint32(settings, FreeRDP_ServerPort, port) &&
              freerdp_settings_set_uint32(settings, FreeRDP_DesktopWidth, width) &&
              freerdp_settings_set_uint32(settings, FreeRDP_DesktopHeight, height) &&
              freerdp_settings_set_uint32(settings, FreeRDP_ColorDepth, 32) &&
              freerdp_settings_set_uint32(settings, FreeRDP_TcpConnectTimeout, 10000) &&
              freerdp_settings_set_bool(settings, FreeRDP_ExternalCertificateManagement, TRUE) &&
              freerdp_settings_set_bool(settings, FreeRDP_IgnoreCertificate, FALSE) &&
              freerdp_settings_set_bool(settings, FreeRDP_RdpSecurity, FALSE) &&
              freerdp_settings_set_bool(settings, FreeRDP_NlaSecurity, TRUE) &&
              freerdp_settings_set_bool(settings, FreeRDP_TlsSecurity, TRUE) &&
              freerdp_settings_set_bool(settings, FreeRDP_SupportGraphicsPipeline, FALSE) &&
              freerdp_settings_set_bool(settings, FreeRDP_NetworkAutoDetect, FALSE) &&
              freerdp_settings_set_bool(settings, FreeRDP_RedirectClipboard, FALSE) &&
              freerdp_settings_set_bool(settings, FreeRDP_AutoReconnectionEnabled, FALSE);
    // No disk, USB, audio, clipboard or input redirection from the controller host.
    std::fill(password.begin(), password.end(), 0);
    std::fill(init.begin(), init.end(), 0);
    instance->PostConnect = post_connect;
    instance->AuthenticateEx = authenticate;
    instance->VerifyX509Certificate = verify_certificate;
    command_event = CreateEvent(nullptr, TRUE, FALSE, nullptr);
    if (!ok || !command_event) return 1;
    std::thread reader([] {
        std::string line;
        while (std::getline(std::cin, line)) {
            if (line.size() > 20000) break;
            if (line.rfind("STOP ", 0) == 0) {
                stopped = true;
                freerdp_abort_connect_context(instance->context);
                SetEvent(command_event); return;
            }
            {
                std::lock_guard<std::mutex> lock(queue_mutex);
                if (commands.size() >= 32) break;
                commands.push_back(line); SetEvent(command_event);
            }
        }
        stopped = true;
        freerdp_abort_connect_context(instance->context);
        SetEvent(command_event);
    });
    // Reader lifetime is bounded by process lifetime; it may be blocked on stdin on disconnect.
    reader.detach();
    if (stopped || !freerdp_connect(instance)) {
        const auto code = freerdp_get_last_error(instance->context);
        const char* name = freerdp_get_last_error_name(code);
        reply("ERR 0 connect_failed_" + std::to_string(code) + " " + (name ? name : "UNKNOWN"));
        std::_Exit(2);
    }
    reply("OK 0 connected");
    while (!stopped && !freerdp_shall_disconnect_context(instance->context)) {
        HANDLE handles[MAXIMUM_WAIT_OBJECTS];
        DWORD count = freerdp_get_event_handles(instance->context, handles, MAXIMUM_WAIT_OBJECTS-1);
        if (!count) break;
        handles[count++] = command_event;
        DWORD result = WaitForMultipleObjects(count, handles, FALSE, 100);
        if (result == WAIT_FAILED || stopped) break;
        if (!freerdp_check_event_handles(instance->context)) break;
        std::deque<std::string> batch;
        { std::lock_guard<std::mutex> lock(queue_mutex); batch.swap(commands); ResetEvent(command_event); }
        for (const auto& line : batch) { if (stopped) break; execute(line); }
    }
    if (!stopped) {
        const auto code = freerdp_get_last_error(instance->context);
        const char* name = freerdp_get_last_error_name(code);
        reply("ERR 0 disconnected_" + std::to_string(code) + " " + (name ? name : "UNKNOWN"));
    }
    stopped = true;
    release_all();
    freerdp_disconnect(instance);
    reply("CLOSED 0 stopped_or_disconnected");
    // Exit without racing a detached reader against a destroyed context.
    std::_Exit(0);
}
