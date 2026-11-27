(() => {
  "use strict";

  async function enableCheckout() {
    const response = await fetch("./stars-checkout.json", { cache: "no-store" });
    if (!response.ok) return;
    const config = await response.json();
    const username = config.pro_bot_username;
    let checkoutUrl;
    if (typeof username === "string" && /^[A-Za-z][A-Za-z0-9_]{1,28}bot$/i.test(username)) {
      checkoutUrl = "https://t.me/" + username + "?start=lisans";
    } else if (typeof config.pro_invoice_url === "string" &&
               /^https:\/\/t\.me\/\$[A-Za-z0-9_-]+$/.test(config.pro_invoice_url)) {
      checkoutUrl = config.pro_invoice_url;
    } else {
      return;
    }

    for (const link of document.querySelectorAll("[data-stars-checkout]")) {
      link.href = checkoutUrl;
      link.hidden = false;
    }
  }

  enableCheckout().catch(() => {
    // Leave checkout hidden when no valid public invoice is configured.
  });
})();
