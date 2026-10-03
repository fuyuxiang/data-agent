(() => {
  // 在首屏样式加载前定好主题，避免刷新时闪一下浅色。
  const saved = localStorage.getItem('shuqing-theme');
  document.documentElement.dataset.theme = saved
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
})();
