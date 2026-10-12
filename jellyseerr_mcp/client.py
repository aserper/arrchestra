"""Backward-compatible Jellyseerr/Seerr tools with explicit mutation policy."""
from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

import httpx

from .config import AppConfig, ServiceConfig
from .http import ServiceClient, ServiceError, non_negative_id, positive_id, select

# Seerr accepts a wider take than this; the cap keeps one tool call from
# pulling an unbounded slice of the request log into the model's context.
MAX_REQUEST_PAGE = 100

# Enum values verified against a live instance (2026-10-12): anything outside
# these sets answers 400, so they are rejected locally instead of round-tripped.
REQUEST_FILTERS = ('all', 'approved', 'available', 'pending', 'processing',
                   'unavailable', 'failed', 'deleted', 'completed')
REQUEST_SORTS = ('added', 'modified')
REQUEST_MEDIA_TYPES = ('movie', 'tv')

REQUEST_FIELDS = ('id', 'status', 'type', 'is4k', 'createdAt', 'updatedAt',
                  'seasonCount', 'profileName', 'rootFolder', 'serverId', 'profileId')
REQUESTER_FIELDS = ('id', 'displayName', 'email', 'plexUsername', 'userType')
SEASON_FIELDS = ('seasonNumber', 'status')
# Seerr's own status enums, from its published OpenAPI document. Invalid values
# are rejected upstream, so they are rejected here without a round trip.
REQUEST_APPROVAL_STATUSES = ('approve', 'decline')
MEDIA_STATUSES = ('available', 'partial', 'processing', 'pending', 'unknown', 'deleted')
ApprovalStatus = Literal['approve', 'decline']
REQUEST_MEDIA_FIELDS = ('id', 'mediaType', 'tmdbId', 'tvdbId', 'status', 'status4k',
                        'serviceId', 'serviceId4k', 'externalServiceId',
                        'externalServiceId4k', 'externalServiceSlug',
                        'externalServiceSlug4k', 'ratingKey', 'mediaUrl', 'serviceUrl',
                        'createdAt', 'mediaAddedAt')


def _bounded_text(value: Any, limit: int = 500) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _request_item(record: Any) -> dict:
    """Project one Seerr request record; never pass an upstream body through.

    The requester identity is the point of this projection, so it is kept
    explicit: ``requested_by`` (who asked) and ``modified_by`` (who last acted,
    usually the approving admin) are different fields and routinely differ.

    ``media.externalServiceId`` is the Sonarr/Radarr instance for the normal
    quality, ``externalServiceId4k`` a separate one — they are not the same
    record, so both are carried through.
    """
    if not isinstance(record, dict):
        return {}
    item = select(record, REQUEST_FIELDS)
    media = record.get('media')
    if isinstance(media, dict):
        item['media'] = select(media, REQUEST_MEDIA_FIELDS)
    seasons = [select(s, SEASON_FIELDS) for s in (record.get('seasons') or [])
               if isinstance(s, dict)]
    item['seasons'] = seasons
    item['season_numbers'] = [s['seasonNumber'] for s in seasons if 'seasonNumber' in s]
    for key, field in (('requested_by', 'requestedBy'), ('modified_by', 'modifiedBy')):
        who = record.get(field)
        if isinstance(who, dict):
            item[key] = select(who, REQUESTER_FIELDS)
    return item


class JellyseerrClient(ServiceClient):
    def __init__(self, config: AppConfig | ServiceConfig, transport: httpx.BaseTransport | None = None):
        if isinstance(config, AppConfig):
            config = config.service_configs()["jellyseerr"]
        super().__init__(config, transport=transport)

    def request(self, method: str, endpoint: str, *, params: dict | None = None,
                json: dict | None = None) -> Any:
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "DELETE"}:
            raise ValueError("Unsupported service method")
        return self.request_json(method, "api/v1/" + endpoint.lstrip("/"),
                                 params=params, json=json, mutation=method != "GET")

    def search_media(self, query: str) -> dict:
        if not query.strip() or len(query) > 500:
            raise ValueError("query must contain 1 to 500 characters")
        # Preserve %20: Seerr rejects httpx's conventional '+' encoding.
        return self.request("GET", "search?query=" + quote(query, safe=""))

    def request_media(self, media_id: int, media_type: str, is_4k: bool = False,
                      seasons: list[int] | None = None) -> dict:
        self.require_write()
        media_id = positive_id(media_id, "media_id")
        if media_type not in {"movie", "tv"}:
            raise ValueError("media_type must be movie or tv")
        if seasons is not None and (media_type != "tv" or not seasons or len(seasons) > 100
                or any(isinstance(s, bool) or not isinstance(s, int) or s < 0 for s in seasons)):
            raise ValueError("seasons must be a nonempty list of TV season numbers")
        service_type = "radarr" if media_type == "movie" else "sonarr"
        services = self.request("GET", "service/" + service_type)
        if isinstance(services, dict):
            services = [services]
        matches = [s for s in (services or []) if bool(s.get("is4k", False)) == is_4k]
        default = [s for s in matches if s.get("isDefault")]
        if len(default) == 1:
            service = default[0]
        elif len(matches) == 1:
            service = matches[0]
        else:
            raise ValueError(f"Configure one default {service_type} service for this quality in Seerr")
        profile = service.get("activeProfileId")
        if profile is None or not service.get("activeDirectory"):
            raise ValueError("Seerr service needs an active quality profile and root directory")
        payload = {"mediaId": media_id, "mediaType": media_type, "is4k": is_4k,
                   "serverId": non_negative_id(service.get("id"), "server_id"),
                   "profileId": profile, "rootFolder": service["activeDirectory"]}
        if media_type == "tv":
            # Legacy default retained. Explicit empty seasons is not silently rewritten.
            payload["seasons"] = sorted(set(seasons)) if seasons is not None else [1]
            language = service.get("activeLanguageProfileId")
            if language is not None:
                payload["languageProfileId"] = language
        return self.request("POST", "request", json=payload)

    def get_request(self, request_id: int) -> dict:
        return self.request("GET", f"request/{positive_id(request_id, 'request_id')}")

    def _requests_query(self, page_number: object, limit: object, filter: object,
                        sort: object, requested_by: object,
                        media_type: object) -> tuple[dict, int, int]:
        """Validate every bounded input before the single outbound read.

        Seerr ignores an unsupported query parameter instead of rejecting it
        (``mediaId=`` returns the whole log) and silently accepts an unknown
        ``requestedBy``, so an unvalidated filter would look like it worked
        while returning everyone's requests. Everything is checked here because
        none of it can be caught from the response.
        """
        page_number, limit = self._request_paging(page_number, limit)
        if filter not in REQUEST_FILTERS:
            raise ValueError("filter must be one of: " + ", ".join(REQUEST_FILTERS))
        if sort not in REQUEST_SORTS:
            raise ValueError("sort must be one of: " + ", ".join(REQUEST_SORTS))
        params = {'take': limit, 'skip': (page_number - 1) * limit,
                  'filter': filter, 'sort': sort}
        if requested_by is not None:
            params['requestedBy'] = positive_id(requested_by, 'requested_by')
        if media_type is not None:
            if media_type not in REQUEST_MEDIA_TYPES:
                raise ValueError("media_type must be one of: " + ", ".join(REQUEST_MEDIA_TYPES))
            params['mediaType'] = media_type
        return params, page_number, limit

    def _request_paging(self, page_number: object, limit: object) -> tuple[int, int]:
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            raise ValueError("page_number must be a positive integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_REQUEST_PAGE:
            raise ValueError(f"limit must be between 1 and {MAX_REQUEST_PAGE}")
        return page_number, limit

    def list_requests(self, page_number: int = 1, limit: int = 25,
                      filter: str = 'all', sort: str = 'added',
                      requested_by: int | None = None,
                      media_type: str | None = None) -> dict:
        """Paged Seerr request log with requester attribution.

        Seerr's own ``pageInfo`` is echoed rather than recomputed: an unknown
        ``requestedBy`` returns a 200 with zero results, so the upstream page
        count is the only trustworthy total.
        """
        params, page_number, limit = self._requests_query(page_number, limit, filter,
                                                          sort, requested_by, media_type)
        response = self.request("GET", "request", params=params)
        if not isinstance(response, dict):
            raise ValueError("Seerr returned an unexpected request list")
        items = [_request_item(r) for r in (response.get('results') or [])]
        info = response.get('pageInfo')
        reported = info.get('results') if isinstance(info, dict) else None
        total = reported if isinstance(reported, int) and not isinstance(reported, bool) \
            else len(items)
        offset = (page_number - 1) * limit
        return {'items': items, 'offset': offset, 'limit': limit, 'total': total,
                'has_more': offset + len(items) < total, 'pagination': 'native'}

    def get_media_requests(self, tmdb_id: int, media_type: str = 'tv') -> dict:
        """Every Seerr request for one title, keyed by TMDB id.

        ``/api/v1/media`` deliberately omits ``requests``, and ``request``
        ignores a ``mediaId`` filter, so the title's own detail endpoint is the
        only route to per-title request history. A title absent from Seerr
        answers 500 (not 404), which is reported as "never requested" rather
        than raised as a fault.
        """
        tmdb_id = positive_id(tmdb_id, 'tmdb_id')
        if media_type not in REQUEST_MEDIA_TYPES:
            raise ValueError("media_type must be one of: " + ", ".join(REQUEST_MEDIA_TYPES))
        try:
            detail = self.request("GET", f"{media_type}/{tmdb_id}")
        except ServiceError:
            detail = None
        if not isinstance(detail, dict):
            return {'tmdb_id': tmdb_id, 'media_type': media_type, 'title': None,
                    'in_seerr': False, 'requests': [], 'total': 0}
        info = detail.get('mediaInfo')
        info = info if isinstance(info, dict) else {}
        requests = [_request_item(r) for r in (info.get('requests') or [])]
        requests.sort(key=lambda r: r.get('createdAt') or '')
        title = detail.get('name') if media_type == 'tv' else detail.get('title')
        return {'tmdb_id': tmdb_id, 'media_type': media_type,
                'title': _bounded_text(title) if title else None,
                'in_seerr': bool(info.get('id')),
                'media': select(info, REQUEST_MEDIA_FIELDS),
                'seasons': [select(s, ('seasonNumber', 'status'))
                            for s in (info.get('seasons') or []) if isinstance(s, dict)],
                'requests': requests, 'total': len(requests)}

    def remove_request(self, request_id: int) -> dict:
        """Remove one Seerr request, without touching the title it asked for.

        Deleting a request only clears the log entry. The ``media`` record it
        pointed at survives, so a later request for the same title is accepted
        again instead of being rejected as a duplicate. ``remove_media`` is the
        operation that also deletes the title and its file.

        A request Seerr has already forgotten answers 404, which is reported as
        ``removed: false`` rather than raised.
        """
        request_id = positive_id(request_id, 'request_id')
        try:
            self.request("DELETE", f"request/{request_id}")
        except ServiceError as exc:
            # The mutation carries no read-back of its own, so a 404 is checked
            # from a fresh read rather than assumed from the error text.
            if self._request_exists(request_id):
                raise
            return {'request_id': request_id, 'removed': False, 'reason': 'not_found'}
        return {'request_id': request_id, 'removed': True}

    def remove_media(self, media_id: int) -> dict:
        """Delete one Seerr media item, discarding its availability status.

        This removes the title from Seerr's tracking, its request history and
        its availability state; it does NOT delete the file on disk or remove
        the title from Sonarr/Radarr. A media id is NOT a TMDB id and NOT a
        request id; both are valid-looking numbers that address other records.

        Seerr answers 204 for ANY media id, present or not, so the response
        cannot confirm a record existed. The result therefore reports
        ``removed`` (the delete was accepted) separately from ``verified``
        (a record is known to have existed), and never claims a file was
        touched.
        """
        media_id = positive_id(media_id, 'media_id')
        self.request("DELETE", f"media/{media_id}")
        return {'media_id': media_id, 'removed': True, 'verified': False,
                'note': 'Seerr tracking only; files and the Sonarr/Radarr entry are '
                        'untouched. Seerr accepts any id here, so removal is not verifiable.'}

    def retry_request(self, request_id: int) -> dict:
        """Re-run the arr push for a request that never reached Sonarr/Radarr.

        This is the supported repair for a request stuck with a null
        ``externalServiceId``: Seerr re-attempts the add instead of creating a
        duplicate request. It does not search for or download anything.
        """
        request_id = positive_id(request_id, 'request_id')
        result = self.request("POST", f"request/{request_id}/retry")
        payload = result if isinstance(result, dict) else {}
        item = _request_item(payload) or {'id': request_id}
        media = item.get('media') or {}
        return {'request_id': request_id, 'retried': True,
                'request': item,
                'external_service_id': media.get('externalServiceId'),
                'pushed_to_arr': bool(media.get('externalServiceId'))}

    def set_request_status(self, request_id: int, status: ApprovalStatus) -> dict:
        """Approve or decline one request.

        Approval is what lets Seerr push a request to Sonarr/Radarr, so this is
        the decision point, not a bookkeeping flag.
        """
        request_id = positive_id(request_id, 'request_id')
        if status not in REQUEST_APPROVAL_STATUSES:
            raise ValueError("status must be one of: " + ", ".join(REQUEST_APPROVAL_STATUSES))
        result = self.request("POST", f"request/{request_id}/{status}")
        payload = result if isinstance(result, dict) else {}
        item = _request_item(payload) or {'id': request_id}
        return {'request_id': request_id, 'status': status, 'request': item}

    def _request_exists(self, request_id: int) -> bool:
        """Whether a request id is still readable; used to classify a delete.

        Deliberately boolean: the point is only to tell "already gone" apart
        from a real failure, never to surface an upstream body.
        """
        try:
            return isinstance(self.request("GET", f"request/{request_id}"), dict)
        except ServiceError:
            return False

    def raw_read(self, method: str, endpoint: str, params: dict | None = None,
                 body: dict | None = None) -> dict:
        # Compatibility escape hatch is inspection-only, even in operator mode.
        if method.upper() != "GET" or body is not None or params:
            raise PermissionError("raw_request permits allowlisted GET endpoints without body/params only")
        if endpoint not in {"status", "/status"}:
            raise PermissionError("raw_request endpoint is not allowlisted")
        return self.request("GET", "status")
