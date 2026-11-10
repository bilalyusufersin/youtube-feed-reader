"""YouTube aboneliklerini içe ve dışa aktarma modülü.

Desteklenen içe aktarma kaynakları:
1. Google Takeout CSV (`abonelikler.csv` / `subscriptions.csv`) — Türkçe ve İngilizce başlıklı
2. Google Takeout JSON (`subscriptions.json`)
3. OPML / XML (`subscriptions.opml`, `.xml`) — klasör/grup yapısını koruyarak
4. NewPipe ve FreeTube JSON abonelik yedekleri
5. Çok satırlı metin / kanal bağlantısı listesi (@kullanıcı, /channel/UC…, RSS)
6. Yerel web tarayıcısı çerezleriyle (`yt-dlp --cookies-from-browser`) doğrudan
   `https://www.youtube.com/feed/channels` sayfasından abonelik çekme (isteğe bağlı)
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp

from .db import Database
from .youtube.http import Cancelled
from .youtube.source import YouTubeSource
from .youtube.urls import (
    CHANNEL_ID_RE,
    InvalidChannelInput,
    channel_feed_url,
    parse_channel_input,
)

log = logging.getLogger(__name__)

_ANY_CHANNEL_ID_RE = re.compile(r"\b(UC[0-9A-Za-z_-]{22})\b")
BACKUP_VERSION = 1


@dataclass(slots=True)
class SubscriptionCandidate:
    channel_id: str | None = None
    title: str | None = None
    handle: str | None = None
    raw_input: str | None = None
    group_name: str | None = None


@dataclass(slots=True)
class UnsupportedEntry:
    title: str
    url: str
    reason: str = "YouTube dışı kaynak"


@dataclass(slots=True)
class SubscriptionParseReport:
    candidates: list[SubscriptionCandidate] = field(default_factory=list)
    unsupported: list[UnsupportedEntry] = field(default_factory=list)


@dataclass(slots=True)
class ImportSummary:
    total_found: int = 0
    added: int = 0
    skipped_existing: int = 0
    unsupported_count: int = 0
    failed: int = 0
    added_channel_ids: list[str] | None = None
    errors: list[str] | None = None


@dataclass(slots=True)
class BackupRestoreSummary:
    version: int = BACKUP_VERSION
    groups_added: int = 0
    channels_added: int = 0
    channels_merged: int = 0
    videos_added: int = 0
    videos_merged: int = 0
    watch_later_count: int = 0


def parse_subscriptions_report_text(content: str, filename: str = "") -> SubscriptionParseReport:
    """Dosya içeriğini (CSV, OPML/XML, JSON veya düz metin) ayrıştırır; desteklenmeyen YouTube dışı kaynakları raporlar."""
    text = (content or "").lstrip("\ufeff").strip()
    if not text:
        return SubscriptionParseReport()

    lower_name = filename.lower()
    if lower_name.endswith((".opml", ".xml")) or text.startswith("<?xml") or "<opml" in text[:400].lower():
        cands, unsup = _parse_opml(text)
        if cands or unsup:
            return SubscriptionParseReport(
                candidates=_deduplicate_candidates(cands),
                unsupported=unsup,
            )

    if lower_name.endswith(".json") or text.startswith(("{", "[")):
        cands, unsup = _parse_json(text)
        if cands or unsup:
            return SubscriptionParseReport(
                candidates=_deduplicate_candidates(cands),
                unsupported=unsup,
            )

    if lower_name.endswith(".csv") or ("," in text.splitlines()[0] and "UC" in text):
        cands, unsup = _parse_csv(text)
        if cands or unsup:
            return SubscriptionParseReport(
                candidates=_deduplicate_candidates(cands),
                unsupported=unsup,
            )

    cands, unsup = _parse_lines(text)
    return SubscriptionParseReport(
        candidates=_deduplicate_candidates(cands),
        unsupported=unsup,
    )


def parse_subscriptions_report_file(path: Path | str) -> SubscriptionParseReport:
    p = Path(path)
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1254", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return parse_subscriptions_report_text(text, filename=p.name)


def parse_subscriptions_text(content: str, filename: str = "") -> list[SubscriptionCandidate]:
    """Dosya içeriğini (CSV, OPML/XML, JSON veya düz metin) otomatik tanıyıp aday kanalları döndürür."""
    return parse_subscriptions_report_text(content, filename=filename).candidates


def parse_subscriptions_file(path: Path | str) -> list[SubscriptionCandidate]:
    return parse_subscriptions_report_file(path).candidates


def _deduplicate_candidates(items: list[SubscriptionCandidate]) -> list[SubscriptionCandidate]:
    out: list[SubscriptionCandidate] = []
    seen_cids: set[str] = set()
    seen_raw: set[str] = set()

    for it in items:
        if it.channel_id and CHANNEL_ID_RE.match(it.channel_id):
            if it.channel_id in seen_cids:
                continue
            seen_cids.add(it.channel_id)
            out.append(it)
        elif it.raw_input:
            key = it.raw_input.strip().lower()
            if key in seen_raw:
                continue
            seen_raw.add(key)
            out.append(it)
    return out


def _is_youtube_host(url_str: str) -> bool:
    try:
        host = (urlparse(url_str).hostname or "").lower()
    except ValueError:
        return False
    return host.endswith("youtube.com") or host == "youtu.be" or host.endswith("youtube-nocookie.com")


def _parse_csv(text: str) -> tuple[list[SubscriptionCandidate], list[UnsupportedEntry]]:
    """Google Takeout `abonelikler.csv` / `subscriptions.csv` ayrıştırıcısı."""
    out: list[SubscriptionCandidate] = []
    unsup: list[UnsupportedEntry] = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row:
            continue
        cells = [c.strip() for c in row if c is not None]
        if not any(cells):
            continue

        # İlk sütun doğrudan UC... kanal kimliği mi? (Google Takeout standart biçimi)
        c0 = cells[0]
        if CHANNEL_ID_RE.match(c0):
            title = cells[2] if len(cells) >= 3 and cells[2] else (cells[-1] if len(cells) > 1 else c0)
            out.append(SubscriptionCandidate(channel_id=c0, title=title or c0))
            continue

        # Satırdaki herhangi bir hücrede kanal bağlantısı veya UC... var mı?
        found_cid: str | None = None
        for cell in cells:
            m = _ANY_CHANNEL_ID_RE.search(cell)
            if m:
                found_cid = m.group(1)
                break
        if found_cid:
            non_url_cells = [
                c for c in cells
                if c != found_cid and not c.startswith(("http://", "https://"))
            ]
            title = non_url_cells[-1] if non_url_cells else found_cid
            out.append(SubscriptionCandidate(channel_id=found_cid, title=title))
            continue

        # Eğer satırda http/https bağlantısı varsa ama YouTube kanalı değilse desteklenmeyen olarak kaydet
        url_cells = [c for c in cells if c.startswith(("http://", "https://"))]
        if url_cells:
            u = url_cells[0]
            non_url = [c for c in cells if c != u]
            label = non_url[-1] if non_url else u
            try:
                ref = parse_channel_input(u)
                if ref.kind == "channel_id":
                    out.append(SubscriptionCandidate(channel_id=ref.value, title=label or ref.value))
                else:
                    out.append(SubscriptionCandidate(title=label or None, raw_input=u))
            except InvalidChannelInput as e:
                reason = "YouTube dışı kaynak" if not _is_youtube_host(u) else str(e)
                unsup.append(UnsupportedEntry(title=label, url=u, reason=reason))
    return out, unsup


def _parse_opml(text: str) -> tuple[list[SubscriptionCandidate], list[UnsupportedEntry]]:
    """OPML / XML abonelik dosyalarını (NewsFlash vb., grup klasörleriyle birlikte) ayrıştırır."""
    out: list[SubscriptionCandidate] = []
    unsup: list[UnsupportedEntry] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out, unsup

    def _walk(elem: ET.Element, current_group: str | None = None) -> None:
        for child in elem:
            tag = child.tag.rsplit("}", 1)[-1].lower()
            if tag != "outline":
                _walk(child, current_group)
                continue

            xml_url = (child.attrib.get("xmlUrl") or child.attrib.get("xmlurl") or "").strip()
            html_url = (child.attrib.get("htmlUrl") or child.attrib.get("htmlurl") or "").strip()
            url_attr = (child.attrib.get("url") or "").strip()
            label = (
                child.attrib.get("title")
                or child.attrib.get("text")
                or child.attrib.get("name")
                or ""
            ).strip()

            urls_to_try = [u for u in (xml_url, html_url, url_attr) if u]
            if urls_to_try:
                matched = False
                for candidate_url in urls_to_try:
                    try:
                        ref = parse_channel_input(candidate_url)
                        if ref.kind == "channel_id":
                            out.append(
                                SubscriptionCandidate(
                                    channel_id=ref.value,
                                    title=label or ref.value,
                                    group_name=current_group,
                                )
                            )
                        else:
                            out.append(
                                SubscriptionCandidate(
                                    title=label or None,
                                    handle=ref.value if ref.kind == "handle" else None,
                                    raw_input=candidate_url,
                                    group_name=current_group,
                                )
                            )
                        matched = True
                        break
                    except InvalidChannelInput:
                        m = _ANY_CHANNEL_ID_RE.search(candidate_url)
                        if m:
                            out.append(
                                SubscriptionCandidate(
                                    channel_id=m.group(1),
                                    title=label or m.group(1),
                                    group_name=current_group,
                                )
                            )
                            matched = True
                            break
                if not matched:
                    primary_url = urls_to_try[0]
                    reason = (
                        "YouTube dışı kaynak"
                        if not any(_is_youtube_host(u) for u in urls_to_try)
                        else "Geçersiz veya desteklenmeyen YouTube bağlantısı"
                    )
                    unsup.append(
                        UnsupportedEntry(
                            title=label or primary_url,
                            url=primary_url,
                            reason=reason,
                        )
                    )
            else:
                # Alt öğeleri olan klasör / grup düğümü
                next_group = label or current_group
                _walk(child, next_group)

    _walk(root, None)
    return out, unsup


def _parse_json(text: str) -> tuple[list[SubscriptionCandidate], list[UnsupportedEntry]]:
    """Google Takeout JSON, NewPipe JSON ve FreeTube JSON/NDJSON biçimlerini ayrıştırır."""
    out: list[SubscriptionCandidate] = []
    unsup: list[UnsupportedEntry] = []
    objects: list = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            objects = parsed
        elif isinstance(parsed, dict):
            if isinstance(parsed.get("subscriptions"), list):
                objects = parsed["subscriptions"]
            elif isinstance(parsed.get("items"), list):
                objects = parsed["items"]
            else:
                objects = [parsed]
    except json.JSONDecodeError:
        # FreeTube NDJSON (her satır bir JSON nesnesi)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    objects.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    for obj in objects:
        if not isinstance(obj, dict):
            continue
        # 1. Google Takeout abonelik JSON biçimi
        snippet = obj.get("snippet")
        if isinstance(snippet, dict):
            res_id = snippet.get("resourceId") or {}
            cid = res_id.get("channelId") if isinstance(res_id, dict) else None
            title = snippet.get("title")
            if cid and CHANNEL_ID_RE.match(str(cid)):
                out.append(SubscriptionCandidate(channel_id=str(cid), title=str(title or cid)))
                continue

        # 2. Doğrudan channelId / id / url alanları (NewPipe / FreeTube / genel JSON)
        cid = obj.get("channel_id") or obj.get("channelId") or obj.get("id")
        title = obj.get("title") or obj.get("name") or obj.get("author")
        url = obj.get("url") or obj.get("channel_url") or obj.get("xmlUrl")

        if isinstance(cid, str) and CHANNEL_ID_RE.match(cid):
            out.append(SubscriptionCandidate(channel_id=cid, title=str(title or cid)))
            continue

        if isinstance(url, str) and url.strip():
            u = url.strip()
            try:
                ref = parse_channel_input(u)
                if ref.kind == "channel_id":
                    out.append(SubscriptionCandidate(channel_id=ref.value, title=str(title or ref.value)))
                else:
                    out.append(SubscriptionCandidate(title=str(title) if title else None, raw_input=u))
            except InvalidChannelInput as e:
                reason = "YouTube dışı kaynak" if not _is_youtube_host(u) else str(e)
                unsup.append(UnsupportedEntry(title=str(title or u), url=u, reason=reason))
    return out, unsup


def _parse_lines(text: str) -> tuple[list[SubscriptionCandidate], list[UnsupportedEntry]]:
    """Çok satırlı kanal listesini veya yapıştırılmış metni ayrıştırır."""
    out: list[SubscriptionCandidate] = []
    unsup: list[UnsupportedEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        parsed_any = False
        for tok in (line, tokens[0], tokens[-1]):
            try:
                ref = parse_channel_input(tok)
                if ref.kind == "channel_id":
                    out.append(SubscriptionCandidate(channel_id=ref.value, title=ref.value))
                else:
                    out.append(
                        SubscriptionCandidate(
                            handle=ref.value if ref.kind == "handle" else None,
                            raw_input=tok,
                        )
                    )
                parsed_any = True
                break
            except InvalidChannelInput:
                continue

        if not parsed_any:
            found_ids = list(_ANY_CHANNEL_ID_RE.finditer(line))
            if found_ids:
                for m in found_ids:
                    out.append(SubscriptionCandidate(channel_id=m.group(1), title=m.group(1)))
            else:
                url_toks = [t for t in tokens if t.startswith(("http://", "https://"))]
                if url_toks:
                    u = url_toks[0]
                    reason = (
                        "YouTube dışı kaynak"
                        if not _is_youtube_host(u)
                        else "Geçersiz YouTube kanal bağlantısı"
                    )
                    unsup.append(UnsupportedEntry(title=line, url=u, reason=reason))
                else:
                    unsup.append(UnsupportedEntry(title=line, url=line, reason="Tanınmayan kanal adresi"))
    return out, unsup


def fetch_subscriptions_from_browser(
    browser: str = "firefox",
    timeout: int = 25,
) -> list[SubscriptionCandidate]:
    """Kullanıcının yerel tarayıcı oturumundaki çerezlerle YouTube abonelik listesini çeker.

    API anahtarı veya Google OAuth kurulumu gerektirmez; tarayıcıda YouTube oturumu açıksa
    `https://www.youtube.com/feed/channels` sayfasındaki kanalları okur.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "socket_timeout": timeout,
        "cookiesfrombrowser": (browser,),
    }
    out: list[SubscriptionCandidate] = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://www.youtube.com/feed/channels", download=False, process=False)
        for entry in info.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("channel_id") or entry.get("id") or ""
            title = entry.get("title") or entry.get("channel") or entry.get("uploader") or ""
            url = entry.get("url") or entry.get("channel_url") or ""
            if CHANNEL_ID_RE.match(str(cid)):
                out.append(SubscriptionCandidate(channel_id=str(cid), title=str(title or cid)))
            elif url:
                try:
                    ref = parse_channel_input(str(url))
                    if ref.kind == "channel_id":
                        out.append(SubscriptionCandidate(channel_id=ref.value, title=str(title or ref.value)))
                    else:
                        out.append(SubscriptionCandidate(title=str(title) if title else None, raw_input=str(url)))
                except InvalidChannelInput:
                    pass
    return _deduplicate_candidates(out)


def import_candidates_to_db(
    db: Database,
    source: YouTubeSource,
    candidates: list[SubscriptionCandidate],
    *,
    default_group_id: int | None = None,
    prefs: tuple[int | None, int | None, int | None] = (None, None, None),
    cancel: threading.Event | None = None,
    on_progress=None,
) -> ImportSummary:
    """Aday kanalları veritabanına ekler.

    `channel_id` değeri bilinen kanallar (Takeout CSV/JSON, OPML) ağ isteği yapmadan
    anında eklenir; yalnızca `@kullanıcı` veya `/c/` adresli kanallar ağ üzerinden çözülür.
    """
    summary = ImportSummary(
        total_found=len(candidates),
        added_channel_ids=[],
        errors=[],
    )
    if not candidates:
        return summary

    # Grup adlarını önbelleğe al
    group_map: dict[str, int] = {
        g["name"].strip().lower(): int(g["id"]) for g in db.groups()
    }

    def _resolve_group(name: str | None) -> int | None:
        if not name or not name.strip():
            return default_group_id
        key = name.strip().lower()
        if key in group_map:
            return group_map[key]
        gid = db.add_group(name.strip())
        group_map[key] = gid
        return gid

    for idx, cand in enumerate(candidates, start=1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()

        gid = _resolve_group(cand.group_name)
        cid = cand.channel_id
        title = (cand.title or "").strip()
        handle = cand.handle

        if not cid and cand.raw_input:
            try:
                ref = parse_channel_input(cand.raw_input)
                if ref.is_resolved:
                    cid = ref.value
                else:
                    resolved = source.resolve(ref)
                    cid = resolved.channel_id
                    title = title or (resolved.title or cid)
                    handle = handle or resolved.handle
            except Exception as e:  # noqa: BLE001
                summary.failed += 1
                summary.errors.append(f"{cand.raw_input}: {e}")
                if on_progress:
                    on_progress(idx, len(candidates), cand.raw_input or "")
                continue

        if not cid or not CHANNEL_ID_RE.match(cid):
            summary.failed += 1
            continue

        inserted = db.insert_channel(
            cid,
            title or cid,
            handle,
            None,
            group_id=gid,
        )
        if inserted:
            if any(p is not None for p in prefs):
                db.set_channel_prefs(cid, *prefs)
            summary.added += 1
            summary.added_channel_ids.append(cid)
        else:
            if title and title != cid:
                db.update_channel_info(cid, title=title, handle=handle)
            summary.skipped_existing += 1

        if on_progress:
            on_progress(idx, len(candidates), title or cid)

    return summary


def export_subscriptions_opml(db: Database, path: Path | str) -> int:
    """Mevcut kanalları (ve kanal gruplarını) standart OPML 2.0 dosyası olarak dışa aktarır."""
    channels = db.channels()
    groups = {int(g["id"]): g["name"] for g in db.groups()}

    opml = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(opml, "head")
    title_el = ET.SubElement(head, "title")
    title_el.text = "YouTube Akış Okuyucu Abonelikleri"
    body = ET.SubElement(opml, "body")

    grouped: dict[int | None, list] = {}
    for ch in channels:
        grouped.setdefault(ch["group_id"], []).append(ch)

    def _add_channel_outline(parent: ET.Element, ch) -> None:
        cid = ch["channel_id"]
        ET.SubElement(
            parent,
            "outline",
            {
                "type": "rss",
                "text": ch["title"],
                "title": ch["title"],
                "xmlUrl": channel_feed_url(cid),
                "htmlUrl": f"https://www.youtube.com/channel/{cid}",
            },
        )

    for gid, gname in groups.items():
        ch_list = grouped.get(gid) or []
        if not ch_list:
            continue
        folder = ET.SubElement(body, "outline", {"text": gname, "title": gname})
        for ch in ch_list:
            _add_channel_outline(folder, ch)

    for ch in grouped.get(None, []):
        _add_channel_outline(body, ch)

    ET.indent(opml, space="  ")
    xml_bytes = ET.tostring(opml, encoding="utf-8", xml_declaration=True)
    Path(path).write_bytes(xml_bytes)
    return len(channels)


# ---------------------------------------------------------------------------
# Sürümlü Tam Yedekleme ve Birleştirerek Geri Yükleme (Backup / Restore)
# ---------------------------------------------------------------------------

def create_backup_payload(db: Database) -> dict[str, Any]:
    """Kanalları, grupları, kanal tür/bildirim tercihlerini, içerikleri,
    okundu/yıldızlı/izlendi durumlarını, Sonra İzle sırasını ve kanıtları kapsayan
    sürümlü yedek sözlüğü oluşturur.
    """
    groups = db.groups()
    group_id_to_name = {int(g["id"]): g["name"] for g in groups}

    settings_rows = db.q("SELECT key, value FROM settings")
    settings_dict: dict[str, Any] = {}
    for sr in settings_rows:
        try:
            settings_dict[sr["key"]] = json.loads(sr["value"])
        except json.JSONDecodeError:
            settings_dict[sr["key"]] = sr["value"]

    channels_out: list[dict[str, Any]] = []
    for ch in db.channels():
        d = dict(ch)
        d["group_name"] = group_id_to_name.get(d.get("group_id")) if d.get("group_id") is not None else None
        channels_out.append(d)

    videos_out = [
        dict(v)
        for v in db.q("SELECT * FROM videos ORDER BY published_at DESC, video_id")
    ]
    evidence_out = [
        dict(e)
        for e in db.q("SELECT video_id, signal, value, observed_at FROM evidence")
    ]
    watch_later_queue = db.watch_later_ids(include_watched=True)

    return {
        "format": "ytrss-backup",
        "version": BACKUP_VERSION,
        "created_at": int(time.time()),
        "settings": settings_dict,
        "groups": [
            {
                "name": g["name"],
                "sort_order": int(g["sort_order"]),
                "expanded": int(g["expanded"]),
            }
            for g in groups
        ],
        "channels": channels_out,
        "videos": videos_out,
        "evidence": evidence_out,
        "watch_later_queue": watch_later_queue,
    }


def export_backup_file(db: Database, path: Path | str) -> dict[str, Any]:
    payload = create_backup_payload(db)
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def restore_backup_payload(
    db: Database,
    payload: dict[str, Any],
    *,
    merge: bool = True,
) -> BackupRestoreSummary:
    """Sürümlü yedek verisini geri yükler.

    Varsayılan davranış birleştirme (`merge=True`) kipidir:
    - Mevcut kanalları veya içerikleri çoğaltmaz.
    - Mevcut kanal filtrelerini, okundu/yıldızlı/izlendi durumlarını ve kullanıcı
      tür seçimlerini sessizce ezmez; birleştirerek tamamlar.
    """
    if not isinstance(payload, dict) or payload.get("format") != "ytrss-backup":
        raise ValueError("Geçerli bir YouTube Akış Okuyucu yedek dosyası değil (format='ytrss-backup' bekleniyordu).")
    ver = payload.get("version")
    if not isinstance(ver, int) or ver < 1 or ver > BACKUP_VERSION:
        raise ValueError(f"Desteklenmeyen yedek sürümü: {ver!r}")

    summary = BackupRestoreSummary(version=ver)
    now = int(time.time())

    if not merge:
        with db.tx() as con:
            con.execute("DELETE FROM evidence")
            con.execute("DELETE FROM videos")
            con.execute("DELETE FROM channels")
            con.execute("DELETE FROM groups")

    # 1. Ayarlar (merge kipinde yalnızca yerel olarak ayarlanmamış anahtarları tamamla)
    existing_setting_keys = {r["key"] for r in db.q("SELECT key FROM settings")}
    for k, val in (payload.get("settings") or {}).items():
        if not merge or k not in existing_setting_keys:
            db.set_setting(k, val)

    # 2. Gruplar
    group_map: dict[str, int] = {
        g["name"].strip().lower(): int(g["id"]) for g in db.groups()
    }
    for g in payload.get("groups") or []:
        if not isinstance(g, dict):
            continue
        gname = (g.get("name") or "").strip()
        if not gname:
            continue
        key = gname.lower()
        if key not in group_map:
            gid = db.add_group(gname)
            if "expanded" in g:
                db.set_group_expanded(gid, bool(g["expanded"]))
            group_map[key] = gid
            summary.groups_added += 1

    # 3. Kanallar ve kanal bazlı tercihler
    for ch in payload.get("channels") or []:
        if not isinstance(ch, dict):
            continue
        cid = ch.get("channel_id")
        if not cid or not CHANNEL_ID_RE.match(str(cid)):
            continue
        gname = (ch.get("group_name") or "").strip()
        gid = None
        if gname:
            gkey = gname.lower()
            if gkey not in group_map:
                group_map[gkey] = db.add_group(gname)
                summary.groups_added += 1
            gid = group_map[gkey]

        existing_ch = db.channel(cid)
        if existing_ch is None:
            with db.tx() as con:
                con.execute(
                    "INSERT INTO channels(channel_id, title, handle, avatar_url, group_id, "
                    "show_video, show_short, show_live, notify_enabled, notify_video, "
                    "notify_short, notify_live, initial_sync_done, added_at, last_refresh_at, "
                    "last_success_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cid,
                        ch.get("title") or cid,
                        ch.get("handle"),
                        ch.get("avatar_url"),
                        gid,
                        ch.get("show_video"),
                        ch.get("show_short"),
                        ch.get("show_live"),
                        ch.get("notify_enabled"),
                        ch.get("notify_video"),
                        ch.get("notify_short"),
                        ch.get("notify_live"),
                        1,
                        int(ch.get("added_at") or now),
                        ch.get("last_refresh_at"),
                        ch.get("last_success_at"),
                    ),
                )
            summary.channels_added += 1
        else:
            # Birleştirme: yerel tercihler varsa koru, NULL olanları yedekten tamamla
            with db.tx() as con:
                con.execute(
                    "UPDATE channels SET "
                    "title=COALESCE(NULLIF(title, channel_id), ?), "
                    "handle=COALESCE(handle, ?), "
                    "avatar_url=COALESCE(avatar_url, ?), "
                    "group_id=COALESCE(group_id, ?), "
                    "show_video=COALESCE(show_video, ?), "
                    "show_short=COALESCE(show_short, ?), "
                    "show_live=COALESCE(show_live, ?), "
                    "notify_enabled=COALESCE(notify_enabled, ?), "
                    "notify_video=COALESCE(notify_video, ?), "
                    "notify_short=COALESCE(notify_short, ?), "
                    "notify_live=COALESCE(notify_live, ?) "
                    "WHERE channel_id=?",
                    (
                        ch.get("title") or cid,
                        ch.get("handle"),
                        ch.get("avatar_url"),
                        gid,
                        ch.get("show_video"),
                        ch.get("show_short"),
                        ch.get("show_live"),
                        ch.get("notify_enabled"),
                        ch.get("notify_video"),
                        ch.get("notify_short"),
                        ch.get("notify_live"),
                        cid,
                    ),
                )
            summary.channels_merged += 1

    # Mevcut Sonra İzle sırasını kaydet
    existing_wl_queue = db.watch_later_ids(include_watched=True)

    # 4. Videolar ve kullanıcı durumları (okundu, yıldızlı, izlendi, Sonra İzle, user_type)
    existing_cids = {r["channel_id"] for r in db.channels()}
    with db.tx() as con:
        for v in payload.get("videos") or []:
            if not isinstance(v, dict):
                continue
            vid = v.get("video_id")
            cid = v.get("channel_id")
            if not vid or not cid or cid not in existing_cids:
                continue
            cur = con.execute("SELECT * FROM videos WHERE video_id=?", (vid,)).fetchone()
            if cur is None:
                con.execute(
                    "INSERT INTO videos(video_id, channel_id, title, description, published_at, "
                    "date_estimated, updated_at, thumb_url, duration, duration_checked_at, views, "
                    "auto_type, user_type, live_state, premiere_state, scheduled_at, class_source, "
                    "class_reason, class_confidence, classified_at, classifier_version, availability, "
                    "meta_checked_at, meta_attempts, next_meta_check_at, is_read, is_starred, "
                    "is_watched, watch_later, watch_later_order, watch_later_added_at, "
                    "notified_new, notified_live_start, first_seen_at, last_seen_in_feed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        vid,
                        cid,
                        v.get("title") or "",
                        v.get("description") or "",
                        v.get("published_at") or now,
                        int(v.get("date_estimated") or 0),
                        v.get("updated_at"),
                        v.get("thumb_url") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                        v.get("duration"),
                        v.get("duration_checked_at"),
                        v.get("views"),
                        v.get("auto_type") or "unknown",
                        v.get("user_type"),
                        v.get("live_state"),
                        v.get("premiere_state"),
                        v.get("scheduled_at"),
                        v.get("class_source"),
                        v.get("class_reason"),
                        v.get("class_confidence"),
                        v.get("classified_at"),
                        v.get("classifier_version"),
                        v.get("availability"),
                        v.get("meta_checked_at"),
                        int(v.get("meta_attempts") or 0),
                        v.get("next_meta_check_at"),
                        int(bool(v.get("is_read"))),
                        int(bool(v.get("is_starred"))),
                        int(bool(v.get("is_watched"))),
                        int(bool(v.get("watch_later"))),
                        v.get("watch_later_order"),
                        v.get("watch_later_added_at"),
                        1,  # Yedekten geri yüklenen içerik yeni bildirim yağmuru üretmemeli
                        int(bool(v.get("notified_live_start", 1))),
                        int(v.get("first_seen_at") or now),
                        v.get("last_seen_in_feed_at"),
                    ),
                )
                summary.videos_added += 1
            else:
                # Mevcut kaydı birleştir: okundu/yıldızlı/izlendi/Sonra İzle durumlarını koru (MAX/COALESCE)
                merged_read = max(int(cur["is_read"] or 0), int(bool(v.get("is_read"))))
                merged_star = max(int(cur["is_starred"] or 0), int(bool(v.get("is_starred"))))
                merged_watched = max(int(cur["is_watched"] or 0), int(bool(v.get("is_watched"))))
                merged_wl = max(int(cur["watch_later"] or 0), int(bool(v.get("watch_later"))))
                merged_user_type = cur["user_type"] if cur["user_type"] is not None else v.get("user_type")
                merged_dur = cur["duration"] if (cur["duration"] and cur["duration"] > 0) else v.get("duration")

                auto_t = cur["auto_type"]
                live_st = cur["live_state"]
                prem_st = cur["premiere_state"]
                cls_src = cur["class_source"]
                cls_rsn = cur["class_reason"]
                cls_cnf = cur["class_confidence"]
                cls_at = cur["classified_at"]
                cls_ver = cur["classifier_version"]
                if auto_t == "unknown" and v.get("auto_type") and v.get("auto_type") != "unknown":
                    auto_t = v.get("auto_type")
                    live_st = v.get("live_state")
                    prem_st = v.get("premiere_state")
                    cls_src = v.get("class_source")
                    cls_rsn = v.get("class_reason")
                    cls_cnf = v.get("class_confidence")
                    cls_at = v.get("classified_at")
                    cls_ver = v.get("classifier_version")

                con.execute(
                    "UPDATE videos SET "
                    "duration=?, user_type=?, auto_type=?, live_state=?, premiere_state=?, "
                    "class_source=?, class_reason=?, class_confidence=?, classified_at=?, "
                    "classifier_version=?, is_read=?, is_starred=?, is_watched=?, watch_later=?, "
                    "watch_later_order=COALESCE(watch_later_order, ?), "
                    "watch_later_added_at=COALESCE(watch_later_added_at, ?) "
                    "WHERE video_id=?",
                    (
                        merged_dur,
                        merged_user_type,
                        auto_t,
                        live_st,
                        prem_st,
                        cls_src,
                        cls_rsn,
                        cls_cnf,
                        cls_at,
                        cls_ver,
                        merged_read,
                        merged_star,
                        merged_watched,
                        merged_wl,
                        v.get("watch_later_order"),
                        v.get("watch_later_added_at"),
                        vid,
                    ),
                )
                summary.videos_merged += 1

        # 5. Kanıtlar
        existing_vids = {r[0] for r in con.execute("SELECT video_id FROM videos").fetchall()}
        for ev in payload.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            ev_vid = ev.get("video_id")
            sig = ev.get("signal")
            val = ev.get("value")
            if ev_vid in existing_vids and sig and val is not None:
                con.execute(
                    "INSERT OR IGNORE INTO evidence(video_id, signal, value, observed_at) VALUES(?,?,?,?)",
                    (ev_vid, str(sig), str(val), int(ev.get("observed_at") or now)),
                )

    # 6. Sonra İzle kuyruk sırasını birleştir ve koru
    backup_wl_queue = [
        str(x) for x in (payload.get("watch_later_queue") or []) if isinstance(x, str)
    ]
    if not backup_wl_queue:
        # Yedekte açık queue listesi yoksa videos içindeki watch_later_order'a göre oluştur
        wl_vids = [
            v for v in (payload.get("videos") or [])
            if isinstance(v, dict) and v.get("watch_later") and v.get("video_id")
        ]
        wl_vids.sort(key=lambda x: (x.get("watch_later_order") is None, x.get("watch_later_order") or 0))
        backup_wl_queue = [str(v["video_id"]) for v in wl_vids]

    combined_wl: list[str] = list(existing_wl_queue) if merge else []
    seen_wl = set(combined_wl)
    for vid in backup_wl_queue:
        if vid not in seen_wl:
            combined_wl.append(vid)
            seen_wl.add(vid)
    if combined_wl:
        db.reorder_watch_later(combined_wl)

    summary.watch_later_count = len(db.watch_later_ids(include_watched=True))
    return summary


def restore_backup_file(
    db: Database, path: Path | str, *, merge: bool = True
) -> BackupRestoreSummary:
    raw_text = Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw_text)
    return restore_backup_payload(db, payload, merge=merge)


