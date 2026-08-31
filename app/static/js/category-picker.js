(function () {
  document.querySelectorAll('[data-category-picker]').forEach(function (picker) {
    const main = picker.querySelector('[data-category-main]');
    const sub = picker.querySelector('[data-category-sub]');
    const hint = picker.querySelector('[data-category-hint]');
    const children = JSON.parse(picker.querySelector('[data-category-options]').textContent);

    function refresh(selected) {
      const keep = main.value === '__keep__';
      const available = children.filter(function (item) { return String(item.parent_id) === main.value; });
      sub.replaceChildren(new Option('不选二级分类', ''));
      available.forEach(function (item) {
        sub.add(new Option(item.name, String(item.id)));
      });
      sub.value = available.some(function (item) { return String(item.id) === selected; }) ? selected : '';
      sub.disabled = keep || !main.value || available.length === 0;
      hint.textContent = keep ? '保留现有分类；选择新的一级分类后可以重新归类。'
        : !main.value ? '请先选择一级分类；草稿可以暂不分类。'
        : available.length === 0 ? '该一级分类暂无二级分类，可以直接保存。'
        : '只显示当前一级分类下的二级分类；不选二级则归入一级分类。';
    }

    main.addEventListener('change', function () { refresh(''); });
    refresh(sub.value);
    // 浏览器恢复表单或返回此页时，重新对齐一级与二级选项。
    window.addEventListener('pageshow', function () { refresh(sub.value); });
  });
})();
