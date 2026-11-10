Name:           youtube-feed-reader
Version:        0.1.0
Release:        1%{?dist}
Summary:        YouTube RSS reader with per-channel content filters
License:        LicenseRef-Unspecified
URL:            https://github.com/bilalyusufersin/youtube-feed-reader
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3
BuildRequires:  desktop-file-utils
Requires:       python3 >= 3.10
Requires:       python3dist(pyside6) >= 6.6
Requires:       python3dist(feedparser) >= 6.0.11
Requires:       python3dist(httpx) >= 0.27
Requires:       python3dist(yt-dlp) >= 2026.1.1

%description
A Linux desktop reader for YouTube subscriptions, with separate filters for
regular videos, Shorts and live streams, Watch Later, groups and local storage.
The interface is currently in Turkish. Tested on Fedora KDE/Wayland.
No project source-code license has been selected; packaging does not add one.

%prep
%setup -q

%build
# Pure Python application; no compilation step is needed.

%install
mkdir -p %{buildroot}%{_datadir}/%{name}
cp -a main.py ytrss %{buildroot}%{_datadir}/%{name}/
find %{buildroot}%{_datadir}/%{name} -type d -name __pycache__ -exec rm -rf '{}' +
install -Dpm0755 packaging/ytrss %{buildroot}%{_bindir}/ytrss
install -Dpm0644 packaging/ytrss.desktop %{buildroot}%{_datadir}/applications/ytrss.desktop
install -Dpm0644 ytrss/assets/ytrss.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/ytrss.svg

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/ytrss.desktop
python3 -c "import ast,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('%{buildroot}%{_datadir}/%{name}').rglob('*.py')]"

%files
%doc README.md INSTALL_LINUX.md docs
%{_bindir}/ytrss
%{_datadir}/%{name}/
%{_datadir}/applications/ytrss.desktop
%{_datadir}/icons/hicolor/scalable/apps/ytrss.svg

%changelog
* Tue Nov 10 2026 Bilal Yusuf Ersin - 0.1.0-1
- First Fedora Linux installer package.
