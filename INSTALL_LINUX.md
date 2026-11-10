# Linux kurulumu / Linux installation

**Bu sürüm yalnızca Linux içindir. Doğrulanan ortam: Fedora 44, KDE Plasma/Wayland, x86_64. Windows ve macOS paketi bulunmuyor.**

İndirme sayfası: https://github.com/bilalyusufersin/youtube-feed-reader/releases/latest

## Fedora: RPM paketi (önerilen)

1. Releases sayfasından `youtube-feed-reader-0.1.0-1.fc44.noarch.rpm` dosyasını indirin.
2. Dosyanın bulunduğu klasörde terminal açıp çalıştırın:

```bash
sudo dnf install ./youtube-feed-reader-0.1.0-1.fc44.noarch.rpm
```

DNF gereken Python/Qt bağımlılıklarını Fedora depolarından yükler. Ardından uygulama menüsünden **YouTube Akış Okuyucu** uygulamasını açın. Terminalden de `ytrss` komutuyla çalışır.

Kaldırma: `sudo dnf remove youtube-feed-reader`. Kullanıcı verileri XDG veri dizininde ayrı tutulur.

## Diğer Linux dağıtımları: kaynak kurulum arşivi

Bu yöntem Fedora dışında ayrıca doğrulanmadı. Python 3.10+ ve çalışan bir `venv`/`pip` kurulumu, grafik masaüstü ve bağımlılık indirmek için internet gerekir. Dağıtımınız Python'un `venv` desteğini ayrı paketliyorsa önce o paketi kurun.

1. `youtube-feed-reader-0.1.0-linux-source.tar.gz` dosyasını indirin ve çıkarın.
2. Çıkardığınız `youtube-feed-reader-0.1.0` klasöründe terminal açın.
3. **Sudo kullanmadan** çalıştırın:

```bash
./install.sh
```

Kurulum yardımcısı uygulamayı varsayılan olarak `~/.local/share/youtube-feed-reader` altına kopyalar, gereken bağımlılıkları özel Python ortamına yükler ve kullanıcı uygulama menüsüne ekler. Kurulumdan sonra indirdiğiniz/çıkardığınız arşiv klasörüne bağlı kalmaz.

Özel klasör veya menü kısayolu olmadan kurulum:

```bash
./install.sh --prefix /path/to/application --no-desktop
```

## Sağlama toplamları

Paketlerin yanındaki `SHA256SUMS` dosyasını da indirirseniz aynı klasörde bütünlüğü kontrol edebilirsiniz:

```bash
sha256sum --ignore-missing -c SHA256SUMS
```

## English

This release is **Linux-only**, tested on **Fedora 44 KDE/Wayland (x86_64)**. There are no Windows or macOS builds.

- **Fedora:** download the `.rpm`, run `sudo dnf install ./youtube-feed-reader-0.1.0-1.fc44.noarch.rpm`, and open **YouTube Akış Okuyucu** from the application menu. DNF resolves the runtime dependencies.
- **Other Linux distributions (not separately verified):** extract the `linux-source.tar.gz` bundle and run `./install.sh` without sudo. Python 3.10+, venv/pip, a graphical desktop and an internet connection are required. This bundle includes source and an installation helper; it is not a self-contained binary.
- The interface is currently in Turkish. User data stays in the XDG application directories described in the README.
