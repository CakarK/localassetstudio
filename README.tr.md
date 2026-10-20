# Local Asset Studio

Yerel GPU üzerinde çalışan; prompt'tan referans görsel, 3D model, PBR ve Blender teslim paketi üreten web arayüzü.

> **Desteklenen tam üretim akışı için en az 24 GB VRAM'e sahip NVIDIA ekran kartı gereklidir.** Geliştirme sistemi Windows 11 + RTX 3090 24 GB kullanır. Daha düşük VRAM veya yalnız CPU ile uçtan uca üretim doğrulanmamıştır. Qwen görsel değerlendirmesi yaklaşık 19.000 MiB boş VRAM ister.

[English README](README.md) · [Kurulum](docs/INSTALLATION.md) · [Kullanım](docs/USAGE.md)

- Türkçe/İngilizce prompt veya kendi isteğe bağlı kataloğunuzdan varlık hazırlama.
- Referansı inceleme, prompt'u değiştirerek yeni görsel hazırlama ve seçilenleri 3D sırasına alma.
- Çalışan varlığı, gerçek aşamayı, ölçülen süreyi, bekleyen revizyonları ve hata nedenlerini aynı sırada görme.
- Bekleyen işleri taşıma/çıkarma, duraklatma ve devam ettirme.
- Hunyuan3D 2.1, poligon optimizasyonu, PBR, Blender ve teknik/görsel raporlar.
- Tarayıcıda 3D inceleme; onay, revizyon veya ret. Teknik puan ya da ham model durumu insan onayını engellemez; gerçek teknik eksikler ayrıca korunur.
- Revizyon notlarından yeni aday üretimi; eski model korunur ve aynı karar için sınırsız otomatik retry yapılmaz.
- Aynı güvenilir yerel ağdaki diğer bilgisayarlardan erişim.

Windows 11 x64 üzerinde PowerShell açıp **tek komut** çalıştırın. Git veya Python'un önceden kurulu olması gerekmez:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/CakarK/localassetstudio/main/install.ps1 | iex"
```

Kurucu `%LOCALAPPDATA%\LocalAssetStudio` altına projeyi indirir; taşınabilir Git/uv/Blender/Ollama, Python 3.12.11, ayrı Hunyuan ve görsel ComfyUI ortamları, üç görsel üretici, Hunyuan/DINO/U2Net ve Ollama modellerini hazırlar. CUDA/eklenti/Blender kontrollerinden sonra servisleri başlatıp arayüzü açar. **Kurulum asset üretimi veya AI inference başlatmaz.**

Yaklaşık **93 GB model indirmesi**, ayrıca paket ve çalışma ortamı indirmeleri vardır. Temiz kurulum için **180 GiB boş alan**, uygun NVIDIA sürücüsü ve **24 GB VRAM** gerekir. Bağlantı kesilirse aynı komutla devam edebilirsiniz. Eski model ve kaynak sürümleri uyumsuzsa üzerine yazılmaz. [Bileşen lisansları](THIRD_PARTY_NOTICES.md) geçerlidir.

Projeyi zaten klonladıysanız:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-LocalAssetStudio.ps1
```

`-Plan` indirme/kurulum planını hiçbir değişiklik yapmadan gösterir. `-NoStart` servisleri son aşamada başlatmaz; Ollama model hazırlığı sırasında yerel Ollama servisi gerekir. Sonraki açılışlarda `Start-LocalAssetStudio.ps1` bütün servisleri birlikte başlatır. Farklı disk, yeniden çalıştırma ve servis çakışmaları için [kurulum belgesine](docs/INSTALLATION.md) bakın.

Kurucunun hash, kesilen indirme, dosya koruma ve Windows PowerShell testleri ile bağımlılık çözümlemesi doğrulandı. **Temiz Windows'ta bütün büyük indirmeler ve GPU üretimi henüz uçtan uca sertifikalandırılmadı.** Kurucu hedef bilgisayarda gerçek smoke kontrollerini yapar; başarısız kontrolü tamamlandı saymaz.

Atölye: `http://localhost:8088` · Canlı sıra: `/queue` · 3D inceleme: `/reviews`.

Yerel ağdaki başka bir bilgisayardan `http://<sunucu-IP>:8088` kullanılır. Erişim kodu sunucu bilgisayardaki `AI3D/State/web-access-code.txt` dosyasında üretilir. İnternete açık barındırma ve kişi bazlı kullanıcı yönetimi bu sürümün kapsamı değildir.

GLB/FBX/OBJ/Blend, mevcut PBR haritaları, LOD, önizlemeler ve raporlar yalnız ilgili aşamalar tamamlanırsa oluşur. Eksik dosya veya teknik PASS uydurulmaz. Zemin/kaide üretmemek varsayılan politikadır; istenen sonucun gerçekten oluşması insan kontrolü gerektirir. Revizyon mevcut mesh üzerinde birebir lokal düzenleme garantisi vermez. Unreal projenize otomatik import yapılmaz.

Projenin özgün kodu MIT lisanslıdır. AI modellerinin, ComfyUI, Blender ve diğer bileşenlerin kendi lisansları ayrıca geçerlidir. [Lisans ve bağımlılık notları](THIRD_PARTY_NOTICES.md).

Bu genel dağıtım oyun kataloğu, özel oyun promptu, karakter listesi, dönem sistemi veya sabit oyun grid ölçüsü içermez. Katalog boş gelir; arayüz sıfırdan prompt moduyla açılır. Görsel stilinizi ve negatif promptunuzu kendiniz girebilirsiniz. `LAS_CATALOG_PATH` ile kendi JSON kataloğunuzu seçebilirsiniz.
