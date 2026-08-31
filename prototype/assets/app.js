(function () {
  const siteMenu = document.querySelector('[data-menu-button]');
  const mobileNav = document.querySelector('[data-mobile-nav]');

  if (siteMenu && mobileNav) {
    siteMenu.addEventListener('click', function () {
      const open = mobileNav.classList.toggle('is-open');
      siteMenu.setAttribute('aria-expanded', String(open));
    });
  }

  const adminMenu = document.querySelector('[data-admin-menu]');
  const adminSidebar = document.querySelector('[data-admin-sidebar]');
  if (adminMenu && adminSidebar) {
    adminMenu.addEventListener('click', function () {
      const open = adminSidebar.classList.toggle('is-open');
      adminMenu.setAttribute('aria-expanded', String(open));
    });
  }

  document.querySelectorAll('[data-toast-message]').forEach(function (button) {
    button.addEventListener('click', function () {
      const toast = document.querySelector('[data-toast]');
      if (!toast) return;
      toast.textContent = button.getAttribute('data-toast-message');
      toast.classList.add('show');
      window.setTimeout(function () { toast.classList.remove('show'); }, 2400);
    });
  });

  const toggleCatalog = document.querySelector('[data-toggle-catalog]');
  if (toggleCatalog) {
    toggleCatalog.addEventListener('click', function () {
      document.querySelectorAll('[data-extra-catalog]').forEach(function (row) {
        row.hidden = !row.hidden;
      });
      const expanded = toggleCatalog.getAttribute('aria-expanded') === 'true';
      toggleCatalog.setAttribute('aria-expanded', String(!expanded));
      toggleCatalog.textContent = expanded ? '展开完整目录' : '收起目录';
    });
  }

  const filterButtons = document.querySelectorAll('[data-import-filter]');
  if (filterButtons.length) {
    filterButtons.forEach(function (button) {
      button.addEventListener('click', function () {
        filterButtons.forEach(function (item) { item.classList.remove('active'); });
        button.classList.add('active');
        const filter = button.getAttribute('data-import-filter');
        document.querySelectorAll('[data-row-state]').forEach(function (row) {
          const state = row.getAttribute('data-row-state');
          row.classList.toggle('is-hidden', filter !== 'all' && state !== filter);
        });
      });
    });
  }

  document.querySelectorAll('[data-search-form]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      const input = form.querySelector('input');
      const toast = document.querySelector('[data-toast]');
      if (toast) {
        toast.textContent = input && input.value.trim()
          ? '视觉原型：将搜索“' + input.value.trim() + '”'
          : '请输入书名、作者或 ISBN';
        toast.classList.add('show');
        window.setTimeout(function () { toast.classList.remove('show'); }, 2400);
      }
    });
  });
})();
