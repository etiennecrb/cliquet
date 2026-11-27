// Highlights, in the page map, the section the reader is in.
const links = [...document.querySelectorAll(".pagemap .pm-card a")];
const lines = [...document.querySelectorAll(".pagemap .pm-lines span")];
const sections = links.map((link) => document.querySelector(link.getAttribute("href")));

function update() {
  const middle = window.innerHeight * 0.35;
  let current = -1;
  sections.forEach((section, index) => {
    if (section && section.getBoundingClientRect().top <= middle) current = index;
  });
  links.forEach((link, index) => link.classList.toggle("on", index === current));
  lines.forEach((line, index) => line.classList.toggle("on", index === current));
}

window.addEventListener("scroll", update, { passive: true });
window.addEventListener("resize", update);
update();
