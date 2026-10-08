(() => {
  const form = document.querySelector("#batch-action-form");
  if (!form) return;

  const selections = [...form.querySelectorAll("[data-path-selection]")];
  const selectAll = form.querySelector("#select-all-visible");
  const clearButton = form.querySelector("#clear-selection");
  const count = form.querySelector("#selected-path-count");
  const status = form.querySelector("#batch-action-eligibility");
  const actions = [...form.querySelectorAll("[data-batch-action]")];
  const maxPaths = Number(form.dataset.maxPaths);

  const selectedItems = () => selections.filter((item) => item.checked);

  const refresh = () => {
    const selected = selectedItems();
    count.textContent = String(selected.length);
    selectAll.checked = selections.length > 0 && selected.length === selections.length;
    selectAll.indeterminate = selected.length > 0 && selected.length < selections.length;

    for (const button of actions) {
      const action = button.dataset.batchAction;
      button.disabled = selected.length === 0 || selected.length > maxPaths
        || selected.some((item) => !item.dataset.actions.split(" ").includes(action));
    }
    status.textContent = selected.length > maxPaths
      ? `Select no more than ${maxPaths} files per action.`
      : selected.length
      ? "An action is available only when every selected file supports it."
      : "Select one or more files. An action is enabled only when every selected file supports it.";
  };

  for (const selection of selections) selection.addEventListener("change", refresh);
  selectAll.addEventListener("change", () => {
    for (const selection of selections) selection.checked = selectAll.checked;
    refresh();
  });
  clearButton.addEventListener("click", () => {
    for (const selection of selections) selection.checked = false;
    refresh();
  });
  form.addEventListener("submit", (event) => {
    const action = event.submitter?.dataset.batchAction;
    const selected = selectedItems();
    if (!action || selected.length === 0 || selected.length > maxPaths
      || selected.some((item) => !item.dataset.actions.split(" ").includes(action))) {
      event.preventDefault();
      refresh();
    }
  });

  refresh();
})();
