// T-008 榜单页交互：行详情默认全部展开（D1/D3 v1.1），单击只折叠/再展开本行，各行独立。
// T-009 关注星标接线（决策 9 动态入池）：点击 → fetch 关注/取消 JSON API → toast＋星标态即时更新；
// 乐观切换星标态（流程说明 §0：<100ms 可见反馈），失败回滚原态并报错 toast。
// T-015（v1.3 关注独立页 P6）：关注/取消后顶栏计数徽标以服务端 follow_count 局部更新（不再往页面插卡）；
// P6 页内点 ★ 取消 → 该行＋详情面板即时移除，组空连组块移除，全空 reload 出空态。
// T-010 标签接线（§3.3）：chip 本体跳 /tags/<tag>（SSR 链接）；× 删除 → DELETE API 乐观移除失败插回；
// "＋ 标签"→ 输入框（maxLength 20，Enter 提交/Escape·blur 还原）；非法输入不提交：红边＋内联提示。
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

// 顶栏计数徽标局部更新（流程说明 §3.2：+1/-1 无需刷新）：直接采用服务端 follow_count 真值——
// 幂等响应（already_followed=true / removed=false）计数自然不变，前端不必判分支
function updateFollowCount(data) {
  if (typeof data.follow_count !== "number") return;
  const badge = document.getElementById("nav-follow-count");
  if (badge) badge.textContent = String(data.follow_count);
  const total = document.getElementById("follow-total"); // P6 元信息行"关注 N 个仓库"，同口径联动
  if (total) total.textContent = String(data.follow_count);
}

// P6 页内点 ★ 取消关注（流程说明 §3.2）：该行＋详情面板即时从 DOM 移除；组空连组块移除；全空 reload 出空态
function removeFollowRow(star) {
  const row = star.closest(".row");
  if (!row || !row.closest("#follow-boards")) return; // 非 P6 页面：行保留，星标已切换即可
  const panel = document.querySelector(`.panel[data-b="${row.dataset.b}"][data-i="${row.dataset.i}"]`);
  const board = row.closest(".board");
  if (panel) panel.remove();
  row.remove();
  if (board && !board.querySelector(".row")) {
    board.remove();
    if (!document.querySelector("#follow-boards .board")) location.reload(); // 全空 → 空态（任务书允许 reload 简化）
  }
}

async function toggleFollow(star) {
  const repo = star.dataset.repo;
  const on = !star.classList.contains("on");
  setAllStars(repo, on); // 乐观切换；失败整体回滚
  try {
    let resp;
    if (on) {
      resp = await fetch("/api/follows", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ full_name: repo }),
      });
    } else {
      resp = await fetch("/api/follows/" + repo.split("/").map(encodeURIComponent).join("/"), { method: "DELETE" });
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || "HTTP " + resp.status);
    toast(on ? "已关注（未入池仓库将即时抓基线，下周起有增量）" : "已取消关注（跟踪保留）");
    updateFollowCount(data);
    if (!on) removeFollowRow(star); // 行移除放在成功之后：失败时行仍在、星标回滚原态
  } catch (err) {
    setAllStars(repo, !on); // 失败回滚原态（流程说明 §3.2）；计数只在成功后动过，无需回滚
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
  // T-010 删除标签：× 命中区（panel 内，不在 .row 上，不影响行折叠）
  const tagX = e.target.closest(".tag b");
  if (tagX) {
    deleteTag(tagX);
    return;
  }
  // T-010 打标入口："＋ 标签"按钮 → 输入框
  const tagAdd = e.target.closest(".tag-add");
  if (tagAdd) {
    startTagInput(tagAdd);
    return;
  }
  const row = e.target.closest(".row");
  if (row) toggleRow(row);
});

// ---- T-010 标签增删交互（流程说明 §3.3；<100ms 可见反馈，失败回滚原态） ----

// chip 链接的 path 段编码：与 SSR 侧 quote safe='' 同语义（空格/%2F 等一律百分号化，FastAPI :path 解码还原）
function tagHref(tag) {
  return "/tags/" + encodeURIComponent(tag);
}

// 删除标签：乐观移除 chip（<100ms 反馈）→ DELETE → 成功 toast；失败插回原位＋报错 toast
async function deleteTag(x) {
  const chip = x.closest(".tag");
  const repo = x.dataset.repo;
  const tag = x.dataset.tag;
  if (!chip || !repo || !tag) return;
  const host = chip.parentElement;
  const anchor = chip.nextSibling; // 回滚插回锚点：原位置的后一个节点（可能为 null → 容器末尾）
  chip.remove();
  try {
    const resp = await fetch(
      "/api/tags/" + repo.split("/").map(encodeURIComponent).join("/") + "?tag=" + encodeURIComponent(tag),
      { method: "DELETE" },
    );
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || "HTTP " + resp.status);
    toast("已删除标签");
  } catch (err) {
    if (anchor && anchor.parentElement) host.insertBefore(chip, anchor);
    else host.appendChild(chip);
    toast("删除标签失败：" + err.message, true);
  }
}

// 打标输入：按钮 → 输入框（maxLength 20）；Enter 提交（非法不提交：红边＋内联提示）；
// Escape/blur 还原按钮；提交中（submitting）忽略 Escape/blur，防止 chip 无处可插
function startTagInput(btn) {
  const host = btn.parentElement;
  const input = document.createElement("input");
  input.className = "tag-input";
  input.maxLength = 20;
  input.placeholder = "1~20 字符，回车确认";
  input.setAttribute("aria-label", "新标签名称");
  const hint = document.createElement("span");
  hint.className = "tag-hint";
  hint.hidden = true;
  btn.replaceWith(input);
  input.insertAdjacentElement("afterend", hint);
  input.focus();
  let submitting = false;

  const restore = () => {
    if (!input.isConnected) return; // 已被 chip 替换（提交成功）→ 无操作
    input.replaceWith(btn);
    hint.remove();
  };
  const fail = (msg) => {
    restore();
    toast(msg, true);
  };
  // 非法态清除：再次输入即恢复正常样式（流程说明 §4：红边＋内联提示是瞬时诊断，不是常驻状态）
  input.addEventListener("input", () => {
    if (input.classList.contains("invalid")) {
      input.classList.remove("invalid");
      hint.hidden = true;
    }
  });
  input.addEventListener("keydown", async (ev) => {
    if (ev.key === "Escape") {
      if (!submitting) restore();
      return;
    }
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    if (submitting) return;
    const tag = input.value.trim();
    if (!tag || tag.length > 20) {
      // 非法输入：不提交（流程说明 §4），红边＋内联提示
      input.classList.add("invalid");
      hint.textContent = tag.length > 20 ? "标签最长 20 字符" : "标签不能为空";
      hint.hidden = false;
      return;
    }
    submitting = true;
    input.disabled = true;
    try {
      const resp = await fetch("/api/tags", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ full_name: btn.dataset.repo, tag }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || "HTTP " + resp.status);
      if (data.added) {
        // 新 chip 出现（带 ×）＋ toast（§3.3 第 1 步）；textContent 插入防 XSS
        const chip = document.createElement("span");
        chip.className = "tag";
        const link = document.createElement("a");
        link.className = "tag-link";
        link.href = tagHref(tag);
        link.textContent = tag;
        const x = document.createElement("b");
        x.title = "删除标签";
        x.dataset.repo = btn.dataset.repo;
        x.dataset.tag = tag;
        x.textContent = "×";
        chip.append(link, x);
        input.replaceWith(chip);
        hint.remove();
        chip.insertAdjacentElement("afterend", btn); // 按钮接回原位：同一行可连续打多枚标签（评审发现漏插）
        toast("已添加标签");
      } else {
        restore(); // 重名幂等：不重复写入，toast 提示（§3.3：同仓同名 → "标签已存在"）
        toast("标签已存在");
      }
    } catch (err) {
      fail("添加标签失败：" + err.message); // 失败回滚原态（输入框还原为按钮）＋报错 toast
    }
  });
  input.addEventListener("blur", () => {
    if (!submitting) restore();
  });
}

// 键盘操作：Enter / Space 与点击行等价（行带 tabindex=0 / role=button）；
// 星标是真 button，键盘 Enter/Space 原生触发 click，走上面的关注逻辑
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.classList && e.target.classList.contains("row")) {
    e.preventDefault();
    toggleRow(e.target);
  }
});
