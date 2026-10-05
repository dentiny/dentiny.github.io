(() => {
  const root = document.documentElement;
  const themeButton = document.querySelector('.theme-toggle');
  const darkPreference = window.matchMedia('(prefers-color-scheme: dark)');
  const currentTheme = () => root.dataset.theme || (darkPreference.matches ? 'dark' : 'light');
  const updateThemeLabel = () => {
    themeButton.setAttribute('aria-label', currentTheme() === 'dark' ? '切换到浅色主题' : '切换到深色主题');
    themeButton.title = themeButton.getAttribute('aria-label');
  };
  if (themeButton) {
    themeButton.hidden = false;
    updateThemeLabel();
    darkPreference.addEventListener('change', updateThemeLabel);
    themeButton.addEventListener('click', () => {
      root.dataset.theme = currentTheme() === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('blog-theme', root.dataset.theme); } catch (e) {}
      updateThemeLabel();
    });
  }
  const search = document.querySelector('#article-search');
  if (search) {
    search.closest('.search-box').hidden = false;
    const entries = Array.from(document.querySelectorAll('.archive-entry'));
    const years = Array.from(document.querySelectorAll('.archive-year'));
    search.addEventListener('input', () => {
      const query = search.value.trim().toLocaleLowerCase();
      let visible = 0;
      entries.forEach(entry => {
        const matches = entry.dataset.search.toLocaleLowerCase().includes(query);
        entry.hidden = !matches;
        if (matches) visible++;
      });
      years.forEach(year => { year.hidden = !year.querySelector('.archive-entry:not([hidden])'); });
      document.querySelector('.search-empty').hidden = visible > 0 || !query;
    });
  }
})();
