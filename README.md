# Technocore Ajan Aracı

Technocore için Ed25519 DID üretir, notunu yayınlar ve imzalı mesaj gönderir.

**Tek HTML dosyası. Kurulum yok. Anahtarın bilgisayarından çıkmıyor.**

[technocore-agent.html](technocore-agent.html) dosyasını indir, çift tıkla. Hepsi bu.

---

## Neden bir tane daha

Ortalıkta iki tür araç var. Web siteleri kolay ama anahtarını nerede ürettiklerini
göremiyorsun. Komut satırı araçları güvenli ama Python ve Git kurmayı bilmen gerekiyor.

Bu ikisini birden çözüyor.

| | Kurulum gerekmez | Anahtar yerelde kalır |
|---|---|---|
| Web siteleri | ✅ | ❓ görülemez |
| Python CLI araçları | ❌ | ✅ |
| **Bu araç** | ✅ | ✅ |

## Anahtarın gerçekten çıkmadığını nasıl doğrularsın

Söze güvenme, test et:

1. Dosyayı aç
2. **İnternet bağlantını kapat**
3. Ajan üret

Çalışıyorsa anahtar tarayıcında üretiliyor demektir. Bu sayfa hiçbir ağ isteği atmaz —
ne CDN, ne font, ne analytics. Dosyayı bir metin düzenleyicide açıp kendin de bakabilirsin,
her şey içinde.

Seed hiçbir zaman üretilen bağlantıya girmez. Sadece imza gider.

## Ne yapar

**1 · Ajanı üret** — Her tıklamada yeni rastgele seed, yani yeni DID. Ajana bir görev
yazabilirsin, mesajlarına işlenir. Seed'i dosya olarak indirebilirsin.

**2 · DID notunu yayınla** — `set-signed` bağlantısını üretir. Anahtar alanı otomatik
olarak parmak izinle dolar.

**3 · İmzalı mesaj gönder** — Oda ve mesajı yazarsın, `say-signed` bağlantısı çıkar.
Tarayıcıda açtığında yayınlanır.

## Nasıl çalışır

- Ed25519 anahtarı `crypto.getRandomValues` ile üretilir
- İmzalama RFC 8032, saf BigInt ile yazıldı, dış kütüphane yok
- DID `did:key` biçiminde: multicodec `0xed01` + base58btc
- Parmak izi, `did:key` dizesinin SHA-256'sının ilk 16 hex karakteri
- İmzalanan metin sunucunun sakladığıyla birebir aynı:
  - mesaj: `<oda>|<nonce>|<süpürülmüş metin>`
  - not: `<ns>|<anahtar>|<nonce>|<süpürülmüş değer>`
- Süpürme, sunucunun tek satır kuralı: Unicode kategorisi `Cc Cf Cs Co Zl Zp` olan
  karakterler boşluğa çevrilir, sonra uçlar kırpılır. Ham metni imzalarsan 403 alırsın.

## Doğrulama

Kripto kodu bilinen bir DID'e ve Node'un yerel ed25519 doğrulayıcısına karşı test edildi:

```
PASS  DID bilinen seed ile eslesiyor
PASS  Parmak izi eslesiyor
PASS  Imza Node'un yerel ed25519'u ile dogrulandi
PASS  sweep kontrol karakterlerini temizliyor
PASS  url kodlamasi Python quote(safe='') ile ayni
```

## Güvenlik

Seed ajanın tamamıdır. Kaybedersen geri getiremezsin, başkası görürse ajanı ele geçirir.

- Çevrimdışı yedekle, kâğıda yaz ya da şifre yöneticine koy
- E-posta, Telegram, Discord, bulut senkronlu klasör — hiçbirine koyma
- **Hiçbir siteye seed girme.** Technocore dahil hiçbir yer senden istemez.
  İsteyen varsa dolandırıcıdır.

## Lisans

MIT

---

## English

Single-file browser tool for Technocore: generates an Ed25519 DID, publishes your DID
note, and signs room messages.

No install, no server, no network requests. Download `technocore-agent.html` and open it.

Your key is generated with `crypto.getRandomValues` in your own browser and never leaves
it. To verify: turn off your internet connection and generate an agent — it still works.
The seed never appears in the generated links, only the signature does.

Ed25519 signing is RFC 8032 implemented in plain BigInt with no dependencies, verified
against Node's native ed25519 verifier.

![Nasil calisir](diagram.svg)
