(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const lang = ((tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.language_code) || "tr").slice(0, 2);

  const STRINGS = {
    tr: {
      tab_shop: "Mağaza", tab_library: "Satın aldıklarım", support: "Ödeme desteği", terms: "Koşullar",
      tagline: "Telegram Stars ile güvenli ödeme · anında teslim", search: "Ürün ara", all: "Tümü",
      outside: "Bu mağaza Telegram içinden açılmalı.", buy_for: "⭐ {n} ile satın al", owned: "Aktif", yours: "✓ Sende",
      monthly: "Aylık abonelik", closed: "Satış şu an kapalı.", no_match: "Aramana uyan ürün yok.",
      empty_library: "Henüz satın aldığın bir şey yok.", open: "Aç", share: "Paylaş",
      fact_instant: "Ödemeden hemen sonra burada açılır, bot mesajı olarak da gelir.",
      fact_sub: "30 günde bir otomatik yenilenir; Telegram ayarlarından istediğin zaman iptal edebilirsin.",
      fact_once: "Tek seferlik ödeme; içerik “Satın aldıklarım”da kalır.",
      fact_support: "Ödeme sorunu için /paysupport yaz.",
      fact_channel: "Ödemeden sonra özel kanala sadece sana ait tek kullanımlık davet linki gelir.",
      fact_channel_sub: "Abonelik bittiğinde kanal erişimi kapanır; yenilersen yeni link gelir.",
      channel_tag: "Özel kanal",
      paid: "Ödeme alındı 🎉", cancelled: "Ödeme iptal edildi.", failed: "Ödeme tamamlanamadı.",
      waiting: "Ödeme onaylanıyor…", slow: "Ödeme alındı; içerik birkaç dakika içinde bot üzerinden de gelecek.",
      error: "Bir şeyler ters gitti. Tekrar dene.", until: "Geçerlilik:", refunded: "İade edildi", expired: "Süresi doldu",
      share_text: "{title} · ⭐ {n}", per_month: "/ 30 gün", admin: "⚙️ Yönetim",
      foot_admin: "Yönetim paneli", foot_license: "Lisans yönetimi", foot_buy: "Sen de mağaza aç",
    },
    en: {
      tab_shop: "Shop", tab_library: "My purchases", support: "Payment support", terms: "Terms",
      tagline: "Secure payment with Telegram Stars · instant delivery", search: "Search products", all: "All",
      outside: "Please open this shop inside Telegram.", buy_for: "Buy for ⭐ {n}", owned: "Active", yours: "✓ Yours",
      monthly: "Monthly subscription", closed: "Sales are closed right now.", no_match: "No products match your search.",
      empty_library: "You haven't bought anything yet.", open: "Open", share: "Share",
      fact_instant: "Opens here right after payment and is also sent by the bot.",
      fact_sub: "Renews every 30 days; cancel anytime in Telegram settings.",
      fact_once: "One-time payment; the content stays in “My purchases”.",
      fact_support: "Payment problem? Send /paysupport.",
      fact_channel: "After payment you get a personal one-time invite to the private channel.",
      fact_channel_sub: "Channel access ends with the subscription; renewing sends a new invite.",
      channel_tag: "Private channel",
      paid: "Payment received 🎉", cancelled: "Payment cancelled.", failed: "Payment failed.",
      waiting: "Confirming payment…", slow: "Payment received; the bot will also send your content in a few minutes.",
      error: "Something went wrong. Please try again.", until: "Valid until:", refunded: "Refunded", expired: "Expired",
      share_text: "{title} · ⭐ {n}", per_month: "/ 30 days", admin: "⚙️ Admin",
      foot_admin: "Admin panel", foot_license: "License", foot_buy: "Open your own shop",
    },
  };
  const t = (key, vars = {}) => {
    const text = (STRINGS[lang] || STRINGS.tr)[key] || STRINGS.tr[key] || key;
    return text.replace(/\{(\w+)\}/g, (_, name) => (name in vars ? String(vars[name]) : ""));
  };

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // Single shop: Mini App at /app, API at /api. Hub: each tenant shop under /s/<bot>/ (API at /s/<bot>/api).
  const BASE = (location.pathname.match(/^\/s\/[^/]+/) || [""])[0];
  let config = { support: "", terms: "" };
  let products = [];
  let owned = new Set();
  let filter = { text: "", category: "" };
  let current = null;          // product shown in the detail view
  let view = "shop";
  let returnTo = "shop";       // where the back button goes from detail/content

  // ---------------------------------------------------------------- api
  async function api(path, options = {}) {
    const response = await fetch(BASE + path, {
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

  function haptic(kind) {
    if (!tg || !tg.HapticFeedback) return;
    kind === "select" ? tg.HapticFeedback.selectionChanged() : tg.HapticFeedback.impactOccurred(kind || "light");
  }

  function show(name) {
    view = name;
    for (const v of ["shop", "detail", "library", "content"]) $(`view-${v}`).hidden = v !== name;
    const tab = name === "detail" ? "shop" : name === "content" ? "library" : name;
    document.querySelectorAll(".tab").forEach((node) => {
      const active = node.dataset.tab === tab;
      node.classList.toggle("active", active);
      node.setAttribute("aria-selected", String(active));
    });
    if (tg) (name === "detail" || name === "content") ? tg.BackButton.show() : tg.BackButton.hide();
    if (name !== "detail") hideMainButton();
    window.scrollTo(0, 0);
  }

  function openLink(url) {
    if (!tg) { window.open(url, "_blank", "noopener"); return; }
    url.startsWith("https://t.me/") ? tg.openTelegramLink(url) : tg.openLink(url);
  }

  // Render text safely; only https links become clickable.
  function renderContent(container, text) {
    container.replaceChildren();
    for (const part of text.split(/(https:\/\/[^\s<>"]+)/g)) {
      if (/^https:\/\/[^\s<>"]+$/.test(part)) {
        const link = el("a", "", part);
        link.href = part;
        link.addEventListener("click", (event) => { event.preventDefault(); openLink(part); });
        container.append(link);
      } else if (part) {
        container.append(document.createTextNode(part));
      }
    }
  }

  function formatDate(seconds) {
    return new Date(seconds * 1000).toLocaleDateString(lang === "tr" ? "tr-TR" : "en-GB", { day: "numeric", month: "long", year: "numeric" });
  }

  // Image, or a colourful placeholder with the product's initial when there is no photo.
  function picture(product) {
    if (product.photo_url) {
      const img = el("img");
      img.src = product.photo_url;
      img.alt = "";
      img.loading = "lazy";
      img.addEventListener("error", () => img.replaceWith(placeholder(product)), { once: true });
      return img;
    }
    return placeholder(product);
  }

  function placeholder(product) {
    let hash = 0;
    for (const ch of product.id) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
    const node = el("div", "ph", (product.title.trim()[0] || "⭐").toUpperCase());
    node.style.background = `linear-gradient(135deg, hsl(${hash} 70% 55%), hsl(${(hash + 40) % 360} 75% 42%))`;
    return node;
  }

  // ---------------------------------------------------------------- shop
  async function loadProducts() {
    const container = $("products");
    container.replaceChildren(...Array.from({ length: 4 }, () => el("div", "skeleton")));
    try {
      products = await api("/api/products");
    } catch (error) {
      container.replaceChildren(el("div", "empty", error.message));
      return;
    }
    renderTools();
    renderProducts();
  }

  function renderTools() {
    const categories = [...new Set(products.map((p) => p.category).filter(Boolean))];
    $("search").hidden = products.length < 6;
    $("chips").hidden = categories.length < 2;
    $("tools").hidden = $("search").hidden && $("chips").hidden;
    if (!categories.includes(filter.category)) filter.category = "";
    $("chips").replaceChildren(...["", ...categories].map((name) => {
      const chip = el("button", "chip" + (name === filter.category ? " active" : ""), name || t("all"));
      chip.addEventListener("click", () => {
        haptic("select");
        filter.category = name;
        renderTools();
        renderProducts();
      });
      return chip;
    }));
  }

  function renderProducts() {
    const container = $("products");
    if (!products.length) {
      container.replaceChildren(el("div", "empty", t("closed")));
      return;
    }
    const query = filter.text.toLocaleLowerCase(lang);
    const visible = products.filter((p) =>
      (!filter.category || p.category === filter.category) &&
      (!query || `${p.title} ${p.description} ${p.category || ""}`.toLocaleLowerCase(lang).includes(query)));
    container.replaceChildren(...(visible.length ? visible.map(productCard) : [el("div", "empty", t("no_match"))]));
  }

  function productCard(product) {
    const card = el("button", "card product");
    card.type = "button";
    const thumb = el("div", "thumb");
    thumb.append(picture(product));
    if (product.badge) thumb.append(el("span", "badge solid flag", product.badge));
    const info = el("div", "info");
    info.append(el("h3", "", product.title), el("p", "", product.description));
    const row = el("div", "row");
    row.append(el("span", "price", String(product.stars)));
    if (owned.has(product.id)) row.append(el("span", "mine", t("yours")));
    else if (product.kind === "subscription") row.append(el("span", "badge", t("per_month")));
    info.append(row);
    card.append(thumb, info);
    card.addEventListener("click", () => { haptic("light"); openDetail(product); });
    return card;
  }

  // ---------------------------------------------------------------- detail
  function openDetail(product) {
    current = product;
    const isOwned = owned.has(product.id);
    $("detail-hero").replaceChildren(picture(product));
    const tags = [];
    if (product.badge) tags.push(el("span", "badge solid", product.badge));
    if (product.category) tags.push(el("span", "badge", product.category));
    if (product.kind === "subscription") tags.push(el("span", "badge", t("monthly")));
    if (product.channel) tags.push(el("span", "badge", t("channel_tag")));
    if (isOwned) tags.push(el("span", "badge ok", t("yours")));
    $("detail-tags").replaceChildren(...tags);
    $("detail-title").textContent = product.title;
    $("detail-price").textContent = String(product.stars);
    $("detail-desc").textContent = product.description;
    const facts = [["⚡", t("fact_instant")], [product.kind === "subscription" ? "🔁" : "📦",
      t(product.kind === "subscription" ? "fact_sub" : "fact_once")]];
    if (product.channel) {
      facts.push(["📣", t("fact_channel")]);
      if (product.kind === "subscription") facts.push(["🔒", t("fact_channel_sub")]);
    }
    facts.push(["💬", t("fact_support")]);
    $("detail-facts").replaceChildren(...facts.map(([icon, text]) => {
      const li = el("li");
      li.append(el("span", "", icon), el("span", "", text));
      return li;
    }));
    const label = isOwned ? t("open") : t("buy_for", { n: product.stars });
    $("detail-buy").textContent = label;
    $("detail-buy").disabled = false;
    $("detail-share").hidden = !config.bot_username;
    returnTo = view === "detail" ? returnTo : (view === "library" ? "library" : "shop");
    show("detail");
    showMainButton(label);
  }

  function detailAction() {
    if (!current) return;
    if (owned.has(current.id)) {
      openOwned(current.id);
    } else {
      buy(current);
    }
  }

  // Telegram's native bottom button mirrors the in-page one; the in-page one stays for desktop clients.
  function showMainButton(label) {
    if (!tg || !tg.MainButton) return;
    tg.MainButton.setParams({ text: label, is_active: true, is_visible: true });
  }
  function hideMainButton() {
    if (tg && tg.MainButton) tg.MainButton.hide();
  }
  function busy(on) {
    $("detail-buy").disabled = on;
    if (tg && tg.MainButton) on ? tg.MainButton.showProgress(false) : tg.MainButton.hideProgress();
  }

  function share() {
    if (!current || !config.bot_username) return;
    const link = `https://t.me/${config.bot_username}?start=p_${current.id}`;
    const text = t("share_text", { title: current.title, n: current.stars });
    openLink(`https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent(text)}`);
  }

  // ---------------------------------------------------------------- library
  let library = [];

  async function loadLibrary() {
    library = await api("/api/purchases");
    owned = new Set(library.filter((i) => i.status === "paid").map((i) => i.product));
    $("library-count").textContent = String(owned.size);
    $("library-count").hidden = owned.size === 0;
    return library;
  }

  async function showLibrary() {
    show("library");
    const container = $("library");
    container.replaceChildren(el("div", "skeleton"));
    try {
      await loadLibrary();
    } catch (error) {
      container.replaceChildren(el("div", "empty", error.message));
      return;
    }
    container.replaceChildren(...(library.length ? library.map(libraryItem) : [el("div", "empty", t("empty_library"))]));
  }

  function libraryItem(item) {
    const card = el("article", "card");
    card.append(el("h3", "", item.title));
    const row = el("div", "row");
    if (item.status === "paid") {
      row.append(el("span", "badge ok", item.access_until ? `${t("until")} ${formatDate(item.access_until)}` : t("owned")));
      const open = el("button", "btn", t("open"));
      open.addEventListener("click", () => { returnTo = "library"; openContent(item); });
      row.append(open);
    } else {
      row.append(el("span", "badge off", t(item.status === "expired" ? "expired" : "refunded")));
    }
    card.append(row);
    return card;
  }

  async function openOwned(productId) {
    let item = library.find((i) => i.product === productId && i.status === "paid");
    if (!item) {
      await loadLibrary().catch(() => {});
      item = library.find((i) => i.product === productId && i.status === "paid");
    }
    if (item) openContent(item); else showLibrary();
  }

  function openContent(item) {
    $("content-title").textContent = item.title;
    $("content-meta").textContent = item.access_until ? `${t("until")} ${formatDate(item.access_until)}` : "";
    renderContent($("content-body"), item.content || "");
    show("content");
  }

  // ---------------------------------------------------------------- purchase
  async function buy(product) {
    if (!tg) return;
    busy(true);
    haptic("medium");
    try {
      const { order_id: orderId, invoice_link: link } = await api("/api/orders", {
        method: "POST", body: JSON.stringify({ product_id: product.id }),
      });
      tg.openInvoice(link, async (status) => {
        busy(false);
        if (status === "paid") {
          tg.HapticFeedback && tg.HapticFeedback.notificationOccurred("success");
          toast(t("waiting"));
          await waitForOrder(orderId);
        } else if (status === "cancelled") {
          toast(t("cancelled"));
        } else if (status === "failed") {
          tg.HapticFeedback && tg.HapticFeedback.notificationOccurred("error");
          toast(t("failed"));
        }
      });
    } catch (error) {
      busy(false);
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
          await loadLibrary().catch(() => {});
          renderProducts();
          returnTo = "library";
          openContent(order);
          return;
        }
      } catch (_) { /* retry */ }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    toast(t("slow"));
  }

  // ---------------------------------------------------------------- boot
  function goBack() {
    if (returnTo === "library") showLibrary(); else show("shop");
  }

  // Deep link: /app?p=<id> (sent by the bot for t.me/<bot>?start=p_<id>) or startapp=p_<id>.
  function requestedProduct() {
    const fromQuery = new URLSearchParams(location.search).get("p");
    const start = tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param;
    const id = fromQuery || (start && start.startsWith("p_") ? start.slice(2) : "");
    return id ? products.find((p) => p.id === id) : null;
  }

  async function boot() {
    document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => { node.placeholder = t(node.dataset.i18nPlaceholder); });
    document.documentElement.lang = lang;
    if (!tg || !tg.initData) {
      $("view-shop").hidden = true;
      $("outside").hidden = false;
      return;
    }
    tg.ready();
    tg.expand();
    tg.BackButton.onClick(goBack);
    tg.MainButton.onClick(detailAction);
    $("detail-buy").addEventListener("click", detailAction);
    $("detail-share").addEventListener("click", share);
    document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
      haptic("select");
      tab.dataset.tab === "library" ? showLibrary() : show("shop");
    }));
    $("search").addEventListener("input", (event) => { filter.text = event.target.value.trim(); renderProducts(); });
    $("support").addEventListener("click", () => tg.showAlert(config.support || t("error")));
    $("terms").addEventListener("click", () => tg.showAlert(config.terms || t("error")));

    try {
      config = await api("/api/config");
      $("shop-name").textContent = config.shop_name;
      $("shop-logo").textContent = (config.shop_name.trim()[0] || "⭐").toUpperCase();
      document.title = config.shop_name;
      const safe = (url) => typeof url === "string" && /^https:\/\//.test(url);
      const footLink = (item, button, url) => {
        $(button).addEventListener("click", () => openLink(url));
        $(item).hidden = false;
      };
      const isAdmin = safe(config.admin_url);   // the server sends it only to the shop's admins
      if (isAdmin) {
        $("admin").hidden = false;
        $("admin").addEventListener("click", () => openLink(config.admin_url));
        footLink("fi-admin", "foot-admin", config.admin_url);
        if (safe(config.license_url)) footLink("fi-license", "foot-license", config.license_url);
      }
      if (config.buy && safe(config.buy.url)) {
        $("buy-text").textContent = config.buy.text;
        $("buy-banner").hidden = false;
        $("buy-banner").addEventListener("click", () => { haptic("light"); openLink(config.buy.url); });
        if (!isAdmin) footLink("fi-buy", "foot-buy", config.buy.url);
      }
      if (config.powered_by && /^https:\/\//.test(config.powered_by.url)) {
        const link = el("button", "link", config.powered_by.text);
        link.addEventListener("click", () => openLink(config.powered_by.url));
        $("powered").replaceChildren(link);
        $("powered").hidden = false;
      }
    } catch (_) { /* keep defaults */ }
    await loadLibrary().catch(() => {});
    show("shop");
    await loadProducts();
    const wanted = requestedProduct();
    if (wanted) openDetail(wanted);
  }

  boot();
})();
