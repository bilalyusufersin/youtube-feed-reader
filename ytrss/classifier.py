"""Kanıta dayalı, sürümlü YouTube içerik türü sınıflandırıcısı.

Bu modül arayüzden ve veritabanından tamamen bağımsızdır; girdi olarak yalnızca
gözlemlenmiş kanıt sinyallerini (`dict[str, tuple[str, int]]`) alır ve
`Classification` döndürür.

Temel ilkeler:
- Süre, başlıktaki/açıklamadaki #shorts etiketi, küçük resim oranı, RSS'deki
  /watch bağlantısı veya kullanıcının girdiği /shorts/ adresi ASLA kanıt sayılmaz.
- Sınırlı bir sekme (/videos, /shorts, /streams) veya tür akışı (UULF, UUSH, UULV)
  sorgusunda videonun bulunmaması, o türe ait olmadığının kanıtı sayılmaz.
- Prömiyerler (planlı, şu anda oynatılan veya tamamlanmış) canlı yayın sayılmaz;
  normal video (veya Shorts) olarak sınıflandırılır ve `premiere_state` alanında
  ayrıca işaretlenir.
- Tamamlanan canlı yayınlar `ContentType.LIVE` kategorisinde kalır ve
  `live_state = LiveState.COMPLETED` alır.
- Kanıtlar yetersiz veya birbiriyle çelişkiliyse içerik zorla normal video
  yapılmaz; `ContentType.UNKNOWN` ("Henüz belirlenemedi") olarak döner.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .models import ContentType, LiveState, VideoMetadata

CLASSIFIER_VERSION = 1


@dataclass(slots=True)
class Classification:
    type: ContentType
    live_state: LiveState | None = None
    premiere_state: LiveState | None = None
    scheduled_at: int | None = None
    source: str = "none"
    reason: str = ""
    confidence: str = "none"   # high | medium | low | none
    classified_at: int = 0
    version: int = CLASSIFIER_VERSION
    needs_meta: bool = False


def _loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


def _map_live_status(status: str | None) -> LiveState | None:
    if status == "is_upcoming":
        return LiveState.SCHEDULED
    if status == "is_live":
        return LiveState.LIVE
    if status in ("was_live", "post_live"):
        return LiveState.COMPLETED
    return None


def classify(evidence: dict[str, tuple[str, int]], now: int | None = None) -> Classification:
    """Ham kanıt sözlüğünden içerik türünü ve yayın durumunu belirler.

    `evidence`: `{signal_adi: (deger_str, gozlem_zamani_unix)}`
    Tanınan sinyaller:
      - `feed_uulf`: "1" (YouTube normal videolar yükleme listesi UULF akışı)
      - `feed_uush`: "1" (YouTube Shorts yükleme listesi UUSH akışı)
      - `feed_uulv`: "1" (YouTube canlı yayınlar yükleme listesi UULV akışı)
      - `tab_videos`: JSON `{"live_status": ..., "duration": ...}` (/videos sekmesi)
      - `tab_shorts`: JSON `{"url": ...}` (/shorts sekmesi)
      - `tab_streams`: JSON `{"live_status": ..., "duration": ...}` (/streams sekmesi)
      - `shorts_head`: "200" (/shorts/ID 200 döndü) veya "303_watch" (/watch'a yönlendirdi)
      - `ytdlp_meta`: JSON `VideoMetadata` (yt-dlp playerResponse/microformat üstverisi)
    """
    ts = int(now if now is not None else time.time())

    in_uulf = "feed_uulf" in evidence and evidence["feed_uulf"][0] == "1"
    in_uush = "feed_uush" in evidence and evidence["feed_uush"][0] == "1"
    in_uulv = "feed_uulv" in evidence and evidence["feed_uulv"][0] == "1"

    tab_v = _loads(evidence["tab_videos"][0]) if "tab_videos" in evidence else None
    tab_s = _loads(evidence["tab_shorts"][0]) if "tab_shorts" in evidence else None
    tab_l = _loads(evidence["tab_streams"][0]) if "tab_streams" in evidence else None
    tab_l_ts = evidence["tab_streams"][1] if "tab_streams" in evidence else 0

    shorts_head = evidence["shorts_head"][0] if "shorts_head" in evidence else None

    meta: VideoMetadata | None = None
    meta_ts = 0
    if "ytdlp_meta" in evidence:
        raw_meta = _loads(evidence["ytdlp_meta"][0])
        if raw_meta:
            meta = VideoMetadata.from_json(raw_meta)
            meta_ts = evidence["ytdlp_meta"][1]

    # 1. yt-dlp üstverisini yorumla
    meta_claim: ContentType | None = None
    meta_live_state: LiveState | None = None
    meta_premiere_state: LiveState | None = None
    meta_scheduled_at: int | None = None
    meta_error_kind: str | None = None

    if meta is not None:
        if meta.ok and (meta.channel_id or meta.live_status or meta.availability):
            m_type = meta.media_type
            m_live = meta.live_status
            m_rel = meta.release_timestamp
            if m_type == "livestream" or m_live in ("was_live", "post_live"):
                meta_claim = ContentType.LIVE
                meta_live_state = _map_live_status(m_live) or LiveState.COMPLETED
                meta_scheduled_at = m_rel
            elif m_type == "short":
                meta_claim = ContentType.SHORT
            elif m_type == "video":
                meta_claim = ContentType.VIDEO
                if m_live == "is_upcoming":
                    meta_premiere_state = LiveState.SCHEDULED
                    meta_scheduled_at = m_rel
                elif m_live == "is_live":
                    meta_premiere_state = LiveState.LIVE
                    meta_scheduled_at = m_rel
                elif m_live == "not_live" and m_rel is not None:
                    meta_premiere_state = LiveState.COMPLETED
                    meta_scheduled_at = m_rel
        else:
            meta_error_kind = meta.error_kind or "other"

    # /videos sekmesinde is_upcoming / is_live görünen bir öğe prömiyer olabilir;
    # yt-dlp media_type kontrolü olmadan doğrudan kesin video/canlı kararı verilmez.
    tab_v_live = tab_v.get("live_status") if tab_v else None
    tab_v_unambiguous = tab_v is not None and tab_v_live not in ("is_upcoming", "is_live")

    # 2. Güçlü üyelik kaynaklarını ve olası çelişkileri topla
    claims: dict[ContentType, list[str]] = {
        ContentType.VIDEO: [],
        ContentType.SHORT: [],
        ContentType.LIVE: [],
    }
    if in_uulf:
        claims[ContentType.VIDEO].append("feed_uulf")
    if tab_v_unambiguous:
        claims[ContentType.VIDEO].append("tab_videos")
    if meta_claim == ContentType.VIDEO:
        claims[ContentType.VIDEO].append("ytdlp_meta")

    if in_uush:
        claims[ContentType.SHORT].append("feed_uush")
    if tab_s is not None:
        claims[ContentType.SHORT].append("tab_shorts")
    if meta_claim == ContentType.SHORT:
        claims[ContentType.SHORT].append("ytdlp_meta")
    if shorts_head == "200":
        claims[ContentType.SHORT].append("shorts_head")

    if in_uulv:
        claims[ContentType.LIVE].append("feed_uulv")
    if tab_l is not None:
        claims[ContentType.LIVE].append("tab_streams")
    if meta_claim == ContentType.LIVE:
        claims[ContentType.LIVE].append("ytdlp_meta")

    non_empty = [ct for ct, srcs in claims.items() if srcs]

    # Çelişki kontrolü 1: Birden fazla kategori için pozitif kanıt varsa
    if len(non_empty) > 1:
        parts = [f"{ct.value}({','.join(claims[ct])})" for ct in non_empty]
        return Classification(
            type=ContentType.UNKNOWN,
            source="conflict:" + "+".join(parts),
            reason="Çelişkili tür sinyalleri tespit edildi: " + ", ".join(parts) + ".",
            confidence="none",
            classified_at=ts,
            needs_meta=meta_claim is None,
        )

    # Çelişki kontrolü 2: Shorts iddiası var ama /shorts/ID 303 ile /watch'a yönlendiriyor
    if claims[ContentType.SHORT] and shorts_head == "303_watch":
        srcs = ",".join(claims[ContentType.SHORT])
        return Classification(
            type=ContentType.UNKNOWN,
            source=f"conflict:short({srcs})+shorts_head_303",
            reason=f"Shorts sinyali ({srcs}) ile /shorts/ yönlendirmesi (303 /watch) çelişiyor.",
            confidence="none",
            classified_at=ts,
            needs_meta=meta_claim is None,
        )

    # 3. Canlı yayın
    if claims[ContentType.LIVE]:
        srcs = claims[ContentType.LIVE]
        tab_state = _map_live_status(tab_l.get("live_status")) if tab_l else None
        # En güncel/doğru canlı durumu seç:
        # Not: 2 saatten uzun DVR yayınlarda yt-dlp post_live döndürebilirken /streams
        # sekmesi rozeti is_live gösterir.
        live_state: LiveState | None = None
        if tab_state == LiveState.LIVE and meta_live_state == LiveState.COMPLETED:
            if meta is not None and meta.live_status == "post_live":
                live_state = LiveState.LIVE
            else:
                live_state = tab_state if tab_l_ts >= meta_ts else meta_live_state
        elif meta_live_state is not None and tab_state is not None:
            live_state = meta_live_state if meta_ts >= tab_l_ts else tab_state
        else:
            live_state = meta_live_state or tab_state

        needs_meta = (
            live_state is None
            or live_state in (LiveState.SCHEDULED, LiveState.LIVE)
        )
        conf = "high" if (meta_claim == ContentType.LIVE or (in_uulv and tab_l is not None)
                          or live_state is not None) else "medium"
        state_txt = live_state.label if live_state else "durumu doğrulanıyor"
        return Classification(
            type=ContentType.LIVE,
            live_state=live_state,
            scheduled_at=meta_scheduled_at,
            source="+".join(srcs),
            reason=f"Canlı yayın kanıtı ({', '.join(srcs)}); durum: {state_txt}.",
            confidence=conf,
            classified_at=ts,
            needs_meta=needs_meta,
        )

    # 4. Shorts
    if claims[ContentType.SHORT]:
        srcs = claims[ContentType.SHORT]
        strong = in_uush or (tab_s is not None) or (meta_claim == ContentType.SHORT)
        conf = "high" if (len(srcs) >= 2 or meta_claim == ContentType.SHORT or in_uush) else "medium"
        return Classification(
            type=ContentType.SHORT,
            source="+".join(srcs),
            reason=f"Shorts kanıtı ({', '.join(srcs)}).",
            confidence=conf,
            classified_at=ts,
            needs_meta=not strong,
        )

    # 5. Normal video (prömiyerler dahil)
    if claims[ContentType.VIDEO]:
        srcs = list(claims[ContentType.VIDEO])
        if shorts_head == "303_watch":
            srcs.append("shorts_head_303")
        conf = "high" if (meta_claim == ContentType.VIDEO or in_uulf) else "medium"
        reason = f"Normal video kanıtı ({', '.join(srcs)})"
        if meta_premiere_state is not None:
            reason += f"; prömiyer ({meta_premiere_state.label.lower()})."
        else:
            reason += "."
        return Classification(
            type=ContentType.VIDEO,
            premiere_state=meta_premiere_state,
            scheduled_at=meta_scheduled_at,
            source="+".join(srcs),
            reason=reason,
            confidence=conf,
            classified_at=ts,
            needs_meta=meta_premiere_state in (LiveState.SCHEDULED, LiveState.LIVE),
        )

    # 6. Yetersiz kanıt -> Henüz belirlenemedi (asla zorla normal video yapılmaz)
    if tab_v is not None and tab_v_live in ("is_upcoming", "is_live"):
        return Classification(
            type=ContentType.UNKNOWN,
            source="tab_videos_pending_premiere_check",
            reason=f"/videos sekmesinde '{tab_v_live}' olarak görüldü; prömiyer/canlı yayın ayrımı için üstveri bekleniyor.",
            confidence="none",
            classified_at=ts,
            needs_meta=True,
        )

    if meta_error_kind:
        err_descriptions = {
            "private": "Video gizli olduğu için tür kanıtı alınamadı.",
            "members_only": "Video yalnızca kanal üyelerine açık; anahtarsız tür kanıtı alınamadı.",
            "age_restricted": "Video yaş kısıtlı; oturumsuz tür kanıtı alınamadı.",
            "unavailable": "Video kullanılamıyor veya kaldırılmış; tür belirlenemedi.",
            "network": "Üstveri sorgusu ağ hatasıyla başarısız oldu; tekrar denenecek.",
            "rate_limited": "YouTube hız sınırı nedeniyle üstveri henüz alınamadı.",
        }
        return Classification(
            type=ContentType.UNKNOWN,
            source=f"meta_error:{meta_error_kind}",
            reason=err_descriptions.get(meta_error_kind, "Üstveri alınamadı; tür belirlenemedi."),
            confidence="none",
            classified_at=ts,
            needs_meta=meta_error_kind not in ("private", "unavailable", "members_only", "age_restricted"),
        )

    if shorts_head == "303_watch":
        return Classification(
            type=ContentType.UNKNOWN,
            source="only_not_short",
            reason="Yalnızca Shorts olmadığı (/watch yönlendirmesi) biliniyor; normal video / canlı yayın ayrımı için kanıt bekleniyor.",
            confidence="none",
            classified_at=ts,
            needs_meta=True,
        )

    return Classification(
        type=ContentType.UNKNOWN,
        source="insufficient",
        reason="Henüz yeterli sınıflandırma kanıtı toplanmadı.",
        confidence="none",
        classified_at=ts,
        needs_meta=True,
    )


def compute_next_meta_check(
    cls: Classification,
    now: int,
    *,
    attempts: int = 0,
    meta_error_kind: str | None = None,
    has_meta: bool = False,
    date_estimated: bool = False,
) -> int | None:
    """Bir sonraki tekil video üstveri kontrol zamanını (unix sn) hesaplar.

    - Tamamlanmış ve sınıflandırılmış normal videolar, Shorts ve tamamlanmış canlı yayınlar
      tekrar sorgulanmaz (`None`).
    - Aktif yayınlar/prömiyerler 5 dakikada bir, yaklaşan yayınlar yayın saatine göre
      5–60 dakikada bir kontrol edilir. İlk kez keşfedilen ve yayın saati/tarihi eksik olan
      aktif/planlı yayınlar hemen (`now`) sorgulanır.
    - Türü belirlenemeyen içerikler üstel geri çekilme ile en fazla 5 kez denenir;
      gizli/kaldırılmış videolarda tekrar deneme durdurulur.
    """
    # Aktif canlı yayın veya aktif prömiyer -> tarih tahmini varsa ilk seferde hemen, sonra 5 dk
    if cls.live_state == LiveState.LIVE or cls.premiere_state == LiveState.LIVE:
        if not has_meta and date_estimated and attempts <= 0:
            return now
        return now + 300

    # Planlanmış canlı yayın veya planlanmış prömiyer -> planlanan saat bilinmiyorsa hemen, sonra 5-60 dk
    if cls.live_state == LiveState.SCHEDULED or cls.premiere_state == LiveState.SCHEDULED:
        if not has_meta and cls.scheduled_at is None and attempts <= 0:
            return now
        if cls.scheduled_at is not None:
            delta = cls.scheduled_at - now
            if delta <= 1800:
                return now + 300
            if delta <= 6 * 3600:
                return now + 900
        return now + 3600

    # Canlı yayın olduğu biliniyor (ör. UULV) ama durumu henüz alınmadı
    if cls.type == ContentType.LIVE and cls.live_state is None:
        if attempts <= 0:
            return now
        return now + min(3600, 300 * (2 ** min(attempts, 4)))

    # Kesinleşmiş normal video, Shorts veya tamamlanmış canlı yayın -> tekrar sorgulama yok
    if cls.type in (ContentType.VIDEO, ContentType.SHORT, ContentType.LIVE):
        return None

    # Türü belirlenemedi (UNKNOWN)
    if meta_error_kind in ("private", "unavailable", "members_only", "age_restricted"):
        return None
    if attempts <= 0:
        return now
    if attempts >= 5:
        return None
    backoff = [60, 900, 3600, 6 * 3600, 24 * 3600]
    return now + backoff[min(attempts, len(backoff) - 1)]
