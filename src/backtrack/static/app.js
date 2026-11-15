// Settings rows save themselves: submit a row's form when its value changes.
document.querySelectorAll("form.bt-autosave input").forEach((input) => {
  input.addEventListener("change", () => input.form.requestSubmit());
});
// A running job's page reloads itself until the job finishes.
if (document.querySelector("[data-refresh]")) setTimeout(() => location.reload(), 5000);
