// Closes the top bar's menu, on phones, after a tap on one of its links, outside it, or Escape.
const menu = document.querySelector(".nav-menu");

if (menu) {
  menu.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => menu.removeAttribute("open"));
  });
  document.addEventListener("click", (event) => {
    if (!menu.contains(event.target)) menu.removeAttribute("open");
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") menu.removeAttribute("open");
  });
}
