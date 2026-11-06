# Kulak Kesil — Akıllı Ekolayzır

*English: [README.md](README.md)*

PipeWire için **her uygulamaya kendi EQ'su, kendiliğinden**: bağlama göre değişen ekolayzır.
Çalan her ses (her uygulama) ayrı ayrı tanınır ve kendi EQ'lu sanal
çıkışına gönderilir. Aynı anda CS2 + Spotify + Discord açıksa her biri
kendi ayarıyla duyulur.

| Profil | Odak | Ne zaman |
|---|---|---|
| 🎯 **CS2** | adım sesi / ses kasma (sessiz sesler öne çıkar) | Counter-Strike 2 |
| 🎮 **Oyun** | atmosfer, derinlik | diğer oyunlar (Steam, Proton, Lutris, Faugus, wine) |
| 🎵 **Müzik** | doğal tını | Spotify, müzik çalarlar, tarayıcıda müzik |
| 🎬 **Film** | diyalog net | VLC/mpv video, tarayıcıda dizi/film |

**Discord, TeamSpeak, görüşmeler ve sistem/bildirim sesleri her zaman EQ'suz kalır.**
Bunlar **Düz** çıkıştan geçer: EQ yok ama ses seviyesi EQ'lu seslerle dengeli.
EQ yalnız varsayılan çıkış bir **kulaklıksa** (USB, Bluetooth ya da 3,5 mm) açılır; hoparlöre
ya da HDMI'ya geçince hiçbir şeye EQ uygulanmaz. **Corsair HS80** için ölçülmüş bir düzeltme
eğrisi de vardır; diğer kulaklıklarda profiller aynı çalışır, yalnız modele özel düzeltme olmadan.

## Tür alt profilleri

**Müzik** ve **Oyun** profilleri içeriğe göre küçük bir ayar farkı daha alır:
müzikte sanatçının türü (Metal / Rock, Elektronik / Hip-hop, Klasik / Caz / Akustik,
Pop / Vokal, Konuşma / Podcast), oyunda oyun türü (Rekabetçi / FPS, Korku / Hayatta kalma,
Aksiyon / RPG, Strateji / İnşa, Yarış / Simülasyon). Geçiş **yalnızca şarkı ya da oyun
değişince** ve yumuşakça olur; şarkının ortasında ses değişmez. Müzik türü şu sırayla
aranır: senin atamaların → şarkı adı ipuçları → yerleşik liste → iTunes → Deezer →
YouTube etiketleri → MusicBrainz → **ses analizi**; oyun türü Steam / SteamSpy etiketlerinden.
Bilgisayardan yalnızca sanatçı + şarkı adı, YouTube video kimliği ve oyun kimliği çıkar, o da
sadece Ayarlar'da **Çevrimiçi tür algılama** açıkken. **Ses analizi** (Ayarlar'dan ayrıca
kapatılabilir) adı hiçbir yerde bulunamayan şarkının türünü Müzik çıkışındaki sesi bu
bilgisayarda dinleyerek tahmin eder; ses kaydedilmez, gönderilmez. İki anahtar da servisi
yeniden başlatmadan anında uygulanır. Yanlış algılanırsa Kurallar → **Tür atamaları**'ndan düzelt, ya da Durum
sayfasında profil etiketinin yanındaki **▾** ile türü elle seç. Yeni bir oyun açılınca kısa bir
bildirim gelir; şarkı değişimlerinde bildirim gelmez.

- Tanınmış Türk sanatçıları (Sezen Aksu, Duman, Ezhel, Müslüm Gürses…) yerleşik listeden,
  ağa sormadan bulunur (MusicBrainz'de Türkçe etiketler seyrek).
- Şarkı adı ipucu türü o şarkı için değiştirir: "(Akustik)/Unplugged" → Klasik / Akustik,
  "Remix" → Elektronik, "Podcast/Bölüm 12" → Konuşma. Kendi ipuçların: kurallar.toml `[tur]`.
- Tür birkaç saniyede bulunamazsa o şarkı **Genel** çalar (önceki sanatçının türü taşınmaz);
  bulunan tür önbelleğe girer, o sanatçının sonraki şarkısında hemen kullanılır.
- Ekolayzır sayfasında Müzik/Oyun seçiliyken **Tür farkı** kutusundan bir tür seçip o türün
  farkını (en çok 4 bant, ±6 dB) düzenleyebilirsin; "Canlı dinle" açıkken önizler.
- Kapatmak için: servise `OTO_EQ_TUR=0` ortam değişkeni (eski davranış).

## Modlar

- **Otomatik** — her uygulama kendi profilini alır (varsayılan).
- **Sabit** — seçtiğin tek profil tüm seslere uygulanır (sesli sohbet/sistem hariç).
- **Kapalı** — EQ kapalı (seviye eşitli düz geçiş): tüm sesler Düz çıkıştan geçer.

## Komutlar (`kulak`)

| Komut | Ne yapar |
|---|---|
| `kulak durum` | kulaklık, mod, baskın profil ve hangi uygulamanın hangi profile gittiği (`--json` ham çıktı) |
| `kulak oto` | otomatik mod |
| `kulak sabitle cs2` | tek profile sabitle (`cs2`, `oyun`, `muzik`, `film`) |
| `kulak kapat` | EQ'yu kapat |
| `kulak izle` | durumu canlı izle (Ctrl+C) |
| `kulak kurallar` | `kurallar.toml`'u düzenleyicide aç |
| `kulak yenile` | kuralları yeniden yükle |
| `kulak profiller` | `profiller.toml`'u düzenleyicide aç |
| `kulak tur` | çalan müziğin / oyunun türü ve nereden bulunduğu; `kulak tur muzik metal_rock`, `kulak tur oyun oto`, `kulak tur muzik genel` ile seçim |
| `kulak tur dinle [saniye]` | Müzik çıkışında çalanı (varsayılan 8 sn) dinleyip ses özelliklerini, sinyal işleme ve model tahminini gösterir; ses yalnızca bellekte tutulur |
| `kulak saglik` | sağlık kontrolü (servisler, EQ çıkışları, eklentiler; hiçbir şeyi değiştirmez) |
| `kulak eq-uret` | EQ'yu yeniden üret ve sanal çıkışları yeniden başlat |
| `kulak eq-ciz` | EQ eğrilerinin grafiğini aç |
| `kulak gunluk` | servis günlüğü (canlı) |
| `kulak mikrofon` | sanal mikrofonun durumu; `test` (5 sn kayıt + dinle), `kalibre [--uygula]` (sesine göre otomatik ayar), `varsayilan` (Kulak Kesil · Mikrofon'u varsayılan yap), `hs80` (HS80 mikrofonuna dön) |

Çıkış kodları: `0` başarı, `1` hata, `2` yanlış kullanım, `3` servis çalışmıyor.

## Uygulama penceresi

Uygulama menüsünde ve masaüstünde **Kulak Kesil** (ya da `kulak-app`):

- **Durum** — şu anki profil, mod seçici (Otomatik | CS2 | Oyun | Müzik | Film | Kapalı),
  çalan uygulamalar ve hangi EQ ile çaldıkları. Bir uygulamanın yanındaki **⋯** ile
  "Bu uygulamayı hep şununla çal" → profil seç; o uygulama artık hep o profille çalar.
- **Ekolayzır** — profillerin EQ eğrileri.
- **Mikrofon** — işlenmiş sanal mikrofon "Kulak Kesil · Mikrofon": varsayılan yap / HS80'e dön,
  canlı seviye göstergesi, mikrofon testi (ham ve işlenmiş sesi art arda dinlersin),
  gürültü bastırma / netlik / kompresör ayarları.
- **Kurallar** — uygulama atamaların, son görülen uygulamalar ("Ata ▾" ile hızlı atama), (ekle / düzenle / sil, çalan uygulamadan ekle),
  gelişmiş kural dosyası.
- **Ayarlar** — bildirimler, tepsinin oturum açılışında başlaması, çevrimiçi tür algılama,
  ses analizi (tür kaynakları ve gizlilik notuyla), servis durumu (başlat / yeniden başlat),
  EQ'yu yeniden üretme, kaldırma bilgisi.

İlk açılışta **Kurulum rehberi** gelir (ses seviyesi, CS2 ve Discord ayarları, mikrofon
testi); sonra Ayarlar → Kurulum rehberi'nden açılır. CS2 çalarken Durum sayfasında CS2
oyun içi ayar listesi görünür ("Bir daha gösterme" ile kapatılır).

Pencereyi kapatmak sadece pencereyi kapatır; EQ ve tepsi çalışmaya devam eder.

## Tepsi simgesi

Uygulama listelerinde profil etiketi: **CS2 / Oyun / Müzik / Film**, **Düz** (EQ'suz,
seviye eşitli) ya da **EQ yok** (akış doğrudan varsayılan çıkışta, ör. elle taşındıysa).

Panelde renkli yuvarlak: **C** turuncu = CS2, **O** mor = Oyun, **M** yeşil = Müzik,
**F** mavi = Film. Gri **–** = şu an EQ'lu ses yok, kırmızı **×** = kapalı,
gri **!** = servis çalışmıyor. Sağ alttaki beyaz nokta = **Sabit** mod.
Üzerine gelince hangi uygulamanın hangi profilde olduğu görünür.

- **Sol tık**: Kulak Kesil penceresini açar.
- **Orta tık**: hızlı aç/kapat (Otomatik ↔ Kapalı).
- **Sağ tık**: menü — mod seçimi, bildirimler, kuralları yenile, EQ eğrileri, ayar klasörü, çıkış.

Baskın profil değişince kısa bir bildirim gelir (öncekinin yerine geçer, birikmez);
menüden "Bildirimler" ile kapatılabilir. Tepsiden "Çıkış" sadece simgeyi kapatır,
EQ çalışmaya devam eder; geri açmak için `kulak-tepsi`.

## Ayarları değiştirme

Ayarlar: `~/.config/oto-eq/`

- **Atamalar** (en kolayı) — pencerede ⋯ menüsü ya da Kurallar sayfası; `atamalar.json`
  dosyasına yazılır ve diğer tüm kurallardan önce gelir.
- **`kurallar.toml`** — hangi uygulama hangi profile gider. Kendi kuralını
  `[[kural]]` olarak ekle (dosyadaki örneklere bak), sonra `kulak yenile`.
  Örnek: bir oyunu film gibi dinlemek için
  ```toml
  [[kural]]
  ad = "Witcher 3 film gibi"
  ikili = "witcher3\\.exe"
  kategori = "film"
  ```
- **`profiller.toml`** — EQ eğrileri (HS80 düzeltmesi + her profilin ek
  filtreleri). Düzenledikten sonra `kulak eq-uret`, görmek için `kulak eq-ciz`.

## Mikrofon

`oto-eq-mic.service` HS80 mikrofonundan işlenmiş bir sanal mikrofon üretir:
**Kulak Kesil · Mikrofon** (gürültü bastırma, netlik, kompresör). Kurulum varsayılan
mikrofonu değiştirmez; pencereden ya da `kulak mikrofon varsayilan` ile seç, veya
Discord'da giriş cihazı olarak elle seç. Discord'da gürültü azaltma (Krisp), otomatik
kazanç ve yankı giderme kapalı olsun. Kaldırırken varsayılan mikrofon buysa önce HS80
mikrofonuna geri alınır. Varsayılan giriş HS80 mikrofonudur; başka bir mikrofon için
`~/.config/oto-eq/mikrofon.toml` içinde `giris_desen` (düzenli ifade) ya da `giris_sabit`
(kaynak adı) ayarla.

## Nasıl çalışır

İki systemd kullanıcı servisi girişte kendiliğinden başlar:

- `oto-eq-sinks.service` — 5 sanal çıkış (4 EQ'lu + Düz) (ayrı bir PipeWire filter-chain
  süreci). PipeWire'a bağlıdır: PipeWire yeniden başlarsa bu da onunla
  birlikte yeniden başlar (`BindsTo` + `WantedBy=pipewire.service`).
- `oto-eq.service` — yönlendirici: sesleri tanır ve doğru çıkışa taşır
  (`WantedBy=default.target`, çökerse 2 sn sonra yeniden başlar; PipeWire
  yeniden başlasa da çalışmaya devam eder).

Tepsi `~/.config/autostart/oto-eq-tray.desktop` ile girişte açılır.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| `kulak` "servis çalışmıyor" diyor | `systemctl --user start oto-eq` · `kulak gunluk` |
| `kulak durum`: "EQ çıkışları HAZIR DEĞİL" | `systemctl --user restart oto-eq-sinks` · `systemctl --user status oto-eq-sinks` |
| "Kulaklık aktif değil" | Varsayılan çıkış bir kulaklık olmalı (ses ayarlarından seç) |
| Bir uygulama yanlış profilde | `kulak durum` ile nedenini gör, `kurallar.toml`'a `[[kural]]` ekle, `kulak yenile` |
| Bir sesi elle başka çıkışa taşıdım | Kulak Kesil buna dokunmaz; geri almak için ses ayarlarından kulaklığa taşı |
| Tepsi simgesi yok | `kulak-tepsi` (zaten açıksa ikinci kopya açılmaz) |
| `kulak` bulunamadı (fish) | `fish_add_path ~/.local/bin` |

## Kurulum / kaldırma

Gereksinimler: PipeWire + `pipewire-pulse`, systemd kullanıcı oturumu, Python 3.11+, PyQt6,
numpy, LSP eklentileri (LV2); mikrofon için noise-suppression-for-voice; ses analizi için
isteğe bağlı `onnxruntime` + modeller (`./tools/download_models.sh`, bkz.
[docs/MODELS.md](docs/MODELS.md)). `install.sh` eksikleri denetler ve söyler.
Arch / CachyOS:

```bash
sudo pacman -S --needed pipewire-pulse lsp-plugins-lv2 python-pyqt6 python-numpy \
    noise-suppression-for-voice python-onnxruntime
```

Servisler kaynağı `~/.local/src/oto-eq` yolunda bekler:

```bash
git clone https://github.com/sobelendimm/kulak-kesil.git ~/.local/src/oto-eq
cd ~/.local/src/oto-eq
./install.sh --dry-run    # önce ne yapacağını gör
./install.sh              # kur (tekrar çalıştırmak güvenli)
./install.sh --force-config   # ayarları varsayılana döndür (eskiler yedeklenir)

./uninstall.sh            # her şeyi kaldırır, ayarların (~/.config/oto-eq) kalır
./uninstall.sh --purge    # ayarları da siler
```

Kurulum şunları ekler: üç systemd kullanıcı servisi (EQ çıkışları, yönlendirici, mikrofon), `~/.local/bin/kulak`,
`kulak-tepsi`, `kulak-app`, tepsi otomatik başlatma, uygulama menüsünde ve
masaüstünde "Kulak Kesil" kısayolu. `uninstall.sh` bunların hepsini geri alır.

Güncelleme: `git pull && ./install.sh`.

## Lisans

[GPL-3.0-or-later](LICENSE). İsteğe bağlı ses analizi modelleri bu lisansa **dahil değildir**:
üçüncü taraf, ticari olmayan kullanım lisanslı indirmelerdir (bkz. [docs/MODELS.md](docs/MODELS.md)).
