"""Read-only Tautulli client: answer Plex viewing questions from watch history.

Only the documented Tautulli API v2 read commands are implemented. The client has
no mutation path at all (no history deletion, session termination, library edits
or server control), so it never calls require_write() and cannot change the media
server. It exists to answer "does anybody watch this", "when was it last played",
per-user viewing and whole-library questions.

Contracts verified against the official reference (Tautulli-API-Reference.md,
v2.18.2, 2026-09). Every call is ``GET /api/v2?cmd=...`` and answers::

    {"response": {"result": "success"|"error", "message": ..., "data": ...}}

Two upstream quirks drive the code below. Tautulli reports API errors with HTTP
200, so the envelope is checked in the body, not the status code. The API key
travels as the documented ``apikey`` query parameter; the shared transport never
logs requests, refuses to forward credentials across origins and redacts the key
from every returned value.

Free-text fields (summaries, titles, messages) are bounded, and media file paths
plus raw stream/ marker lists are dropped: they disclose server layout without
answering a viewing question.
"""
from __future__ import annotations

from datetime import date

from ..http import ServiceClient, ServiceError, pagination, positive_id, select

SERVER_STATUS_FIELDS = ('result', 'message')

ACTIVITY_FIELDS = ('session_key', 'session_id', 'user', 'user_id', 'friendly_name',
                   'media_type', 'title', 'full_title', 'parent_title', 'grandparent_title',
                   'year', 'rating_key', 'parent_rating_key', 'grandparent_rating_key',
                   'library_name', 'section_id', 'state', 'progress_percent', 'view_offset',
                   'duration', 'bandwidth', 'bitrate', 'container', 'video_decision',
                   'audio_decision', 'transcode_decision', 'stream_video_resolution',
                   'stream_audio_codec', 'stream_subtitle_language', 'platform', 'product',
                   'player', 'device', 'location', 'ip_address', 'ip_address_public',
                   'secure', 'relayed', 'live', 'quality_profile', 'subtitles')

HISTORY_FIELDS = ('row_id', 'reference_id', 'date', 'started', 'stopped', 'user', 'user_id',
                  'friendly_name', 'media_type', 'title', 'full_title', 'parent_title',
                  'grandparent_title', 'year', 'rating_key', 'parent_rating_key',
                  'grandparent_rating_key', 'guid', 'section_id', 'media_index',
                  'parent_media_index', 'platform', 'product', 'player', 'location',
                  'ip_address', 'transcode_decision', 'play_duration', 'paused_counter',
                  'percent_complete', 'watched_status', 'live', 'relayed', 'secure',
                  'session_key', 'group_count', 'group_ids')

METADATA_FIELDS = ('rating_key', 'media_type', 'title', 'full_title', 'parent_title',
                   'grandparent_title', 'year', 'original_title', 'section_id',
                   'library_name', 'summary', 'tagline', 'studio', 'content_rating',
                   'rating', 'audience_rating', 'user_rating', 'duration', 'added_at',
                   'updated_at', 'last_viewed_at', 'originally_available_at', 'guid',
                   'guids', 'grandparent_guids', 'genres', 'directors', 'actors',
                   'writers', 'collections', 'labels', 'live', 'media_index',
                   'parent_media_index', 'parent_rating_key', 'grandparent_rating_key',
                   'thumb', 'art', 'banner')

# Sizes and codecs answer "what copy is this"; the `file` path does not, so only
# part metadata is kept from media_info and the raw streams list is dropped.
MEDIA_INFO_FIELDS = ('id', 'container', 'bitrate', 'duration', 'width', 'height',
                     'aspect_ratio', 'video_codec', 'video_profile', 'video_resolution',
                     'video_full_resolution', 'video_framerate', 'video_dynamic_range',
                     'audio_codec', 'audio_channels', 'audio_channel_layout',
                     'optimized_version')

LIBRARY_FIELDS = ('section_id', 'section_name', 'section_type', 'count', 'child_count',
                  'parent_count', 'is_active')

LIBRARY_MEDIA_INFO_FIELDS = ('rating_key', 'title', 'sort_title', 'year', 'media_type',
                             'section_id', 'section_type', 'media_index',
                             'parent_media_index', 'parent_rating_key',
                             'grandparent_rating_key', 'added_at', 'last_played',
                             'play_count', 'file_size', 'container', 'bitrate',
                             'video_codec', 'video_resolution', 'video_framerate',
                             'audio_codec', 'audio_channels', 'guid', 'thumb')

USER_FIELDS = ('user_id', 'username', 'friendly_name', 'email', 'is_active', 'is_admin',
               'is_home_user', 'is_restricted', 'is_allow_sync', 'keep_history',
               'shared_libraries', 'thumb', 'row_id')

USER_NAME_FIELDS = ('user_id', 'friendly_name', 'username')

ITEM_USER_STATS_FIELDS = ('user_id', 'username', 'friendly_name', 'user_thumb',
                          'total_plays', 'total_time')

WATCH_TIME_FIELDS = ('query_days', 'total_plays', 'total_time')

PLAYER_STATS_FIELDS = ('result_id', 'platform', 'platform_name', 'player_name',
                       'total_plays', 'total_time')

HOME_STATS_ROW_FIELDS = ('row_id', 'title', 'full_title', 'parent_title',
                         'grandparent_title', 'media_type', 'rating_key', 'section_id',
                         'user', 'friendly_name', 'users_watched', 'platform',
                         'total_plays', 'total_duration', 'last_play', 'content_rating',
                         'guid', 'year', 'rating', 'live', 'labels', 'thumb')

SEARCH_FIELDS = ('rating_key', 'title', 'full_title', 'parent_title', 'grandparent_title',
                 'media_type', 'media_index', 'parent_media_index', 'year', 'section_id',
                 'library_name', 'guid', 'thumb')

HISTORY_MEDIA_TYPES = ('movie', 'episode', 'track', 'live', 'collection', 'playlist')
HISTORY_ORDER_DIRECTIONS = ('desc', 'asc')
HISTORY_ORDER_COLUMNS = ('date', 'friendly_name', 'ip_address', 'platform', 'player',
                         'full_title', 'started', 'paused_counter', 'stopped', 'duration')
TRANSCODE_DECISIONS = ('direct play', 'copy', 'transcode')
# Which history column holds the item a user actually played. Tautulli records a play
# against the concrete file, so a show is only reachable through an episode's
# grandparent key and a season through its parent key. Asking with the wrong column
# returns zero rows, which would read as "never watched" instead of an error.
LAST_PLAY_SCOPES = {'movie': 'rating_key', 'episode': 'rating_key',
                    'season': 'parent_rating_key', 'show': 'grandparent_rating_key'}
HOME_STAT_IDS = ('top_movies', 'popular_movies', 'top_tv', 'popular_tv', 'top_music',
                 'popular_music', 'top_libraries', 'top_users', 'top_platforms',
                 'last_watched', 'most_concurrent')
HOME_STATS_TYPES = ('plays', 'duration')
DEFAULT_QUERY_DAYS = '1,7,30,0'
MAX_TEXT = 512


def _bound_text(value: object, limit: int = MAX_TEXT) -> object:
    """Recursively cap free text anywhere inside an upstream record.

    Bounding is by value type, never by how a string looks: a digit-only string is
    still text and still has to be capped, because "1" * 100000 parses as infinity
    and "0" * 100000 parses as zero, so any "looks numeric" exemption lets both
    through. Numbers that Tautulli sends as real JSON numbers stay untouched, and
    its string-encoded ids and timestamps are preserved separately by
    ``NUMERIC_FIELDS`` in ``_project``. Lists and dicts are walked because the
    transport's 8 MiB cap is a whole-body limit, not a per-field bound.
    """
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_bound_text(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _bound_text(item, limit) for key, item in value.items()}
    return value


# Fields whose string form is an identifier, timestamp or measurement rather than
# prose. Tautulli returns these as strings ("year": "2016"), so they are exempt from
# the text bound: truncating one would corrupt an id, a play date or a file size.
# The exemption is keyed on the field name, never on the value, so a title or status
# message that merely happens to be digits is still bounded. Everything not listed
# here is treated as free text.
NUMERIC_FIELDS = frozenset({
    'row_id', 'reference_id', 'rating_key', 'parent_rating_key', 'grandparent_rating_key',
    'user_id', 'section_id', 'media_index', 'parent_media_index', 'id', 'duration',
    'play_duration', 'total_time', 'total_duration', 'total_plays', 'group_count',
    'group_ids', 'file_size', 'size', 'bitrate', 'stream_bitrate', 'bandwidth',
    'percent_complete', 'paused_counter', 'query_days', 'stats_count', 'stats_start',
    'time_range', 'count', 'child_count', 'parent_count', 'last_played', 'added_at',
    'updated_at', 'last_viewed_at', 'last_play', 'originally_available_at', 'date',
    'started', 'stopped', 'year', 'secure', 'relayed', 'live', 'watched_status',
    'is_active', 'is_admin', 'is_home_user', 'is_restricted', 'is_allow_sync',
    'keep_history', 'allow_guest', 'deleted_user', 'duration_ms', 'progress_percent',
})

# Envelope-level fields copied straight from the response rather than read from a
# data record, so they need the bound applied explicitly.
ENVELOPE_TEXT_FIELDS = ('result', 'message')


def _project(record: dict, fields: tuple | list) -> dict:
    """``select`` plus the free-text bound applied by field meaning.

    Strings under a ``NUMERIC_FIELDS`` key are ids/timestamps and pass through
    unchanged; every other string, including nested ones, is capped.
    """
    return {key: value if key in NUMERIC_FIELDS else _bound_text(value)
            for key, value in select(record, fields).items()}


def _day(value: object, name: str) -> str:
    """Tautulli history/stats date filters expect an exact YYYY-MM-DD string."""
    text = value.strip() if isinstance(value, str) else ''
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ValueError(f'{name} must be an ISO date (YYYY-MM-DD)') from None
    return text


def _flag(value: object, name: str) -> int:
    """Tautulli models booleans as 0/1 integers, never JSON booleans."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ValueError(f'{name} must be 0, 1 or a boolean')


def _choice(value: object, name: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value.lower() not in allowed:
        raise ValueError(f'{name} must be one of: {", ".join(allowed)}')
    return value.lower()


def _text(value: object, name: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a non-empty string')
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f'{name} must be at most {limit} characters')
    return text


def _count(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer between {minimum} and {maximum}')
    return value


def _query_days(value: object) -> str:
    """Validate Tautulli's comma-separated day list, e.g. "1,7,30,0" (0 = all time)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('query_days must be a comma-separated list such as "1,7,30,0"')
    parts = [part.strip() for part in value.split(',')]
    if not 1 <= len(parts) <= 10:
        raise ValueError('query_days accepts between 1 and 10 values')
    days = []
    for part in parts:
        try:
            number = int(part)
        except ValueError:
            raise ValueError('query_days must be a comma-separated list of numbers') from None
        if number < 0:
            raise ValueError('query_days values must not be negative')
        days.append(str(number))
    if len(set(days)) != len(days):
        raise ValueError('query_days must not repeat a day value')
    return ','.join(days)


def _all_time_plays(watch_time: list) -> int | None:
    """Plays over all time, or None when the caller excluded the all-time window.

    Tautulli's windows nest (1 day is inside 7, which is inside 30, and 0 means
    all time), so adding them together double-counts. Only the all-time row is a
    well-defined total; when ``query_days`` omits 0 there is no honest single
    number to report, so the caller gets None and must present per-window values.
    """
    for entry in watch_time:
        if entry.get('query_days') == 0:
            return entry.get('total_plays') or 0
    return None


def _unwrap(payload: object, service: str):
    """Return the envelope's data, or fail on an API error reported inside HTTP 200."""
    if not isinstance(payload, dict) or not isinstance(payload.get('response'), dict):
        raise ServiceError(f'{service}: unexpected Tautulli response')
    response = payload['response']
    if str(response.get('result', '')).lower() != 'success':
        # Classify a bounded cause instead of echoing the upstream message.
        message = str(response.get('message') or '').lower()
        if 'apikey' in message or 'api key' in message:
            raise ServiceError(f'{service}: Tautulli rejected the API key')
        raise ServiceError(f'{service}: Tautulli API request failed')
    return response.get('data')


def _native_page(response: object, items: list, offset: int, limit: int) -> dict:
    """Tautulli's history/media-info tables page with start/length natively."""
    total = response.get('recordsTotal') if isinstance(response, dict) else None
    filtered = response.get('recordsFiltered') if isinstance(response, dict) else None
    if not isinstance(total, int):
        total = len(items)
    if not isinstance(filtered, int):
        filtered = total
    return {'items': items, 'offset': offset, 'limit': limit, 'total': total,
            'filtered': filtered, 'has_more': offset + len(items) < filtered,
            'pagination': 'native'}


class TautulliClient(ServiceClient):
    """Tautulli API v2, read commands only (apikey query parameter)."""

    def _fetch(self, cmd: str, **params: object):
        query: dict = {'apikey': self.config.api_key, 'cmd': cmd, 'out_type': 'json'}
        query.update({key: value for key, value in params.items() if value is not None})
        return _unwrap(self.request_json('GET', 'api/v2', params=query), self.config.name)

    def _rows(self, cmd: str, **params: object) -> list:
        data = self._fetch(cmd, **params)
        if data is None:
            return []
        return data if isinstance(data, list) else [data]

    def server_status(self) -> dict:
        """Tautulli's connection health; the safest first call to verify setup."""
        data = self._fetch('server_status')
        return _project(data, SERVER_STATUS_FIELDS) if isinstance(data, dict) else {}

    def activity(self, session_key: int | str | None = None,
                 session_id: str | None = None) -> dict:
        """Current Plex streams. Optionally narrow to one session.

        This is live state, not history: an empty session list means nobody is
        watching right now, which is different from an item never being watched.
        """
        params: dict = {}
        if session_key is not None:
            params['session_key'] = positive_id(session_key, 'session_key')
        if session_id is not None:
            params['session_id'] = _text(session_id, 'session_id')
        data = self._fetch('get_activity', **params)
        sessions = []
        if isinstance(data, dict):
            for session in data.get('sessions') or []:
                sessions.append(_project(session, ACTIVITY_FIELDS))
        return {'stream_count': len(sessions), 'sessions': sessions}

    def history(self, user: str | None = None, user_id: int | str | None = None,
                rating_key: int | str | None = None, parent_rating_key: int | str | None = None,
                grandparent_rating_key: int | str | None = None, section_id: int | str | None = None,
                media_type: str | None = None, transcode_decision: str | None = None,
                search: str | None = None, start_date: str | None = None,
                before: str | None = None, after: str | None = None,
                order_column: str = 'date', order_dir: str = 'desc',
                grouping: bool = False, offset: int = 0, limit: int = 25) -> dict:
        """Watch history, newest first by default; a row is one play.

        This answers "who watched this and when". Narrow by user, item
        (``rating_key`` for an episode/movie, ``grandparent_rating_key`` for the
        show) or library, or by ``search`` across titles, and by date with
        ``start_date``/``before``/``after`` (YYYY-MM-DD).
        """
        pagination(offset, limit)
        params: dict = {'order_column': _choice(order_column, 'order_column', HISTORY_ORDER_COLUMNS),
                        'order_dir': _choice(order_dir, 'order_dir', HISTORY_ORDER_DIRECTIONS),
                        'grouping': _flag(grouping, 'grouping'), 'start': offset, 'length': limit}
        if user is not None:
            params['user'] = _text(user, 'user')
        if user_id is not None:
            params['user_id'] = positive_id(user_id, 'user_id')
        for value, wire in ((rating_key, 'rating_key'), (parent_rating_key, 'parent_rating_key'),
                            (grandparent_rating_key, 'grandparent_rating_key'),
                            (section_id, 'section_id')):
            if value is not None:
                params[wire] = positive_id(value, wire)
        if media_type is not None:
            params['media_type'] = _choice(media_type, 'media_type', HISTORY_MEDIA_TYPES)
        if transcode_decision is not None:
            params['transcode_decision'] = _choice(transcode_decision, 'transcode_decision',
                                                   TRANSCODE_DECISIONS)
        if search is not None:
            params['search'] = _text(search, 'search')
        for value, wire in ((start_date, 'start_date'), (before, 'before'), (after, 'after')):
            if value is not None:
                params[wire] = _day(value, wire)
        response = self._fetch('get_history', **params)
        records = response.get('data') if isinstance(response, dict) else None
        items = [_project(record, HISTORY_FIELDS) for record in records or []]
        return _native_page(response, items, offset, limit)

    def last_played(self, rating_key: int | str, media_type: str = 'movie',
                    user: str | None = None, user_id: int | str | None = None) -> dict:
        """The most recent play of one item, with its timestamp.

        This is the "when was it last played, and by whom" lookup. ``media_type``
        selects the history column that actually matches, because Tautulli records
        each play against the concrete file: a movie or episode is its own
        ``rating_key``, a show has to be matched through the episode's
        ``grandparent_rating_key`` and a season through ``parent_rating_key``. Using
        the wrong column silently returns nothing, so the column is chosen here
        rather than left to the caller. ``played`` is False with a null
        ``last_played`` when the item has no recorded plays: a normal answer, not
        an error.
        """
        rating_key = positive_id(rating_key, 'rating_key')
        scope = LAST_PLAY_SCOPES.get(media_type)
        if scope is None:
            raise ValueError(f'media_type must be one of: {", ".join(LAST_PLAY_SCOPES)}')
        params: dict = {scope: rating_key, 'order_column': 'date', 'order_dir': 'desc',
                        'start': 0, 'length': 1, 'grouping': 0}
        if user is not None:
            params['user'] = _text(user, 'user')
        if user_id is not None:
            params['user_id'] = positive_id(user_id, 'user_id')
        response = self._fetch('get_history', **params)
        records = response.get('data') if isinstance(response, dict) else None
        matched = [_project(record, HISTORY_FIELDS) for record in (records or [])[:1]]
        if not matched:
            return {'rating_key': rating_key, 'media_type': media_type,
                    'matched_by': scope, 'played': False, 'last_played': None,
                    'last_play': None}
        entry = matched[0]
        return {'rating_key': rating_key, 'media_type': media_type, 'matched_by': scope,
                'played': True, 'last_played': entry.get('date'),
                'last_play': entry}

    def metadata(self, rating_key: int | str) -> dict:
        """Metadata for one item by Plex rating key.

        Use this to turn a name from the user into a rating key before asking
        about its plays. Media file paths and raw stream lists are omitted.
        """
        rating_key = positive_id(rating_key, 'rating_key')
        data = self._fetch('get_metadata', rating_key=rating_key)
        if not isinstance(data, dict):
            return {}
        projected = _project(data, METADATA_FIELDS)
        projected['media_info'] = [
            {**_project(entry, MEDIA_INFO_FIELDS),
             'parts': [_project(part, ('id', 'file_size', 'duration'))
                       for part in entry.get('parts') or []]}
            for entry in data.get('media_info') or []
        ]
        # Free text is already bounded by _project for this record and its nested
        # media_info, so no per-field pass is needed here.
        return projected

    def libraries(self) -> list[dict]:
        """Plex libraries with their item counts; section_id feeds the other tools."""
        return [_project(item, LIBRARY_FIELDS) for item in self._rows('get_libraries')]

    def library_media_info(self, section_id: int | str, section_type: str | None = None,
                           search: str | None = None, order_column: str = 'sort_title',
                           order_dir: str = 'asc', offset: int = 0, limit: int = 25) -> dict:
        """Items in one library with play counts and last-played dates.

        This is the whole-library view: it shows what is never watched
        (``play_count`` 0) as well as what is, without needing a rating key.
        """
        section_id = positive_id(section_id, 'section_id')
        pagination(offset, limit)
        params: dict = {'section_id': section_id,
                        'order_column': _choice(order_column, 'order_column',
                                                ('added_at', 'sort_title', 'container', 'bitrate',
                                                 'video_codec', 'video_resolution',
                                                 'video_framerate', 'audio_codec',
                                                 'audio_channels', 'file_size',
                                                 'last_played', 'play_count')),
                        'order_dir': _choice(order_dir, 'order_dir', HISTORY_ORDER_DIRECTIONS),
                        'start': offset, 'length': limit}
        if section_type is not None:
            params['section_type'] = _choice(section_type, 'section_type',
                                             ('movie', 'show', 'artist', 'photo'))
        if search is not None:
            params['search'] = _text(search, 'search')
        response = self._fetch('get_library_media_info', **params)
        records = response.get('data') if isinstance(response, dict) else None
        items = [_project(record, LIBRARY_MEDIA_INFO_FIELDS) for record in records or []]
        return _native_page(response, items, offset, limit)

    def users(self) -> list[dict]:
        """Everyone with access to the server: ids, names and share permissions."""
        return [_project(item, USER_FIELDS) for item in self._rows('get_users')]

    def user_names(self) -> list[dict]:
        """Light id-to-name lookup when only the ``user_id`` is needed."""
        return [_project(item, USER_NAME_FIELDS) for item in self._rows('get_user_names')]

    def item_watch_stats(self, rating_key: int | str, media_type: str | None = None,
                         grouping: bool = False,
                         query_days: str = DEFAULT_QUERY_DAYS) -> dict:
        """Who watched one item, and how much: per-user plays plus time windows.

        This answers "has anyone watched this" and "who watched it", not when it
        was last played: use ``history`` with ``limit=1`` for the most recent play
        timestamp. ``total_time`` is seconds. ``total_plays`` is defined only for
        the all-time window (``query_days`` 0); it is None when the requested
        windows omit it, because nested windows cannot be summed.
        """
        rating_key = positive_id(rating_key, 'rating_key')
        window = _query_days(query_days)
        params: dict = {'rating_key': rating_key, 'grouping': _flag(grouping, 'grouping')}
        if media_type is not None:
            params['media_type'] = _choice(media_type, 'media_type',
                                           ('movie', 'show', 'season', 'episode', 'artist',
                                            'album', 'track', 'collection'))
        users = [_project(entry, ITEM_USER_STATS_FIELDS)
                 for entry in self._rows('get_item_user_stats', **params)]
        watch_time = [_project(entry, WATCH_TIME_FIELDS)
                      for entry in self._rows('get_item_watch_time_stats',
                                              **params, query_days=window)]
        return {'rating_key': rating_key, 'users': users, 'watch_time': watch_time,
                'total_plays': _all_time_plays(watch_time)}

    def user_watch_stats(self, user_id: int | str, grouping: bool = False,
                         query_days: str = DEFAULT_QUERY_DAYS) -> dict:
        """One user's viewing totals per time window and per player.

        ``total_plays`` is the all-time total, or None when ``query_days`` excludes
        the all-time window (0); the per-window rows in ``watch_time`` are always
        returned as-is.
        """
        user_id = positive_id(user_id, 'user_id')
        window = _query_days(query_days)
        params: dict = {'user_id': user_id, 'grouping': _flag(grouping, 'grouping')}
        watch_time = [_project(entry, WATCH_TIME_FIELDS)
                      for entry in self._rows('get_user_watch_time_stats',
                                              **params, query_days=window)]
        players = [_project(entry, PLAYER_STATS_FIELDS)
                   for entry in self._rows('get_user_player_stats', **params)]
        return {'user_id': user_id, 'watch_time': watch_time, 'players': players,
                'total_plays': _all_time_plays(watch_time)}

    def home_stats(self, stat_id: str | None = None, stats_type: str = 'plays',
                   time_range: int = 30, section_id: int | str | None = None,
                   user_id: int | str | None = None, before: str | None = None,
                   after: str | None = None, stats_count: int = 5,
                   stats_start: int = 0, grouping: bool = False) -> dict:
        """Homepage-style rankings: top/popular titles, users, platforms, last watched.

        ``stats_type='duration'`` ranks by watch time instead of play count.
        ``stat_id`` narrows the reply to a single ranking.
        """
        params: dict = {'stats_type': _choice(stats_type, 'stats_type', HOME_STATS_TYPES),
                        'time_range': _count(time_range, 'time_range', 1, 3650),
                        'stats_count': _count(stats_count, 'stats_count', 1, 100),
                        'stats_start': _count(stats_start, 'stats_start', 0, 100_000),
                        'grouping': _flag(grouping, 'grouping')}
        if stat_id is not None:
            params['stat_id'] = _choice(stat_id, 'stat_id', HOME_STAT_IDS)
        if section_id is not None:
            params['section_id'] = positive_id(section_id, 'section_id')
        if user_id is not None:
            params['user_id'] = positive_id(user_id, 'user_id')
        for value, wire in ((before, 'before'), (after, 'after')):
            if value is not None:
                params[wire] = _day(value, wire)
        data = self._fetch('get_home_stats', **params)
        stats = []
        for entry in data if isinstance(data, list) else []:
            if not isinstance(entry, dict):
                continue
            # stat_id/stat_type come from the envelope rather than a data row, so
            # they are bounded explicitly (the bound, not a value check, also keeps
            # an oversized upstream string out of the reply).
            stats.append({'stat_id': _bound_text(entry.get('stat_id')),
                          'stat_type': _bound_text(entry.get('stat_type')),
                          'rows': [_project(row, HOME_STATS_ROW_FIELDS)
                                   for row in entry.get('rows') or []]})
        return {'time_range': params['time_range'], 'stats_type': params['stats_type'],
                'stats': stats}

    def search(self, query: str, limit: int | None = None) -> dict:
        """Search the Plex library for a title to find its rating key.

        Tautulli caps each media type separately, so the reply is grouped by type
        rather than ranked into one list.
        """
        query = _text(query, 'query')
        params: dict = {'query': query}
        if limit is not None:
            params['limit'] = _count(limit, 'limit', 1, 100)
        data = self._fetch('search', **params)
        if not isinstance(data, dict):
            return {'query': query, 'results_count': 0, 'results': {}}
        raw = data.get('results_list')
        results: dict = {}
        if isinstance(raw, dict):
            for media_type, entries in raw.items():
                if isinstance(entries, dict):
                    entries = [entries]
                if isinstance(entries, list):
                    # The upstream media-type name becomes a dict key, so it needs
                    # the same bound as any other upstream text.
                    key = str(_bound_text(str(media_type)))
                    results[key] = [_project(entry, SEARCH_FIELDS)
                                    for entry in entries if isinstance(entry, dict)]
        count = data.get('results_count')
        return {'query': query, 'results_count': count if isinstance(count, int) else 0,
                'results': results}
