"use strict";

(() => {
  const allowedSurfaces = new Set(["textarea", "contenteditable", "iframe", "delayed"]);
  const parameters = new URLSearchParams(window.location.search);
  const runId = parameters.get("run_id");
  const surface = parameters.get("surface");
  const root = document.querySelector('[data-testid="draftbox"]');
  const save = document.querySelector('[data-testid="save-draft"]');
  const status = document.querySelector('[data-testid="fixture-status"]');
  const container = document.getElementById("composer-container");
  const recipient = document.querySelector('[data-testid="recipient"]');
  const account = document.querySelector('[data-testid="account"]');
  const counter = document.querySelector('[data-testid="save-count"]');
  const savedContent = document.querySelector('[data-testid="saved-content"]');
  const savedRecipient = document.querySelector('[data-testid="saved-recipient"]');
  const savedAccount = document.querySelector('[data-testid="saved-account"]');
  let editor = null;
  let saveCount = 0;
  let authorization = null;

  const editorText = () => surface === "contenteditable" ? editor?.innerText : editor?.value;
  const matches = grant => Boolean(grant && editor &&
    grant.tab_id === root.dataset.runId &&
    grant.page_epoch === root.dataset.pageEpoch &&
    grant.surface === surface &&
    grant.account === account.textContent &&
    grant.recipient === recipient.value &&
    grant.save_count === saveCount &&
    typeof grant.text === "string" && grant.text.length <= 8000 &&
    grant.text === editorText());

  // The owned fixture participates in the transaction: an event-time target
  // change is rejected at the actual commit boundary. This is not a guarantee
  // that arbitrary third-party sites provide an equivalent atomic operation.
  Object.defineProperty(window, "G1DraftBox", {value: Object.freeze({
    armSave(grant) {
      authorization = null;
      root.dataset.saveRejected = "false";
      if (!matches(grant)) return false;
      authorization = Object.freeze({...grant});
      return true;
    }
  }), writable: false, configurable: false});

  // Invalid fixture arguments never become an enabled composer. All caller
  // text is assigned through DOM text/value properties, never interpreted.
  if (!runId || !allowedSurfaces.has(surface) || parameters.getAll("run_id").length !== 1 ||
      parameters.getAll("surface").length !== 1 || [...parameters.keys()].length !== 2) {
    status.textContent = "Invalid owned-fixture parameters; save is disabled.";
    return;
  }
  root.dataset.runId = runId;
  root.dataset.surface = surface;
  root.dataset.pageEpoch = crypto.randomUUID();
  document.querySelector('[data-testid="surface-label"]').textContent = surface;

  const ready = () => {
    if (!editor) return;
    if (surface !== "contenteditable") editor.disabled = false;
    save.disabled = false;
    root.dataset.ready = "true";
    status.textContent = "Ready — synthetic draft only";
  };

  if (surface === "iframe") {
    const frame = document.createElement("iframe");
    frame.dataset.testid = "composer-frame";
    frame.title = "Owned synthetic draft composer";
    frame.src = "/fixtures/assets/frame.html";
    frame.addEventListener("load", () => {
      // This is our one fixed, same-origin frame; no URL or selector comes
      // from a caller, page text, or the content of a draft.
      editor = frame.contentDocument.querySelector('[data-testid="composer"]');
      ready();
    }, { once: true });
    container.append(frame);
  } else {
    editor = document.createElement(surface === "contenteditable" ? "div" : "textarea");
    editor.dataset.testid = "composer";
    editor.setAttribute("aria-label", "Synthetic draft text");
    editor.setAttribute("aria-describedby", "compose-help");
    editor.spellcheck = false;
    if (surface === "contenteditable") {
      editor.contentEditable = "true";
      editor.setAttribute("role", "textbox");
      editor.setAttribute("aria-multiline", "true");
    } else {
      editor.rows = 7;
      editor.disabled = surface === "delayed";
    }
    container.append(editor);
    if (surface === "delayed") {
      status.textContent = "Deterministic fixture delay — enabling composer…";
      window.setTimeout(ready, 250);
    } else {
      ready();
    }
  }

  save.addEventListener("click", () => {
    if (root.dataset.ready !== "true" || !editor || save.disabled) return;
    const grant = authorization;
    authorization = null; // One use even if the page changed during pointerdown/click.
    if (!matches(grant)) {
      root.dataset.saveRejected = "true";
      status.textContent = "Rejected: stale or missing owned-fixture authorization";
      return;
    }
    const text = editorText();
    savedContent.textContent = text;
    savedRecipient.textContent = recipient.value;
    savedAccount.textContent = account.textContent;
    saveCount += 1;
    counter.textContent = String(saveCount);
    status.textContent = "Saved locally — no messages sent";
  });
})();
