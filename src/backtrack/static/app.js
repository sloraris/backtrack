// Settings rows save themselves: submit a row's form when its value changes.
document.querySelectorAll("form.bt-autosave input").forEach((input) => {
  input.addEventListener("change", () => input.form.requestSubmit());
});
// A running job's page reloads itself until the job finishes.
if (document.querySelector("[data-refresh]")) setTimeout(() => location.reload(), 5000);
// A select-all checkbox (shown only with JavaScript) ticks every checkbox of the same name.
document.querySelectorAll("[data-select-all]").forEach((all) => {
  const boxes = () => document.querySelectorAll(`input[type=checkbox][name="${all.dataset.selectAll}"]`);
  all.hidden = false;
  all.addEventListener("change", () => boxes().forEach((b) => { b.checked = all.checked; }));
  boxes().forEach((b) => b.addEventListener("change", () => {
    const list = [...boxes()];
    all.checked = list.every((x) => x.checked);
    all.indeterminate = !all.checked && list.some((x) => x.checked);
  }));
});
