(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const lang = ((tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.language_code) || "tr").slice(0, 2);

  const STRINGS = {
    tr: {
      tab_shop: "Mağaza", tab_library: "Satın aldıklarım", back: "‹ Geri", support: "Ödeme desteği", terms: "Koşullar",
      outside: "Bu mağaza Telegram içinden açılmalı.", buy: "Satın al", owned: "Aktif", monthly: "Aylık abonelik",
      closed: "Satış şu an kapalı.", empty_library: "Henüz satın aldığın bir şey yok.", open: "Aç",
      paid: "Ödeme alındı 🎉", cancelled: "Ödeme iptal edildi.", failed: "Ödeme tamamlanamadı.",
      waiting: "Ödeme onaylanıyor…", slow: "Ödeme alındı; içerik birkaç dakika içinde bot üzerinden de gelecek.",
      error: "Bir şeyler ters gitti. Tekrar dene.", until: "Geçerlilik:", refunded: "İade edildi", expired: "Süresi doldu",
    },
    en: {
      tab_shop: "Shop", tab_library: "My purchases", back: "‹ Back", support: "Payment support", terms: "Terms",
      outside: "Please open this shop inside Telegram.", buy: "Buy", owned: "Active", monthly: "Monthly subscription",
      closed: "Sales are closed right now.", empty_library: "You haven't bought anything yet.", open: "Open",
      paid: "Payment received 🎉", cancelled: "Payment cancelled.", failed: "Payment failed.",
      waiting: "Confirming payment…", slow: "Payment received; the bot will also send your content in a few minutes.",
      error: "Something went wrong. Please try again.", until: "Valid until:", refunded: "Refunded", expired: "Expired",
    },
  };
  const t = (key) => (STRINGS[lang] || STRINGS.tr)[key] || STRINGS.tr[key] || key;

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  let config = { support: "", terms: "" };
  let owned = new Set();

  // ---------------------------------------------------------------- api
  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": tg ? tg.initData : "", ...(options.headers || {}) },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || t("error"));
    return body;
  }

  // ---------------------------------------------------------------- ui helpers
  function toast(message) {
    const node = $("toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.hidden = true; }, 3200);
  }

  function show(view) {
    for (const name of ["shop", "library", "content"]) $(`view-${name}`).hidden = name !== view;
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.dataset.tab === view;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    if (tg) {
      view === "content" ? tg.BackButton.show() : tg.BackButton.hide();
    }
  }

  function skeletons(container, count) {
    container.replaceChildren(...Array.from({ length: count }, () => el("div", "skeleton")));
  }

  // Render text safely; only https links become clickable.
  function renderContent(container, text) {
    container.replaceChildren();
    for (const part of text.split(/(https:\/\/[^\s<>"]+)/g)) {
      if (/^https:\/\/[^\s<>"]+$/.test(part)) {
        const link = el("a", "", part);
        link.href = part;
        link.addEventListener("click", (event) => {
          if (!tg) return;
          event.preventDefault();
          part.startsWith("https://t.me/") ? tg.openTelegramLink(part) : tg.openLink(part);
        });
        container.append(link);
      } else if (part) {
        container.append(document.createTextNode(part));
      }
    }
  }

  function formatDate(seconds) {
    return new Date(seconds * 1000).toLocaleDateString(lang === "tr" ? "tr-TR" : "en-GB", { day: "numeric", month: "long", year: "numeric" });
  }

  // ---------------------------------------------------------------- views
  async function loadProducts() {
    const container = $("products");
    skeletons(container, 3);
    try {
      const products = await api("/api/products");
      if (!products.length) {
        container.replaceChildren(el("div", "empty", t("closed")));
        return;
      }
      container.replaceChildren(...products.map(productCard));
    } catch (error) {
      container.replaceChildren(el("div", "empty", error.message));
    }
  }

  function productCard(product) {
    const card = el("article", "card");
    if (product.photo_url) {
      const img = el("img");
      img.src = product.photo_url;
      img.alt = "";
      img.loading = "lazy";
      card.append(img);
    }
    card.append(el("h3", "", product.title), el("p", "", product.description));
    if (product.kind === "subscription") card.append(el("span", "badge", t("monthly")));
    const row = el("div", "row");
    row.append(el("span", "price", String(product.stars)));
    // Already owned: open the library instead of charging the buyer twice.
    const isOwned = owned.has(product.id);
    const button = el("button", "btn", isOwned ? t("open") : t("buy"));
    button.addEventListener("click", () => {
      if (isOwned) {
        show("library");
        loadLibrary();
      } else {
        buy(product, button);
      }
    });
    row.append(button);
    card.append(row);
    return card;
  }

  async function loadLibrary() {
    const container = $("library");
    skeletons(container, 2);
    try {
      const items = await api("/api/purchases");
      owned = new Set(items.filter((i) => i.status === "paid").map((i) => i.product));
      if (!items.length) {
        container.replaceChildren(el("div", "empty", t("empty_library")));
        return;
      }
      container.replaceChildren(...items.map(libraryItem));
    } catch (error) {
      container.replaceChildren(el("div", "empty", error.message));
    }
  }

  function libraryItem(item) {
    const card = el("article", "card");
    card.append(el("h3", "", item.title));
    const row = el("div", "row");
    if (item.status === "paid") {
      row.append(el("span", "badge", item.access_until ? `${t("until")} ${formatDate(item.access_until)}` : t("owned")));
      const open = el("button", "btn", t("open"));
      open.addEventListener("click", () => openContent(item));
      row.append(open);
    } else {
      row.append(el("span", "badge off", t(item.status === "expired" ? "expired" : "refunded")));
    }
    card.append(row);
    return card;
  }

  function openContent(item) {
    $("content-title").textContent = item.title;
    $("content-meta").textContent = item.access_until ? `${t("until")} ${formatDate(item.access_until)}` : "";
    renderContent($("content-body"), item.content || "");
    show("content");
  }

  // ---------------------------------------------------------------- purchase
  async function buy(product, button) {
    if (!tg) return;
    button.disabled = true;
    tg.HapticFeedback && tg.HapticFeedback.impactOccurred("light");
    try {
      const { order_id: orderId, invoice_link: link } = await api("/api/orders", {
        method: "POST", body: JSON.stringify({ product_id: product.id }),
      });
      tg.openInvoice(link, async (status) => {
        button.disabled = false;
        if (status === "paid") {
          tg.HapticFeedback && tg.HapticFeedback.notificationOccurred("success");
          toast(t("waiting"));
          await waitForOrder(orderId);
        } else if (status === "cancelled") {
          toast(t("cancelled"));
        } else if (status === "failed") {
          toast(t("failed"));
        }
      });
    } catch (error) {
      button.disabled = false;
      toast(error.message);
    }
  }

  // The webhook may land a moment after the client sees "paid": poll briefly.
  async function waitForOrder(orderId) {
    for (let attempt = 0; attempt < 10; attempt += 1) {
      try {
        const order = await api(`/api/orders/${encodeURIComponent(orderId)}`);
        if (order.status === "paid") {
          toast(t("paid"));
          owned.add(order.product);
          loadProducts();
          openContent(order);
          return;
        }
      } catch (_) { /* retry */ }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    toast(t("slow"));
  }

  // ---------------------------------------------------------------- boot
  async function boot() {
    document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
    document.documentElement.lang = lang;
    if (!tg || !tg.initData) {
      $("view-shop").hidden = true;
      $("outside").hidden = false;
      return;
    }
    tg.ready();
    tg.expand();
    tg.BackButton.onClick(() => show("library"));
    $("back").addEventListener("click", () => show("library"));
    document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
      show(tab.dataset.tab);
      if (tab.dataset.tab === "library") loadLibrary();
    }));
    $("support").addEventListener("click", () => tg.showAlert(config.support || t("error")));
    $("terms").addEventListener("click", () => tg.showAlert(config.terms || t("error")));

    try {
      config = await api("/api/config");
      $("shop-name").textContent = config.shop_name;
      document.title = config.shop_name;
    } catch (_) { /* keep defaults */ }
    await loadLibrary().catch(() => {});
    show("shop");
    await loadProducts();
  }

  boot();
})();
