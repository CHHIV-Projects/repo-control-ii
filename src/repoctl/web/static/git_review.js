(() => {
  const form = document.querySelector("#batch-action-form");
  if (!form) return;

  const selections = [...form.querySelectorAll("[data-path-selection]")];
  if (!selections.length) return;
  const selectAll = form.querySelector("#select-all-visible");
  const clearButton = form.querySelector("#clear-selection");
  const count = form.querySelector("#selected-path-count");
  const status = form.querySelector("#batch-action-eligibility");
  const actions = [...form.querySelectorAll("[data-batch-action]")];
  const positiveSafeInteger = (value) => {
    if (typeof value !== "string" || !/^[1-9]\d*$/.test(value)) return null;
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) ? parsed : null;
  };
  const maxStagePaths = positiveSafeInteger(form.dataset.maxStagePaths);
  const maxStagePathBytes = positiveSafeInteger(form.dataset.maxStagePathBytes);
  const maxActionPaths = positiveSafeInteger(form.dataset.maxActionPaths);
  const hasValidStageLimits = maxStagePaths !== null && maxStagePathBytes !== null;
  const hasValidActionLimit = maxActionPaths !== null;

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

  const actionEligible = (action, selected, bytes) => {
    const pathLimit = action === "stage" ? maxStagePaths : maxActionPaths;
    return pathLimit !== null
      && (action !== "stage" || maxStagePathBytes !== null && bytes <= maxStagePathBytes)
      && selected.length > 0
      && selected.length <= pathLimit
      && selected.every((item) => item.dataset.actions.split(" ").includes(action));
  };

  const refresh = () => {
    const selected = selectedItems();
    const bytes = selectedPathBytes(selected);
    count.textContent = String(selected.length);
    selectAll.checked = selections.length > 0 && selected.length === selections.length;
    selectAll.indeterminate = selected.length > 0 && selected.length < selections.length;

    for (const button of actions) {
      const action = button.dataset.batchAction;
      button.disabled = !actionEligible(action, selected, bytes);
    }
    if (!hasValidStageLimits) {
      status.textContent = "Staging is unavailable because the server-provided path limits are missing or invalid. Refresh the page or contact an operator.";
    } else if (!hasValidActionLimit) {
      status.textContent = "Unstage and restore are unavailable because the server-provided action limit is missing or invalid. Staging remains bounded by its separate limits.";
    } else if (selected.length > maxStagePaths) {
      status.textContent = `Staging supports at most ${maxStagePaths} paths; unstage and restore support at most ${maxActionPaths}.`;
    } else if (selected.length > maxActionPaths) {
      status.textContent = `Staging supports up to ${maxStagePaths} paths; unstage and restore support at most ${maxActionPaths}.`;
    } else if (bytes > maxStagePathBytes) {
      status.textContent = `Selected path names exceed the ${maxStagePathBytes}-byte staging limit.`;
    } else if (selected.length) {
      status.textContent = `Selected path names: ${bytes} bytes. Staging is bounded to ${maxStagePathBytes} bytes; unstage and restore remain limited to ${maxActionPaths} paths.`;
    } else {
      status.textContent = "Select one or more files. An action is enabled only when every selected file supports it.";
    }
    status.classList.toggle(
      "warning",
      !hasValidStageLimits || !hasValidActionLimit || selected.length > maxStagePaths
        || selected.length > maxActionPaths || bytes > maxStagePathBytes,
    );
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
    if (!action || !actionEligible(action, selected, bytes)) {
      event.preventDefault();
      refresh();
    }
  });

  refresh();
})();
