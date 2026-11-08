const themeRoot = document.documentElement;
try {
    themeRoot.dataset.theme = /^(dark|ink)$/.test(localStorage.getItem('ui-theme') || '') ? 'dark' : 'light';
} catch (error) {}

document.addEventListener('DOMContentLoaded', () => {
    const themeButton = document.getElementById('theme');
    if (!themeButton) return;

    function syncThemeLabel() {
        const isDark = themeRoot.dataset.theme === 'dark';
        themeButton.setAttribute('aria-label', themeRoot.lang === 'en'
            ? (isDark ? 'Switch to light mode' : 'Switch to dark mode')
            : (isDark ? '切换为浅色模式' : '切换为深色模式'));
    }

    syncThemeLabel();
    themeButton.addEventListener('click', () => {
        themeRoot.dataset.theme = themeRoot.dataset.theme === 'dark' ? 'light' : 'dark';
        try {
            localStorage.setItem('ui-theme', themeRoot.dataset.theme);
        } catch (error) {}
        syncThemeLabel();
    });
});
