"""Tests for the read-only Tautulli client (jellyseerr_mcp.clients.tautulli).

All HTTP is mocked with httpx.MockTransport; requests are recorded and the
assertions run after the call so a client error cannot mask a mismatch. Shapes
mirror the verified upstream contracts in the official API reference
(Tautulli-API-Reference.md): every response is wrapped in
{"response": {"result": ..., "data": ...}} and API errors arrive with HTTP 200,
so the envelope, not the status code, decides success.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from jellyseerr_mcp.clients.tautulli import TautulliClient
from jellyseerr_mcp.config import ServiceConfig
from jellyseerr_mcp.http import ServiceError

TAUTULLI_URL = "http://tautulli.test:8181"
API_KEY = "tautulli-key-123"


def make_config(**overrides: Any) -> ServiceConfig:
    values: dict[str, Any] = dict(name="tautulli", url=TAUTULLI_URL, api_key=API_KEY,
                                  timeout=10.0, allow_writes=False)
    values.update(overrides)
    return ServiceConfig(**values)


class Fake:
    """MockTransport handler that records every request."""

    def __init__(self, responder: Any) -> None:
        self.seen: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self._responder(request)

    def params(self, index: int = 0) -> dict:
        return dict(self.seen[index].url.params)


def make_client(handler: Fake, **overrides: Any) -> TautulliClient:
    return TautulliClient(make_config(**overrides), transport=httpx.MockTransport(handler))


def envelope(data: Any, result: str = "success", message: Any = None) -> httpx.Response:
    return httpx.Response(200, json={"response": {"result": result, "message": message, "data": data}})


def cmd_of(request: httpx.Request) -> str:
    return str(request.url.params.get("cmd"))


# --------------------------------------------------------------- envelope contract

def test_success_unwraps_the_data_and_sends_cmd_and_apikey():
    calls: list[str] = []

    def responder(request):
        calls.append(cmd_of(request))
        return envelope({"connected": True})

    client = make_client(Fake(responder))
    # The live 2.17 shape: data is {"connected": true}, not a result/message pair.
    assert client.server_status() == {"connected": True}
    assert calls == ["server_status"]
    client.close()


def test_api_error_inside_http_200_is_raised_not_returned():
    """Tautulli reports failures with HTTP 200; the envelope decides success."""
    client = make_client(Fake(lambda request: envelope(None, result="error", message="boom")))
    with pytest.raises(ServiceError, match="Tautulli API request failed"):
        client.libraries()
    client.close()


def test_a_rejected_api_key_is_reported_without_echoing_the_upstream_message():
    secret_message = "Invalid apikey: tautulli-key-123"
    client = make_client(Fake(lambda request: envelope(None, result="error", message=secret_message)))
    with pytest.raises(ServiceError) as caught:
        client.server_status()
    assert "rejected the API key" in str(caught.value)
    assert API_KEY not in str(caught.value)  # never leak the credential or the body
    client.close()


def test_the_api_key_travels_as_a_query_parameter_and_never_as_a_header():
    handler = Fake(lambda request: envelope([]))
    client = make_client(handler)
    client.users()
    params = handler.params()
    assert params["apikey"] == API_KEY
    assert params["out_type"] == "json"
    assert handler.seen[0].url.path == "/api/v2"
    assert "X-Api-Key" not in handler.seen[0].headers  # selected transport, per user choice
    client.close()


def test_a_non_tautulli_body_is_rejected():
    client = make_client(Fake(lambda request: httpx.Response(200, json={"unexpected": True})))
    with pytest.raises(ServiceError, match="unexpected Tautulli response"):
        client.libraries()
    client.close()


# ------------------------------------------------------------------------- activity

def test_activity_returns_current_streams_and_counts_them():
    session = {"session_key": "27", "user": "LordCommanderSnow", "user_id": 133788,
               "media_type": "episode", "title": "The Red Woman",
               "grandparent_title": "Game of Thrones", "state": "playing",
               "progress_percent": "0", "ip_address": "10.10.10.1", "location": "lan",
               "platform": "Plex Media Player", "file": "/media/secret.mkv",
               "streams": [{"id": "1"}], "markers": [{"id": 1}]}
    client = make_client(Fake(lambda request: envelope({"stream_count": 1, "sessions": [session]})))
    result = client.activity()
    assert result["stream_count"] == 1
    row = result["sessions"][0]
    assert row["user"] == "LordCommanderSnow" and row["title"] == "The Red Woman"
    assert row["ip_address"] == "10.10.10.1"  # the user chose to keep IPs
    assert "file" not in row and "streams" not in row and "markers" not in row
    client.close()


def test_activity_reports_no_streams_without_erroring():
    client = make_client(Fake(lambda request: envelope({"stream_count": 0, "sessions": []})))
    assert client.activity() == {"stream_count": 0, "sessions": []}
    client.close()


def test_activity_validates_the_session_selector_and_the_key():
    handler = Fake(lambda request: envelope({}))
    client = make_client(handler)
    with pytest.raises(ValueError, match="session_key"):
        client.activity(session_key=0)
    with pytest.raises(ValueError, match="session_id"):
        client.activity(session_id="")
    assert handler.seen == []  # validated before any request
    client.close()


# -------------------------------------------------------------------------- history

def test_history_sends_validated_filters_and_projects_rows():
    seen: list[dict] = []

    def responder(request):
        seen.append(dict(request.url.params))
        return envelope({"recordsTotal": 1000, "recordsFiltered": 250,
                         "data": [{"row_id": 1124, "date": 1462687607, "user": "DanyKhaleesi69",
                                   "full_title": "Game of Thrones - The Red Woman",
                                   "media_type": "episode", "rating_key": 4348,
                                   "percent_complete": 84, "file": "/media/x.mkv",
                                   "ip_address": "192.168.1.5"}]})

    client = make_client(Fake(responder))
    result = client.history(user="DanyKhaleesi69", rating_key=4348, section_id=2,
                            media_type="episode", start_date="2026-01-01",
                            before="2026-02-01", after="2026-01-01", limit=10)
    params = seen[0]
    assert params["cmd"] == "get_history"
    assert params["user"] == "DanyKhaleesi69"
    assert params["rating_key"] == "4348"
    assert params["media_type"] == "episode"
    assert params["start_date"] == "2026-01-01" and params["length"] == "10"
    assert params["grouping"] == "0" and params["order_dir"] == "desc"
    row = result["items"][0]
    assert row["user"] == "DanyKhaleesi69" and row["percent_complete"] == 84
    assert "file" not in row
    assert result["total"] == 1000 and result["filtered"] == 250
    assert result["pagination"] == "native" and result["has_more"] is True
    client.close()


def test_history_has_more_is_false_on_the_last_page():
    client = make_client(Fake(lambda request: envelope(
        {"recordsTotal": 3, "recordsFiltered": 3, "data": [{"row_id": 3}]})))
    result = client.history(offset=2, limit=25)
    assert result["has_more"] is False
    client.close()


@pytest.mark.parametrize("kwargs, message", [
    ({"media_type": "book"}, "media_type"),
    ({"order_dir": "sideways"}, "order_dir"),
    ({"order_column": "nope"}, "order_column"),
    ({"transcode_decision": "magic"}, "transcode_decision"),
    ({"start_date": "01/01/2026"}, "start_date"),
    ({"before": "not-a-date"}, "before"),
    ({"user_id": 0}, "user_id"),
    ({"limit": 0}, "limit"),
    ({"offset": -1}, "offset"),
])
def test_history_rejects_bad_filters_before_any_network(kwargs, message):
    handler = Fake(lambda request: envelope({}))
    client = make_client(handler)
    with pytest.raises(ValueError, match=message):
        client.history(**kwargs)
    assert handler.seen == []
    client.close()


# ------------------------------------------------------------------------- metadata

# ---------------------------------------------------------------------- last played

def test_last_played_for_a_movie_matches_on_its_own_rating_key():
    seen: list[dict] = []

    def responder(request):
        seen.append(dict(request.url.params))
        return envelope({"recordsTotal": 3, "recordsFiltered": 3, "data": [
            {"row_id": 9, "date": 1762000000, "user": "alice", "full_title": "Arrival",
             "media_type": "movie", "rating_key": 4348, "file": "/media/arrival.mkv"}]})

    client = make_client(Fake(responder))
    result = client.last_played(4348, media_type="movie")
    assert seen[0]["rating_key"] == "4348"          # a movie is its own key
    assert "grandparent_rating_key" not in seen[0]  # and must not be asked as one
    assert seen[0]["order_dir"] == "desc" and seen[0]["length"] == "1"
    assert result["played"] is True and result["last_played"] == 1762000000
    assert result["last_play"]["user"] == "alice"
    assert "file" not in result["last_play"]
    client.close()


def test_last_played_for_a_show_matches_through_the_grandparent_key():
    """A show has no plays of its own: Tautulli records them against episodes."""
    seen: list[dict] = []

    def responder(request):
        seen.append(dict(request.url.params))
        return envelope({"recordsTotal": 1, "recordsFiltered": 1, "data": [
            {"row_id": 4, "date": 1762100000, "user": "bob",
             "grandparent_title": "Severance", "media_type": "episode"}]})

    client = make_client(Fake(responder))
    result = client.last_played(1219, media_type="show")
    assert seen[0]["grandparent_rating_key"] == "1219"
    assert "rating_key" not in seen[0]  # asking by the show's own key would find nothing
    assert result["played"] is True and result["last_played"] == 1762100000
    client.close()


def test_last_played_for_a_season_matches_through_the_parent_key():
    seen: list[dict] = []

    def responder(request):
        seen.append(dict(request.url.params))
        return envelope({"recordsTotal": 0, "recordsFiltered": 0, "data": []})

    client = make_client(Fake(responder))
    result = client.last_played(153036, media_type="season")
    assert seen[0]["parent_rating_key"] == "153036"
    assert result["played"] is False and result["last_played"] is None
    client.close()


def test_last_played_for_a_season_succeeds_with_a_timestamp_and_user():
    """A covered season records plays against the episode's parent key, so the
    successful case must return the timestamp and user, not just apply the filter."""
    seen: list[dict] = []

    def responder(request):
        seen.append(dict(request.url.params))
        return envelope({"recordsTotal": 7, "recordsFiltered": 7, "data": [
            {"row_id": 12, "date": 1762200000, "user": "carol", "user_id": 555,
             "media_type": "episode", "parent_title": "Season 6",
             "grandparent_title": "Severance", "percent_complete": 100}]})

    client = make_client(Fake(responder))
    result = client.last_played(153036, media_type="season")
    assert seen[0]["parent_rating_key"] == "153036"
    assert "rating_key" not in seen[0] and "grandparent_rating_key" not in seen[0]
    assert result["played"] is True and result["last_played"] == 1762200000
    assert result["last_play"]["user"] == "carol" and result["last_play"]["user_id"] == 555
    client.close()


def test_last_played_reports_no_history_as_a_normal_answer():
    client = make_client(Fake(lambda request: envelope(
        {"recordsTotal": 0, "recordsFiltered": 0, "data": []})))
    result = client.last_played(999, media_type="movie")
    assert result == {"rating_key": 999, "media_type": "movie", "matched_by": "rating_key",
                      "played": False, "last_played": None, "last_play": None}
    client.close()


def test_last_played_honors_user_filters_and_validates_inputs():
    seen: list[dict] = []

    def responder(request):
        seen.append(dict(request.url.params))
        return envelope({"data": []})

    client = make_client(Fake(responder))
    client.last_played(4348, media_type="movie", user="alice", user_id=133788)
    assert seen[0]["user"] == "alice" and seen[0]["user_id"] == "133788"
    with pytest.raises(ValueError, match="media_type"):
        client.last_played(4348, media_type="playlist")
    with pytest.raises(ValueError, match="rating_key"):
        client.last_played(0)
    assert len(seen) == 1  # the invalid calls never reached the network
    client.close()


def test_metadata_projects_fields_and_drops_paths_and_raw_streams():
    payload = {"rating_key": "153037", "media_type": "episode", "title": "The Red Woman",
               "grandparent_title": "Game of Thrones", "year": "2016",
               "summary": "s" * 900, "guid": "plex://episode/x", "genres": ["Drama"],
               "media_info": [{"id": "257925", "container": "mkv", "video_codec": "h264",
                               "video_resolution": "1080", "file": "/media/x.mkv",
                               "parts": [{"id": "274169", "file": "/media/secret.mkv",
                                          "file_size": "3979115377", "duration": "2998290",
                                          "streams": [{"id": "511663"}]}]}]}
    client = make_client(Fake(lambda request: envelope(payload)))
    result = client.metadata(153037)
    assert result["title"] == "The Red Woman" and result["genres"] == ["Drama"]
    assert len(result["summary"]) == 512  # bounded free text
    info = result["media_info"][0]
    assert info["video_resolution"] == "1080" and "file" not in info
    part = info["parts"][0]
    assert part["file_size"] == "3979115377" and "file" not in part and "streams" not in part
    client.close()


def test_metadata_requires_a_positive_rating_key():
    handler = Fake(lambda request: envelope({}))
    client = make_client(handler)
    for bad in (0, -1, "abc", None, True):
        with pytest.raises(ValueError, match="rating_key"):
            client.metadata(bad)
    assert handler.seen == []
    client.close()


# ------------------------------------------------------------------------ libraries

def test_libraries_and_library_media_info_report_never_watched_items():
    def responder(request):
        if cmd_of(request) == "get_libraries":
            return envelope([{"section_id": "2", "section_name": "TV Shows", "section_type": "show",
                              "count": "62", "art": "/:/resources/show-fanart.jpg"}])
        return envelope({"recordsTotal": 82, "recordsFiltered": 82, "data": [
            {"rating_key": "1219", "title": "Game of Thrones", "media_type": "show",
             "play_count": 15, "last_played": 1462380698, "file_size": 253660175293,
             "section_id": 2, "art": "/art/1"}]})

    client = make_client(Fake(responder))
    libraries = client.libraries()
    assert libraries[0]["section_name"] == "TV Shows" and "art" not in libraries[0]
    assert libraries[0]["section_id"] == "2"  # the id the other tools take
    page = client.library_media_info(2, order_column="play_count", order_dir="desc")
    assert page["items"][0]["play_count"] == 15
    assert page["items"][0]["last_played"] == 1462380698
    client.close()


def test_library_media_info_surfaces_never_watched_items():
    """AC-002's explicit case: an item with play_count 0 must survive projection
    with its rating key intact and a null last_played, which is how a caller
    discovers something nobody has ever played."""
    def responder(request):
        return envelope({"recordsTotal": 2, "recordsFiltered": 2, "data": [
            {"rating_key": "1219", "title": "Game of Thrones", "media_type": "show",
             "play_count": 15, "last_played": 1462380698, "section_id": 2},
            {"rating_key": "2222", "title": "Never Played Show", "media_type": "show",
             "play_count": 0, "last_played": None, "section_id": 2}]})

    client = make_client(Fake(responder))
    page = client.library_media_info(2, order_column="play_count", order_dir="asc")
    unwatched = page["items"][1]
    assert unwatched["rating_key"] == "2222"      # the key survives, so it is actionable
    assert unwatched["play_count"] == 0
    assert unwatched["last_played"] is None       # never played, not a fabricated date
    assert page["items"][0]["play_count"] == 15   # the watched item is still present
    assert page["total"] == 2 and page["has_more"] is False
    client.close()


def test_search_returns_rating_keys_to_feed_the_other_tools():
    def responder(request):
        return envelope({"results_count": 1, "results_list": {
            "movie": [{"rating_key": "4348", "title": "Arrival", "media_type": "movie"}]}})

    client = make_client(Fake(responder))
    results = client.search("arrival")["results"]["movie"]
    assert results[0]["rating_key"] == "4348"
    client.close()


def test_library_media_info_asks_for_section_and_orders():
    seen: list[dict] = []

    def responder(request):
        seen.append(dict(request.url.params))
        return envelope({"recordsTotal": 0, "recordsFiltered": 0, "data": []})

    client = make_client(Fake(responder))
    client.library_media_info(7, section_type="show", search="Thrones", limit=5)
    params = seen[0]
    assert params["section_id"] == "7" and params["section_type"] == "show"
    assert params["search"] == "Thrones" and params["length"] == "5"
    client.close()


@pytest.mark.parametrize("kwargs, message", [
    ({"section_id": 0}, "section_id"),
    ({"section_type": "podcast"}, "section_type"),
    ({"order_column": "title"}, "order_column"),
])
def test_library_media_info_rejects_bad_arguments(kwargs, message):
    handler = Fake(lambda request: envelope({}))
    client = make_client(handler)
    args = {"section_id": 2, **kwargs}
    with pytest.raises(ValueError, match=message):
        client.library_media_info(**args)
    assert handler.seen == []
    client.close()


# ---------------------------------------------------------------------------- users

def test_users_and_user_names_are_projected():
    def responder(request):
        if cmd_of(request) == "get_users":
            return envelope([{"user_id": "133788", "username": "LordCommanderSnow",
                              "friendly_name": "Jon Snow", "is_admin": 0, "email": "a@b.c",
                              "thumb": "https://plex.tv/x", "extra": "dropped"}])
        return envelope([{"user_id": 133788, "friendly_name": "Jon Snow"}])

    client = make_client(Fake(responder))
    users = client.users()
    assert users[0]["friendly_name"] == "Jon Snow" and "extra" not in users[0]
    assert client.user_names() == [{"user_id": 133788, "friendly_name": "Jon Snow"}]
    client.close()


def test_item_watch_stats_combines_users_and_time_windows():
    def responder(request):
        if cmd_of(request) == "get_item_user_stats":
            return envelope([{"user_id": 133788, "friendly_name": "Jon Snow",
                              "total_plays": 6, "total_time": 28743}])
        return envelope([{"query_days": 30, "total_plays": 35, "total_time": 63054},
                         {"query_days": 0, "total_plays": 508, "total_time": 1183080}])

    client = make_client(Fake(responder))
    result = client.item_watch_stats(1219)
    assert result["rating_key"] == 1219
    assert result["users"][0]["total_plays"] == 6
    # Nested windows must never be summed: 30 days sits inside all time, so adding
    # them would report 543 for an item that was actually played 508 times.
    assert result["total_plays"] == 508
    client.close()


def test_watch_stats_total_is_null_when_all_time_is_not_requested():
    """Without the all-time row there is no honest single total to report."""
    client = make_client(Fake(lambda request: envelope(
        [{"query_days": 1, "total_plays": 2, "total_time": 10},
         {"query_days": 30, "total_plays": 35, "total_time": 63054}])))
    result = client.item_watch_stats(1219, query_days="1,30")
    assert result["total_plays"] is None
    assert [row["query_days"] for row in result["watch_time"]] == [1, 30]  # rows kept as-is
    client.close()


def test_user_watch_stats_total_uses_only_the_all_time_window():
    def responder(request):
        if cmd_of(request) == "get_user_watch_time_stats":
            return envelope([{"query_days": 7, "total_plays": 3, "total_time": 15694},
                             {"query_days": 0, "total_plays": 508, "total_time": 1183080}])
        return envelope([{"platform": "Chrome", "total_plays": 170, "total_time": 349618}])

    client = make_client(Fake(responder))
    result = client.user_watch_stats(133788)
    assert result["total_plays"] == 508  # not 511
    assert result["players"][0]["platform"] == "Chrome"
    client.close()


def test_user_watch_stats_total_is_null_without_the_all_time_window():
    client = make_client(Fake(lambda request: envelope(
        [{"query_days": 7, "total_plays": 3, "total_time": 15694}])))
    assert client.user_watch_stats(133788, query_days="7,30")["total_plays"] is None
    client.close()


def test_query_days_rejects_repeated_values():
    """A repeated window would double-count in any consumer that summed rows."""
    handler = Fake(lambda request: envelope([]))
    client = make_client(handler)
    with pytest.raises(ValueError, match="repeat"):
        client.item_watch_stats(1219, query_days="7,7")
    with pytest.raises(ValueError, match="repeat"):
        client.user_watch_stats(133788, query_days="0,0")
    assert handler.seen == []
    client.close()


def test_item_watch_stats_sends_the_validated_day_window():
    seen: list[dict] = []

    def responder(request):
        seen.append(dict(request.url.params))
        return envelope([{"query_days": 1, "total_plays": 0, "total_time": 0}])

    client = make_client(Fake(responder))
    client.item_watch_stats(1219, query_days="1,7,30,0")
    # The window belongs to the watch-time call; the user-stats call has no such param.
    assert seen[0]["cmd"] == "get_item_user_stats" and "query_days" not in seen[0]
    assert seen[1]["cmd"] == "get_item_watch_time_stats"
    assert seen[1]["query_days"] == "1,7,30,0"
    assert seen[0]["grouping"] == "0" and seen[1]["grouping"] == "0"
    client.close()


def test_user_watch_stats_reports_windows_and_players():
    def responder(request):
        if cmd_of(request) == "get_user_watch_time_stats":
            return envelope([{"query_days": 0, "total_plays": 508, "total_time": 1183080}])
        return envelope([{"result_id": 1, "platform": "Chrome", "player_name": "Plex Web (Chrome)",
                          "total_plays": 170, "total_time": 349618}])

    client = make_client(Fake(responder))
    result = client.user_watch_stats(133788)
    assert result["user_id"] == 133788 and result["total_plays"] == 508
    assert result["players"][0]["platform"] == "Chrome"
    client.close()


@pytest.mark.parametrize("kwargs, message", [
    ({"query_days": "abc"}, "query_days"),
    ({"query_days": "1,-2"}, "query_days"),
    ({"query_days": ""}, "query_days"),
])
def test_watch_stats_validate_query_days(kwargs, message):
    handler = Fake(lambda request: envelope([]))
    client = make_client(handler)
    with pytest.raises(ValueError, match=message):
        client.item_watch_stats(1219, **kwargs)
    with pytest.raises(ValueError, match=message):
        client.user_watch_stats(133788, **kwargs)
    assert handler.seen == []
    client.close()


# ----------------------------------------------------------------------- home stats

def test_home_stats_projects_rankings_and_validates_choices():
    payload = [{"stat_id": "top_tv", "stat_type": "total_plays",
                "rows": [{"row_id": 1116, "title": "Game of Thrones", "total_plays": 69,
                          "grandparent_thumb": "/library/x.jpg", "labels": ["a"]}]}]
    client = make_client(Fake(lambda request: envelope(payload)))
    result = client.home_stats(stat_id="top_tv", stats_type="duration", time_range=90)
    assert result["stats_type"] == "duration" and result["time_range"] == 90
    row = result["stats"][0]["rows"][0]
    assert row["total_plays"] == 69 and "grandparent_thumb" not in row
    client.close()


@pytest.mark.parametrize("kwargs, message", [
    ({"stat_id": "top_books"}, "stat_id"),
    ({"stats_type": "views"}, "stats_type"),
    ({"time_range": 0}, "time_range"),
    ({"stats_count": 0}, "stats_count"),
    ({"after": "yesterday"}, "after"),
])
def test_home_stats_rejects_bad_arguments(kwargs, message):
    handler = Fake(lambda request: envelope([]))
    client = make_client(handler)
    with pytest.raises(ValueError, match=message):
        client.home_stats(**kwargs)
    assert handler.seen == []
    client.close()


# ---------------------------------------------------------------------------- search

def test_search_groups_results_by_media_type():
    payload = {"results_count": 69, "results_list": {
        "movie": [{"rating_key": "4348", "title": "Arrival", "media_type": "movie", "extra": 1}],
        "episode": [{"rating_key": "153037", "title": "The Red Woman", "media_type": "episode"}]}}
    client = make_client(Fake(lambda request: envelope(payload)))
    result = client.search("game", limit=5)
    assert result["results_count"] == 69
    assert result["results"]["movie"][0]["title"] == "Arrival"
    assert "extra" not in result["results"]["movie"][0]
    assert set(result["results"]) == {"movie", "episode"}
    client.close()


def test_search_requires_a_query():
    handler = Fake(lambda request: envelope({}))
    client = make_client(handler)
    for bad in ("", "   ", None, 5):
        with pytest.raises(ValueError, match="query"):
            client.search(bad)
    assert handler.seen == []
    client.close()


# ----------------------------------------------------------------- free-text bounds

HUGE = "x" * 100_000


def test_free_text_is_bounded_across_every_exposed_record():
    """The 8 MiB transport cap is a whole-body limit, not a per-field bound, so a
    single oversized upstream string must be truncated before it reaches a caller.
    Covers the fields the Boss named: status message, history and last-play titles,
    search titles, user names and nested metadata text."""
    def responder(request):
        cmd = cmd_of(request)
        if cmd == "server_status":
            return envelope({"connected": True, "message": HUGE})
        if cmd == "get_history":
            return envelope({"recordsTotal": 1, "recordsFiltered": 1, "data": [
                {"row_id": 1, "date": 1762000000, "full_title": HUGE, "title": HUGE,
                 "grandparent_title": HUGE, "user": HUGE, "ip_address": HUGE}]})
        if cmd == "get_user_names":
            return envelope([{"user_id": 1, "friendly_name": HUGE}])
        if cmd == "search":
            return envelope({"results_count": 1, "results_list": {
                "movie": [{"rating_key": "1", "title": HUGE, "full_title": HUGE}]}})
        if cmd == "get_libraries":
            return envelope([{"section_id": "2", "section_name": HUGE,
                              "section_type": "show", "count": "62"}])
        return envelope({})

    client = make_client(Fake(responder))
    assert len(client.server_status()["message"]) == 512
    row = client.history()["items"][0]
    for field in ("full_title", "title", "grandparent_title", "user", "ip_address"):
        assert len(row[field]) == 512, field
    assert len(client.user_names()[0]["friendly_name"]) == 512
    assert len(client.search("x")["results"]["movie"][0]["title"]) == 512
    assert len(client.libraries()[0]["section_name"]) == 512
    client.close()


def test_free_text_bounds_apply_to_nested_metadata_text():
    """Metadata nests media_info and parts; an unbounded nested string would slip
    past a top-level-only check."""
    payload = {"rating_key": "153037", "media_type": "episode", "summary": HUGE,
               "tagline": HUGE, "title": HUGE, "studio": HUGE,
               "media_info": [{"id": "257925", "container": HUGE,
                               "parts": [{"id": "274169", "file_size": "123"}]}]}
    client = make_client(Fake(lambda request: envelope(payload)))
    result = client.metadata(153037)
    assert len(result["summary"]) == 512 and len(result["tagline"]) == 512
    assert len(result["title"]) == 512 and len(result["studio"]) == 512
    assert len(result["media_info"][0]["container"]) == 512  # nested, still bounded
    client.close()


def test_free_text_bounds_keep_ids_and_numeric_strings_intact():
    """Tautulli encodes numbers as strings; truncation must never corrupt them."""
    history = {"row_id": 1124, "date": 1762000000, "user_id": 555,
               "percent_complete": 84, "year": "2016", "rating_key": 4348,
               "section_id": 2, "play_duration": 263, "title": "Arrival"}
    media = {"rating_key": "1219", "title": "Game of Thrones", "year": "2011",
             "added_at": "1403553078", "last_played": "1462380698",
             "file_size": 253660175293, "play_count": 15, "section_id": 2}

    def responder(request):
        if cmd_of(request) == "get_history":
            return envelope({"recordsTotal": 1, "recordsFiltered": 1, "data": [history]})
        return envelope({"recordsTotal": 1, "recordsFiltered": 1, "data": [media]})

    client = make_client(Fake(responder))
    row = client.history()["items"][0]
    assert row["year"] == "2016"                  # numeric-looking string preserved
    assert row["section_id"] == 2 and row["percent_complete"] == 84
    assert row["user_id"] == 555 and row["rating_key"] == 4348
    item = client.library_media_info(2)["items"][0]
    assert item["last_played"] == "1462380698"    # a timestamp as a string, intact
    assert item["added_at"] == "1403553078"
    assert item["file_size"] == 253660175293 and item["play_count"] == 15
    client.close()


def test_digit_only_free_text_is_still_bounded():
    """A "looks numeric" exemption is unsafe: float('1'*100000) is inf and
    float('0'*100000) is 0.0, so digit-only titles, messages and names must be
    truncated like any other text. Only known id/timestamp FIELDS are exempt."""
    digits = "1" * 100_000
    zeros = "0" * 100_000

    def responder(request):
        cmd = cmd_of(request)
        if cmd == "server_status":
            return envelope({"result": "success", "message": digits})
        if cmd == "get_history":
            return envelope({"recordsTotal": 1, "recordsFiltered": 1, "data": [
                {"row_id": 1, "date": 1762000000, "title": zeros, "user": digits,
                 "full_title": digits}]})
        if cmd == "search":
            return envelope({"results_count": 1, "results_list": {
                digits: [{"rating_key": "1", "title": digits}]}})
        if cmd == "get_user_names":
            return envelope([{"user_id": 1, "friendly_name": zeros}])
        return envelope({})

    client = make_client(Fake(responder))
    assert len(client.server_status()["message"]) == 512
    row = client.history()["items"][0]
    assert len(row["title"]) == 512 and len(row["user"]) == 512
    assert len(row["full_title"]) == 512
    assert row["date"] == 1762000000          # a real JSON number is untouched
    assert row["row_id"] == 1
    results = client.search("x")["results"]
    assert len(list(results)) == 1 and len(list(results)[0]) == 512   # key bounded
    assert len(results[list(results)[0]][0]["title"]) == 512
    assert len(client.user_names()[0]["friendly_name"]) == 512
    client.close()


def test_digit_only_metadata_text_is_bounded_but_ids_survive():
    digits = "9" * 100_000
    payload = {"rating_key": "153037", "section_id": "2", "year": "2016",
               "summary": digits, "title": digits, "tagline": digits,
               "media_info": [{"id": "257925", "container": digits,
                               "parts": [{"id": "274169", "file_size": "123"}]}]}
    client = make_client(Fake(lambda request: envelope(payload)))
    result = client.metadata(153037)
    assert result["year"] == "2016" and result["section_id"] == "2"
    assert result["rating_key"] == "153037"
    assert len(result["summary"]) == 512 and len(result["title"]) == 512
    assert len(result["tagline"]) == 512
    assert len(result["media_info"][0]["container"]) == 512
    assert result["media_info"][0]["parts"][0]["file_size"] == "123"
    client.close()


def test_home_stats_envelope_fields_do_not_bypass_the_bound():
    """stat_id/stat_type are copied from the envelope, not from a data row."""
    huge = "7" * 100_000
    client = make_client(Fake(lambda request: envelope([
        {"stat_id": huge, "stat_type": huge,
         "rows": [{"row_id": 1, "title": huge, "total_plays": 69}]}])))
    result = client.home_stats()["stats"][0]
    assert len(result["stat_id"]) == 512 and len(result["stat_type"]) == 512
    assert len(result["rows"][0]["title"]) == 512
    assert result["rows"][0]["total_plays"] == 69      # numbers untouched
    client.close()


def test_last_played_titles_are_bounded_too():
    client = make_client(Fake(lambda request: envelope(
        {"recordsTotal": 1, "recordsFiltered": 1, "data": [
            {"row_id": 1, "date": 1762000000, "user": HUGE, "full_title": HUGE,
             "media_type": "movie"}]})))
    result = client.last_played(4348, media_type="movie")
    assert len(result["last_play"]["user"]) == 512
    assert len(result["last_play"]["full_title"]) == 512
    client.close()


# ------------------------------------------------------------------------ read-only

def test_the_client_exposes_no_mutating_command():
    """The whole point of this client: it cannot change the media server."""
    # Only the methods this class defines; inherited transport helpers do not count.
    own = {name for name, value in vars(TautulliClient).items()
           if callable(value) and not name.startswith("_")}
    assert own == {"server_status", "activity", "history", "metadata", "libraries",
                   "library_media_info", "users", "user_names", "item_watch_stats",
                   "user_watch_stats", "home_stats", "search", "last_played"}


def test_no_destructive_or_administrative_commands_are_reachable():
    """Guard against a future edit wiring in a write command."""
    source = (__import__("pathlib").Path(__file__).resolve().parents[1]
              / "jellyseerr_mcp" / "clients" / "tautulli.py").read_text()
    for forbidden in ("terminate_session", "delete_history", "delete_all", "restart",
                      "update_metadata_details", "notify", "edit_library", "delete_user"):
        assert f"'{forbidden}'" not in source and f'"{forbidden}"' not in source


def test_server_status_reports_a_lost_plex_connection_explicitly():
    """An empty or false status must surface as connected=False, never as {}."""
    for data in ({"connected": False}, {}):
        client = make_client(Fake(lambda request, d=data: envelope(d)))
        assert client.server_status()["connected"] is False
        client.close()
