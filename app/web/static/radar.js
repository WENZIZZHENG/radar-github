// T-008 榜单页交互：行详情默认全部展开（D1/D3 v1.1），单击只折叠/再展开本行，各行独立。
// T-009 关注星标接线（决策 9 动态入池）：点击 → fetch 关注/取消 JSON API → toast＋星标态＋关注区即时更新；
// 乐观切换星标态（流程说明 §0：<100ms 可见反馈），失败回滚原态并报错 toast。标签仍只读（T-010 接线）。
const STAR_O = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.6l2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.6 9.7l5.8-.8z"/></svg>';
const STAR_F = '<svg viewBox="0 0 24 24" width="17" height="17" fill="currentColor" aria-hidden="true"><path d="M12 3.6l2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.6 9.7l5.8-.8z"/></svg>';

function toggleRow(row) {
  const panel = document.querySelector(`.panel[data-b="${row.dataset.b}"][data-i="${row.dataset.i}"]`);
  if (!panel) return;
  const open = row.classList.toggle("expanded");
  row.setAttribute("aria-expanded", String(open));
  panel.classList.toggle("open", open);
}

function toast(msg, isErr) {
  const t = document.createElement("div");
  t.className = isErr ? "toast err" : "toast";
  t.textContent = msg;
  document.getElementById("toasts").appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

function setStar(star, on) {
  star.classList.toggle("on", on);
  star.innerHTML = on ? STAR_F : STAR_O;
  star.title = on ? "取消关注" : "关注";
  star.setAttribute("aria-label", star.title);
}

// 同一仓库可跨榜重复出现（口径允许）：一次关注/取消，页面上它的全部星标一起切
function setAllStars(repo, on) {
  document.querySelectorAll('.star[data-repo="' + repo + '"]').forEach((s) => setStar(s, on));
}

function updateFollowSection(repo, on, data) {
  const title = document.getElementById("follows-title");
  if (title && typeof data.follow_count === "number") title.textContent = "我的关注（" + data.follow_count + "）";
  const row = document.getElementById("follow-row");
  if (!row) return; // 季度/总星页无关注区：星标已切换，回到周报页自然呈现
  const empty = document.getElementById("follows-empty");
  if (on) {
    if (data.card_html && !row.querySelector('.fcard[data-repo="' + repo + '"]')) {
      row.insertAdjacentHTML("beforeend", data.card_html); // 服务端渲染新卡，卡面与 SSR 同模板
    }
    row.hidden = false;
    if (empty) empty.hidden = true;
  } else {
    const card = row.querySelector('.fcard[data-repo="' + repo + '"]');
    if (card) card.remove();
    if (!row.children.length) {
      row.hidden = true;
      if (empty) empty.hidden = false;
    }
  }
}

async function toggleFollow(star) {
  const repo = star.dataset.repo;
  const on = !star.classList.contains("on");
  setAllStars(repo, on); // 乐观切换；失败整体回滚
  const sec = document.getElementById("follows-sec");
  try {
    let resp;
    if (on) {
      resp = await fetch("/api/follows", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ full_name: repo, as_of: sec ? sec.dataset.asOf : null }),
      });
    } else {
      resp = await fetch("/api/follows/" + repo.split("/").map(encodeURIComponent).join("/"), { method: "DELETE" });
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || "HTTP " + resp.status);
    toast(on ? "已关注（未入池仓库将即时抓基线，下周起有增量）" : "已取消关注（跟踪保留）");
    updateFollowSection(repo, on, data);
  } catch (err) {
    setAllStars(repo, !on); // 失败回滚原态（流程说明 §3.2）
    toast((on ? "关注失败：" : "取消关注失败：") + err.message, true);
  }
}

document.addEventListener("click", (e) => {
  // 星标命中区优先于行：点星标不触发行折叠
  const star = e.target.closest(".star");
  if (star) {
    toggleFollow(star);
    return;
  }
  const row = e.target.closest(".row");
  if (row) toggleRow(row);
});

// 键盘操作：Enter / Space 与点击行等价（行带 tabindex=0 / role=button）；
// 星标是真 button，键盘 Enter/Space 原生触发 click，走上面的关注逻辑
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.classList && e.target.classList.contains("row")) {
    e.preventDefault();
    toggleRow(e.target);
  }
});
