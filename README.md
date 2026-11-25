# Stars Mini App Kit

Telegram botları için **Telegram Stars (XTR)** ile dijital ürün ve abonelik satan, Mini App arayüzlü hazır mağaza.
Telegram, botlarda ve Mini App'lerde satılan dijital ürünlerin, aboneliklerin ve AI erişiminin yalnızca Stars ile
ödenmesini istiyor. Bu kit, bir botu bu kurala birkaç saatte uygun hale getirmek için yapıldı.

*English summary at the end.*

## Ne yapar

- **Mini App mağaza:** ürün listesi, Stars ile tek dokunuşla ödeme (`openInvoice`), "Satın aldıklarım" kütüphanesi.
  Telegram temasına uyar (açık/koyu), Türkçe ve İngilizce.
- **Tek seferlik ürün ve aylık abonelik:** abonelikler Telegram'ın 30 günlük Star aboneliğiyle otomatik yenilenir;
  süre bitince erişim kendiliğinden kapanır.
- **Güvenli ödeme akışı:**
  - Sipariş önce sunucuda oluşur; fatura `payload`'ı sipariş kimliğidir.
  - `pre_checkout_query` yalnızca aynı kullanıcı, `XTR`, tam tutar ve süresi dolmamış, ödenmemiş sipariş için onaylanır.
  - Ödeme yalnızca `successful_payment` ile kaydedilir; `charge_id` tekildir, tekrar gelen güncellemeler zarar vermez.
  - Eşleşmeyen, çift ya da işlenemeyen hiçbir ödeme kaybolmaz: `payment_issues` tablosuna düşer, operatör iade eder.
- **Teslimat:** içerik Mini App'te anında açılır ve bot mesajı olarak da (`protect_content`) gönderilir.
  Mesaj gidemezse 5 dakikada bir, en fazla 5 kez yeniden denenir; sonra operatöre düşer.
- **İade:** `/refund <charge_id>` (yalnızca admin) ya da `python -m app.cli refund`. Telegram destek üzerinden yapılan
  iadeler de (`refunded_payment`) erişimi kapatır.
- **Telegram'ın zorunlu komutları:** `/paysupport`, `/terms`; ayrıca `/orders` (içeriği yeniden gönder).
- **Kimlik doğrulama:** Mini App'ten gelen her istek `initData` HMAC imzasıyla doğrulanır; istemciye güvenilmez.
  Webhook, `X-Telegram-Bot-Api-Secret-Token` ile korunur.

## Yapı

```
app/
  main.py       FastAPI: Mini App, /api/*, /telegram/webhook
  shop.py       ödeme, teslimat, iade ve komut mantığı
  store.py      SQLite tabloları (orders, payments, refunds, payment_issues)
  initdata.py   Mini App initData doğrulaması
  telegram.py   Bot API istemcisi (token'ı loglara yazmaz)
  config.py     ortam değişkenleri ve products.json doğrulaması
  cli.py        setup / poll / issues / refund / check
webapp/         Mini App (derleme gerektirmeyen HTML/CSS/JS)
tests/          47 test: imza, ödeme kuralları, iade, abonelik, webhook
```

## Kurulum

1. **Bot:** @BotFather'da bot oluştur, token'ı al.
2. **Ayarlar:**
   ```bash
   cp .env.example .env               # BOT_TOKEN, WEBHOOK_SECRET, WEBAPP_URL, destek ve koşul metinleri
   cp products.example.json products.json
   ```
3. **Ürünler (`products.json`):**
   ```json
   {"id": "vip-club", "title": "VIP Kulüp", "description": "…", "stars": 250,
    "kind": "subscription", "content": "https://t.me/+davet"}
   ```
   `kind`: `one_time` (varsayılan) ya da `subscription` (30 günde bir yenilenir). `content` ödemeden sonra gösterilen
   metin ya da bağlantıdır (en fazla 4096 karakter). İsteğe bağlı: `photo_url` (https).
4. **Çalıştır:**
   ```bash
   docker compose up -d --build
   # ya da: pip install -r requirements.txt && uvicorn app.main:app --factory --port 8000
   ```
5. **HTTPS:** Mini App ve webhook HTTPS ister. Sunucunun önüne Caddy/Nginx koy ya da geliştirmede
   `cloudflared tunnel --url http://localhost:8000` kullan ve çıkan adresi `WEBAPP_URL` yap.
6. **Telegram'a bağla:**
   ```bash
   python -m app.cli check    # ayarları ve ürünleri doğrula
   python -m app.cli setup    # webhook + "Mağaza" menü butonu + komutlar
   ```
7. **Test et:** önce `SALES_ENABLED=false` ile arayüzü kontrol et. Sonra 1 Star'lık bir ürünle gerçek bir ödeme
   yapıp `/refund` ile iade et. Her şey yolundaysa `SALES_ENABLED=true`.

Webhook kurmadan yerelde denemek için: `python -m app.cli poll` (bot API'den güncellemeleri çeker; webhook
ayarlıysa çalışmayı reddeder).

## Operatör işleri

```bash
python -m app.cli issues                 # iade ya da elle teslimat bekleyen ödemeler
python -m app.cli refund CHARGE_ID       # onay sorar; --yes ile sormaz
```

Bot içinden admin (`ADMIN_IDS`): `/issues`, `/refund CHARGE_ID`.

## Bilinmesi gerekenler

- **Kazanç:** Stars, Fragment üzerinden çekilir; Telegram'ın güncel kurunu ve komisyonlarını kontrol et.
  Telegram, Stars bakiyesinin kullanılabilir hale gelmesi için 21 güne kadar bekletme uygulayabilir.
- **Fiziksel ürünler bu kitin kapsamı dışında:** fiziksel ürün ve hizmetler Stars dışındaki sağlayıcılarla satılabilir.
- **Ölçek:** SQLite tek süreç içindir; tek replika çalıştır. Birden fazla sunucuya geçerken tabloları PostgreSQL'e taşı.
- **İçerik sorumluluğu satıcıdadır.** Telegram kurallarına ve yerel mevzuata uymayan ürünler hesabın kapanmasına yol açar.

## Testler

```bash
pip install -r requirements-dev.txt
pytest
```

---

## English summary

A ready-to-deploy Telegram **Mini App shop for Telegram Stars (XTR)**: one-time products and 30-day auto-renewing
subscriptions, server-side `initData` verification, strict pre-checkout validation, idempotent payment recording,
operator queue for unmatched payments, delivery with retries, refunds (admin command, CLI and Telegram-initiated),
and the required `/paysupport` and `/terms` commands. FastAPI + SQLite backend, no-build HTML/JS front end,
Docker setup and 47 tests. Configure `.env` and `products.json`, run `python -m app.cli setup`, done.
