(() => {
  const rootSelector = "#milestone-workflow";
  let refreshing = false;

  function initialize(root) {
    const form = root.querySelector("[data-workflow-form]");
    if (!form) return;

    const action = form.querySelector("#workflow-event-type");
    const summary = form.querySelector("#workflow-summary");
    const related = form.querySelector("#related-event");
    const waitingCategory = form.querySelector("#waiting-category");
    const waitingReason = form.querySelector("#waiting-reason");
    const bugScope = form.querySelector("#bug-scope");
    const actionGroups = [...form.querySelectorAll("[data-events]")];
    const requiredFields = {
      answer_recorded: ["related_event_id"],
      bug_classified: ["related_event_id", "bug_scope"],
      bug_fixed: ["related_event_id"],
      test_result_recorded: ["evidence_source", "result"],
    };

    function updateActionFields(updateDefault = false) {
      const selectedAction = action.value;
      const explanation = form.querySelector("#workflow-action-explanation");
      const submit = form.querySelector("#workflow-submit");
      if (explanation) explanation.textContent = action.selectedOptions[0]?.dataset.explanation || "";
      if (submit) submit.textContent = selectedAction ? action.selectedOptions[0].textContent.trim() : "Record workflow event";
      actionGroups.forEach((group) => {
        const visible = group.dataset.events.split(",").includes(selectedAction);
        group.hidden = !visible;
        group.querySelectorAll("input, select, textarea").forEach((field) => {
          field.disabled = !visible;
          field.required = visible && (requiredFields[selectedAction] || []).includes(field.name);
          if (visible && selectedAction === "resolve_and_resume" && field.name === "related_event_id") {
            field.required = true;
          }
        });
      });

      if (related) {
        [...related.options].forEach((option) => {
          if (!option.value) return;
          const visible = option.dataset.actions.split(",").includes(selectedAction);
          option.disabled = !visible;
          option.hidden = !visible;
        });
        const eligible = [...related.options].filter(
          (option) => option.value && !option.disabled
        );
        if (related.value && related.selectedOptions[0]?.disabled) related.value = "";
        if (eligible.length === 1 && !related.value) related.value = eligible[0].value;
      }

      if (waitingReason) {
        waitingReason.required =
          selectedAction === "escalation_opened" ||
          (selectedAction === "bug_classified" && bugScope?.value === "needs_escalation") ||
          (selectedAction === "question_submitted" && Boolean(waitingCategory?.value));
      }
      if (waitingCategory) {
        waitingCategory.required =
          selectedAction === "question_submitted" && Boolean(waitingReason?.value);
      }

      if (updateDefault && !summary.dataset.edited) {
        const pattern = action.selectedOptions[0]?.dataset.summary || "";
        const relatedAlias = related?.selectedOptions[0]?.dataset.alias || "related item";
        summary.value = pattern.replace("{related}", relatedAlias);
      }
    }

    action.addEventListener("change", () => updateActionFields(true));
    related?.addEventListener("change", () => updateActionFields(true));
    waitingCategory?.addEventListener("change", () => updateActionFields(false));
    waitingReason?.addEventListener("input", () => updateActionFields(false));
    bugScope?.addEventListener("change", () => updateActionFields(false));
    summary.addEventListener("input", () => {
      summary.dataset.edited = "true";
    });
    form.addEventListener("input", () => {
      root.dataset.dirty = "true";
    });
    form.addEventListener("change", () => {
      root.dataset.dirty = "true";
    });
    form.addEventListener("submit", (event) => {
      if (root.dataset.stale === "true") {
        event.preventDefault();
        const notice = root.querySelector("#milestone-refresh-notice");
        notice.hidden = false;
        notice.textContent = "Workflow state changed while this form was edited. Refresh the page and re-enter the action.";
      }
    });

    updateActionFields(false);
  }

  async function refresh() {
    const root = document.querySelector(rootSelector);
    if (!root || refreshing) return;
    refreshing = true;
    try {
      const response = await fetch(root.dataset.refreshUrl, {
        headers: { Accept: "text/html" },
        cache: "no-store",
      });
      if (response.status === 409) {
        root.dataset.stale = "true";
        const notice = root.querySelector("#milestone-refresh-notice");
        notice.hidden = false;
        notice.textContent = "Repository or milestone selection changed. Refresh this page before recording a workflow action.";
        return;
      }
      if (!response.ok) throw new Error(`Refresh returned HTTP ${response.status}.`);
      const markup = await response.text();
      const replacement = document.createElement("template");
      replacement.innerHTML = markup.trim();
      const nextRoot = replacement.content.querySelector(rootSelector);
      if (!nextRoot) throw new Error("Refresh response did not contain the workflow panel.");
      if (root.dataset.stale === "true") return;
      const notice = root.querySelector("#milestone-refresh-notice");
      notice.hidden = true;
      notice.textContent = "";
      if (root.dataset.stateVersion === nextRoot.dataset.stateVersion) return;
      if (root.dataset.dirty === "true") {
        root.dataset.stale = "true";
        const notice = root.querySelector("#milestone-refresh-notice");
        notice.hidden = false;
        notice.textContent = "Workflow state changed while this form was edited. Your entries are preserved; refresh the page before submitting.";
        return;
      }
      root.replaceWith(nextRoot);
      initialize(nextRoot);
    } catch (error) {
      const notice = root.querySelector("#milestone-refresh-notice");
      if (notice) {
        notice.hidden = false;
        notice.textContent = `Workflow refresh unavailable: ${error.message}`;
      }
    } finally {
      refreshing = false;
    }
  }

  function start() {
    const root = document.querySelector(rootSelector);
    if (!root) return;
    initialize(root);
    window.setInterval(refresh, 5000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
