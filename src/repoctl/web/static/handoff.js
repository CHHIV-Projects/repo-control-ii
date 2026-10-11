"use strict";

const copyButton = document.getElementById("copy-handoff-packet");
const packetField = document.getElementById("handoff-packet");
const copyStatus = document.getElementById("handoff-copy-status");

if (copyButton && packetField && copyStatus) {
  copyButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(packetField.value);
      copyStatus.textContent = "Packet copied.";
    } catch {
      packetField.focus();
      packetField.select();
      copyStatus.textContent = "Clipboard unavailable; packet text is selected for manual copying.";
    }
  });
}
