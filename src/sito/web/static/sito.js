// Вкладки ленты: колесо мыши прокручивает их вбок, активная вкладка всегда видна,
// при переключении (HTMX меняет только ленту под вкладками) подсвечивается выбранная.
(() => {
  function revealActive(tabs) {
    const active = tabs.querySelector(".tab.active");
    if (active) active.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function setup() {
    const tabs = document.querySelector(".tabs");
    if (!tabs || tabs.dataset.ready) return;
    tabs.dataset.ready = "1";

    tabs.addEventListener("wheel", (event) => {
      // Вертикальное колесо — в горизонтальную прокрутку, если вкладкам есть куда ехать
      if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
      if (tabs.scrollWidth <= tabs.clientWidth) return;
      event.preventDefault();
      tabs.scrollLeft += event.deltaY;
    }, { passive: false });

    tabs.addEventListener("click", (event) => {
      const tab = event.target.closest(".tab");
      if (!tab) return;
      tabs.querySelectorAll(".tab.active").forEach((el) => el.classList.remove("active"));
      tab.classList.add("active");
    });

    revealActive(tabs);
  }

  // «← Лента» на странице сюжета: если пришли из ленты Сито, вернуться браузерным «назад» —
  // браузер восстановит ту же страницу и место прокрутки. Иначе — обычная ссылка на последнюю вкладку.
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-back]");
    if (!link) return;
    const from = document.referrer ? new URL(document.referrer) : null;
    if (from && from.origin === location.origin && from.pathname === "/" && history.length > 1) {
      event.preventDefault();
      history.back();
    }
  });

  // Временные сообщения («Убрано из темы… Отменить», «Пароль изменён»): через data-autohide секунд
  // плавно исчезают, через data-settle — сменяются обычным видом. Пока курсор на сообщении — ждут.
  function expire(el) {
    if (!el.isConnected) return;
    if (el.matches(":hover, :focus-within")) {
      setTimeout(() => expire(el), 1500);
      return;
    }
    if (el.dataset.settle) {
      el.classList.add("settled");
      return;
    }
    el.classList.add("fading");
    setTimeout(() => {
      if (!el.isConnected) return;
      // Схлопнуть место плавно, чтобы лента под сообщением не прыгала
      el.style.overflow = "hidden";
      el.style.height = `${el.offsetHeight}px`;
      el.style.transition = "height .3s ease, margin .3s ease, padding .3s ease";
      requestAnimationFrame(() => {
        Object.assign(el.style, { height: "0", marginTop: "0", marginBottom: "0", paddingTop: "0", paddingBottom: "0", borderWidth: "0" });
      });
      setTimeout(() => {
        const block = el.closest(".ask");
        el.remove();
        // Ответили на все вопросы «Подходит?» — убрать и сам блок
        if (block && !block.querySelector(".story-card")) block.remove();
      }, 350);
    }, 400);
  }

  function scheduleExpiry(root) {
    const found = root.querySelectorAll ? [...root.querySelectorAll("[data-autohide], [data-settle]")] : [];
    if (root.matches && root.matches("[data-autohide], [data-settle]")) found.push(root);
    for (const el of found) {
      if (el.dataset.timed) continue;
      el.dataset.timed = "1";
      const seconds = Number(el.dataset.autohide || el.dataset.settle) || 8;
      setTimeout(() => expire(el), seconds * 1000);
    }
  }

  // Таблица с поиском (input[data-filter]), фильтром по состоянию (кнопки data-state-filter)
  // и сортировкой (select[data-sort]): строка видна, если подходит и под поиск, и под фильтр
  function applyFilters(selector) {
    const search = document.querySelector(`[data-filter="${selector}"]`);
    const query = search ? search.value.trim().toLowerCase() : "";
    const tools = document.querySelector(`[data-table-tools="${selector}"]`);
    const active = tools ? tools.querySelector(".chip.active") : null;
    const state = active ? active.dataset.stateFilter : "";
    document.querySelectorAll(`${selector} tbody tr`).forEach((row) => {
      const byText = !query || (row.dataset.name || "").includes(query);
      const byState = !state || row.dataset.state === state;
      row.hidden = !(byText && byState);
      if (row.hidden) row.querySelectorAll("input[type=checkbox]").forEach((box) => { box.checked = false; });
    });
    updateBulkBars();
  }

  function sortRows(selector, key) {
    const body = document.querySelector(`${selector} tbody`);
    if (!body) return;
    const desc = !key.startsWith("-");
    const field = key.replace("-", "") || "order";
    const value = (row) => field === "name" ? row.dataset.name : Number(row.dataset[field] || 0);
    const rows = [...body.rows].sort((a, b) => {
      const [x, y] = [value(a), value(b)];
      if (field === "name") return x.localeCompare(y, "ru");
      if (field === "order") return x - y;
      return desc ? y - x : x - y;
    });
    body.append(...rows);
  }

  document.addEventListener("input", (event) => {
    const input = event.target.closest("[data-filter]");
    if (input) applyFilters(input.dataset.filter);
  });
  document.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-state-filter]");
    if (!chip) return;
    const tools = chip.closest("[data-table-tools]");
    tools.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c === chip));
    applyFilters(tools.dataset.tableTools);
  });
  document.addEventListener("change", (event) => {
    const select = event.target.closest("[data-sort]");
    if (select) sortRows(select.closest("[data-table-tools]").dataset.tableTools, select.value);
  });

  // Строка действий над отмеченными: видна, только когда что-то отмечено, и пишет сколько
  function updateBulkBars() {
    document.querySelectorAll("[data-bulk-bar]").forEach((bar) => {
      const n = document.querySelectorAll(`${bar.dataset.bulkBar} tbody input[type=checkbox]:checked`).length;
      bar.hidden = n === 0;
      const count = bar.querySelector("[data-bulk-count]");
      if (count) count.textContent = `Отмечено: ${n}`;
      const hint = document.querySelector("[data-bulk-hint]");
      if (hint) hint.hidden = n > 0;
    });
  }
  document.addEventListener("change", (event) => {
    if (event.target.matches("input[type=checkbox]")) updateBulkBars();
  });

  // «Отметить все» — только видимые строки: можно найти в списке и отметить найденное
  document.addEventListener("change", (event) => {
    const all = event.target.closest("[data-check-all]");
    if (!all) return;
    document.querySelectorAll(`${all.dataset.checkAll} tbody tr:not([hidden]) input[type=checkbox]`)
      .forEach((box) => { box.checked = all.checked; });
    updateBulkBars();
  });

  document.addEventListener("htmx:load", (event) => scheduleExpiry(event.target));
  document.addEventListener("DOMContentLoaded", () => {
    setup();
    scheduleExpiry(document);
  });
})();
