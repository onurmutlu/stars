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
- **Operatör:** `python -m app.hubcli tenants` (tüm mağazalar, plan, ödeme, sorun), `suspend` / `unsuspend`
  (askıdaki mağaza satış yapamaz ama gelen ödemeler kaydedilmeye devam eder).

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
   metin ya da bağlantıdır (en fazla 4096 karakter). İsteğe bağlı: `photo_url` (https), `category`
   (Mini App filtresi), `badge` ("Yeni", "Popüler"), `channel_id`.

   **Özel kanal/grup erişimi satmak (`channel_id`):** Botu kanala yönetici yap ("Kullanıcı davet et" ve
   "Üyeleri yasakla" açık). Bot yöneticilere kanalın ID'sini yazar (`-100…`). Bu ID'yi ürüne `channel_id` olarak
   ekle. Ödeyen kişiye sadece ona ait, tek kullanımlık ve 7 gün geçerli bir davet linki gider. Abonelik biter ya da
   ödeme iade edilirse kişi kanaldan çıkarılır (kalıcı yasak yok); abonelik yenilenirse yeni link gelir.
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

Bot içinden admin (`ADMIN_IDS`): `/admin`, `/musteriler`, `/kod`, `/duyuru`, `/dosya`, `/rapor`, `/kaynaklar`, `/yorumlar`, `/paylas`, `/foto`, `/issues`, `/refund CHARGE_ID`.
Alıcı: `/orders`, `/davet`, `/durdur`, `/baslat`, `/paysupport`, `/terms`.

**Dosya teslimi:** Dosyayı (PDF, video, zip, ses, foto) bota gönder, açıklamasına `/dosya <ürün-kimliği>` yaz. Ödemeden
sonra bot dosyayı alıcıya iletilemez/kaydedilemez olarak gönderir. `/dosya <ürün> sil` kaldırır.

**Süreli erişim:** Tek seferlik ürüne `access_days` (1–3650) ver: tek ödeme, N gün erişim. Süre bitince içerik kapanır,
kanal ürünüyse kişi kanaldan çıkar, alıcıya "süren doldu, yeniden al" mesajı gider. Otomatik çekim yok.
Bitişe 3 gün kala hatırlatma gider; o günlerde tekrar alırsa yeni süre kalan günlerin üstüne eklenir.

**Paket:** Ürüne `includes: ["rehber", "sablon"]` (en fazla 10 tek seferlik ürün) ver: tek ödemeyle hepsi açılır.
Mini App "ayrı ayrı ⭐N, paketle ⭐M kazanırsın" gösterir. Paket iade edilirse içindekiler de geri alınır.

**Stok ve satış bitişi:** `stock: 50` sınırlı adet ("Son 4", "Tükendi"); `sale_until: "2026-12-31"` ya da
`"2026-12-31 18:00"` o andan sonra ürünü gizler, son 7 günde geri sayım görünür. Sipariş ve ödeme anında kontrol edilir.

**Davet programı:** `REFERRAL_PERCENT=15` (Hub'da mağaza ayarı). Alıcı `/davet` ya da Mini App'teki "Arkadaşını davet et"
ile `t.me/<bot>?start=ref_<id>` linkini paylaşır. Gelen kişinin ilk ücretli alışverişinde davet edene %15'lik, tek
kullanımlık, 30 gün geçerli bir kod gider. Kişi başına bir ödül; kendi kendini davet işe yaramaz.

**Hediye:** Ürün sayfasındaki "🎁 Hediye et" ile alıcı tek seferlik bir ürünü başkası için öder. Bot ona
`t.me/<bot>?start=gift_XXXX` linkini ve "Hediyeyi gönder" düğmesini yollar. Linki ilk açan kişi ürünü alır (süreli üründe süre
açıldığı an başlar), hediye eden "Hediyen açıldı" mesajı alır. Hediye iade edilirse alan kişiden de geri alınır. Stok bir adet düşer.

**Satış sonrası öneri:** Ürüne `upsell: "ileri"` ve `upsell_percent: 30` ver: ödemeden hemen sonra alıcıya o ürün önerilir,
yüzde verdiysen 48 saat geçerli, tek kullanımlık, sadece o ürüne geçen bir kodla. `compare_at: 250` üstü çizili eski fiyat gösterir.

**Yorumlar:** Sadece satın alan puan (1–5) ve yorum bırakabilir; kişi başına bir yorum, sonradan güncellenir. Satın almadan bir gün
sonra bot bir kez "nasıldı?" diye sorar (`REVIEW_REQUEST=false` kapatır). Kartlarda ★ ortalama görünür. `/yorumlar` son yorumları
listeler, `/yorumlar gizle <ürün> <kullanıcı>` gizler; Hub panelinde de gizle/göster var.

**Bekleme listesi:** Tükenen üründe "🔔 Gelince haber ver" çıkar. Stok artırılınca ya da iade ile adet boşalınca bekleyen herkese
bot yazar. Hub panelinde ürünün yanında "🔔 N bekliyor" görünür.

**Kaynaklar:** `/kaynaklar` (ve panelde "Kaynaklar") ilk geliş kaynağına göre kişi, alıcı, ⭐ ve dönüşüm oranını verir.
Davet, kampanya kodu, ürün ve hediye linkleri kendi başlıklarında toplanır.

**Ürün görseli:** Fotoğrafı bota gönder, açıklamasına `/foto <ürün>` yaz; görsel mağazada ve ödeme ekranında görünür
(https adresi gerekmez; mağaza görseli Telegram'dan bir kez indirip saklar). `/foto <ürün> sil` kaldırır.

**Seçenekler:** Aynı ürünün süre/fiyat seçenekleri için her seçeneği ayrı ürün olarak ekle ve hepsine aynı `group`
("Premium Kurs") ile kendi `option` adını ("7 gün", "30 gün") ver. Mini App bunları tek kartta "⭐90+ · 2 seçenek" olarak
gösterir, ürün sayfasında seçenek düğmeleriyle geçilir. Stok, kod, hediye ve yorum her seçenekte ayrı çalışır.

**Satış grafiği:** Hub panelinde son 30 günün günlük ⭐ grafiği ve en çok satan 5 ürün.

**Alıcıya soru:** Ürüne `ask: "Hangi gün uygunsun?"` ver: Mini App ödemeden önce cevabı ister. Cevap satış bildiriminde,
panelde son ödemelerde, CSV'de ve webhook'ta görünür. Hizmet, randevu ve kişiye özel ürünler için.

**İstediğin kadar öde:** `flexible: true` ile fiyat en az tutar olur; alıcı ⭐25 / 50 / 125 / 250 gibi hazır tutarlardan
birini ya da kendi tutarını seçer. Bağış, destek, bahşiş için. İndirim kodu geçmez, abonelikte kullanılamaz.

**Yarım sipariş indirimi:** `CART_DISCOUNT=10` (Hub'da mağaza ayarı) ile bir saat sonra giden "siparişin yarım kaldı"
mesajına o ürüne özel, 24 saat geçerli, tek kullanımlık %10 kod eklenir.

**Satış webhook'u:** `SALE_WEBHOOK_URL=https://...` (Hub'da mağaza ayarı) ile her ödemede bu adrese JSON gider:
`event` (sale/renewal), `product`, `title`, `stars`, `promo`, `gift`, `answer`, `user_id`, `username`, `name`, `time`.
Zapier, Make ya da Google Apps Script ile tabloya yazmak için. Adres çalışmazsa satış etkilenmez.

**Kurs modu:** Ürüne `lessons` ver (JSON'da `[{"title": "Giriş", "content": "..."}]`, panelde her ders `# Başlık`
satırıyla başlar). `drip_days: 1` ile ilk ders hemen, sonra her gün bir yeni ders açılır ve bot alıcıya gönderir
(uzun bir aradan sonra bile her turda en fazla bir ders, sel olmaz). Mini App'te "Satın aldıklarım"da ders listesi,
kilitli derslerin açılış tarihi ve "2/10 ders" ilerlemesi görünür. En fazla 100 ders.

**Kanala ürün kartı:** `/paylas <ürün>` kartı sana gönderir, kanalına iletirsin; `/paylas <ürün> @kanal` ya da `-100…`
doğrudan kanala/gruba atar (bot orada yönetici olmalı). Kartta görsel, fiyat, puan, ders sayısı ve "⭐ N ile al" düğmesi
var; düğme botu ürün sayfasıyla açar. Panelde her ürünün "📣 Paylaş" düğmesi aynı işi yapar.

**Yardımcı yöneticiler:** Hub panelinde "Yardımcılar"a @kullanıcıadı ile en fazla 5 kişi eklenir (önce botu açıp
/start yazmış olmalılar). Satış bildirimleri ve bot komutları onlara da gelir; panel ve iadeler sahibinde kalır.

**Uyarılar:** Stok 10, 5, 3, 1 ve 0'a inince, ve her yeni yorumda yöneticilere mesaj gider.

**CSV rapor:** `/rapor` (ya da Hub panelinde "CSV raporu bota gönder") satislar.csv ve musteriler.csv dosyalarını bot
sohbetine gönderir; Excel Türkçe karakterleri doğru açar (UTF-8 BOM, `;` ayraç).

**İndirim kodları:** `/kod YAZ20 20 7g 50x [ürün]` → %20, 7 gün, 50 kullanım. `/kod` listeler, `/kod YAZ20 sil` kapatır.
Alıcı Mini App'te kodu yazar ya da `t.me/<bot>?start=kod_YAZ20` kampanya linkiyle gelir (kod otomatik uygulanır).
Kodlar aboneliklerde geçmez; indirimli fiyat yukarı yuvarlanır, en az 1 ⭐.

**Duyuru:** `/duyuru <metin>` (herkese) ya da `/duyuru @alicilar <metin>`, `@ziyaretciler`, `@sepet`, `@urun:<ürün>`
ile bir kitleye; panelde "Kimlere" seçimi aynı işi yapar. Önizleme gösterilir, `/duyuru_onay` gönderir, `/duyuru_iptal` vazgeçer. Sadece botla konuşmuş
kişilere gider, günde en fazla bir duyuru, her mesajın altında `/durdur` (çıkış) ve `/baslat` (geri açma) var. Botu
engelleyenler listeden düşer. Gönderim saniyede ~25 mesajla arka planda yapılır, bitince yöneticiye rapor gelir.

**Müşteri takibi (CRM):** Bota yazan ya da mağazayı açan herkes kaydedilir: ad, kullanıcı adı, ilk geldiği kaynak
(`t.me/<bot>?start=instagram` → kaynak `instagram`) ve aşama (ziyaretçi → sepette → müşteri / abone / iade).
Müşterinin bota yazdığı mesaj yöneticilere gelir; yönetici o mesajı yanıtlayınca cevap müşteriye bot üzerinden gider.
`/musteriler` özet verir. Ödemeyi yarım bırakan alıcıya bir saat sonra bir kez hatırlatma gider (`CART_REMINDER=false`
kapatır). Hub panelinde her mağazanın "Müşteriler" bölümü: aşama filtresi, not, bot üzerinden mesaj.

Yöneticiler her satışta, abonelik yenilemesinde ve iadede bota mesaj alır (`NOTIFY_SALES=false` kapatır).
Her sabah `DAILY_SUMMARY_HOUR` (varsayılan 9, sunucu saati; `-1` kapatır) son 24 saatin özetini gönderir:
satış, ⭐ toplamı, yeni müşteri, yenileme, iade, aktif abonelik ve en çok satan ürün. Hiç hareket yoksa mesaj atmaz.

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
