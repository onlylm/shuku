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

  document.querySelectorAll('[data-copy-code]').forEach(function (button) {
    button.addEventListener('click', async function () {
      const code = button.getAttribute('data-copy-code') || '';
      if (!code) return;
      try {
        await navigator.clipboard.writeText(code);
      } catch (error) {
        const helper = document.createElement('textarea');
        helper.value = code;
        helper.setAttribute('readonly', '');
        helper.style.position = 'fixed';
        helper.style.opacity = '0';
        document.body.appendChild(helper);
        helper.select();
        document.execCommand('copy');
        helper.remove();
      }
      const label = button.querySelector('small');
      if (!label) return;
      label.textContent = '已复制';
      window.setTimeout(function () { label.textContent = '复制'; }, 1600);
    });
  });

  document.querySelectorAll('[data-provider-detect]').forEach(function (editor) {
    const input = editor.querySelector('[data-share-url]');
    const result = editor.querySelector('[data-provider-result]');
    if (!input || !result) return;
    const detect = function () {
      const value = input.value.trim().toLowerCase();
      result.classList.remove('is-success', 'is-warning');
      if (!value) {
        result.textContent = '粘贴后自动识别百度网盘或夸克网盘';
      } else if (value.includes('pan.baidu.com')) {
        result.textContent = '已识别：百度网盘';
        result.classList.add('is-success');
      } else if (value.includes('pan.quark.cn')) {
        result.textContent = '已识别：夸克网盘';
        result.classList.add('is-success');
      } else {
        result.textContent = '暂未识别：目前仅支持百度网盘和夸克网盘';
        result.classList.add('is-warning');
      }
    };
    input.addEventListener('input', detect);
    input.addEventListener('change', detect);
    detect();
  });
})();
