(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const fmtDate = (s) => new Date(s * 1000).toLocaleDateString("tr-TR", { day: "numeric", month: "short", year: "numeric" });
  const fmtNum = (n) => Number(n || 0).toLocaleString("tr-TR");
  const PLAN_NAMES = { pro: "Lisanslı (Pro)", trial: "Deneme", free: "Ücretsiz" };

  let me = null;       // /panel/api/me
  let shop = null;     // current shop detail
  let editing = null;  // product id being edited, or null for a new one
  let busy = false;
  let customers = { funnel: {}, customers: [] };
  let stageFilter = "";
  const STAGES = ["ziyaretçi", "sepette", "müşteri", "abone", "iade"];

  // ---------------------------------------------------------------- api
  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": tg ? tg.initData : "" },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.detail || "Bir şeyler ters gitti.");
      error.body = body;
      error.status = response.status;
      throw error;
    }
    return body;
  }
  const json = (method, data) => ({ method, body: JSON.stringify(data) });

  async function guarded(button, fn) {
    if (busy) return;
    busy = true;
    if (button) button.disabled = true;
    try { await fn(); } catch (error) { toast(error.message); } finally {
      busy = false;
      if (button) button.disabled = false;
    }
  }

  function toast(message) {
    const node = $("toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.hidden = true; }, 3500);
  }

  const confirmBox = (text) => new Promise((resolve) => {
    if (tg && tg.showConfirm) tg.showConfirm(text, resolve); else resolve(window.confirm(text));
  });

  function show(view) {
    for (const name of ["home", "connect", "shop", "product"]) $(`view-${name}`).hidden = name !== view;
    window.scrollTo(0, 0);
    if (tg) view === "home" ? tg.BackButton.hide() : tg.BackButton.show();
    show.current = view;
  }

  function back() {
    if (show.current === "product") return openShop(shop.id);
    loadHome();
  }

  function openLink(url) {
    if (!tg) return void window.open(url, "_blank");
    url.startsWith("https://t.me/") ? tg.openTelegramLink(url) : tg.openLink(url);
  }

  // ---------------------------------------------------------------- home
  async function loadHome() {
    me = await api("/panel/api/me");
    $("title").textContent = me.hub_name;
    $("subtitle").textContent = "Telegram botun için hazır Stars mağazası";
    renderPlan();
    const list = $("shops");
    if (!me.shops.length) {
      list.replaceChildren(el("div", "empty", "Henüz mağazan yok. Botunu bağlayarak başla."));
    } else {
      list.replaceChildren(...me.shops.map((s) => {
        const card = el("article", "card shop-item");
        const row = el("div", "row");
        row.append(el("h3", "", `${s.shop_name} · @${s.username}`));
        row.append(statusBadge(s));
        card.append(row, el("p", "", s.mode === "forward" ? "Mevcut bot sunucusuyla birlikte çalışıyor" : "Bağımsız mağaza botu"));
        card.addEventListener("click", () => guarded(null, () => openShop(s.id)));
        return card;
      }));
    }
    show("home");
  }

  function statusBadge(s) {
    if (s.status === "suspended") return el("span", "badge off", "Askıda");
    if (s.last_sync_error) return el("span", "badge off", "Bağlantı hatası");
    return el("span", s.sales_enabled ? "badge" : "badge off", s.sales_enabled ? "Satışta" : "Satış kapalı");
  }

  function renderPlan() {
    const p = me.plan;
    const card = $("plan");
    const until = p.until ? ` · ${fmtDate(p.until)} tarihine kadar` : "";
    card.replaceChildren(
      el("span", "muted", "Planın"),
      el("span", "name", `${PLAN_NAMES[p.name]}${until}`),
      el("span", "limits", `${p.max_shops} mağaza · mağaza başına ${p.max_products} ürün · ${p.branding ? "vitrinde platform etiketi" : "markasız vitrin"}`),
    );
    if (p.name !== "pro" && me.billing_enabled) {
      const button = el("button", "btn", `Lisans satın al · ⭐${fmtNum(me.pro_stars)} / 30 gün`);
      button.addEventListener("click", () => guarded(button, buyPro));
      card.append(button);
    } else if (p.name === "pro") {
      card.append(el("span", "limits", `Lisansın her 30 günde ⭐${fmtNum(me.pro_stars)} ile otomatik uzar. İptal: Telegram → Ayarlar → Stars.`));
    }
  }

  async function buyPro() {
    if (!tg) return;
    const { order_id: orderId, invoice_link: link } = await api("/panel/api/plan/invoice", { method: "POST" });
    tg.openInvoice(link, async (status) => {
      if (status !== "paid") return status === "failed" && toast("Ödeme tamamlanamadı.");
      toast("Ödeme alındı, plan açılıyor…");
      for (let i = 0; i < 10; i += 1) {
        try {
          if ((await api(`/panel/api/plan/orders/${orderId}`)).status === "paid") { toast("Lisans aktif 🎉"); return loadHome(); }
        } catch (_) { /* retry */ }
        await new Promise((r) => setTimeout(r, 1500));
      }
      toast("Ödeme alındı; plan birkaç dakika içinde açılacak.");
    });
  }

  // ---------------------------------------------------------------- connect
  function openConnect() {
    $("token").value = "";
    $("forward-secret").value = "";
    $("forward-box").hidden = true;
    $("connect").textContent = "Bağla";
    show("connect");
  }

  async function connect() {
    const confirmForward = !$("forward-box").hidden;
    const payload = { token: $("token").value.trim(), confirm_forward: confirmForward };
    if (confirmForward && $("forward-secret").value.trim()) payload.forward_secret = $("forward-secret").value.trim();
    try {
      const tenant = await api("/panel/api/shops", json("POST", payload));
      $("token").value = "";
      toast(`@${tenant.username} bağlandı ✅`);
      await openShop(tenant.id);
    } catch (error) {
      if (error.body && error.body.needs_confirm) {
        $("forward-url").textContent = error.body.webhook_url;
        $("forward-box").hidden = false;
        $("connect").textContent = "Anladım, yönlendirmeyle bağla";
        return;
      }
      throw error;
    }
  }

  // ---------------------------------------------------------------- shop
  async function openShop(id) {
    shop = await api(`/panel/api/shops/${id}`);
    renderShop();
    show("shop");
    loadCustomers().catch((error) => toast(error.message));
  }

  // ---------------------------------------------------------------- customers (CRM)
  async function loadCustomers() {
    customers = await api(`/panel/api/shops/${shop.id}/customers`);
    renderCustomers();
  }

  function renderCustomers() {
    $("funnel").replaceChildren(...STAGES.map((stage) => {
      const step = el("button", "step" + (stage === stageFilter ? " active" : ""));
      step.append(el("b", "", fmtNum(customers.funnel[stage])), el("span", "", stage));
      step.addEventListener("click", () => { stageFilter = stageFilter === stage ? "" : stage; renderCustomers(); });
      return step;
    }));
    const list = customers.customers.filter((c) => !stageFilter || c.stage === stageFilter);
    $("customer-count").textContent = fmtNum(customers.customers.length);
    $("customers").replaceChildren(...(list.length ? list.slice(0, 100).map(customerItem)
      : [el("div", "empty", stageFilter ? "Bu aşamada kimse yok." : "Henüz kimse botunla konuşmadı. Bot linkini kanalında paylaş.")]));
  }

  function customerItem(c) {
    const card = el("article", "card cust-item");
    const row = el("div", "row");
    row.append(el("h3", "", (c.name || "İsimsiz") + (c.username ? ` · @${c.username}` : "")),
      el("span", `badge stage-${c.stage}`, c.stage));
    const details = [`son: ${fmtDate(c.last_seen)}`];
    if (c.spent) details.push(`⭐${fmtNum(c.spent)}`);
    if (c.source) details.push(`kaynak: ${c.source}`);
    card.append(row, el("span", "meta", details.join(" · ")));
    if (c.note) card.append(el("span", "meta", `📝 ${c.note}`));
    card.addEventListener("click", (event) => {
      if (card.querySelector(".more") || event.target.closest(".more")) return;
      card.append(customerActions(c));
    });
    return card;
  }

  function customerActions(c) {
    const box = el("div", "more");
    const text = el("textarea");
    text.rows = 2;
    text.maxLength = 4096;
    text.placeholder = "Mesaj (botun üzerinden gider)";
    const send = el("button", "btn small", "Gönder");
    send.addEventListener("click", () => guarded(send, async () => {
      if (!text.value.trim()) return;
      await api(`/panel/api/shops/${shop.id}/customers/${c.user}/message`, json("POST", { text: text.value.trim() }));
      text.value = "";
      toast("Mesaj gönderildi.");
    }));
    const note = el("input");
    note.maxLength = 500;
    note.placeholder = "Not: ör. 'pazartesi ara', 'VIP ilgili'";
    note.value = c.note || "";
    const save = el("button", "btn small ghost", "Notu kaydet");
    save.addEventListener("click", () => guarded(save, async () => {
      await api(`/panel/api/shops/${shop.id}/customers/${c.user}/note`, json("PUT", { note: note.value }));
      c.note = note.value.trim() || null;
      toast("Not kaydedildi.");
      renderCustomers();
    }));
    const actions = el("div", "actions");
    actions.append(send, save);
    if (c.username) {
      const chat = el("button", "btn small ghost", "Telegram'da aç");
      chat.addEventListener("click", () => openLink(`https://t.me/${c.username}`));
      actions.append(chat);
    }
    box.append(text, note, actions);
    return box;
  }

  function renderShop() {
    const s = shop;
    $("shop-title").textContent = `${s.shop_name}`;
    $("shop-status").replaceWith(Object.assign(statusBadge(s), { id: "shop-status" }));
    const meta = $("shop-meta");
    meta.replaceChildren();
    const botLink = el("button", "link", `@${s.username}`);
    botLink.addEventListener("click", () => openLink(s.bot_link));
    meta.append(botLink, document.createTextNode(s.mode === "forward"
      ? " · güncellemeler mevcut sunucuna iletiliyor · alıcılar /magaza ile açar"
      : " · menü butonu: Mağaza"));

    $("sync-error").hidden = !s.last_sync_error;
    $("sync-error-text").textContent = s.last_sync_error ? `Telegram ayarı başarısız: ${s.last_sync_error}` : "";

    const ready = s.support_text && s.terms_text && s.products.length;
    $("sales").setAttribute("aria-checked", String(s.sales_enabled));
    $("sales-hint").textContent = s.status === "suspended" ? "mağaza askıda"
      : ready ? (s.sales_enabled ? "alıcılar ürünleri görüyor" : "ürünler gizli")
        : "önce ürün, destek ve koşul metni ekle";

    const st = s.stats;
    $("stats").replaceChildren(...[
      ["⭐ " + fmtNum(st.stars_30d), "Son 30 gün"], ["⭐ " + fmtNum(st.stars), "Toplam"],
      [fmtNum(st.buyers), "Alıcı"], [fmtNum(st.refunds), "İade"],
    ].map(([value, label]) => {
      const tile = el("div", "stat");
      tile.append(el("div", "value", value), el("div", "label", label));
      return tile;
    }));

    $("product-count").textContent = `${s.products.length}/${me ? me.plan.max_products : ""}`;
    const products = $("products");
    products.replaceChildren(...(s.products.length ? s.products.map(productItem)
      : [el("div", "empty", "Henüz ürün yok. İlk ürününü ekle.")]));

    $("f-shop-name").value = s.shop_name;
    $("f-support").value = s.support_text;
    $("f-terms").value = s.terms_text;
    $("f-forward-wrap").hidden = s.mode !== "forward";
    $("f-forward-hint").textContent = s.forward_secret_set ? "(kayıtlı)" : "(kayıtlı değil)";
    $("f-forward-secret").value = "";

    const payments = $("payments");
    payments.replaceChildren(...(st.recent.length ? st.recent.map(paymentItem) : [el("div", "empty", "Henüz ödeme yok.")]));
    $("issues-wrap").hidden = !st.issues.length;
    $("issues").replaceChildren(...st.issues.map(issueItem));
  }

  function productItem(p) {
    const card = el("article", `card product-item${p.over_limit ? " over" : ""}`);
    const row = el("div", "row");
    row.append(el("h3", "", p.title), el("span", "price", fmtNum(p.stars)));
    card.append(row, el("p", "", p.description));
    const foot = el("div", "row");
    foot.append(el("span", p.over_limit ? "badge off" : "badge",
      p.over_limit ? "Plan sınırı dışında, satılmıyor" : (p.kind === "subscription" ? "Aylık abonelik" : "Tek seferlik")));
    const actions = el("div", "actions");
    const edit = el("button", "btn small ghost", "Düzenle");
    edit.addEventListener("click", () => openProduct(p));
    const remove = el("button", "btn small ghost", "Kaldır");
    remove.addEventListener("click", () => guarded(remove, async () => {
      if (!(await confirmBox(`"${p.title}" satıştan kaldırılsın mı? Daha önce alanlar erişimini korur.`))) return;
      shop = { ...shop, ...(await api(`/panel/api/shops/${shop.id}/products/${p.id}`, { method: "DELETE" })) };
      renderShop();
    }));
    actions.append(edit, remove);
    foot.append(actions);
    card.append(foot);
    return card;
  }

  function paymentItem(p) {
    const card = el("article", "card pay-item");
    const row = el("div", "row");
    row.append(el("h3", "", p.title), el("span", "price", fmtNum(p.stars)));
    card.append(row);
    const foot = el("div", "row");
    const details = [fmtDate(p.created), `alıcı ${p.user}`];
    if (p.expires) details.push(`bitiş ${fmtDate(p.expires)}`);
    foot.append(el("span", "meta", details.join(" · ")));
    if (p.refunded) {
      foot.append(el("span", "badge off", "İade edildi"));
    } else {
      foot.append(refundButton(p.charge, `${p.title} (⭐${p.stars}) iade edilsin mi? Alıcının erişimi kapanır.`));
    }
    card.append(foot);
    return card;
  }

  function issueItem(i) {
    const card = el("article", "card pay-item");
    const row = el("div", "row");
    row.append(el("h3", "", i.reason), el("span", "price", fmtNum(i.stars)));
    const foot = el("div", "row");
    foot.append(el("span", "meta", `${fmtDate(i.created)} · alıcı ${i.user}`));
    if (!String(i.charge).startsWith("nocharge:")) foot.append(refundButton(i.charge, `⭐${i.stars} iade edilsin mi?`));
    card.append(row, foot);
    return card;
  }

  function refundButton(charge, question) {
    const button = el("button", "btn small ghost", "İade et");
    button.addEventListener("click", () => guarded(button, async () => {
      if (!(await confirmBox(question))) return;
      const { refunded } = await api(`/panel/api/shops/${shop.id}/refund`, json("POST", { charge }));
      toast(refunded ? "İade edildi." : "Bu ödeme zaten iade edilmiş.");
      await openShop(shop.id);
    }));
    return button;
  }

  async function toggleSales() {
    const next = $("sales").getAttribute("aria-checked") !== "true";
    shop = { ...shop, ...(await api(`/panel/api/shops/${shop.id}`, json("PATCH", { sales_enabled: next }))) };
    toast(next ? "Satış açıldı. Önce 1⭐'lık bir ürünle deneyip iade etmeni öneririz." : "Satış kapatıldı.");
    renderShop();
  }

  async function saveSettings() {
    const data = { shop_name: $("f-shop-name").value, support_text: $("f-support").value, terms_text: $("f-terms").value };
    if (shop.mode === "forward" && $("f-forward-secret").value.trim()) data.forward_secret = $("f-forward-secret").value.trim();
    shop = { ...shop, ...(await api(`/panel/api/shops/${shop.id}`, json("PATCH", data))) };
    toast("Kaydedildi.");
    renderShop();
  }

  // ---------------------------------------------------------------- product editor
  function openProduct(p) {
    editing = p ? p.id : null;
    $("product-heading").textContent = p ? "Ürünü düzenle" : "Yeni ürün";
    $("p-id").value = p ? p.id : "";
    $("p-id").disabled = Boolean(p);
    $("p-title").value = p ? p.title : "";
    $("p-description").value = p ? p.description : "";
    $("p-stars").value = p ? p.stars : 50;
    $("p-kind").value = p ? p.kind : "one_time";
    $("p-content").value = p ? p.content : "";
    $("p-photo").value = p && p.photo_url ? p.photo_url : "";
    $("p-category").value = p && p.category ? p.category : "";
    $("p-badge").value = p && p.badge ? p.badge : "";
    $("p-channel").value = p && p.channel_id ? String(p.channel_id) : "";
    $("p-channels").replaceChildren(...(shop.channels || []).map((c) => {
      const option = el("option");
      option.value = String(c.id);
      option.label = `${c.title}${c.ready ? "" : " (yetki eksik)"}`;
      return option;
    }));
    show("product");
  }

  async function saveProduct() {
    const item = {
      id: $("p-id").value.trim().toLowerCase(), title: $("p-title").value.trim(),
      description: $("p-description").value.trim(), stars: parseInt($("p-stars").value, 10),
      kind: $("p-kind").value, content: $("p-content").value.trim(), photo_url: $("p-photo").value.trim() || null,
      category: $("p-category").value.trim() || null, badge: $("p-badge").value.trim() || null,
      channel_id: $("p-channel").value.trim() || null,
    };
    const path = `/panel/api/shops/${shop.id}/products` + (editing ? `/${encodeURIComponent(editing)}` : "");
    await api(path, json(editing ? "PUT" : "POST", item));
    toast("Ürün kaydedildi.");
    await openShop(shop.id);
  }

  // ---------------------------------------------------------------- boot
  async function boot() {
    if (!tg || !tg.initData) {
      $("view-home").hidden = true;
      $("outside").hidden = false;
      return;
    }
    tg.ready();
    tg.expand();
    tg.BackButton.onClick(back);
    document.querySelectorAll("[data-back]").forEach((b) => b.addEventListener("click", loadHome));
    document.querySelectorAll("[data-back-shop]").forEach((b) => b.addEventListener("click", () => openShop(shop.id)));
    $("go-connect").addEventListener("click", openConnect);
    $("connect").addEventListener("click", () => guarded($("connect"), connect));
    $("sales").addEventListener("click", () => guarded($("sales"), toggleSales));
    $("save-settings").addEventListener("click", () => guarded($("save-settings"), saveSettings));
    $("add-product").addEventListener("click", () => openProduct(null));
    $("refresh-customers").addEventListener("click", () => guarded($("refresh-customers"), loadCustomers));
    $("save-product").addEventListener("click", () => guarded($("save-product"), saveProduct));
    $("resync").addEventListener("click", () => guarded($("resync"), async () => {
      shop = { ...shop, ...(await api(`/panel/api/shops/${shop.id}/sync`, { method: "POST" })) };
      toast(shop.last_sync_error ? "Hâlâ bağlanamıyor." : "Bağlandı ✅");
      renderShop();
    }));
    $("delete-shop").addEventListener("click", () => guarded($("delete-shop"), async () => {
      const text = shop.mode === "forward"
        ? "Mağaza kaldırılsın mı? Botun eski webhook'una geri döner. Ödeme kayıtları iadeler için saklanır."
        : "Mağaza kaldırılsın mı? Botun webhook'u ve menü butonu sıfırlanır. Ödeme kayıtları iadeler için saklanır.";
      if (!(await confirmBox(text))) return;
      await api(`/panel/api/shops/${shop.id}`, { method: "DELETE" });
      toast("Mağaza kaldırıldı.");
      await loadHome();
    }));
    try { await loadHome(); } catch (error) { $("shops").replaceChildren(el("div", "empty", error.message)); show("home"); }
  }

  boot();
})();
