(() => {
  const currentPage = (() => {
    const file = window.location.pathname.split("/").pop();
    return file && file.includes(".") ? file : "index.html";
  })();

  async function loadFragment(targetId, filename) {
    const target = document.getElementById(targetId);
    if (!target) return;

    try {
      const response = await fetch(filename, { cache: "no-cache" });
      if (!response.ok) throw new Error(`${filename}: ${response.status}`);
      target.innerHTML = await response.text();
    } catch (error) {
      console.error(`Could not load ${filename}`, error);
      target.innerHTML = `<div class="component-load-error">Could not load ${filename}.</div>`;
    }
  }

  function setActiveNavigation() {
    document.querySelectorAll("#site-nav [data-page]").forEach((link) => {
      const isActive = link.dataset.page === currentPage;
      link.classList.toggle("active", isActive);

      if (isActive) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  function bindMobileMenu() {
    const wrap = document.getElementById("menuWrap");
    const button = document.getElementById("menuButton");
    if (!wrap || !button) return;

    const setOpen = (open) => {
      wrap.classList.toggle("open", open);
      button.setAttribute("aria-expanded", String(open));
    };

    button.addEventListener("click", (event) => {
      event.stopPropagation();
      setOpen(!wrap.classList.contains("open"));
    });

    document.addEventListener("click", (event) => {
      if (!wrap.contains(event.target)) setOpen(false);
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        setOpen(false);
        button.focus();
      }
    });
  }

  async function initSharedComponents() {
    await Promise.all([
      loadFragment("site-nav", "nav.html"),
      loadFragment("site-footer", "footer.html")
    ]);

    setActiveNavigation();
    bindMobileMenu();
  }

  initSharedComponents();
})();
