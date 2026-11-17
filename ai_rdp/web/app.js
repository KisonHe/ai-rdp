"use strict";
let token = location.hash.slice(1) || sessionStorage.getItem("ai-rdp-watch");
if (location.hash) { sessionStorage.setItem("ai-rdp-watch", token); history.replaceState(null, "", "/watch"); }
const $ = id => document.getElementById(id);
let headers = {Authorization: `Bearer ${token}`};
let objectURL = null, stopping = false;
addEventListener("hashchange", () => {
  if (!location.hash) return;
  token = location.hash.slice(1);
  sessionStorage.setItem("ai-rdp-watch", token);
  headers = {Authorization: `Bearer ${token}`};
  history.replaceState(null, "", "/watch");
  stopping = false;
});
async function refresh() {
  try {
    const response = await fetch("/v1/status", {headers});
    if (!response.ok) throw new Error("观察链接无效或服务已重新启动，请使用新的观察链接。");
    const status = await response.json();
    const labels = {connecting:"正在连接", connected:"AI 可操作", failed:"连接失败", stopped:"已停止"};
    $("state").textContent = (status.demo ? "模拟测试 · " : "") + (labels[status.state] || status.state);
    $("curtain").hidden = !status.stopped;
    $("stop").disabled = status.stopped;
    $("stop").textContent = status.stopped ? "已停止" : "立即停止 AI 操作";
    $("detail").textContent = status.stopped ? "显示停止前的最后画面" : status.desktop_size ? `画面更新于 ${Math.round(status.frame_age_ms / 1000)} 秒前` : "等待桌面画面";
    $("error").textContent = status.error || "";
    $("last").textContent = status.last_action ? `最近操作：${status.last_action.summary}` : "尚无操作";
    $("size").textContent = status.desktop_size ? status.desktop_size.join(" × ") : "";
    if (status.desktop_size && !stopping) {
      const image = await fetch("/v1/frame.png", {headers});
      if (image.ok) {
        const next = URL.createObjectURL(await image.blob());
        const previous = objectURL; objectURL = next;
        $("desktop").src = next; $("desktop").style.display = "block"; $("empty").hidden = true;
        if (previous) URL.revokeObjectURL(previous);
      }
    }
  } catch (error) {
    $("error").textContent = error.message;
    $("state").textContent = "观察连接中断";
    $("detail").textContent = "画面可能已过期，请检查本机服务";
  }
  setTimeout(refresh, 750);
}
$("stop").addEventListener("click", async () => {
  stopping = true; $("stop").textContent = "正在停止…";
  try {
    const response = await fetch("/v1/stop", {method:"POST", headers});
    if (!response.ok) throw new Error("未能确认停止，请在本机终端按 Ctrl+C 结束服务。");
    $("curtain").hidden = false; $("stop").disabled = true; $("stop").textContent = "已停止";
  } catch (error) { $("error").textContent = error.message; stopping = false; }
});
refresh();
