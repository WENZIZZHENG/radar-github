// T-008 榜单页交互：行详情默认全部展开（D1/D3 v1.1），单击只折叠/再展开本行，各行独立。
// T-009 关注星标接线（决策 9 动态入池）：点击 → fetch 关注/取消 JSON API → toast＋星标态即时更新；
// 乐观切换星标态（流程说明 §0：<100ms 可见反馈），失败回滚原态并报错 toast。
// T-015（v1.3 关注独立页 P6）：关注/取消后顶栏计数徽标以服务端 follow_count 局部更新（不再往页面插卡）；
// P6 页内点 ★ 取消 → 该行＋详情面板即时移除，组空连组块移除，全空 reload 出空态。
// T-010 标签接线（§3.3）：chip 本体跳 /tags/<tag>（SSR 链接）；× 删除 → DELETE API 乐观移除失败插回；
// "＋ 标签"→ 输入框（maxLength 20，Enter 提交/Escape·blur 还原）；非法输入不提交：红边＋内联提示。
// T-016 翻译接线（§7）：行内按钮单个强制重译（成功 toast＋面板中文即时替换不刷新）；顶栏批量补译全部缺失
// （只补 NULL，后台任务：POST 202 → 2s 轮询 /status 进度）；按钮置灰防连点，分支 toast 文案严格按 v1.5 §7.3
// （服务端 400 detail 即 §7.3 文案，直用）。
// T-017 推荐语接线（§8）：行内按钮单个强制重生（按当前页维度，成功 toast＋面板推荐语块即时替换不刷新）；
// 顶栏批量补齐推荐语（只补范围内缺失，独立后台任务不共用翻译的：POST 202 → 2s 轮询 /recommend-missing/status）；
// 分支 toast 文案严格按 §8.3 文案表。推荐语生成中按钮置灰"生成中…"防连点。
const STAR_O = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.6l2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.6 9.7l5.8-.8z"/></svg>';
const STAR_F = '<svg viewBox="0 0 24 24" width="17" height="17" fill="currentColor" aria-hidden="true"><path d="M12 3.6l2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.6 9.7l5.8-.8z"/></svg>';

function toggleRow(row) {
  const panel = document.querySelector(`.panel[data-b="${row.dataset.b}"][data-i="${row.dataset.i}"]`);
  if (!panel) return;
  const open = row.classList.toggle("expanded");
  row.setAttribute("aria-expanded", String(open));
  panel.classList.toggle("open", open);
}

function toast(msg, isErr, opts) {
  const t = document.createElement("div");
  t.className = isErr ? "toast err" : "toast";
  t.textContent = msg;
  // T-023 可选动作按钮（如删除标签的"撤销"）：小按钮，点击立即禁用防连点（防重入）；
  // 既有全部调用点签名兼容（不带 opts 即纯文本 toast，行为不变）
  if (opts && opts.action) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "toast-action";
    btn.textContent = opts.action.label;
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      btn.disabled = true;
      opts.action.onClick();
    });
    t.appendChild(btn);
  }
  document.getElementById("toasts").appendChild(t);
  setTimeout(() => t.remove(), (opts && opts.duration) || 4000); // 普通 4s；撤销类 6s 由调用方传
  return t; // T-023 调用方（deleteTag 撤销成功）需移除原 toast
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

// P6 页内点 ★ 取消关注（流程说明 §3.2）：该行＋详情面板即时从 DOM 移除；组空连组块移除；全空 reload 出空态。
// T-021 §12.2：P6 带筛选行时，移除后按当前筛选重算可见性/组头计数/空态与 chips 计数（与筛选逻辑兼容）
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
  if (document.querySelector(".tag-filter")) {
    // 筛选行存在（P6 有关注分组）：行移除后按当前筛选重算；无筛选时顺带把组头计数纠正为剩余数
    applyFollowTagFilter();
    refreshFollowChipCounts();
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
  // T-016 行内翻译按钮（§7.2）：单个强制重译，成功局部替换不刷新页面
  const translateBtn = e.target.closest(".translate-btn");
  if (translateBtn) {
    translateRow(translateBtn);
    return;
  }
  // T-016 顶栏批量补译（§7.2）：全池只补 NULL，后台任务＋进度轮询；409/auth 文案见 §7.3
  const translateAll = e.target.closest("#translate-all");
  if (translateAll) {
    translateMissing(translateAll);
    return;
  }
  // T-017 行内推荐语按钮（§8.2）：单个强制重生（按当前页维度），成功局部替换不刷新页面
  const recommendBtn = e.target.closest(".recommend-btn");
  if (recommendBtn) {
    recommendRow(recommendBtn);
    return;
  }
  // T-017 顶栏批量补齐推荐语（§8.2）：只补范围内缺失，独立后台任务＋进度轮询；409/auth 文案见 §8.3
  const recommendAll = e.target.closest("#recommend-all");
  if (recommendAll) {
    recommendMissing(recommendAll);
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

// 删除标签：乐观移除 chip（<100ms 反馈）→ DELETE → 成功 toast 带"撤销"（T-023：该 toast 存续 6s，普通 4s 不动）；
// 点撤销 → POST /api/tags 重新添加（幂等，同打标调用）→ 成功 chip 插回原位（删除前 nextSibling 锚点）＋toast("已恢复标签")；
// 失败 → chip 不回插＋报错 toast（与删除失败插回路径不共享状态、互不干扰）；超时未点 → toast 消失不动作。
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
    const undoToast = toast("已删除标签", false, {
      duration: 6000, // T-023：撤销 toast 存续 6 秒（普通 toast 4s 不动）
      action: {
        label: "撤销", // 文案钉死
        onClick: async () => {
          try {
            const addResp = await fetch("/api/tags", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ full_name: repo, tag }),
            });
            const addData = await addResp.json().catch(() => ({}));
            if (!addResp.ok) throw new Error(addData.detail || "HTTP " + addResp.status);
            undoToast.remove(); // 撤销成功：撤掉"已删除标签"toast，避免与"已恢复标签"并排矛盾（已移除节点 remove 是 no-op）
            if (anchor && anchor.parentElement) host.insertBefore(chip, anchor);
            else host.appendChild(chip);
            toast("已恢复标签");
          } catch (err) {
            toast("恢复标签失败：" + err.message, true); // 失败不回插（与删除失败插回路径互不干扰）
          }
        },
      },
    });
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
  // T-022 打标输入建议（datalist）：list 指向页内唯一 datalist（既有标签全量注入，base.html 渲染）；
  // 空库页无 datalist → 安静跳过（浏览器对缺失 datalist 的 list 属性本就忽略，显式守卫语义清楚）
  if (document.getElementById("all-tags")) input.setAttribute("list", "all-tags");
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

// ---- T-016 翻译交互（§7.2 主路径 / §7.3 分支文案钉死，前端不发明文案） ----

// 面板中文描述即时局部替换（不刷新页面）：有则覆写，无则插到英文简介之后；textContent 防 XSS（同 T-010 chip）
function replaceDescZh(panel, zh) {
  if (!panel) return;
  let el = panel.querySelector(".desc-zh");
  if (el) {
    el.textContent = zh;
    return;
  }
  el = document.createElement("div");
  el.className = "desc-zh";
  el.textContent = zh;
  const descEn = panel.querySelector(".desc-en");
  if (descEn) descEn.insertAdjacentElement("afterend", el);
  else panel.insertBefore(el, panel.firstChild);
}

// 单个强制重译：按钮置灰"翻译中…"防连点（§7.2）；失败分支 toast 文案严格按 §7.3，按钮恢复按态文案
async function translateRow(btn) {
  if (btn.disabled) return; // 防连点（已置灰时忽略再次点击）
  const repo = btn.dataset.repo;
  const hadZh = btn.dataset.hasZh === "1"; // 失败恢复按态文案用（成功态固定"重新翻译"）
  const panel = btn.closest(".panel");
  btn.disabled = true;
  btn.textContent = "翻译中…";
  const restore = () => {
    btn.disabled = false;
    btn.textContent = hadZh ? "重新翻译" : "翻译";
  };
  let resp;
  try {
    resp = await fetch("/api/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ full_name: repo }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      // 400 两分支（无简介/原文含中文）：detail 即 §7.3 文案，直用；其余（AI/网络）统一"翻译失败，稍后再试"
      toast(resp.status === 400 ? (data.detail || "无简介可译") : "翻译失败，稍后再试", true);
      restore();
      return;
    }
    toast("已更新翻译");
    replaceDescZh(panel, data.description_zh);
    btn.dataset.hasZh = "1"; // 现在已有译文：下次按态即"重新翻译"
    btn.textContent = "重新翻译";
    btn.disabled = false;
  } catch (err) {
    toast("翻译失败，稍后再试", true); // 网络层失败（§7.3"AI/网络失败"分支），旧译文保留（服务端未写库）
    restore();
  }
}

let batchTranslating = false; // 前端防连点（服务端另有 running 态 409 兜底，§7.3）
let batchPollTimer = null; // 批量进度轮询句柄（409/202/页面加载接管共用单轮询）

// 批量进度文案（§7.3 钉死）：T=0 时保持"补译中…"，避免 0/0 歧义
function batchProgress(state) {
  return state.total > 0 ? "补译中…（已补 " + state.translated + "/" + state.total + " 条）" : "补译中…";
}

// 恢复按钮常态并解锁防连点
function restoreBatch(btn) {
  batchTranslating = false;
  btn.disabled = false;
  btn.textContent = "补译全部缺失";
}

// 停止进度轮询（页面卸载自然停止；显式停止用于完成/轮询失败）
function stopBatchPolling() {
  if (batchPollTimer) {
    clearInterval(batchPollTimer);
    batchPollTimer = null;
  }
}

// 2s 轮询批量进度（§7.2 后台形态）：running → 按钮进度文案；finished → 停轮询＋按钮恢复＋按 error/failed 分支 toast
function pollBatchStatus(btn) {
  if (batchPollTimer) return; // 已在轮询：409/202/页面接管共用同一轮询
  const tick = async () => {
    try {
      const resp = await fetch("/api/translate-missing/status");
      const state = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      // 响应畸形（resp.ok 但 JSON 非预期/缺判态字段）：无法判态，按失败兜底（不误报完成）
      if (typeof state !== "object" || state === null || !("running" in state) || !("finished" in state)) {
        stopBatchPolling();
        restoreBatch(btn);
        toast("补译失败，稍后再试", true);
        return;
      }
      if (state.running === true) {
        btn.textContent = batchProgress(state);
        return; // 任务进行中：下一周期再查
      }
      if (state.finished === true) {
        stopBatchPolling();
        restoreBatch(btn);
        if (state.error === "auth") toast("未配置 DeepSeek API key", true);
        else if (state.error === "unknown") toast("补译失败，稍后再试", true);
        else if (state.failed > 0) toast("补译完成：新译 " + state.translated + " 条，失败 " + state.failed + " 条");
        else toast("补译完成：新译 " + state.translated + " 条");
        return;
      }
      // running/finished 双假（如服务端重启丢内存态）：非完成终态，不误报"补译完成"，按失败兜底
      stopBatchPolling();
      restoreBatch(btn);
      toast("补译失败，稍后再试", true);
    } catch (err) {
      stopBatchPolling();
      restoreBatch(btn);
      toast("补译失败，稍后再试", true); // 轮询网络层失败：停轮询恢复常态（服务端任务仍在跑，可再点，409 会接管）
    }
  };
  batchPollTimer = setInterval(tick, 2000);
  tick(); // 立即查一次：POST 202 后秒级反馈进度
}

// 顶栏批量补译（§7.2 后台形态）：点击置灰 → POST（202 起任务 / 409 已有任务）→ 2s 轮询 /status 显示进度；
// 完成/进行中/auth/网络失败文案严格按 §7.3，轮询期间 batchTranslating 保持锁防连点
async function translateMissing(btn) {
  if (batchTranslating) return;
  batchTranslating = true;
  btn.disabled = true;
  btn.textContent = "补译中…";
  try {
    const resp = await fetch("/api/translate-missing", { method: "POST" });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 409) {
      toast("补译进行中…"); // 服务端已有任务在跑：不另起，直接进轮询看它
      pollBatchStatus(btn);
      return;
    }
    if (!resp.ok) throw new Error(data.detail || "HTTP " + resp.status); // 其余异常（如 500）按失败兜底
    pollBatchStatus(btn); // 202：后台任务已起
  } catch (err) {
    toast("补译失败，稍后再试", true); // 网络层失败（§7.3 兜底文案），按钮恢复可重试
    restoreBatch(btn);
  }
}

// ---- T-016 页面加载接管在跑批量任务（§7.2：按钮在顶栏、各页通用） ----
(async () => {
  const btn = document.getElementById("translate-all");
  if (!btn) return;
  try {
    const resp = await fetch("/api/translate-missing/status");
    const state = await resp.json().catch(() => ({}));
    if (!resp.ok || !state.running) return; // 无在跑任务/查询失败：安静忽略（finished 态不 toast 不动按钮）
    batchTranslating = true; // 接管在跑任务：锁防连点，轮询接手进度显示
    btn.disabled = true;
    btn.textContent = batchProgress(state);
    pollBatchStatus(btn);
  } catch (err) {
    /* 接管查询网络异常：安静忽略，不影响页面 */
  }
})();

// ---- T-017 推荐语交互（§8.2 主路径 / §8.3 分支文案钉死，前端不发明文案） ----

// 面板推荐语块即时局部替换（不刷新页面）：有则覆写文本（块标题 <b> 保留——维度标题与页面一致），
// 无则插到概要/端点/窗口标注/操作区的首个存在块之前（模板顺序 desc→reason→summary→endpoint-note→window-note→acts，
// §11.1 钉死推荐语在概要上方），新块标题用 API 返回的 reason_label（服务端单一映射）；
// 全部走 textContent 防 XSS（同 T-010/T-016 口径）
function replaceReason(panel, text, label) {
  if (!panel) return;
  let el = panel.querySelector(".reason");
  if (el) {
    const b = el.querySelector("b");
    el.textContent = "";
    if (b) el.appendChild(b);
    el.appendChild(document.createTextNode(text));
    return;
  }
  el = document.createElement("div");
  el.className = "reason";
  const b = document.createElement("b");
  b.textContent = label || "推荐理由";
  el.appendChild(b);
  el.appendChild(document.createTextNode(text));
  const anchor = panel.querySelector(".summary, .endpoint-note, .window-note, .acts");
  if (anchor) anchor.insertAdjacentElement("beforebegin", el);
  else panel.appendChild(el);
}

// 单个强制重生（§8.2）：按当前页维度（data-dim/data-period-label，历史周页即该周标签）覆盖写；
// 按钮置灰"生成中…"防连点；失败分支 toast 文案严格按 §8.3，按钮恢复按态文案、旧文本保留（服务端未写库）
async function recommendRow(btn) {
  if (btn.disabled) return; // 防连点（已置灰时忽略再次点击）
  const repo = btn.dataset.repo;
  const hadReason = btn.dataset.hasReason === "1"; // 失败恢复按态文案用（成功态固定"重新生成"）
  const panel = btn.closest(".panel");
  btn.disabled = true;
  btn.textContent = "生成中…";
  const restore = () => {
    btn.disabled = false;
    btn.textContent = hadReason ? "重新生成" : "生成推荐语";
  };
  try {
    const resp = await fetch("/api/recommend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ full_name: repo, dimension: btn.dataset.dim, period_label: btn.dataset.periodLabel }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      // 500 = DeepSeek 账户类（key 未配置/无效/余额，§8.3 钉死文案）；其余（AI/网络）统一"推荐语生成失败，稍后再试"
      toast(resp.status === 500 ? "未配置 DeepSeek API key" : "推荐语生成失败，稍后再试", true);
      restore();
      return;
    }
    toast("已更新推荐语");
    replaceReason(panel, data.text, data.reason_label);
    btn.dataset.hasReason = "1"; // 现在已有推荐语：下次按态即"重新生成"
    btn.textContent = "重新生成";
    btn.disabled = false;
  } catch (err) {
    toast("推荐语生成失败，稍后再试", true); // 网络层失败（§8.3"AI/网络失败"分支），旧文本保留（服务端未写库）
    restore();
  }
}

let recBatchTranslating = false; // 前端防连点（服务端另有 running 态 409 兜底，§8.3）
let recBatchPollTimer = null; // 批量推荐进度轮询句柄（409/202/页面加载接管共用单轮询）

// 批量推荐进度文案（§8.3 钉死）：T=0 时保持"补齐中…"，避免 0/0 歧义
function recBatchProgress(state) {
  return state.total > 0 ? "补齐中…（已补 " + state.recommended + "/" + state.total + " 条）" : "补齐中…";
}

// 恢复按钮常态并解锁防连点
function restoreRecBatch(btn) {
  recBatchTranslating = false;
  btn.disabled = false;
  btn.textContent = "补齐推荐语";
}

// 停止批量推荐进度轮询（页面卸载自然停止；显式停止用于完成/轮询失败）
function stopRecBatchPolling() {
  if (recBatchPollTimer) {
    clearInterval(recBatchPollTimer);
    recBatchPollTimer = null;
  }
}

// 2s 轮询批量推荐进度（§8.2 后台形态）：running → 按钮进度文案；finished → 停轮询＋按钮恢复＋按 error/failed 分支 toast
function pollRecBatchStatus(btn) {
  if (recBatchPollTimer) return; // 已在轮询：409/202/页面接管共用同一轮询
  const tick = async () => {
    try {
      const resp = await fetch("/api/recommend-missing/status");
      const state = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      // 响应畸形（resp.ok 但 JSON 非预期/缺判态字段）：无法判态，按失败兜底（不误报完成）
      if (typeof state !== "object" || state === null || !("running" in state) || !("finished" in state)) {
        stopRecBatchPolling();
        restoreRecBatch(btn);
        toast("补齐失败，稍后再试", true);
        return;
      }
      if (state.running === true) {
        btn.textContent = recBatchProgress(state);
        return; // 任务进行中：下一周期再查
      }
      if (state.finished === true) {
        stopRecBatchPolling();
        restoreRecBatch(btn);
        if (state.error === "auth") toast("未配置 DeepSeek API key", true);
        else if (state.error === "unknown") toast("补齐失败，稍后再试", true);
        else if (state.failed > 0) toast("补齐完成：新生成 " + state.recommended + " 条，失败 " + state.failed + " 条");
        else toast("补齐完成：新生成 " + state.recommended + " 条");
        return;
      }
      // running/finished 双假（如服务端重启丢内存态）：非完成终态，不误报"补齐完成"，按失败兜底
      stopRecBatchPolling();
      restoreRecBatch(btn);
      toast("补齐失败，稍后再试", true);
    } catch (err) {
      stopRecBatchPolling();
      restoreRecBatch(btn);
      toast("补齐失败，稍后再试", true); // 轮询网络层失败：停轮询恢复常态（服务端任务仍在跑，可再点，409 会接管）
    }
  };
  recBatchPollTimer = setInterval(tick, 2000);
  tick(); // 立即查一次：POST 202 后秒级反馈进度
}

// 顶栏批量补齐推荐语（§8.2 后台形态，与翻译批量并列独立不共用）：点击置灰 → POST（202 起任务 / 409 已有任务）
// → 2s 轮询 /status 显示进度；完成/进行中/auth/网络失败文案严格按 §8.3，轮询期间 recBatchTranslating 保持锁防连点
async function recommendMissing(btn) {
  if (recBatchTranslating) return;
  recBatchTranslating = true;
  btn.disabled = true;
  btn.textContent = "补齐中…";
  try {
    const resp = await fetch("/api/recommend-missing", { method: "POST" });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 409) {
      toast("补齐进行中…"); // 服务端已有任务在跑：不另起，直接进轮询看它
      pollRecBatchStatus(btn);
      return;
    }
    if (!resp.ok) throw new Error(data.detail || "HTTP " + resp.status); // 其余异常（如 500）按失败兜底
    pollRecBatchStatus(btn); // 202：后台任务已起
  } catch (err) {
    toast("补齐失败，稍后再试", true); // 网络层失败（§8.3 兜底文案），按钮恢复可重试
    restoreRecBatch(btn);
  }
}

// ---- T-017 页面加载接管在跑批量推荐任务（§8.2：按钮在顶栏、各页通用，与翻译批量各自接管） ----
(async () => {
  const btn = document.getElementById("recommend-all");
  if (!btn) return;
  try {
    const resp = await fetch("/api/recommend-missing/status");
    const state = await resp.json().catch(() => ({}));
    if (!resp.ok || !state.running) return; // 无在跑任务/查询失败：安静忽略（finished 态不 toast 不动按钮）
    recBatchTranslating = true; // 接管在跑任务：锁防连点，轮询接手进度显示
    btn.disabled = true;
    btn.textContent = recBatchProgress(state);
    pollRecBatchStatus(btn);
  } catch (err) {
    /* 接管查询网络异常：安静忽略，不影响页面 */
  }
})();

// ---- T-021 §12.1 左侧边栏（仅 P1/P2/P3，页面含 .sidebar 才激活）：收起/展开记忆、移动抽屉、scrollspy ----

const SB_KEY = "t021-sb-collapsed"; // 桌面收起态记忆键（§12.1：localStorage 跨会话保持）
const SB_MQ = window.matchMedia("(max-width: 640px)"); // 断点与 radar.css 同值

function closeSidebarDrawer() {
  document.body.classList.remove("drawer-open");
}

// 边栏形态按视口切换：桌面读 localStorage 收起记忆；窄屏恒收起（FAB+抽屉，不记忆展开态）
function applySidebarView() {
  const wrap = document.querySelector(".with-sidebar");
  if (!wrap) return;
  const toggle = wrap.querySelector(".sb-toggle");
  if (SB_MQ.matches) {
    wrap.classList.remove("collapsed");
    closeSidebarDrawer();
  } else {
    let collapsed = false;
    try {
      collapsed = localStorage.getItem(SB_KEY) === "1"; // 读失败（隐私模式/禁用站点数据）按展开默认，不中断 scrollspy
    } catch (err) {
      collapsed = false;
    }
    wrap.classList.toggle("collapsed", collapsed);
    if (toggle) toggle.title = collapsed ? "展开边栏" : "收起边栏";
  }
}

// scrollspy（§12.1，原型已确认交互）：滚动时高亮当前所在榜对应边栏项（根 = 视口，与页面滚动一体）
function initScrollSpy() {
  const sidebar = document.querySelector(".sidebar");
  if (!sidebar) return;
  const items = [...sidebar.querySelectorAll(".sb-item")];
  const boards = items.map((a) => document.getElementById(a.getAttribute("href").slice(1))).filter(Boolean);
  if (!boards.length) return;
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((en) => {
        if (!en.isIntersecting) return;
        items.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#" + en.target.id));
      });
    },
    { rootMargin: "-70px 0px -62% 0px", threshold: 0 } // 顶栏+榜头区让出 70px，底部 62% 判区（原型同参数）
  );
  boards.forEach((b) => io.observe(b));
}

// 边栏交互（桌面收起/展开、移动抽屉开合、抽屉内点选自动收回）：
// 边栏项跳榜走原生锚点（href="#anchor"，全页 scroll-behavior: smooth 已就位），JS 不拦
document.addEventListener("click", (e) => {
  const toggle = e.target.closest(".sb-toggle");
  if (toggle) {
    const wrap = toggle.closest(".with-sidebar");
    if (!wrap) return;
    const collapsed = wrap.classList.toggle("collapsed");
    try {
      localStorage.setItem(SB_KEY, collapsed ? "1" : "0"); // 写失败仅不记忆，不影响本次交互
    } catch (err) {
      /* 隐私模式/禁用站点数据：静默忽略 */
    }
    toggle.title = collapsed ? "展开边栏" : "收起边栏";
    return;
  }
  if (e.target.closest(".fab")) {
    document.body.classList.add("drawer-open");
    return;
  }
  if (e.target.closest(".drawer-mask") || e.target.closest("[data-close-drawer]")) {
    closeSidebarDrawer();
    return;
  }
  if (e.target.closest(".sb-item") && SB_MQ.matches) closeSidebarDrawer(); // 抽屉内点选后自动收回
});

if (document.querySelector(".sidebar")) {
  applySidebarView();
  SB_MQ.addEventListener("change", applySidebarView);
  initScrollSpy();
}

// ---- T-021 §12.2 P6 标签筛选（客户端筛选，不发请求；与取消关注即时移除兼容） ----

let activeFollowTag = ""; // "" = 全部（与"全部" chip 的 data-tag 同值）

// data-tags 是 JSON 数组（服务端 tojson 编码，T-021 F2-1）：标签名可含逗号，须 JSON.parse 精确取数组；
// parse 异常（旧形态/畸形数据）按空数组处理，不炸页面
function parseRowTags(rowEl) {
  try {
    const raw = rowEl.dataset.tags;
    return raw ? JSON.parse(raw) : [];
  } catch (err) {
    return [];
  }
}

// 按当前 activeFollowTag 收窄行集：无该标签的行隐藏、空组整组隐藏、组头计数更新为可见数、0 命中显空态
function applyFollowTagFilter() {
  const boards = document.querySelectorAll("#follow-boards .board");
  const empty = document.getElementById("filter-empty");
  let anyVisible = false;
  boards.forEach((g) => {
    let vis = 0;
    g.querySelectorAll(".row").forEach((r) => {
      const hit = activeFollowTag === "" || parseRowTags(r).includes(activeFollowTag);
      r.hidden = !hit;
      const panel = document.querySelector(`.panel[data-b="${r.dataset.b}"][data-i="${r.dataset.i}"]`);
      if (panel) panel.hidden = !hit; // 行隐藏时面板一并隐藏（面板是行后的兄弟 div）
      if (hit) vis++;
    });
    g.hidden = vis === 0;
    if (!g.hidden) {
      const n = g.querySelector("h3 .n");
      if (n) n.textContent = `${vis} 个关注`;
      anyVisible = true;
    }
  });
  if (empty) empty.hidden = anyVisible;
}

// 筛选 chips 计数重算（取消关注后按剩余行集重算："全部"=剩余行数，各标签=剩余命中数，0 也列出）
function refreshFollowChipCounts() {
  const rows = document.querySelectorAll("#follow-boards .row");
  const counts = {};
  rows.forEach((r) =>
    parseRowTags(r).forEach((t) => {
      if (t) counts[t] = (counts[t] || 0) + 1;
    })
  );
  document.querySelectorAll(".tag-filter .fchip").forEach((c) => {
    const i = c.querySelector("i");
    if (!i) return;
    i.textContent = String(c.dataset.tag === "" ? rows.length : counts[c.dataset.tag] || 0);
  });
}

// 点 chip：再点当前标签或"全部" → 取消筛选恢复（§12.2）
document.addEventListener("click", (e) => {
  const chip = e.target.closest(".fchip");
  if (!chip) return;
  const tag = chip.dataset.tag || "";
  activeFollowTag = activeFollowTag === tag ? "" : tag;
  document
    .querySelectorAll(".tag-filter .fchip")
    .forEach((c) => c.classList.toggle("on", (c.dataset.tag || "") === activeFollowTag));
  applyFollowTagFilter();
});
