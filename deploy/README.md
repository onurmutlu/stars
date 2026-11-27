# Yayına alma: https://stars.siyahkare.com

Docker + Cloudflare Tunnel. Sunucuda port açılmaz, HTTPS'i Cloudflare sağlar.
Bir VPS'te (önerilen: Ubuntu, 1 vCPU / 1 GB yeterli) ya da Docker kurulu bir bilgisayarda çalışır.

## 1. Telegram botu
@BotFather → `/newbot` → token'ı al.

## 2. Cloudflare Tunnel
1. Cloudflare → Zero Trust → Networks → Tunnels → **Create a tunnel** → Cloudflared → ad: `stars`.
2. Kurulum ekranındaki token'ı kopyala (`eyJ...` ile başlayan uzun değer). Komutu çalıştırma, token yeter.
3. **Public Hostname** ekle: Subdomain `stars`, Domain `siyahkare.com`, Service `HTTP` → `shop:8000`.

## 3. Sunucuda
```bash
git clone https://github.com/onurmutlu/stars && cd stars
bash deploy/install.sh
```
Kurulum bot token'ını, tunnel token'ını ve admin ID'ni sorar; webhook gizli değerini kendisi üretir.
Değerler sadece sunucudaki `.env` dosyasında kalır (izin: 600). Sonunda webhook ve menü butonu otomatik ayarlanır.

## 4. Kontrol
- https://stars.siyahkare.com/health → `{"ok":true}`
- Telegram'da bota `/start` yaz.
- Satış kapalı başlar. Test ettikten sonra `.env` içinde `SALES_ENABLED=true` yap ve:
  `docker compose -f docker-compose.yml -f deploy/docker-compose.tunnel.yml up -d`

## Güncelleme ve yedek
```bash
git pull && docker compose -f docker-compose.yml -f deploy/docker-compose.tunnel.yml up -d --build
docker compose cp shop:/srv/data ./yedek-$(date +%F)     # veritabanı yedeği
docker compose logs -f shop                                # loglar
```

---

# Hub: kiralık mağaza altyapısı

Önerilen adres: **https://magaza.siyahkare.com** (tek mağazayla aynı sunucuda, ayrı tunnel hostname'iyle de çalışır).

## 1. Platform botu
@BotFather → `/newbot` (örn. @SiyahKareStarsBot). Satıcılar paneli bu bottan açar ve Pro planı bu bottan alır.

## 2. Ayarlar
```bash
cp hub.env.example .env.hub
docker compose -f docker-compose.hub.yml run --rm hub python -m app.hubcli keys   # çıktıyı .env.hub'a yapıştır
chmod 600 .env.hub
```
`HUB_MASTER_KEY` bağlı botların token'larını şifreler. **Kaybedersen tüm satıcıların botu yeniden bağlaması gerekir;**
parola yöneticisinde yedekle, asla değiştirme.

## 3. Cloudflare Tunnel
Tunnel'a Public Hostname ekle: `magaza` · `siyahkare.com` → `HTTP` · `hub:8000`.
```bash
TUNNEL_TOKEN=eyJ... docker compose -f docker-compose.hub.yml -f deploy/docker-compose.hub-tunnel.yml up -d --build
docker compose -f docker-compose.hub.yml exec hub python -m app.hubcli setup
```
`setup` platform botunun webhook'unu, "Panel" menü butonunu ve komutlarını ayarlar, kayıtlı tüm mağazaları yeniden senkronlar.

## 4. Kontrol
- https://magaza.siyahkare.com/health → `{"ok":true}`
- Platform botuna `/start` → **Paneli aç** → kendi test botunun token'ıyla mağaza bağla.
- 1⭐'lık ürün ekle, satışı aç, test botunun menüsündeki **Mağaza**'dan satın al, panelden iade et.
- Pro'yu önce `HUB_PRO_STARS=1` ile kendin al ve platform botunda `/refund CHARGE_ID` ile iade et; sonra gerçek fiyatı koy.

## İşletme
```bash
docker compose -f docker-compose.hub.yml exec hub python -m app.hubcli tenants          # mağazalar, plan, Stars, sorunlar
docker compose -f docker-compose.hub.yml exec hub python -m app.hubcli suspend ID       # kural dışı içerik
docker compose -f docker-compose.hub.yml cp hub:/srv/data ./hub-yedek-$(date +%F)       # yedek (hub.sqlite3, billing, tenants/)
```

## Bilinmesi gerekenler
- **Satıcı sözleşmesi şart:** satıcılar senin altyapında kendi alıcılarına satış yapıyor. Kullanım koşulları
  (yasaklı içerik, sorumluluk, askıya alma) ve KVKK açısından veri işleyen sözleşmesi hazırla; hub alıcıların
  Telegram ID'lerini ve sipariş kayıtlarını satıcı adına işler.
- **Yönlendirme modu sınırları:** iletilen güncellemelere satıcı sunucusunun webhook yanıtındaki metod çağrıları
  uygulanmaz (sunucu Bot API'yi doğrudan çağırmalı). `getUpdates` ile çalışan botlar webhook kurulunca polling yapamaz.
  `/paysupport` ve `/terms` mağazada kalır (Stars satan botlar için zorunlu).
- **Ölçek:** her mağaza ayrı SQLite dosyası, tek süreç. Yüzlerce mağaza için yeterli; daha fazlası için PostgreSQL'e geçiş.
