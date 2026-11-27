(() => {
  "use strict";

  async function enableCheckout() {
    const response = await fetch("./stars-checkout.json", { cache: "no-store" });
    if (!response.ok) return;
    const { pro_invoice_url: invoiceUrl } = await response.json();
    // Accept only a Telegram invoice link, never a chat, redirect, or generic URL.
    if (typeof invoiceUrl !== "string" || !/^https:\/\/t\.me\/\$[A-Za-z0-9_-]+$/.test(invoiceUrl)) return;

    for (const link of document.querySelectorAll("[data-stars-checkout]")) {
      link.href = invoiceUrl;
      link.hidden = false;
    }
  }

  enableCheckout().catch(() => {
    // Leave checkout hidden when no valid public invoice is configured.
  });
})();
