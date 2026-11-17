# 更新记录

## v0.1.0 — 2026-11-17（预览版）

- 新增 Windows x64 发行包，包含 C++ FreeRDP 后端、运行 DLL、OpenSSL provider、第三方许可证和文件摘要。使用者安装 Python 与 venv 依赖即可运行。
- 适配 Windows 用户专属 ACL、文件锁、Unicode 路径、截图原子替换及 UTF-8 子进程通信。
- 修复 Windows 低精度休眠导致长文本输入超过命令上限的问题，使用高精度定时器保持输入节奏。
- 终端、观察页与 MCP 提供中文错误原因及排查建议，区分网络、证书、登录限制和断线原因；保留原始错误码及最初的超时原因。
- 保留全屏、差分、裁剪与有界等待截图，以及人类观察页的锁定停止行为。
- Mac、Windows 和最终 Windows ZIP 的独立环境均通过 41 项测试；真实 Windows MCP/RDP 验证包括中文、emoji、点击、持续动画截图及停止后拒绝输入。完整范围见 [VALIDATION.md](VALIDATION.md)。

本版本未完成长期可靠性、故障恢复、复杂输入法及硬件 KVM 对照验收。
