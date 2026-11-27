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

## Hub: başka bot sahiplerine kiralık mağaza

Aynı motor, tek sunucuda **birçok botun mağazasını** çalıştırır. Bot sahibi platform botuna token'ını verir,
panelden ürünlerini ekler, satışı açar. Kod, sunucu ya da HTTPS kurmaz.

- **Para platforma uğramaz.** Faturalar satıcının kendi botuyla kesilir; Stars doğrudan onun bot bakiyesine yatar,
  Fragment'ten kendisi çeker. Platform geliri: Pro plan (platform botunda aylık Star aboneliği).
- **Planlar:** yeni satıcıya 14 gün deneme (Pro özellikleri). Ücretsiz: 1 mağaza, 3 ürün, vitrinde platform etiketi.
  Pro (varsayılan 500⭐/ay): 5 mağaza, mağaza başına 100 ürün, markasız. Hepsi `.env.hub` ile ayarlanır.
  Plan düşerse fazla ürünler silinmez, sadece satıştan çekilir.
- **Mevcut botu bozmadan bağlama (yönlendirme modu):** botun zaten bir sunucusu varsa panel bunu fark eder ve onay ister.
  Onaylanınca mağaza güncellemelerini (kendi faturaları, ödemeleri, iadeleri, `/magaza`, `/orders`, `/paysupport`, `/terms`)
  hub işler, geri kalan her güncelleme eski webhook adresine aynen iletilir. Menü butonu ve komutlara dokunulmaz.
  Mağaza kaldırılınca eski webhook geri yüklenir.
- **Satıcı paneli (Mini App):** mağaza bağlama, ürün ekle/düzenle/kaldır, destek ve koşul metni, satış aç/kapat,
  son 30 gün ve toplam Stars, alıcı sayısı, son ödemeler, tek dokunuşla iade, bekleyen ödeme sorunları, Pro'ya geçiş.
- **Ayrım ve güvenlik:** bot token'ları `HUB_MASTER_KEY` ile şifreli saklanır, hiçbir API'de dönmez; her mağazanın ayrı
  SQLite dosyası, ayrı webhook yolu ve gizli anahtarı vardır; alıcı oturumu o mağazanın kendi bot imzasıyla doğrulanır;
  satıcı sadece kendi mağazalarını görür. Sohbete yapıştırılan token mesajı bot tarafından silinir.
- **Operatör:** `HUB_ADMIN_IDS` içindeki hesaplar satıcı panelinde platform geneli mağaza/satıcı/Stars/sorun
  özetini görür, mağazayı askıya alıp yeniden açabilir. Aynı işlemler `python -m app.hubcli tenants`,
  `suspend` / `unsuspend` ile de yapılır (askıdaki mağaza satış yapamaz ama gelen ödemeler kaydedilmeye devam eder).

```bash
cp hub.env.example .env.hub && python -m app.hubcli keys     # anahtarları .env.hub'a koy
docker compose -f docker-compose.hub.yml up -d --build
docker compose -f docker-compose.hub.yml exec hub python -m app.hubcli setup
```

Adresler: `/panel` (satıcı paneli), `/s/<bot_kullanıcı_adı>` (alıcı mağazası), `/t/<id>/webhook`, `/hub/webhook`.
Ayrıntılar: [deploy/README.md](deploy/README.md#hub-kiralık-mağaza-altyapısı).

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
  hub.py        çok kiracılı sunucu: kiracı mağazaları, ana bot, satıcı paneli API'si, yönlendirme
  tenants.py    kiracı kaydı, şifreli token, planlar, ürün kataloğu
  hubcli.py     keys / check / setup / tenants / suspend / unsuspend
webapp/         alıcı Mini App'i (derleme gerektirmeyen HTML/CSS/JS; tek mağaza ve hub ortak)
panel/          satıcı paneli Mini App'i
tests/          67 test: imza, ödeme kuralları, iade, abonelik, webhook, kiracı ayrımı, planlar, yönlendirme
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
Docker setup and 67 tests. Configure `.env` and `products.json`, run `python -m app.cli setup`, done.

**Hub mode** (`app.hub`) rents the same shop to other bot owners: they connect their own bot token in a seller panel
Mini App, Stars go straight to their bot balance, and the platform sells a monthly Pro plan as a Star subscription.
Tokens are encrypted at rest, each tenant has its own database file, webhook path and secret, and bots that already
have a backend can be connected in forward mode (non-shop updates are relayed to their original webhook).
