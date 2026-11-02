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

  document.addEventListener("DOMContentLoaded", setup);
})();
