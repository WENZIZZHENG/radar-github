// T-008 榜单页交互：行详情默认全部展开（D1/D3 v1.1），单击只折叠/再展开本行，各行独立——
// 交互语义照抄示意图 v3（已确认冻结物）。关注星标为禁用态（T-009 接线）、标签只读（T-010），
// 故本版无关注/标签/toast 逻辑，接线时从示意图脚本补回对应段落。
function toggleRow(row) {
  const panel = document.querySelector(`.panel[data-b="${row.dataset.b}"][data-i="${row.dataset.i}"]`);
  if (!panel) return;
  const open = row.classList.toggle("expanded");
  row.setAttribute("aria-expanded", String(open));
  panel.classList.toggle("open", open);
}

document.addEventListener("click", (e) => {
  // Chrome M120 起禁用表单控件的鼠标事件会越过控件冒泡（whatwg/html#5886）：
  // 点禁用星标的 SVG 子元素仍可命中本处理器——显式排除星标命中区，防误触发行折叠
  if (e.target.closest(".star")) return;
  const row = e.target.closest(".row");
  if (row) toggleRow(row);
});

// 键盘操作：Enter / Space 与点击行等价（行带 tabindex=0 / role=button）
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.classList && e.target.classList.contains("row")) {
    e.preventDefault();
    toggleRow(e.target);
  }
});
