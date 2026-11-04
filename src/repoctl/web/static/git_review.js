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
  const maxPathBytes = Number(form.dataset.maxPathBytes);

  const selectedItems = () => selections.filter((item) => item.checked);
  const selectedPathBytes = (items) => items.reduce((total, item) => {
    const encoded = item.value.replace(/-/g, "+").replace(/_/g, "/");
    const padded = encoded.padEnd(Math.ceil(encoded.length / 4) * 4, "=");
    try {
      return total + atob(padded).length;
    } catch {
      return Number.POSITIVE_INFINITY;
    }
  }, 0);

  const refresh = () => {
    const selected = selectedItems();
    const bytes = selectedPathBytes(selected);
    count.textContent = String(selected.length);
    selectAll.checked = selections.length > 0 && selected.length === selections.length;
    selectAll.indeterminate = selected.length > 0 && selected.length < selections.length;

    for (const button of actions) {
      const action = button.dataset.batchAction;
      const pathLimit = action === "stage" ? maxPaths : 256;
      button.disabled = selected.length === 0 || selected.length > pathLimit
        || action === "stage" && bytes > maxPathBytes
        || selected.some((item) => !item.dataset.actions.split(" ").includes(action));
    }
    status.textContent = selected.length > maxPaths
      ? `Staging supports at most ${maxPaths} paths; unstage and restore support at most 256.`
      : bytes > maxPathBytes
      ? `Selected path names exceed the ${maxPathBytes}-byte staging limit.`
      : selected.length
      ? `Selected path names: ${bytes} bytes. Staging is bounded to ${maxPathBytes} bytes; unstage and restore remain limited to 256 paths.`
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
    const bytes = selectedPathBytes(selected);
    if (!action || selected.length === 0 || selected.length > (action === "stage" ? maxPaths : 256)
      || action === "stage" && bytes > maxPathBytes
      || selected.some((item) => !item.dataset.actions.split(" ").includes(action))) {
      event.preventDefault();
      refresh();
    }
  });

  refresh();
})();
