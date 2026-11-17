# ruff: noqa: S101
import asyncio
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from src.meta import Meta
from src.trackers.GAZELLE.tvvault import TVVault


def tracker() -> TVVault:
    site = TVVault({"DEFAULT": {}, "TRACKERS": {"TVVAULT": {"announce_url": "http://tracker.tv-vault.me:34000/key/announce"}}})
    site.search_delay = 0
    return site


def make_meta(**overrides: Any) -> Meta:
    values: dict[str, Any] = {
        "category": "TV",
        "title": "Almost Grown",
        "year": 1988,
        "first_air_date": "1988-11-27",
        "last_air_date": "1989-02-27",
        "season_int": 1,
        "season": "S01",
        "episode_int": 0,
        "episode": "",
        "tv_pack": True,
        "origin_country": ["US"],
        "genres": ["Drama"],
        "imdb_tt": "tt0094414",
        "tvdb_id": 77122,
        "overview": "A couple's life over three decades.",
        "type": "DVDRIP",
        "source": "NTSC",
        "sd": 1,
        "audio": "DD 2.0",
        "video_codec": "AVC",
        "video_encode": "x264",
        "filelist": ["Almost.Grown.S01E01.mkv", "Almost.Grown.S01E02.mkv"],
        "subtitle_languages": ["English"],
        "uuid": "tvv-test",
        "base_dir": ".",
        "ua_signature": "Upload-Assistant",
        "tracker_status": {"TVVAULT": {}},
    }
    values.update(overrides)
    return Meta(**values)


@pytest.fixture(autouse=True)
def clear_group_ids():
    TVVault.group_ids.clear()
    TVVault.show_conflicts.clear()
    yield
    TVVault.group_ids.clear()
    TVVault.show_conflicts.clear()


def test_compacts_episode_lists_like_the_site():
    assert TVVault.compact_numbers([9, 1, 2, 4, 5, 6]) == "01-02, 04-06, 09"
    assert TVVault.compact_numbers([7]) == "07"
    assert TVVault.compact_numbers([7, 8]) == "07-08"


def test_season_and_episode_labels():
    site = tracker()
    assert site.get_season_title(make_meta()) == "Season 1"
    assert site.get_season_title(make_meta(origin_country=["GB"], season_int=2)) == "Series 2"
    assert site.get_season_title(make_meta(season_int=0)) == "Specials"

    episode = make_meta(tv_pack=False, episode="E01", episode_int=1, episode_title="Pilot")
    assert site.get_season_title(episode) == "Season 1, Episode 01: Pilot"
    # TMDb's placeholder names add nothing.
    assert site.get_season_title(make_meta(tv_pack=False, episode="E05", episode_int=5, tmdb_episode_data={"name": "Episode 5"})) == "Season 1, Episode 05"
    assert site.get_season_title(make_meta(tv_pack=False, episode="E01E02", episode_int=1)) == "Season 1, Episode 01-02"

    partial = make_meta(season_pack_incomplete=True, filelist=[f"Almost.Grown.S01E{n:02d}.mkv" for n in (1, 2, 4, 5, 6, 9)])
    assert site.get_season_title(partial) == "Season 1, Episode 01-02, 04-06, 09"


def test_year_is_when_the_season_or_episode_first_aired():
    site = tracker()
    assert site.get_year(make_meta(tmdb_season_data={"air_date": "1989-01-02"})) == "1989"
    assert site.get_year(make_meta(tv_pack=False, episode_int=3, tmdb_episode_data={"air_date": "1988-12-11"})) == "1988"
    assert site.get_year(make_meta()) == "1988"
    assert site.get_ending_year(make_meta()) == "1989"
    assert site.get_ending_year(make_meta(last_air_date="1988-12-30")) == ""


def test_display_name():
    assert asyncio.run(tracker().get_name(make_meta())) == "Almost Grown - Season 1 (1988)"
    assert asyncio.run(tracker().get_name(make_meta(manual_name="Custom Name "))) == "Custom Name"


def test_rip_specifics_use_the_forms_values():
    site = tracker()
    assert site.get_container(make_meta()) == "MKV"
    assert site.get_container(make_meta(filelist=["a.mkv", "b.avi"])) == "Multiple"
    assert site.get_container(make_meta(is_disc="DVD")) == "VOB/IFO"
    assert site.get_video_format(make_meta()) == "H.264"
    assert site.get_video_format(make_meta(video_codec="HEVC", video_encode="x265")) == "H.265"
    xvid = make_meta(video_codec="", video_encode="", mediainfo={"media": {"track": [{"@type": "Video", "Format": "MPEG-4 Visual", "CodecID": "XVID"}]}})
    assert site.get_video_format(xvid) == "XviD"
    assert site.get_audio_format(make_meta()) == "AC3"
    assert site.get_audio_format(make_meta(audio="DD+ 5.1 Atmos")) == "E-AC3"
    assert site.get_audio_format(make_meta(audio="AAC 2.0")) == "AAC"
    assert site.get_audio_format(make_meta(audio="LPCM 2.0")) == "(L)PCM"
    assert site.get_media(make_meta()) == "DVD"
    assert site.get_media(make_meta(type="WEBDL", source="WEB", sd=0)) == "Web"
    assert site.get_media(make_meta(type="HDTV", source="HDTV", sd=0)) == "HDTV"
    assert site.get_media(make_meta(type="HDTV", source="HDTV", sd=1)) == "PDTV"
    assert site.get_media(make_meta(type="ENCODE", source="BluRay", sd=0)) == "Blu-ray"
    assert site.get_hdr(make_meta()) == "0"
    assert site.get_hdr(make_meta(hdr="DV HDR")) == "3"
    assert site.get_hdr(make_meta(hdr="HDR")) == "1"


def test_category_and_official_tags():
    site = tracker()
    assert site.get_category(make_meta()) == "Drama"
    assert site.get_category(make_meta(genres=["Comedy", "Drama"])) == "Comedy"
    assert site.get_category(make_meta(genres=["Sci-Fi & Fantasy", "Drama"])) == "Sci-Fi"
    assert site.get_category(make_meta(genres=["Talk"])) == "Chatshow"
    assert site.get_category(make_meta(genres=["Reality"])) == "Other"
    assert site.get_tags(make_meta(genres=["Drama", "Crime"])) == "80s, drama, crime"
    assert site.get_tags(make_meta(genres=["Action & Adventure"], tmdb_type="Miniseries", first_air_date="2016-01-01")) == "10s, action, adventure, mini.series"
    # 1930s is not an official decade tag.
    assert site.get_tags(make_meta(first_air_date="1934-01-01", genres=[])) == ""


def test_upload_form_fields():
    data = asyncio.run(tracker().get_upload_data(make_meta(), preview=True))
    assert data == {
        "submit": "true",
        "groupid": "",
        "type": "Drama",
        "show": "Almost Grown",
        "title": "Season 1",
        "year": "1988",
        "ending_year": "1989",
        "container": "MKV",
        "format": "H.264",
        "hdrvideo": "0",
        "aformat": "AC3",
        "media": "DVD",
        "tags": "80s, drama",
        "image": "",
        "imdb_link": "tt0094414",
        "wiki_link": "",
        "rt_link": "",
        "rt_link_type": "tv",
        "tvtropes_link": "",
        "tvtropes_link_type": "Series",
        "tvdb_link": "77122",
        "tvdb_link_type": "series",
        "show_desc": "A couple's life over three decades.",
        "release_desc": "",
        "subtitles": "on",
    }


def test_existing_show_group_is_used_and_ending_year_left_out():
    TVVault.group_ids["tvv-test"] = "25455"
    data = asyncio.run(tracker().get_upload_data(make_meta(), preview=True))
    assert data["groupid"] == "25455"
    assert data["ending_year"] == ""


def test_description_has_full_mediainfo_and_screenshots(tmp_path):
    mi_dir = tmp_path / "tmp" / "tvv-test"
    mi_dir.mkdir(parents=True)
    (mi_dir / "MEDIAINFO_CLEANPATH.txt").write_text("General\nComplete name : Almost.Grown.S01E01.mkv\n", encoding="utf-8")
    item = make_meta(
        base_dir=str(tmp_path), description="Sourced from the 1989 broadcast.", image_list=[{"raw_url": "https://i.example/1.png"}, {"raw_url": "https://i.example/2.png"}]
    )
    description = asyncio.run(tracker().generate_description(item))
    assert description.startswith("Sourced from the 1989 broadcast.\n\n[mediainfo]\nGeneral\nComplete name : Almost.Grown.S01E01.mkv\n[/mediainfo]")
    assert "[img]https://i.example/1.png[/img]\n[img]https://i.example/2.png[/img]" in description


SEARCH_HTML = """
<table class="torrent_table" id="torrent_table"><tbody>
<tr class="colhead"><td></td></tr>
<tr class="torrent ">
  <td class="center"><img src="static/common/caticons/drama.png" alt="Drama"></td>
  <td><span></span>
    <a href="torrents.php?id=25455&amp;torrentid=104112" title="View Torrent">Almost Grown - Season 1, Episode 1-2, 4-6, 9</a>
    (1988) (MKV/H.264/AAC/TV)<br><div class="tags"><a href="torrents.php?searchtags=80s">80s</a></div></td>
  <td>6</td><td class="nobr">1y37w</td>
  <td class="nobr">2.4255 GB<br><div class="center">(<span class="thread_unread">2</span>)</div></td>
  <td>19</td><td>5</td><td>0</td>
</tr>
<tr class="torrent ">
  <td class="center"><img alt="Drama"></td>
  <td><a href="torrents.php?id=25455&amp;torrentid=200000">Almost Grown - Season 2</a> (1989) (MKV/H.264/AAC/DVD)</td>
  <td>9</td><td class="nobr">1d</td><td class="nobr">4.1 GB</td><td>1</td><td>1</td><td>0</td>
</tr>
</tbody></table>
"""


def test_search_finds_the_show_group_and_same_season_torrents():
    async def run():
        site = tracker()
        site.cookie_validator.load_session_cookies = AsyncMock(return_value=httpx.Cookies({"session": "test"}))
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, text=SEARCH_HTML, request=request)

        site.session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        results = await site.search_existing(make_meta())
        await site.session.aclose()
        return requests, results

    requests, results = asyncio.run(run())
    # The IMDb search found the show, so no second (name) search is made.
    assert [dict(request.url.params) for request in requests] == [{"imdbid": "tt0094414", "disablegrouping": "1"}]
    assert TVVault.group_ids["tvv-test"] == "25455"
    assert results == [
        {
            "name": "Almost Grown - Season 1, Episode 1-2, 4-6, 9 (1988) (MKV/H.264/AAC/TV)",
            "size": "2.4255 GB",
            "link": "https://tv-vault.me/torrents.php?id=25455&torrentid=104112",
        }
    ]


def test_name_search_runs_when_imdb_finds_nothing_and_a_refusal_is_not_an_error():
    async def run(name_status: int):
        site = tracker()
        site.cookie_validator.load_session_cookies = AsyncMock(return_value=httpx.Cookies({"session": "test"}))
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if "imdbid" in request.url.params:
                return httpx.Response(200, text='<table id="torrent_table"></table>', request=request)
            return httpx.Response(name_status, text=SEARCH_HTML, request=request)

        site.session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        results = await site.search_existing(make_meta())
        await site.session.aclose()
        return requests, results

    requests, results = asyncio.run(run(200))
    assert dict(requests[1].url.params) == {"searchstr": "Almost Grown", "disablegrouping": "1"}
    assert len(results) == 1

    requests, results = asyncio.run(run(403))
    assert len(requests) == 2
    assert results == []


def test_preview_shows_the_full_tv_vault_name():
    preview = asyncio.run(tracker().get_payload_preview(make_meta(resolution="2160p", year=2019, first_air_date="2019-06-02")))
    assert preview.name == "Almost Grown - Season 1 (2160p) (2019)"


def test_search_reports_expired_cookies():
    async def run():
        site = tracker()
        site.cookie_validator.load_session_cookies = AsyncMock(return_value=httpx.Cookies({"session": "old"}))

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text='<form name="loginform" id="loginform"></form>', request=request)

        site.session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await site.search_existing(make_meta())
        finally:
            await site.session.aclose()

    with pytest.raises(RuntimeError, match="cookies"):
        asyncio.run(run())


def test_preview_lists_the_editable_fields():
    preview = asyncio.run(tracker().get_payload_preview(make_meta()))
    keys = {field.key for field in preview.fields}
    assert {"type", "show", "title", "container", "format", "aformat", "media", "tags"} <= keys


# ----- upload rules (rules.php?p=upload) ---------------------------------------


def test_show_title_leads_with_the_original_name_and_an_aka():
    site = tracker()
    assert site.get_show_title(make_meta()) == "Almost Grown"
    french = make_meta(title="The Returned", original_title="Les Revenants", original_language="fr")
    assert site.get_show_title(french) == "Les Revenants (aka The Returned)"
    # Non-Latin originals should be romanized, which UA can't do, so the English title stays.
    assert site.get_show_title(make_meta(title="Midnight Diner", original_title="深夜食堂", original_language="ja")) == "Midnight Diner"
    assert site.get_show_title(make_meta(original_title="Almost Grown", original_language="en")) == "Almost Grown"


def test_nearly_complete_seasons_list_the_missing_episodes():
    season = {"episodes": [{"episode_number": n} for n in range(1, 18)]}
    have = [n for n in range(1, 18) if n not in (14, 17)]
    nearly = make_meta(season_pack_incomplete=True, tmdb_season_data=season, filelist=[f"Show.S01E{n:02d}.mkv" for n in have])
    assert tracker().get_season_title(nearly) == "Season 1 (Missing Episode 14, 17)"
    # With most episodes missing, the episodes that are there are listed instead.
    sparse = make_meta(season_pack_incomplete=True, tmdb_season_data=season, filelist=["Show.S01E01.mkv", "Show.S01E07.mkv"])
    assert tracker().get_season_title(sparse) == "Season 1, Episode 01, 07"


def test_miniseries_and_tv_movies_use_the_rules_labels():
    site = tracker()
    assert site.get_season_title(make_meta(tmdb_type="Miniseries")) == "Miniseries"
    movie = make_meta(category="MOVIE", tv_pack=False, genres=["TV Movie", "Drama"], year=1977)
    assert site.get_season_title(movie) == "TV Movie"
    assert asyncio.run(site.get_name(movie)) == "Almost Grown - TV Movie (1977)"
    data = asyncio.run(site.get_upload_data(movie, preview=True))
    assert (data["rt_link_type"], data["tvtropes_link_type"], data["tvdb_link_type"]) == ("m", "Film", "movies")
    assert "tv.movie" in data["tags"]
    assert asyncio.run(site.get_additional_checks(movie))
    assert not asyncio.run(site.get_additional_checks(make_meta(category="MOVIE", tv_pack=False, genres=["Drama"])))


def test_container_and_format_notes():
    site = tracker()
    assert site.get_video_format(make_meta(is_disc="DVD", video_codec="", video_encode="")) == "MPEG-2"
    assert site.get_container(make_meta(filelist=["Show.S01E01.ts"])) == "TS"
    assert site.get_container(make_meta(filelist=["00001.m2ts"])) == "m2ts/bdmv"
    assert site.get_audio_format(make_meta(audio="MPEG Audio Layer 3")) == "MP3"


def test_hevc_is_only_allowed_at_2160p_and_up():
    site = tracker()
    assert not asyncio.run(site.get_additional_checks(make_meta(video_codec="HEVC", video_encode="x265", resolution="1080p")))
    assert asyncio.run(site.get_additional_checks(make_meta(video_codec="HEVC", video_encode="x265", resolution="2160p")))
    assert asyncio.run(site.get_additional_checks(make_meta(resolution="576p")))


def test_youtube_is_not_a_web_source():
    assert tracker().get_media(make_meta(type="WEBDL", source="WEB", service="YT")) == "Unknown"
    assert tracker().get_media(make_meta(type="WEBDL", source="WEB", service="NF")) == "Web"


OTHER_SHOW_HTML = """
<table id="torrent_table"><tbody>
<tr class="torrent "><td><img alt="Comedy"></td>
  <td><a href="torrents.php?id=900&amp;torrentid=901">Almost Grown - Series 1</a> (2005) (AVI/XviD/MP3/TV)</td>
  <td>6</td><td class="nobr">1y</td><td class="nobr">1 GB</td><td>1</td><td>1</td><td>0</td></tr>
</tbody></table>
"""


def test_same_name_show_without_imdb_match_blocks_the_upload_until_renamed():
    async def run():
        site = tracker()
        site.cookie_validator.load_session_cookies = AsyncMock(return_value=httpx.Cookies({"session": "test"}))

        async def handler(request: httpx.Request) -> httpx.Response:
            html = '<table id="torrent_table"></table>' if "imdbid" in request.url.params else OTHER_SHOW_HTML
            return httpx.Response(200, text=html, request=request)

        site.session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await site.search_existing(make_meta())
        await site.session.aclose()
        item = make_meta()
        blocked = await site.upload(item)
        message = item.tracker_status["TVVAULT"]["status_message"]

        renamed = make_meta(payload_edits={"TVVAULT": {"show": "Almost Grown (US)"}})
        site.cookie_auth_uploader.handle_upload = AsyncMock(return_value=True)
        allowed = await site.upload(renamed)
        return blocked, message, allowed

    blocked, message, allowed = asyncio.run(run())
    assert TVVault.show_conflicts["tvv-test"] == "Almost Grown"
    assert "tvv-test" not in TVVault.group_ids
    assert blocked is False
    assert "already on TV Vault" in message
    assert allowed is True


# ----- staff feedback: resolution in the name, 1-3 full-size screenshots -----


def test_hd_releases_carry_their_resolution_in_the_name():
    site = tracker()
    assert site.get_season_title(make_meta(resolution="1080p")) == "Season 1 (1080p)"
    assert site.get_season_title(make_meta(resolution="720p", tv_pack=False, episode="E01", episode_int=1, episode_title="Pilot")) == "Season 1, Episode 01: Pilot (720p)"
    assert site.get_season_title(make_meta(resolution="576p")) == "Season 1"
    assert asyncio.run(site.get_name(make_meta(resolution="1080p"))) == "Almost Grown - Season 1 (1080p) (1988)"


def test_description_keeps_at_most_three_full_size_screenshots(tmp_path):
    images = [{"raw_url": f"https://i.example/full{n}.png", "img_url": f"https://i.example/thumb{n}.png"} for n in range(1, 7)]
    description = asyncio.run(tracker().generate_description(make_meta(base_dir=str(tmp_path), image_list=images)))
    assert description.count("[img]") == 3
    assert "[img]https://i.example/full3.png[/img]" in description
    assert "thumb" not in description

    one = TVVault({"DEFAULT": {}, "TRACKERS": {"TVVAULT": {"image_count": 1}}})
    assert asyncio.run(one.generate_description(make_meta(base_dir=str(tmp_path), image_list=images))).count("[img]") == 1


def test_signature_is_left_out_when_turned_off(tmp_path):
    with_signature = asyncio.run(tracker().generate_description(make_meta(base_dir=str(tmp_path))))
    assert "Upload-Assistant" in with_signature
    # DEFAULT "ua_signature": False makes upload.py set an empty signature.
    without = asyncio.run(tracker().generate_description(make_meta(base_dir=str(tmp_path), ua_signature="")))
    assert "[align=right]" not in without


def test_torrents_use_pieces_of_16_mib_or_less():
    mib = 1024 * 1024
    policy = TVVault.torrent_policy
    assert policy.max_piece_size == 16 * mib
    # A 2160p season pack that would normally get 32 MiB pieces is capped at 16 MiB.
    assert policy.choose_piece_size(80 * 1024 * mib, 32 * mib) == 16 * mib
    assert policy.choose_piece_size(2 * 1024 * mib, 4 * mib) == 4 * mib
