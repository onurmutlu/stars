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
      days_tag: "{n} gün erişim", per_days: "/ {n} gün",
      fact_days: "Tek ödeme, {n} gün erişim. Süre bitince istersen yeniden alırsın; otomatik çekim yok.",
      fact_file: "Dosya (PDF, video…) ödemeden hemen sonra bot sohbetine gelir.",
      promo_placeholder: "İndirim kodu", promo_apply: "Uygula", promo_ok: "{code} uygulandı: %{p} indirim", promo_ready: "🎁 {code} kodu hazır: ürünü seçince uygulanır.",
      paid: "Ödeme alındı 🎉", cancelled: "Ödeme iptal edildi.", failed: "Ödeme tamamlanamadı.",
      waiting: "Ödeme onaylanıyor…", slow: "Ödeme alındı; içerik birkaç dakika içinde bot üzerinden de gelecek.",
      error: "Bir şeyler ters gitti. Tekrar dene.", until: "Geçerlilik:", refunded: "İade edildi", expired: "Süresi doldu",
      share_text: "{title} · ⭐ {n}", per_month: "/ 30 gün", admin: "⚙️ Yönetim",
      foot_admin: "Yönetim paneli", foot_license: "Lisans yönetimi", foot_buy: "Sen de mağaza aç",
      from_price: "{n}+", n_options: "{n} seçenek",
      bundle_tag: "Paket", fact_bundle: "Pakette: {list}. Ayrı ayrı ⭐ {n}; paketle ⭐ {save} kazanırsın.",
      fact_bundle_plain: "Pakette: {list}. Hepsi tek ödemeyle açılır.",
      left_n: "Son {n}", sold_out: "Tükendi", fact_left: "Sınırlı sayıda: {n} adet kaldı.",
      ends_in: "⏰ {when} kaldı", fact_ends: "Satış {date} tarihinde kapanır.", days_short: "{n} gün", hours_short: "{n} saat",
      invite_title: "🎁 Arkadaşını davet et", invite_text: "Linkinle gelen biri ilk alışverişini yapınca sana %{p} indirim kodu gelir.",
      gift_buy: "🎁 Hediye et", gift_tag: "🎁 Hediye", gift_open: "Açıldı ✓", gift_wait: "Henüz açılmadı",
      gift_share: "Hediye linkini gönder", gift_msg: "Sana bir hediyem var 🎁 {title}", gift_ready: "🎁 Hediye linkin hazır; bot sohbetine de gönderildi.",
      gift_cancel: "İptal edildi",
      wait_join: "🔔 Gelince haber ver", wait_ok: "Listedesin; yeniden satışa çıkınca bot sana yazacak.",
      reviews_title: "Yorumlar", review_placeholder: "Kısaca deneyimin (isteğe bağlı)", review_send: "Gönder",
      review_thanks: "Teşekkürler! Puanın kaydedildi.", review_pick: "Önce yıldız seç.", review_none: "Henüz yorum yok.",
      review_count: "{n} değerlendirme", review_mine: "Puanını güncelleyebilirsin:",
      invite_share: "Davet linkini paylaş", invite_msg: "{shop} · Telegram içinde Stars ile alışveriş",
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
      days_tag: "{n}-day access", per_days: "/ {n} days",
      fact_days: "One payment, {n} days of access. Buy again when it ends; no automatic charge.",
      fact_file: "The file (PDF, video…) arrives in the bot chat right after payment.",
      promo_placeholder: "Discount code", promo_apply: "Apply", promo_ok: "{code} applied: {p}% off", promo_ready: "🎁 Code {code} is ready: pick a product to use it.",
      paid: "Payment received 🎉", cancelled: "Payment cancelled.", failed: "Payment failed.",
      waiting: "Confirming payment…", slow: "Payment received; the bot will also send your content in a few minutes.",
      error: "Something went wrong. Please try again.", until: "Valid until:", refunded: "Refunded", expired: "Expired",
      share_text: "{title} · ⭐ {n}", per_month: "/ 30 days", admin: "⚙️ Admin",
      foot_admin: "Admin panel", foot_license: "License", foot_buy: "Open your own shop",
      from_price: "from {n}", n_options: "{n} options",
      bundle_tag: "Bundle", fact_bundle: "Includes: {list}. Separately ⭐ {n}; you save ⭐ {save}.",
      fact_bundle_plain: "Includes: {list}. All unlocked with one payment.",
      left_n: "{n} left", sold_out: "Sold out", fact_left: "Limited: {n} left.",
      ends_in: "⏰ {when} left", fact_ends: "Sale closes on {date}.", days_short: "{n} days", hours_short: "{n} hours",
      invite_title: "🎁 Invite a friend", invite_text: "When someone from your link makes a first purchase, you get a {p}% discount code.",
      gift_buy: "🎁 Send as a gift", gift_tag: "🎁 Gift", gift_open: "Opened ✓", gift_wait: "Not opened yet",
      gift_share: "Send the gift link", gift_msg: "I have a gift for you 🎁 {title}", gift_ready: "🎁 Your gift link is ready; it was also sent in the bot chat.",
      gift_cancel: "Cancelled",
      wait_join: "🔔 Notify me", wait_ok: "You're on the list; the bot will message you when it's back.",
      reviews_title: "Reviews", review_placeholder: "Your experience (optional)", review_send: "Send",
      review_thanks: "Thanks! Your rating is saved.", review_pick: "Pick the stars first.", review_none: "No reviews yet.",
      review_count: "{n} reviews", review_mine: "You can update your rating:",
      invite_share: "Share your invite link", invite_msg: "{shop} · shop with Stars inside Telegram",
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
  let promoCode = "";          // code typed or brought by a campaign link
  let quote = null;            // price of the current product with promoCode

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
      img.src = /^https:\/\//.test(product.photo_url) ? product.photo_url : `${BASE}/${product.photo_url}`;
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
  // limited stock, sale deadline and bundle contents
  const byId = (id) => products.find((p) => p.id === id);
  function timeLeft(until) {
    const seconds = until - Date.now() / 1000;
    if (seconds <= 0) return null;
    return seconds >= 86400 ? t("days_short", { n: Math.ceil(seconds / 86400) }) : t("hours_short", { n: Math.max(1, Math.ceil(seconds / 3600)) });
  }
  function scarcity(product) {
    if (product.sold_out) return el("span", "badge off", t("sold_out"));
    if (product.left != null && product.left <= 10) return el("span", "badge hot", t("left_n", { n: product.left }));
    const left = product.sale_until && timeLeft(product.sale_until);
    if (left && product.sale_until - Date.now() / 1000 < 7 * 86400) return el("span", "badge hot", t("ends_in", { when: left }));
    return null;
  }
  function bundleFact(product) {
    const parts = (product.includes || []).map(byId).filter(Boolean);
    const names = parts.map((p) => p.title);
    if (!parts.length) return null;
    const total = parts.reduce((sum, p) => sum + p.stars, 0);
    return total > product.stars
      ? t("fact_bundle", { list: names.join(", "), n: total, save: total - product.stars })
      : t("fact_bundle_plain", { list: names.join(", ") });
  }

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
    // options of one product (same group) share a card: the cheapest one stands for the group
    const cards = [];
    const seen = new Map();
    for (const p of visible) {
      if (!p.group) { cards.push(p); continue; }
      if (!seen.has(p.group)) { seen.set(p.group, cards.length); cards.push(p); continue; }
      const index = seen.get(p.group);
      if (p.stars < cards[index].stars) cards[index] = p;
    }
    container.replaceChildren(...(cards.length ? cards.map(productCard) : [el("div", "empty", t("no_match"))]));
  }

  const optionsOf = (product) => product.group ? products.filter((p) => p.group === product.group) : [product];

  function productCard(product) {
    const card = el("button", "card product");
    card.type = "button";
    const thumb = el("div", "thumb");
    thumb.append(picture(product));
    if (product.badge) thumb.append(el("span", "badge solid flag", product.badge));
    else if ((product.includes || []).length) thumb.append(el("span", "badge solid flag", t("bundle_tag")));
    if (product.sold_out) card.classList.add("sold");
    const info = el("div", "info");
    const options = optionsOf(product);
    const many = options.length > 1;
    info.append(el("h3", "", many ? product.group : product.title), el("p", "", product.description));
    if (product.reviews) info.append(el("span", "rating", `★ ${product.rating} (${product.reviews})`));
    const row = el("div", "row");
    const price = el("span", "price-wrap");
    price.append(el("span", "price", many && options.some((o) => o.stars !== product.stars)
      ? t("from_price", { n: product.stars }) : String(product.stars)));
    if (product.compare_at && !many) price.append(el("s", "was", String(product.compare_at)));
    row.append(price);
    if (options.some((o) => owned.has(o.id))) row.append(el("span", "mine", t("yours")));
    else if (many) row.append(el("span", "badge", t("n_options", { n: options.length })));
    else if (product.kind === "subscription") row.append(el("span", "badge", t("per_month")));
    else if (product.access_days) row.append(el("span", "badge", t("per_days", { n: product.access_days })));
    const hint = !owned.has(product.id) && scarcity(product);
    if (hint) row.append(hint);
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
    if (product.access_days) tags.push(el("span", "badge", t("days_tag", { n: product.access_days })));
    if ((product.includes || []).length) tags.push(el("span", "badge", t("bundle_tag")));
    const hint = !isOwned && scarcity(product);
    if (hint) tags.push(hint);
    if (isOwned) tags.push(el("span", "badge ok", t("yours")));
    $("detail-tags").replaceChildren(...tags);
    const options = optionsOf(product);
    $("detail-options").hidden = options.length < 2;
    $("detail-options").replaceChildren(...(options.length < 2 ? [] : options.map((o) => {
      const chip = el("button", "option" + (o.id === product.id ? " active" : ""));
      chip.type = "button";
      chip.append(el("span", "", o.option || o.title), el("b", "", `⭐${o.stars}`));
      if (o.sold_out) chip.classList.add("sold");
      chip.addEventListener("click", () => { if (o.id !== current.id) { haptic("select"); openDetail(o); } });
      return chip;
    })));
    $("detail-title").textContent = product.title;
    $("detail-price").textContent = String(product.stars);
    $("detail-was").textContent = product.compare_at ? `⭐${product.compare_at}` : "";
    $("detail-was").hidden = !product.compare_at;
    $("detail-rating").hidden = !product.reviews;
    $("detail-rating").textContent = product.reviews ? `★ ${product.rating} · ${t("review_count", { n: product.reviews })}` : "";
    $("detail-desc").textContent = product.description;
    const facts = [["⚡", t("fact_instant")], product.kind === "subscription" ? ["🔁", t("fact_sub")]
      : product.access_days ? ["⏳", t("fact_days", { n: product.access_days })] : ["📦", t("fact_once")]];
    const bundle = bundleFact(product);
    if (bundle) facts.push(["🧺", bundle]);
    if (product.file) facts.push(["📎", t("fact_file")]);
    if (product.left != null && !product.sold_out) facts.push(["📉", t("fact_left", { n: product.left })]);
    if (product.sale_until) facts.push(["⏰", t("fact_ends", { date: formatDate(product.sale_until) })]);
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
    quote = null;
    $("promo-box").hidden = isOwned || product.kind === "subscription";
    $("promo-code").value = promoCode;
    $("promo-note").hidden = true;
    const soldOut = !isOwned && product.sold_out;
    const label = isOwned ? t("open") : soldOut ? t("sold_out") : t("buy_for", { n: product.stars });
    $("detail-buy").textContent = label;
    $("detail-buy").disabled = soldOut;
    if (soldOut) $("promo-box").hidden = true;
    $("detail-share").hidden = !config.bot_username;
    $("detail-gift").hidden = soldOut || product.kind === "subscription";
    $("detail-wait").hidden = !soldOut;
    $("detail-wait").disabled = false;
    loadReviews(product);
    returnTo = view === "detail" ? returnTo : (view === "library" ? "library" : "shop");
    show("detail");
    soldOut ? hideMainButton() : showMainButton(label);
    if (promoCode && !isOwned && !soldOut && product.kind !== "subscription") applyPromo(true);
  }

  // ---------------------------------------------------------------- reviews
  let reviewRating = 0;
  function drawStars() {
    $("stars-pick").replaceChildren(...[1, 2, 3, 4, 5].map((n) => {
      const star = el("button", n <= reviewRating ? "star on" : "star", "★");
      star.type = "button";
      star.setAttribute("aria-label", String(n));
      star.addEventListener("click", () => { reviewRating = n; haptic("select"); drawStars(); });
      return star;
    }));
  }
  async function loadReviews(product) {
    $("reviews").hidden = true;
    let data;
    try { data = await api(`/api/reviews?product_id=${encodeURIComponent(product.id)}`); } catch (_) { return; }
    if (!current || current.id !== product.id) return;
    $("reviews").hidden = !data.count && !data.can_review;
    $("review-form").hidden = !data.can_review;
    reviewRating = data.mine ? data.mine.rating : 0;
    $("review-text").value = data.mine && data.mine.text ? data.mine.text : "";
    drawStars();
    $("review-list").replaceChildren(...(data.items.length ? data.items.map((r) => {
      const item = el("div", "review");
      const head = el("div", "review-head");
      head.append(el("span", "review-stars", "★".repeat(r.rating) + "☆".repeat(5 - r.rating)), el("b", "", r.name),
        el("span", "muted", formatDate(r.created)));
      item.append(head);
      if (r.text) item.append(el("p", "", r.text));
      return item;
    }) : [el("p", "muted", t("review_none"))]));
    if (wantReview && data.can_review) {
      wantReview = false;
      $("reviews").scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
  async function sendReview(event) {
    event.preventDefault();
    if (!reviewRating) { toast(t("review_pick")); return; }
    try {
      await api("/api/reviews", { method: "POST", body: JSON.stringify({
        product_id: current.id, rating: reviewRating, text: $("review-text").value.trim() || null }) });
      haptic("light");
      toast(t("review_thanks"));
      loadReviews(current);
    } catch (error) { toast(error.message); }
  }
  async function joinWaitlist() {
    try {
      await api("/api/waitlist", { method: "POST", body: JSON.stringify({ product_id: current.id }) });
      $("detail-wait").disabled = true;
      toast(t("wait_ok"));
    } catch (error) { toast(error.message); }
  }
  function shareGift(item) {
    const text = t("gift_msg", { title: item.title });
    openLink(`https://t.me/share/url?url=${encodeURIComponent(item.gift.link)}&text=${encodeURIComponent(text)}`);
  }

  // Discount code: ask the server for the price, show it, and buy with it.
  async function applyPromo(quiet) {
    if (!current) return;
    const code = $("promo-code").value.trim().toUpperCase();
    const note = $("promo-note");
    if (!code) { quote = null; promoCode = ""; note.hidden = true; return renderPrice(); }
    try {
      quote = await api(`/api/quote?product_id=${encodeURIComponent(current.id)}&code=${encodeURIComponent(code)}`);
      promoCode = code;
      note.textContent = t("promo_ok", { code: quote.code, p: quote.percent });
      note.className = "promo-note";
      note.hidden = false;
      haptic("light");
    } catch (error) {
      quote = null;
      if (quiet) { note.hidden = true; } else {
        note.textContent = error.message;
        note.className = "promo-note bad";
        note.hidden = false;
      }
    }
    renderPrice();
  }

  function renderPrice() {
    if (!current) return;
    const price = $("detail-price");
    price.textContent = String(quote ? quote.stars : current.stars);
    const discounted = quote && quote.stars !== quote.original;
    if (discounted) price.append(el("span", "old-price", `⭐${quote.original}`));
    $("detail-was").hidden = discounted || !current.compare_at;  // one struck price is enough
    if (!owned.has(current.id)) {
      const label = t("buy_for", { n: quote ? quote.stars : current.stars });
      $("detail-buy").textContent = label;
      showMainButton(label);
    }
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
    const items = library.length ? library.map(libraryItem) : [el("div", "empty", t("empty_library"))];
    const invite = inviteCard();
    container.replaceChildren(...(invite ? [invite] : []), ...items);
  }

  function inviteCard() {
    const me = tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id;
    if (!config.referral_percent || !config.bot_username || !me) return null;
    const card = el("article", "card invite");
    card.append(el("h3", "", t("invite_title")), el("p", "", t("invite_text", { p: config.referral_percent })));
    const button = el("button", "btn", t("invite_share"));
    button.addEventListener("click", () => {
      haptic("light");
      const link = `https://t.me/${config.bot_username}?start=ref_${me}`;
      const text = t("invite_msg", { shop: config.shop_name });
      openLink(`https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent(text)}`);
    });
    card.append(button);
    return card;
  }

  function libraryItem(item) {
    const card = el("article", "card");
    card.append(el("h3", "", item.title));
    const row = el("div", "row");
    if (item.status === "gift") {
      row.append(el("span", "badge", t("gift_tag")),
        el("span", item.gift.claimed ? "badge ok" : "badge off", item.gift.refunded ? t("gift_cancel") : item.gift.claimed ? t("gift_open") : t("gift_wait")));
      if (item.gift.link && !item.gift.claimed) {
        const send = el("button", "btn small", t("gift_share"));
        send.addEventListener("click", () => shareGift(item));
        row.append(send);
      }
    } else if (item.status === "paid") {
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
  async function buy(product, gift = false) {
    if (!tg) return;
    busy(true);
    haptic("medium");
    try {
      const { order_id: orderId, invoice_link: link } = await api("/api/orders", {
        method: "POST", body: JSON.stringify({ product_id: product.id, code: quote ? quote.code : null, gift }),
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
        if (order.status === "gift") {
          toast(t("gift_ready"));
          await showLibrary();
          return;
        }
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
    const params = new URLSearchParams(location.search);
    const start = tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param;
    promoCode = (params.get("code") || (start && start.startsWith("kod_") ? start.slice(4) : "") || "").toUpperCase();
    wantReview = params.get("review") === "1";
    const fromQuery = params.get("p");
    const id = fromQuery || (start && start.startsWith("p_") ? start.slice(2) : "");
    return id ? products.find((p) => p.id === id) : null;
  }

  let wantReview = false;

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
    $("detail-gift").addEventListener("click", () => { if (current) buy(current, true); });
    $("detail-wait").addEventListener("click", joinWaitlist);
    $("detail-rating").addEventListener("click", () => $("reviews").scrollIntoView({ behavior: "smooth" }));
    $("review-form").addEventListener("submit", sendReview);
    $("promo-apply").addEventListener("click", () => applyPromo(false));
    $("promo-code").addEventListener("keydown", (event) => { if (event.key === "Enter") applyPromo(false); });
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
    else if (promoCode) toast(t("promo_ready", { code: promoCode }));
  }

  boot();
})();
