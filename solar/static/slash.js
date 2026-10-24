export const commands = [
  ["/model ", "Choose a model"], ["/effort ", "Reasoning effort, including max"],
  ["/permissions ", "Set the actual sandbox policy"], ["/approvals ", "Set the approval policy"],
  ["/workspace ", "Select a registered project folder"], ["/agent ", "Select an agent role"],
  ["/skill ", "Enable a skill"], ["/skills", "List enabled skills"],
  ["/status", "Current session settings"], ["/stop", "Cancel the active run"],
  ["/plan", "Use read-only planning"], ["/review", "Review workspace files"],
  ["/compact", "Summarize the conversation"], ["/native", "Full provider terminal and commands"],
  ["/help", "Show web commands"]
];

export function suggestions(value, argumentsFor = () => []) {
  if (!value.startsWith("/") || /[\r\n]/.test(value)) return [];
  const split = value.indexOf(" ");
  if (split < 0) return commands.filter(([name]) => name.startsWith(value.toLowerCase()))
    .map(([value, description]) => ({value, label:value.trim(), description, command:true}));
  const name = value.slice(0, split).toLowerCase(), query = value.slice(split + 1).trim().toLowerCase();
  return argumentsFor(name).filter(item => `${item.label} ${item.value}`.toLowerCase().includes(query))
    .slice(0, 80).map(item => ({...item, value:`${name} ${item.value}`, command:false}));
}

export function nextSelection(index, direction, length) {
  return length ? (index + direction + length) % length : -1;
}

let menuNumber = 0;
export function attachSlashMenu(input, menu, argumentsFor) {
  let items = [], selected = 0;
  menu.id = `slash-menu-${++menuNumber}`;
  menu.setAttribute("role", "listbox");
  menu.setAttribute("aria-label", "Slash commands and arguments");
  input.setAttribute("aria-controls", menu.id);
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  function close() {
    menu.hidden = true; input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }
  function highlight() {
    [...menu.children].forEach((row, index) => {
      row.classList.toggle("selected", index === selected);
      row.setAttribute("aria-selected", String(index === selected));
    });
    const active = menu.children[selected];
    if (active) {
      input.setAttribute("aria-activedescendant", active.id);
      active.scrollIntoView({block:"nearest"});
    }
  }
  function choose(index) {
    const item = items[index]; if (!item) return;
    input.value = item.value; input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    close();
    if (item.command && item.value.endsWith(" ")) update();
  }
  function update() {
    items = suggestions(input.value, argumentsFor); selected = 0; menu.replaceChildren();
    if (!items.length) {close(); return;}
    for (const [index, item] of items.entries()) {
      const row = document.createElement("button"); row.type = "button"; row.tabIndex = -1;
      row.id = `${menu.id}-${index}`; row.setAttribute("role", "option");
      const label = document.createElement("span"), description = document.createElement("small");
      label.textContent = item.label; description.textContent = item.description || "";
      row.append(label, description);
      row.addEventListener("pointerdown", event => event.preventDefault());
      row.addEventListener("click", () => choose(index)); menu.append(row);
    }
    menu.hidden = false; input.setAttribute("aria-expanded", "true"); highlight();
  }
  input.addEventListener("input", update);
  input.addEventListener("blur", close);
  input.addEventListener("keydown", event => {
    if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "Escape" && !menu.hidden) {
      event.preventDefault(); event.stopImmediatePropagation(); close(); return;
    }
    if (!["ArrowDown", "ArrowUp", "Enter", "Tab"].includes(event.key) || event.shiftKey) return;
    if (menu.hidden) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") update();
      else return;
    }
    if (menu.hidden) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (event.key === "Enter" || event.key === "Tab") choose(selected);
    else {selected = nextSelection(selected, event.key === "ArrowDown" ? 1 : -1, items.length); highlight();}
  });
  return {close, update};
}
