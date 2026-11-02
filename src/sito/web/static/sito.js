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

  document.addEventListener("DOMContentLoaded", setup);
})();
