(() => {
  const saved = localStorage.getItem('meridian-theme');
  document.documentElement.dataset.theme = saved
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
})();
