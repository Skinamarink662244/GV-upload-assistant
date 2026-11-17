# Upload Assistant © 2025 Audionut & wastaken7 — Licensed under UAPL v1.0
"""TV Vault (TVV): a Gazelle tracker for old and rare television.

TV Vault has no API, so this logs in with exported browser cookies and fills
in the site's own upload form (upload.php). Field names and allowed values are
taken from that form, and naming follows the site's upload rules.
"""

import platform
import re
from pathlib import Path
from typing import Any, ClassVar, TypeIs, cast

import aiofiles
import httpx
from bs4 import BeautifulSoup, Tag

from src import payload_preview
from src.console import logger
from src.cookie_auth import CookieAuthUploader, CookieValidator
from src.get_desc import DescriptionBuilder
from src.mediainfo import strip_report_by_line
from src.meta import Meta
from src.tracker_images import get_tracker_image_collection
from src.trackers.common import Common


def _is_str_dict(val: object) -> TypeIs[dict[str, object]]:
    return isinstance(val, dict)


# Upload form values, copied from TV Vault's upload.php.
CATEGORIES = (
    "Action",
    "Advertising",
    "Animation",
    "Chatshow",
    "Comedy",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "Gameshow",
    "Horror",
    "Instructional",
    "Music",
    "Sci-Fi",
    "Soap",
    "Thriller",
    "Western",
    "Other",
)
CONTAINERS = ("AVI", "MPG", "MKV", "MP4", "TS", "VOB/IFO", "m2ts/bdmv", "Audio", "CBR", "ePUB", "PDF", "Subtitle", "Multiple", "Other")
VIDEO_FORMATS = ("XviD", "DivX", "H.264", "AV1", "VC-1", "H.265", "MPEG-2", "MPEG-1", "VP8/9", "Unknown", "Multiple", "Other")
AUDIO_FORMATS = ("AC3", "AAC", "DTS", "E-AC3", "MP3", "MP2", "FLAC", "(L)PCM", "Vorbis", "Opus", "Unknown", "Multiple", "Other")
MEDIA = (
    "DVD",
    "HDTV",
    "PDTV",
    "TV",
    "VHS",
    "Blu-ray",
    "HD-DVD",
    "LaserDisc",
    "Master",
    "Kinescope",
    "Web",
    "ScreenCap",
    "CD",
    "Scan",
    "Unknown",
    "Multiple",
    "Other",
)
HDR_VIDEO = {"0": "No: 8-bit", "1": "General 10-bit", "3": "Dolby Vision"}

# The site's official tag list (decades and genres). Its rules say to leave a tag out when unsure.
OFFICIAL_DECADES = ("00s", "10s", "20s", "40s", "50s", "60s", "70s", "80s", "90s")

# TMDb genre -> TV Vault category, most specific first; the first match wins.
CATEGORY_BY_GENRE = (
    ("animation", "Animation"),
    ("documentary", "Documentary"),
    ("soap", "Soap"),
    ("talk", "Chatshow"),
    ("western", "Western"),
    ("horror", "Horror"),
    ("sci-fi & fantasy", "Sci-Fi"),
    ("science fiction", "Sci-Fi"),
    ("fantasy", "Fantasy"),
    ("music", "Music"),
    ("comedy", "Comedy"),
    ("thriller", "Thriller"),
    ("action & adventure", "Action"),
    ("action", "Action"),
    ("kids", "Family"),
    ("family", "Family"),
    ("drama", "Drama"),
)

# TMDb genre -> official TV Vault tags.
TAGS_BY_GENRE = {
    "action": ("action",),
    "action & adventure": ("action", "adventure"),
    "adventure": ("adventure",),
    "animation": ("animation",),
    "comedy": ("comedy",),
    "crime": ("crime",),
    "documentary": ("documentary",),
    "drama": ("drama",),
    "family": ("family",),
    "kids": ("family",),
    "fantasy": ("fantasy",),
    "horror": ("horror",),
    "music": ("music",),
    "mystery": ("mystery",),
    "science fiction": ("scifi",),
    "sci-fi & fantasy": ("scifi", "fantasy"),
    "soap": ("soap",),
    "talk": ("chatshow",),
    "thriller": ("thriller",),
    "tv movie": ("tv.movie",),
    "western": ("western",),
}

# HD resolutions that are added to the season / episode label.
HD_RESOLUTIONS = ("720p", "1080i", "1080p", "2160p", "4320p")

# TV Vault staff ask for 1-3 full-size screenshots.
DEFAULT_SCREENSHOT_COUNT = 3

# Countries whose shows are labelled "Series N" rather than "Season N".
SERIES_COUNTRIES = {"GB", "UK", "IE"}


class TVVault:
    """TV Vault is a Private Torrent Tracker for old and rare TV."""

    auth_type = "cookies"
    tracker = "TVVAULT"
    display_name = "TV Vault"
    source_flag = "TVV"
    base_url = "https://tv-vault.me"
    upload_url = f"{base_url}/upload.php"
    torrent_url = f"{base_url}/torrents.php?id="
    tracker_urls = ("tracker.tv-vault.me",)
    # MOVIE is only for TV movies (TMDb genre "TV Movie"); see get_additional_checks.
    supported_categories = ("TV", "MOVIE")
    banned_groups: tuple[str, ...] = ()
    # Show group IDs found by the IMDb search, by release uuid, so the upload goes into the existing show.
    group_ids: ClassVar[dict[str, str]] = {}
    # Releases whose show title matches a different show already on the site, by release uuid.
    show_conflicts: ClassVar[dict[str, str]] = {}

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.common = Common(config)
        self.cookie_validator = CookieValidator(config)
        self.cookie_auth_uploader = CookieAuthUploader(config)
        self.session = httpx.AsyncClient(
            headers={"User-Agent": f"Upload-Assistant ({platform.system()} {platform.release()})"},
            timeout=60.0,
            follow_redirects=True,
        )

    async def validate_credentials(self, meta: Meta) -> bool:
        cookie_jar = await self.cookie_validator.load_session_cookies(meta, self.tracker)
        if cookie_jar:
            self.session.cookies = cast(Any, cookie_jar)
            return True
        return False

    # ----- names -------------------------------------------------------------

    @staticmethod
    def _genres(meta: Meta) -> list[str]:
        genres: Any = meta.genres or meta.genre
        if isinstance(genres, str):
            genres = genres.split(",")
        return [str(genre).strip().lower() for genre in genres or [] if str(genre).strip()]

    @staticmethod
    def _is_latin(text: str) -> bool:
        """True when every letter is Latin script (accents allowed), so it can be typed into a search box."""
        return all(ord(char) <= 0x24F or not char.isalpha() for char in text)

    @classmethod
    def get_show_title(cls, meta: Meta) -> str:
        """The show's full name. Shows first made in another language lead with the original
        title and add the English one as " (aka English Title)", unless the original is in a
        non-Latin script, which the rules ask to romanize instead.
        """
        title = str(meta.title or "").strip()
        original = str(meta.original_title or "").strip()
        language = str(meta.original_language or "").lower()
        if original and language and not language.startswith("en") and original.casefold() != title.casefold() and cls._is_latin(original):
            return f"{original} (aka {title})" if title else original
        return title

    @staticmethod
    def _is_tv_movie(meta: Meta) -> bool:
        return str(meta.category).upper() == "MOVIE"

    @staticmethod
    def _season_word(meta: Meta) -> str:
        countries = {str(country).upper() for country in meta.origin_country or []}
        return "Series" if countries & SERIES_COUNTRIES else "Season"

    @staticmethod
    def compact_numbers(numbers: list[int]) -> str:
        """[1, 2, 4, 5, 6, 9] -> "1-2, 4-6, 9", the way TV Vault lists partial seasons."""
        runs: list[list[int]] = []
        for number in sorted(set(numbers)):
            if runs and number == runs[-1][1] + 1:
                runs[-1][1] = number
            else:
                runs.append([number, number])
        return ", ".join(f"{first:02d}" if first == last else f"{first:02d}-{last:02d}" for first, last in runs)

    @staticmethod
    def _episodes_in_files(meta: Meta) -> list[int]:
        episodes: set[int] = set()
        for item in meta.filelist or []:
            for match in re.finditer(r"(?i)S\d{1,4}((?:[ .-]?E\d{1,4})+)", Path(str(item)).name):
                episodes.update(int(number) for number in re.findall(r"\d+", match.group(1)))
        return sorted(episodes)

    @staticmethod
    def _episode_numbers(meta: Meta) -> list[int]:
        numbers = [int(number) for number in re.findall(r"\d+", str(meta.episode or ""))]
        if not numbers and meta.episode_int:
            numbers = [int(meta.episode_int)]
        if len(numbers) == 2 and numbers[1] > numbers[0]:
            numbers = list(range(numbers[0], numbers[1] + 1))
        return numbers

    def _episode_name(self, meta: Meta) -> str:
        tmdb_episode = meta.tmdb_episode_data if _is_str_dict(meta.tmdb_episode_data) else {}
        for candidate in (meta.episode_title, meta.tvdb_episode_name, tmdb_episode.get("name")):
            name = str(candidate or "").strip()
            # TMDb fills unnamed episodes with "Episode 5"; that adds nothing.
            if name and not re.fullmatch(r"(?i)episode\s*\d+", name):
                return name
        return ""

    @staticmethod
    def _season_episode_count(meta: Meta) -> list[int]:
        season = meta.tmdb_season_data if _is_str_dict(meta.tmdb_season_data) else {}
        episodes = season.get("episodes")
        if not isinstance(episodes, list):
            return []
        return sorted({int(ep["episode_number"]) for ep in episodes if _is_str_dict(ep) and str(ep.get("episode_number") or "").isdigit()})

    def get_season_title(self, meta: Meta) -> str:
        """The "Season (or Series) / Episode" field, with " (1080p)" or " (720p)" added for HD
        releases, as TV Vault's staff ask (e.g. "Season 1 (1080p)")."""
        title = self._season_title_without_resolution(meta)
        resolution = str(meta.resolution or "").strip().lower()
        return f"{title} ({resolution})" if resolution in HD_RESOLUTIONS else title

    def _season_title_without_resolution(self, meta: Meta) -> str:
        """The season / episode label from the site's upload rules:
        "Series 1", "Season 1, Episode 01: Name", "Season 1, Episode 01-04, 07",
        "Season 1 (Missing Episode 14, 17)", "Miniseries" or "TV Movie".
        """
        if self._is_tv_movie(meta):
            return "TV Movie"

        season = int(meta.season_int or 0)
        label = "Specials" if season == 0 else f"{self._season_word(meta)} {season}"

        if meta.tv_pack:
            if meta.season_pack_incomplete and (episodes := self._episodes_in_files(meta)):
                missing = sorted(set(self._season_episode_count(meta)) - set(episodes))
                # Nearly complete seasons list what is missing rather than what is there.
                if missing and len(missing) < len(episodes):
                    return f"{label} (Missing Episode {self.compact_numbers(missing)})"
                return f"{label}, Episode {self.compact_numbers(episodes)}"
            if season == 1 and str(meta.tmdb_type or "").lower() == "miniseries":
                return "Miniseries"
            return label

        episodes = self._episode_numbers(meta)
        if not episodes:
            return label
        title = f"{label}, Episode {self.compact_numbers(episodes)}"
        if len(episodes) == 1 and (episode_name := self._episode_name(meta)):
            title = f"{title}: {episode_name}"
        return title

    def get_year(self, meta: Meta) -> str:
        """Year this episode or season was first broadcast."""
        candidates: list[Any] = []
        if not meta.tv_pack:
            if _is_str_dict(meta.tmdb_episode_data):
                candidates.append(meta.tmdb_episode_data.get("air_date"))
            candidates.append(meta.tvdb_episode_year)
        if _is_str_dict(meta.tmdb_season_data):
            candidates.append(meta.tmdb_season_data.get("air_date"))
        candidates.append(meta.year)
        for candidate in candidates:
            if match := re.match(r"\d{4}", str(candidate or "")):
                return match.group(0)
        return ""

    @staticmethod
    def get_ending_year(meta: Meta) -> str:
        """Last year the show was on air, for shows that ran over several years."""
        first = re.match(r"\d{4}", str(meta.first_air_date or ""))
        last = re.match(r"\d{4}", str(meta.last_air_date or ""))
        if first and last and last.group(0) > first.group(0):
            return last.group(0)
        return ""

    async def get_name(self, meta: Meta) -> str:
        """How the release will read on TV Vault: "Show - Season 1, Episode 01 (1988)"."""
        if meta.manual_name is not None:
            return meta.manual_name.strip()
        year = self.get_year(meta)
        return f"{self.get_show_title(meta)} - {self.get_season_title(meta)}{f' ({year})' if year else ''}"

    # ----- rip specifics -----------------------------------------------------

    def _mediainfo_tracks(self, meta: Meta, track_type: str) -> list[dict[str, Any]]:
        media = meta.mediainfo.get("media") if _is_str_dict(meta.mediainfo) else None
        tracks = media.get("track") if _is_str_dict(media) else None
        return [track for track in tracks or [] if _is_str_dict(track) and track.get("@type") == track_type] if isinstance(tracks, list) else []

    def get_category(self, meta: Meta) -> str:
        genres = self._genres(meta)
        keywords = " ".join(str(keyword).lower() for keyword in meta.keywords or [])
        if "game show" in keywords or "gameshow" in keywords:
            return "Gameshow"
        for genre, category in CATEGORY_BY_GENRE:
            if genre in genres:
                return category
        return "Other"

    @staticmethod
    def get_container(meta: Meta) -> str:
        if meta.is_disc == "DVD":
            return "VOB/IFO"
        if meta.is_disc == "BDMV":
            return "m2ts/bdmv"
        # A broadcast .ts that hasn't been remuxed is "TS"; Blu-ray .m2ts files are "m2ts/bdmv".
        by_extension = {".mkv": "MKV", ".mp4": "MP4", ".m4v": "MP4", ".avi": "AVI", ".ts": "TS", ".m2ts": "m2ts/bdmv", ".mpg": "MPG", ".mpeg": "MPG", ".vob": "VOB/IFO"}
        found = {
            by_extension.get(Path(str(item)).suffix.lower(), "Other")
            for item in meta.filelist or []
            if Path(str(item)).suffix.lower() not in {".nfo", ".srt", ".sub", ".idx", ".txt", ".jpg", ".png"}
        }
        if not found:
            return "Other"
        return found.pop() if len(found) == 1 else "Multiple"

    def get_video_format(self, meta: Meta) -> str:
        # By definition a full DVD is MPEG-2 video.
        if meta.is_disc == "DVD":
            return "MPEG-2"
        video = self._mediainfo_tracks(meta, "Video")
        codec_id = str(video[0].get("CodecID", "")).lower() if video else ""
        if "xvid" in codec_id:
            return "XviD"
        if "divx" in codec_id or "dx50" in codec_id:
            return "DivX"
        codec = f"{meta.video_encode} {meta.video_codec} {video[0].get('Format', '') if video else ''}".lower()
        for needles, value in (
            (("x264", "h.264", "h264", "avc"), "H.264"),
            (("x265", "h.265", "h265", "hevc"), "H.265"),
            (("av1",), "AV1"),
            (("vc-1", "vc1"), "VC-1"),
            (("mpeg-2", "mpeg2"), "MPEG-2"),
            (("mpeg-1", "mpeg1"), "MPEG-1"),
            (("vp8", "vp9"), "VP8/9"),
            (("xvid",), "XviD"),
            (("divx",), "DivX"),
        ):
            if any(needle in codec for needle in needles):
                return value
        return "Unknown" if not codec.strip() else "Other"

    @staticmethod
    def get_audio_format(meta: Meta) -> str:
        audio = str(meta.audio or "").lower()
        for needles, value in (
            (("dd+", "ddp", "e-ac-3", "eac3", "e-ac3"), "E-AC3"),
            (("truehd",), "Other"),
            (("dts",), "DTS"),
            (("dd ", "dd5", "dd2", "dd1", "ac3", "ac-3", "dolby digital"), "AC3"),
            (("aac",), "AAC"),
            (("flac",), "FLAC"),
            (("pcm",), "(L)PCM"),
            (("mp3", "layer 3"), "MP3"),
            (("mp2", "layer 2"), "MP2"),
            (("opus",), "Opus"),
            (("vorbis",), "Vorbis"),
        ):
            if any(needle in f"{audio} " for needle in needles):
                return value
        return "Unknown" if not audio else "Other"

    @staticmethod
    def _is_youtube(meta: Meta) -> bool:
        return str(meta.service or "").upper() == "YT"

    @classmethod
    def get_media(cls, meta: Meta) -> str:
        """The rip's source. "VHS" and "DVD" mean commercial releases (home recordings are "TV"),
        and "Master" is only for studio masters, so neither is guessed beyond what the release says.
        """
        release = f"{meta.type or ''} {meta.source or ''}".lower().replace("-", "").replace(" ", "")
        # YouTube is not "Web": its real source is usually TV or DVD, which only the uploader knows.
        if cls._is_youtube(meta):
            return "Unknown"
        if "web" in release:
            return "Web"
        if "hddvd" in release:
            return "HD-DVD"
        if "bluray" in release or meta.is_disc == "BDMV":
            return "Blu-ray"
        if "hdtv" in release:
            return "HDTV" if not meta.sd else "PDTV"
        if "dvd" in release or "ntsc" in release or "pal" in release or meta.is_disc == "DVD":
            return "DVD"
        if "sdtv" in release or "pdtv" in release:
            return "PDTV"
        if "vhs" in release:
            return "VHS"
        return "Unknown"

    @staticmethod
    def get_hdr(meta: Meta) -> str:
        hdr = str(meta.hdr or "").upper()
        if "DV" in hdr.split() or "DOVI" in hdr:
            return "3"
        if hdr.strip() or str(meta.bit_depth or "") == "10":
            return "1"
        return "0"

    def get_tags(self, meta: Meta) -> str:
        """Official TV Vault tags only: the decade the show first aired and its genres."""
        tags: list[str] = []
        first_year = re.match(r"\d{4}", str(meta.first_air_date or meta.year or ""))
        if first_year and (decade := f"{first_year.group(0)[2]}0s") in OFFICIAL_DECADES:
            tags.append(decade)
        for genre in self._genres(meta):
            tags.extend(TAGS_BY_GENRE.get(genre, ()))
        if str(meta.tmdb_type or "").lower() == "miniseries":
            tags.append("mini.series")
        return ", ".join(dict.fromkeys(tags))

    def get_cover(self, meta: Meta) -> str:
        candidates: list[Any] = []
        if isinstance(meta.hosted_artwork, list):
            candidates.extend(entry.get("raw_url") for entry in meta.hosted_artwork if _is_str_dict(entry))
        candidates.extend((getattr(meta, "rehosted_artwork_url", ""), meta.artwork_url))
        if meta.tmdb_poster_path:
            candidates.append(f"https://image.tmdb.org/t/p/w500{meta.tmdb_poster_path}")
        return next((candidate for candidate in candidates if isinstance(candidate, str) and candidate.startswith("http")), "")

    # ----- description ---------------------------------------------------------

    def _screenshot_count(self) -> int:
        tracker_config = self.config.get("TRACKERS", {}).get(self.tracker, {})
        try:
            count = int(tracker_config.get("image_count", DEFAULT_SCREENSHOT_COUNT))
        except TypeError, ValueError:
            count = DEFAULT_SCREENSHOT_COUNT
        return max(count, 1)

    async def _full_mediainfo(self, meta: Meta, builder: DescriptionBuilder) -> str:
        """TV Vault requires the full MediaInfo output for a representative file."""
        mi_path = Path(meta.base_dir) / "tmp" / meta.uuid / "MEDIAINFO_CLEANPATH.txt"
        if mi_path.is_file():
            async with aiofiles.open(mi_path, encoding="utf-8") as mi_file:
                media_info = await mi_file.read()
        else:
            media_info = await builder.get_mediainfo_section(meta)
        return strip_report_by_line(media_info).strip() if media_info else ""

    async def generate_description(self, meta: Meta) -> str:
        """The Torrent-Specific Info field: notes, full MediaInfo, then screenshots."""
        builder = DescriptionBuilder(self.tracker, self.config)
        sections: list[str] = []

        notes = "\n\n".join(part for part in (str(meta.description or "").strip(), await builder.get_user_description(meta)) if part)
        if notes:
            sections.append(notes)

        if meta.is_disc == "BDMV":
            if bd_info := await builder.get_bdinfo_section(meta):
                sections.append(f"[mediainfo]\n{bd_info}\n[/mediainfo]")
        elif media_info := await self._full_mediainfo(meta, builder):
            sections.append(f"[mediainfo]\n{media_info}\n[/mediainfo]")

        # Full-size images (raw_url, not thumbnails), at most image_count of them.
        screenshots = [
            str(image["raw_url"])
            for image in get_tracker_image_collection(meta, self.tracker, "screenshots")
            if _is_str_dict(image) and isinstance(image.get("raw_url"), str) and image["raw_url"]
        ][: self._screenshot_count()]
        if screenshots:
            sections.append("\n".join(f"[img]{url}[/img]" for url in screenshots))

        if meta.ua_signature:
            sections.append(f"[align=right][size=1]{meta.ua_signature}[/size][/align]")
        description = "\n\n".join(section for section in sections if section.strip())

        if meta.debug:
            desc_file = Path(meta.base_dir) / "tmp" / meta.uuid / f"[{self.tracker}]DESCRIPTION.txt"
            logger.debug(f"DEBUG: Saving final description to [yellow]{desc_file}[/yellow]")
            async with aiofiles.open(desc_file, "w", encoding="utf-8") as description_file:
                await description_file.write(description)
        return description

    # ----- upload form -----------------------------------------------------------

    async def get_upload_data(self, meta: Meta, preview: bool = False) -> dict[str, str]:
        """Build TV Vault's upload form. A ``preview`` leaves out the description, which needs the screenshots."""
        group_id = self.group_ids.get(str(meta.uuid), "")
        tvdb_id = meta.tvdb_id or meta.tvdb
        movie = self._is_tv_movie(meta)
        data = {
            "submit": "true",
            "groupid": group_id,
            "type": self.get_category(meta),
            "show": self.get_show_title(meta),
            "title": self.get_season_title(meta),
            "year": self.get_year(meta),
            # Only asked for when the show is new to the site.
            "ending_year": "" if group_id else self.get_ending_year(meta),
            "container": self.get_container(meta),
            "format": self.get_video_format(meta),
            "hdrvideo": self.get_hdr(meta),
            "aformat": self.get_audio_format(meta),
            "media": self.get_media(meta),
            "tags": self.get_tags(meta),
            "image": self.get_cover(meta),
            "imdb_link": str(meta.imdb_tt or ""),
            "wiki_link": "",
            "rt_link": "",
            "rt_link_type": "m" if movie else "tv",
            "tvtropes_link": "",
            "tvtropes_link_type": "Film" if movie else "Series",
            "tvdb_link": str(tvdb_id) if tvdb_id else "",
            "tvdb_link_type": "movies" if movie else "series",
            "show_desc": str(meta.overview or meta.overview_meta or "").strip(),
            "release_desc": "" if preview else await self.generate_description(meta),
        }
        # Checkboxes are only sent when ticked.
        if meta.subtitle_languages:
            data["subtitles"] = "on"
        if meta.has_commentary:
            data["commentary"] = "on"
        return data

    async def get_payload_preview(self, meta: Meta) -> payload_preview.TrackerPreview:
        """The upload form as it is known before screenshots, for the Web UI to show and edit."""
        specs = {
            "submit": payload_preview.readonly("Submit"),
            "groupid": payload_preview.readonly("Existing show group"),
            "type": payload_preview.choice("Category", CATEGORIES),
            "show": payload_preview.text("Show title"),
            "title": payload_preview.text("Season / episode"),
            "year": payload_preview.number("Year"),
            "ending_year": payload_preview.number("Ending year"),
            "container": payload_preview.choice("Container", CONTAINERS),
            "format": payload_preview.choice("Video format", VIDEO_FORMATS),
            "hdrvideo": payload_preview.choice("HDR / 10-bit", HDR_VIDEO),
            "aformat": payload_preview.choice("Audio format", AUDIO_FORMATS),
            "media": payload_preview.choice("Source", MEDIA),
            "subtitles": payload_preview.flag("Includes subtitles", on="on", off=None),
            "commentary": payload_preview.flag("Includes commentary", on="on", off=None),
            "tags": payload_preview.text("Tags", max_length=1000),
            "image": payload_preview.text("Image"),
            "imdb_link": payload_preview.text("IMDb ID"),
            "tvdb_link": payload_preview.text("TheTVDB ID"),
            "show_desc": payload_preview.text("Show description", max_length=5000),
        }
        data = await self.get_upload_data(meta, preview=True)
        data.setdefault("subtitles", "")
        data.setdefault("commentary", "")
        deferred = {"release_desc": payload_preview.DESCRIPTION_NOTE}
        return payload_preview.describe(self.tracker, data, specs, deferred=deferred, name_key="show")

    async def get_additional_checks(self, meta: Meta) -> bool:
        """Enforce the TV Vault upload rules that can be checked from the release."""
        if self._is_tv_movie(meta) and "tv movie" not in self._genres(meta):
            logger.warning(f"{self.tracker}: [red]TV Vault takes TV movies only, and TMDb doesn't list this film as a TV Movie.[/red]")
            return False

        if meta.valid_mi is False:
            logger.warning(f"{self.tracker}: [red]Invalid MediaInfo; TV Vault requires full MediaInfo output.[/red]")
            return False

        # HEVC/H.265 is only accepted for 2160p and larger.
        if self.get_video_format(meta) == "H.265":
            height = int(match.group(1)) if (match := re.match(r"(\d{3,4})", str(meta.resolution or ""))) else 0
            if height < 2160:
                logger.warning(f"{self.tracker}: [red]H.265/HEVC is only allowed for 2160p and larger; this release is {meta.resolution or 'unknown'}.[/red]")
                return False

        if self._is_youtube(meta):
            logger.warning(f"{self.tracker}: [yellow]YouTube isn't a 'Web' source on TV Vault. Source is set to Unknown; change it to TV or DVD if you know which.[/yellow]")
        return True

    # ----- duplicate search --------------------------------------------------------

    @staticmethod
    def _row_name(row: Tag) -> str:
        cell = row.select_one("td:nth-of-type(2)")
        if not cell:
            return ""
        cell = BeautifulSoup(str(cell), "html.parser")
        for unwanted in cell.select("div.tags, span"):
            unwanted.decompose()
        return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()

    @staticmethod
    def _row_size(row: Tag) -> str:
        cell = row.select_one("td:nth-of-type(5)")
        if not cell:
            return ""
        cell = BeautifulSoup(str(cell), "html.parser")
        for unwanted in cell.select("div"):
            unwanted.decompose()
        return cell.get_text(" ", strip=True)

    def _parse_results(self, html: str, page_url: str) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for row in BeautifulSoup(html, "html.parser").select("table#torrent_table tr.torrent"):
            link = row.select_one('a[href*="torrentid="]')
            href = str(link.get("href", "")) if link else ""
            if not href:
                continue
            results.append({"name": self._row_name(row), "size": self._row_size(row), "link": str(httpx.URL(page_url).join(href))})
        return results

    @classmethod
    def _same_season(cls, name: str, meta: Meta) -> bool:
        if cls._is_tv_movie(meta):
            return "tv movie" in name.lower()
        season = int(meta.season_int or 0)
        if season == 0:
            return "special" in name.lower()
        return re.search(rf"(?i)\b(?:season|series)\s+0*{season}\b", name) is not None

    async def search_existing(self, meta: Meta) -> list[dict[str, str]]:
        """Search TV Vault for the show, remember its group, and return torrents from the same season."""
        cookie_jar = await self.cookie_validator.load_session_cookies(meta, self.tracker)
        if not cookie_jar:
            return []
        self.session.cookies = cast(Any, cookie_jar)

        searches: list[dict[str, str]] = []
        if meta.imdb_tt:
            searches.append({"imdbid": meta.imdb_tt, "disablegrouping": "1"})
        searches.append({"searchstr": str(meta.title), "disablegrouping": "1"})

        uuid = str(meta.uuid)
        TVVault.group_ids.pop(uuid, None)
        TVVault.show_conflicts.pop(uuid, None)
        our_titles = {name.casefold() for name in (str(meta.title or ""), self.get_show_title(meta)) if name}

        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, params in enumerate(searches):
            response = await self.session.get(f"{self.base_url}/torrents.php", params=params)
            response.raise_for_status()
            if "login.php" in str(response.url) or 'id="loginform"' in response.text:
                raise RuntimeError(f"{self.tracker}: [yellow]Could not search; the cookies may have expired. Export them again.[/yellow]")

            rows = self._parse_results(response.text, str(response.url))
            if index == 0 and meta.imdb_tt and rows:
                group = re.search(r"[?&]id=(\d+)", rows[0]["link"])
                if group:
                    TVVault.group_ids[uuid] = group.group(1)
                    logger.info(f"{self.tracker}: show already exists (group {group.group(1)}); the upload will be added to it.")
            elif uuid not in TVVault.group_ids:
                # The show isn't on the site under this IMDb ID, but another show may already use its name.
                for row in rows:
                    existing = row["name"].split(" - ", 1)[0].strip()
                    if existing.casefold() in our_titles:
                        TVVault.show_conflicts[uuid] = existing
                        logger.warning(
                            f"{self.tracker}: [yellow]'{existing}' is already on TV Vault without this IMDb ID. If it's a different show, "
                            "the rules ask you to make the title unique, e.g. add ' (US)', the genre, or the first-broadcast year. "
                            "If it's the same show, add the IMDb link to its show page instead.[/yellow]"
                        )
                        break
            for row in rows:
                if row["link"] in seen or not self._same_season(row["name"], meta):
                    continue
                seen.add(row["link"])
                results.append(row)
        return results

    # ----- upload ----------------------------------------------------------------

    async def upload(self, meta: Meta) -> bool:
        cookie_jar = await self.cookie_validator.load_session_cookies(meta, self.tracker)
        if not cookie_jar:
            meta.tracker_status[self.tracker]["status_message"] = "data error: No TV Vault cookies found. Export them to data/cookies/TVVAULT.txt."
            return False
        self.session.cookies = cast(Any, cookie_jar)

        # Don't create a second show with the same name; the user has to make the title unique first.
        conflict = self.show_conflicts.get(str(meta.uuid))
        edits = cast(dict[str, dict[str, Any]], getattr(meta, "payload_edits", None) or {}).get(self.tracker) or {}
        if conflict and not str(edits.get("show") or "").strip():
            meta.tracker_status[self.tracker]["status_message"] = (
                f"data error: A show called '{conflict}' is already on TV Vault but doesn't match this IMDb ID. "
                "Change the Show title to something unique (e.g. add ' (US)' or the year) in the Web UI, "
                "or add the IMDb link to the existing show page if it's the same show."
            )
            return False

        return await self.cookie_auth_uploader.handle_upload(
            meta=meta,
            tracker=self.tracker,
            source_flag=self.source_flag,
            torrent_url=self.torrent_url,
            data=await self.get_upload_data(meta),
            torrent_field_name="file_input",
            upload_cookies=self.session.cookies,
            upload_url=self.upload_url,
            id_pattern=r"torrents\.php\?id=(\d+)",
            # A successful upload lands on the show page, which has download links; a failed one shows the form again.
            success_list=["action=download&amp;id=", "action=download&id="],
        )
