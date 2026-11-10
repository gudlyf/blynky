// Applied before the stylesheet so a chosen theme never flashes the other one
// first. A file of its own rather than inline, which the page's
// Content-Security-Policy does not allow.
try {
  const theme = localStorage.getItem("blynky.theme");
  if (theme === "light" || theme === "dark") {
    document.documentElement.dataset.theme = theme;
  }
} catch (err) {}
