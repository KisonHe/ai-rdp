"""Safe, actionable diagnostics for the pinned FreeRDP connection errors.

Numeric codes come from FreeRDP 3.32.2 include/freerdp/error.h. A general
connection failure does not identify whether the host, service or firewall failed.
Only explicit certificate callbacks / socket log facts refine that diagnosis.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorSpec:
    symbol: str
    title: str
    message: str
    hint: str
    stage: str


def spec(symbol, title, message, hint, stage):
    return ErrorSpec(symbol, title, message, hint, stage)


CONNECT = {
    1: spec("PRE_CONNECT_FAILED", "连接初始化失败", "本机连接配置未能初始化。", "检查连接配置和 native.log；必要时重新运行构建脚本。", "configuration"),
    2: spec("CONNECT_UNDEFINED", "连接失败，原因未明确", "连接组件未提供更具体的失败原因。", "查看本机 native.log 中本次连接的记录。", "unknown"),
    3: spec("POST_CONNECT_FAILED", "桌面初始化失败", "连接已进入桌面初始化阶段，但本机组件未能完成初始化。", "查看 native.log，检查桌面尺寸和本机原生组件。", "desktop"),
    4: spec("DNS_ERROR", "无法解析电脑地址", "主机名未能解析为网络地址。", "检查主机名和 DNS；也可使用目标电脑的正确 IP 地址。", "network"),
    5: spec("DNS_NAME_NOT_FOUND", "找不到这台电脑的主机名", "DNS 没有找到配置中的主机名。", "核对地址拼写，或改用可达的 IP 地址。", "network"),
    6: spec("CONNECT_FAILED", "无法连接到远程桌面端口", "网络连接未能建立，尚未进入账号验证阶段。仅凭此错误不能判断密码是否正确。", "检查电脑或 VM 是否开机、地址和端口是否正确、Windows 远程桌面是否开启，以及防火墙和路由是否允许连接。", "network"),
    7: spec("MCS_CONNECT_INITIAL_ERROR", "远程桌面会话协商失败", "RDP 初始会话协商未能完成。", "确认端口提供的是 RDP 服务，并查看远端远程桌面服务状态与 native.log。", "session"),
    8: spec("TLS_CONNECT_FAILED", "无法建立加密连接", "TLS 握手失败；当前信息尚不足以确定是证书、协议还是端口配置问题。", "核对 RDP 地址、端口及证书指纹，并查看 native.log 中的握手记录。", "security"),
    9: spec("AUTHENTICATION_FAILED", "Windows 登录验证未通过", "远端拒绝了登录验证，可能与用户名、密码、域或账户策略有关，不能仅凭此码断定密码错误。", "核对 Windows 用户名、账户密码及域，确认该账户允许远程登录。", "authentication"),
    10: spec("INSUFFICIENT_PRIVILEGES", "账户权限不足", "远端报告此账户没有建立会话所需的权限。", "检查该用户的远程桌面登录授权和 Windows 策略。", "authentication"),
    11: spec("CONNECT_CANCELLED", "连接已取消", "连接过程已被取消。", "如需继续，请由用户决定后重新启动服务。", "session"),
    12: spec("SECURITY_NEGO_CONNECT_FAILED", "远程桌面安全协商失败", "客户端与远端未能完成 RDP 安全协议协商。", "确认目标为 RDP 服务，并核对远端 TLS/NLA 配置与 native.log。", "security"),
    13: spec("CONNECT_TRANSPORT_FAILED", "连接在传输阶段中断", "RDP 传输连接未能建立或已被中断。此码不代表密码错误。", "检查网络、远端 RDP 服务和 native.log 中的后续原因。", "transport"),
    14: spec("PASSWORD_EXPIRED", "Windows 密码已过期", "远端报告该账户的密码已经过期。", "请在 Windows 的正常登录入口完成密码更新后再连接。", "authentication"),
    15: spec("PASSWORD_CERTAINLY_EXPIRED", "Windows 密码已过期", "远端报告该账户必须更新密码。", "请在 Windows 的正常登录入口完成密码更新后再连接。", "authentication"),
    16: spec("CLIENT_REVOKED", "远端撤销了登录资格", "远端拒绝了当前客户端或账户的登录资格。", "请管理员检查账户状态和远程登录策略。", "authentication"),
    17: spec("KDC_UNREACHABLE", "无法访问域认证服务", "登录所需的域认证服务不可达。", "检查域、DNS 和域网络连通性。", "authentication"),
    18: spec("ACCOUNT_DISABLED", "Windows 账户已停用", "远端报告该账户处于停用状态。", "请管理员核对账户并按需要恢复其登录资格。", "authentication"),
    19: spec("PASSWORD_MUST_CHANGE", "Windows 要求先更改密码", "远端要求该账户先完成密码变更。", "请通过 Windows 的正常登录入口完成变更后再连接。", "authentication"),
    20: spec("LOGON_FAILURE", "Windows 登录失败", "远端没有接受本次登录，未提供更具体的凭据原因。", "核对用户名、域、账户密码及登录策略。", "authentication"),
    21: spec("WRONG_PASSWORD", "Windows 报告密码错误", "远端明确报告提交的密码不正确。", "核对用户名和域，并在本机隐藏提示中重新输入该账户的密码。", "authentication"),
    22: spec("ACCESS_DENIED", "Windows 拒绝访问", "远端拒绝了当前账户的访问请求。", "检查账户状态、远程桌面权限及相关策略。", "authentication"),
    23: spec("ACCOUNT_RESTRICTION", "Windows 账户受到登录限制", "账户策略不允许当前这次登录。", "请管理员检查账户的登录时段、来源及远程登录限制。", "authentication"),
    24: spec("ACCOUNT_LOCKED_OUT", "Windows 账户已锁定", "远端报告该账户已被锁定。", "先停止重复尝试，请管理员解锁或等待账户策略规定的解锁时间。", "authentication"),
    25: spec("ACCOUNT_EXPIRED", "Windows 账户已过期", "远端报告账户的有效期已经结束。", "请管理员核对账户有效期。", "authentication"),
    26: spec("LOGON_TYPE_NOT_GRANTED", "账户没有远程登录权限", "远端报告该账户未被授予此登录类型。", "请管理员检查允许或拒绝通过远程桌面服务登录的策略。", "authentication"),
    27: spec("NO_OR_MISSING_CREDENTIALS", "缺少登录凭据", "连接所需的登录凭据不完整。", "检查用户名和域，并从本机隐藏提示输入账户密码。", "authentication"),
    28: spec("ACTIVATION_TIMEOUT", "等待远程桌面就绪超时", "RDP 会话未能在限定时间内完成激活。", "检查远端系统负载和远程桌面服务状态，再由用户决定是否重试。", "desktop"),
    29: spec("TARGET_BOOTING", "远程电脑正在启动", "远端报告目标系统尚未准备好。", "等待系统完成启动后，再由用户重新发起连接。", "session"),
    30: spec("HYBRID_REQUIRED_BY_SERVER", "远端要求 NLA 身份验证", "远端没有接受当前连接的安全协商方式。", "检查用户名、域和客户端构建，保留远端要求的 NLA 保护。", "security"),
}

SESSION = {
    1: ("RPC_INITIATED_DISCONNECT", "远端主动断开了会话"),
    2: ("RPC_INITIATED_LOGOFF", "远端注销了会话"),
    3: ("IDLE_TIMEOUT", "远端因空闲超时断开了会话"),
    4: ("LOGON_TIMEOUT", "远端等待登录超时"),
    5: ("DISCONNECTED_BY_OTHER_CONNECTION", "另一个连接接管了远程会话"),
    6: ("OUT_OF_MEMORY", "远端报告内存不足"),
    7: ("SERVER_DENIED_CONNECTION", "远端拒绝了会话连接"),
    9: ("SERVER_INSUFFICIENT_PRIVILEGES", "远端报告会话权限不足"),
    11: ("RPC_INITIATED_DISCONNECT_BY_USER", "远端用户断开了会话"),
    12: ("LOGOFF_BY_USER", "远端用户注销了会话"),
}

LOCAL = {
    "connect_timeout": ("连接等待超时", "连接组件在 25 秒内未完成响应。", "检查目标电脑和网络，以及 native.log；本次连接已停止。"),
    "command_timeout": ("远程操作响应超时", "连接组件未能在 10 秒内完成操作，本次连接已停止。", "先检查远端画面与操作结果，避免直接重复提交；查看 native.log。"),
    "transport_closed": ("RDP 连接进程已退出", "无法继续与本机 RDP 连接组件通信，底层未提供更具体原因。", "查看本机 native.log，检查连接是否被远端结束或原生组件是否异常退出。"),
    "native_missing": ("未找到 RDP 连接组件", "本机平台的原生后端不存在，或程序路径已经失效。", "使用包含对应平台 bin 目录的发布包；开发者可按 README 构建。"),
    "native_start_failed": ("无法启动 RDP 连接组件", "本机无法启动原生后端进程。", "检查可执行文件权限、依赖库及 native.log。"),
    "native_not_running": ("RDP 连接组件未运行", "当前没有可以接收操作的连接进程。", "查看连接状态和 native.log；如需恢复，请由用户重新启动服务。"),
    "stopped": ("本次会话已停止", "停止状态已锁定，不再接受键鼠操作。", "等待用户决定后，在本机重新启动服务。"),
    "capture_failed": ("无法读取远程桌面画面", "原生组件没有成功生成桌面帧。", "检查远端会话和本机状态目录，查看 native.log。"),
    "input_failed": ("键鼠输入发送失败", "连接组件未能确认输入事件已发送；部分先前事件可能已生效。", "先重新观察目标应用，避免直接重复操作；检查连接和 native.log。"),
}


class RdpError(RuntimeError):
    def __init__(self, title, message, hint, *, reason, stage="unknown", code=None,
                 symbol=None, target=None, elapsed=None, expected_certificate=None,
                 observed_certificate=None):
        self.detail = {"title": title, "message": message, "hint": hint,
                       "reason": reason, "stage": stage}
        if target: self.detail["target"] = target
        if code is not None:
            self.detail.update(code=code, code_hex=f"0x{code:08X}")
        if symbol: self.detail["symbol"] = symbol
        if elapsed is not None: self.detail["elapsed_seconds"] = round(elapsed, 1)
        for key, value in (("expected_certificate", expected_certificate),
                           ("observed_certificate", observed_certificate)):
            if value and re.fullmatch(r"[a-fA-F0-9]{64}", value): self.detail[key] = value
        text = f"{title}：{message}"
        if target: text += f"\n目标：{target}"
        if elapsed is not None: text += f"\n本次连接已等待约 {elapsed:.1f} 秒。"
        text += f"\n建议：{hint}"
        diagnostic = symbol or reason
        if code is not None: diagnostic += f" / 0x{code:08X} / {code}"
        super().__init__(text + f"\n诊断：{diagnostic}")


def network_fact(log_tail):
    """Return only a known fact, never surface arbitrary log contents."""
    facts = (("connection refused", "refused"), ("no route to host", "unreachable"),
             ("network is unreachable", "unreachable"), ("connection timed out", "timeout"),
             ("operation timed out", "timeout"))
    for line in reversed(log_tail.splitlines()):
        lower = line.lower()
        if "failed with error:" not in lower: continue
        for phrase, reason in facts:
            if phrase in lower: return reason
    return None


def native_error(token, *, target=None, elapsed=None, certificate_reason=None,
                 observed_certificate=None, expected_certificate=None, network_reason=None):
    match = re.fullmatch(r"(connect_failed|disconnected)_(\d{1,10})", token)
    if match and int(match[2]) <= 0xFFFFFFFF:
        code = int(match[2])
        entry = CONNECT.get(code & 0xFFFF) if code >> 16 == 2 else None
        if entry:
            title, message, hint, stage = entry.title, entry.message, entry.hint, entry.stage
            reason = entry.symbol.lower()
            symbol = "ERRCONNECT_" + entry.symbol
        elif code >> 16 == 1 and (code & 0xFFFF) in SESSION:
            name, title = SESSION[code & 0xFFFF]
            message, hint, stage = "远端提供了上述会话结束原因。", "检查 Windows 会话与远端策略；如需继续，请由用户重新连接。", "session"
            reason, symbol = name.lower(), "ERRINFO_" + name
        else:
            title = "远程桌面连接已断开" if match[1] == "disconnected" else "远程桌面连接失败"
            message, hint, stage = "底层没有提供已知的具体原因。", "查看本机 native.log 中本次连接的记录。", "unknown"
            reason, symbol = "unknown_rdp_error", None
        if code == 0x20006 and network_reason:
            if network_reason == "refused":
                title, reason = "目标电脑拒绝了端口连接", "connection_refused"
                message = "底层明确报告连接被拒绝，尚未进入账号验证阶段。"
                hint = "检查目标端口是否有 RDP 服务监听，以及 Windows 远程桌面和防火墙的配置。"
            elif network_reason == "unreachable":
                title, reason = "无法到达目标电脑的网络", "network_unreachable"
                message = "底层明确报告网络或主机不可达。"
                hint = "检查目标地址、网段、路由和所需的 VPN 或网络连接。"
            elif network_reason == "timeout":
                title, reason = "连接远程桌面端口超时", "network_timeout"
                message = "底层明确报告端口连接超时，尚未进入账号验证阶段。"
        if code == 0x20008 and certificate_reason:
            stage, reason = "security", certificate_reason
            if certificate_reason == "certificate_mismatch":
                title = "远程电脑的证书指纹不匹配"
                message = "远端证书与连接配置中保存的指纹不同，已停止登录。"
                hint = "使用 probe 查看当前证书，并通过可信的连接详情核对；确认目标身份后再更新配置。"
            elif certificate_reason == "certificate_untrusted":
                title, message = "尚未确认远程电脑的证书", "没有提供与远端匹配的受信任证书指纹，已停止登录。"
                hint = "使用 probe 获取指纹，核对后填入连接配置的 certificate 字段。"
            elif certificate_reason in {"unexpected_target", "unsupported_redirect"}:
                title, message = "连接目标与配置不一致", "连接出现了不同主机、端口或不受支持的服务器重定向，已停止登录。"
                hint = "核对连接地址与端口；本工具只连接明确配置的目标。"
            elif certificate_reason == "certificate_invalid":
                title, message = "无法读取远端证书", "未能从远端证书提取有效指纹，已停止登录。"
        return RdpError(title, message, hint, reason=reason, stage=stage, code=code,
                        symbol=symbol, target=target, elapsed=elapsed,
                        expected_certificate=expected_certificate if certificate_reason else None,
                        observed_certificate=observed_certificate if certificate_reason else None)
    title, message, hint = LOCAL.get(token, ("RDP 连接组件报告错误", "没有可识别的具体原因。", "查看本机 native.log。"))
    return RdpError(title, message, hint, reason=token if token in LOCAL else "unknown_native_error", target=target)
