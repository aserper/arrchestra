import json

import httpx
import pytest

from jellyseerr_mcp.client import JellyseerrClient
from jellyseerr_mcp.config import AppConfig, ServiceConfig
from jellyseerr_mcp.http import ServiceError


def test_client_and_search_encoding():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'results': []})
    client = JellyseerrClient(AppConfig(jellyseerr_url='https://test.local', jellyseerr_api_key='test-api-key'),
                            httpx.MockTransport(handler))
    assert client.search_media('Test Movie & café') == {'results': []}
    assert str(requests[0].url) == 'https://test.local/api/v1/search?query=Test%20Movie%20%26%20caf%C3%A9'
    assert requests[0].headers['X-Api-Key'] == 'test-api-key'
    client.close()
    assert client._http.is_closed


def test_get_request_details():
    def handler(request):
        assert request.url.path == '/api/v1/request/123'
        return httpx.Response(200, json={'id':123})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local'), httpx.MockTransport(handler))
    assert client.get_request(123) == {'id':123}
    client.close()


def test_list_requests_projects_requester_and_reports_native_total():
    """The request log is only useful if it says WHO asked.

    ``requestedBy`` is the requester and ``modifiedBy`` is whoever last acted
    (usually the approving admin); they routinely differ, so both are kept.
    """
    seen = []
    page = {'pageInfo': {'pages': 9, 'pageSize': 2, 'results': 18, 'page': 2},
            'results': [{'id': 72, 'status': 5, 'type': 'tv', 'is4k': False,
                         'createdAt': '2026-02-03T13:41:53.000Z', 'seasonCount': 2,
                         'seasons': [{'seasonNumber': 6, 'status': 5},
                                     {'seasonNumber': 7, 'status': 5}],
                         'media': {'id': 541, 'tmdbId': 1104, 'tvdbId': 80337,
                                   'status': 4, 'externalServiceId': 181,
                                   'externalServiceId4k': None, 'apiKey': 'leak'},
                         'requestedBy': {'id': 8, 'displayName': 'Chromenom',
                                         'email': 'p.noamtal@gmail.com', 'userType': 1,
                                         'password': 'leak'},
                         'modifiedBy': {'id': 1, 'displayName': 'aserper'}}]}
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=page)
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local'),
                              httpx.MockTransport(handler))
    result = client.list_requests(page_number=2, limit=2, requested_by=8, media_type='tv')
    url = str(seen[0].url)
    assert 'take=2' in url and 'skip=2' in url
    assert 'filter=all' in url and 'sort=added' in url
    assert 'requestedBy=8' in url and 'mediaType=tv' in url
    item = result['items'][0]
    assert item['requested_by']['displayName'] == 'Chromenom'
    assert item['modified_by']['displayName'] == 'aserper'
    assert item['season_numbers'] == [6, 7]
    assert item['media']['externalServiceId'] == 181
    # Upstream pageInfo drives the total, not the page length.
    assert (result['total'], result['has_more'], result['pagination']) == (18, True, 'native')
    # Credential-shaped keys never survive the projection.
    assert 'password' not in item['requested_by'] and 'apiKey' not in item['media']
    client.close()


def test_request_filters_are_validated_before_the_network():
    """Seerr 400s on a bad filter/sort/mediaType but SILENTLY ignores unknown
    ``requestedBy`` and ``mediaId``, so an unvalidated filter would look like it
    worked while returning the whole log."""
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local'),
                              httpx.MockTransport(lambda request: pytest.fail('network call')))
    bad_calls: list[dict] = [
        {'filter': 'declined'}, {'filter': 'bogus'}, {'sort': 'id'},
        {'media_type': 'series'}, {'requested_by': 0}, {'requested_by': 'abc'},
        {'requested_by': True}, {'page_number': 0}, {'page_number': True},
        {'limit': 0}, {'limit': 101}, {'limit': True},
    ]
    for kwargs in bad_calls:
        with pytest.raises(ValueError):
            client.list_requests(**kwargs)
    client.close()


def test_get_media_requests_collects_every_request_for_one_title():
    """``/api/v1/media`` omits ``requests`` and ``request?mediaId=`` is ignored,
    so per-title history only comes from the title's own detail endpoint."""
    seen = []
    detail = {'id': 1104, 'name': 'Mad Men', 'mediaInfo': {
        'id': 541, 'tmdbId': 1104, 'status': 4, 'externalServiceId': 181,
        'seasons': [{'seasonNumber': 2, 'status': 5}],
        'requests': [
            {'id': 72, 'status': 5, 'type': 'tv', 'createdAt': '2026-02-03T13:41:53.000Z',
             'seasons': [{'seasonNumber': 6, 'status': 5}],
             'requestedBy': {'id': 8, 'displayName': 'Chromenom'}},
            {'id': 63, 'status': 5, 'type': 'tv', 'createdAt': '2026-01-07T20:51:34.000Z',
             'seasons': [{'seasonNumber': 2, 'status': 5}],
             'requestedBy': {'id': 8, 'displayName': 'Chromenom'}}]}}
    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=detail)
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local'),
                              httpx.MockTransport(handler))
    result = client.get_media_requests(1104, media_type='tv')
    assert seen == ['/api/v1/tv/1104']
    assert result['title'] == 'Mad Men' and result['in_seerr'] is True
    # Oldest first, so the original requester reads as the first entry.
    assert [r['id'] for r in result['requests']] == [63, 72]
    assert result['requests'][0]['requested_by']['displayName'] == 'Chromenom'
    assert result['total'] == 2
    client.close()


def test_unknown_title_is_not_an_error():
    """Seerr answers 500 (not 404) for a TMDB id it has never seen; that is
    "never requested", not a service fault to raise at the caller."""
    def handler(request):
        return httpx.Response(500, json={'message': 'Item does not exist'})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local'),
                              httpx.MockTransport(handler))
    result = client.get_media_requests(999999, media_type='movie')
    assert result['in_seerr'] is False and result['requests'] == []
    assert result['title'] is None and result['total'] == 0
    with pytest.raises(ValueError):
        client.get_media_requests(0)
    with pytest.raises(ValueError):
        client.get_media_requests(1104, media_type='series')
    client.close()


def test_tv_request_does_not_invent_language_profile_or_seasons():
    sent = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json=[{'id':1,'is4k':False,'isDefault':True,
                'activeProfileId':4,'activeDirectory':'/tv'}])
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={'id':9,'status':'pending'})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),httpx.MockTransport(handler))
    assert client.request_media(123,'tv',seasons=[3,1,3])['id'] == 9
    assert sent[0]['seasons'] == [1,3]
    assert 'languageProfileId' not in sent[0]
    with pytest.raises(ValueError):
        client.request_media(123,'tv',seasons=[])
    client.close()


def test_permissions_before_network_and_raw_allowlist():
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local'),
                             httpx.MockTransport(lambda request: pytest.fail('network call')))
    with pytest.raises(PermissionError):
        client.request_media(1,'movie')
    for method,endpoint in [('DELETE','request/1'),('GET','settings/main'),('POST','status')]:
        with pytest.raises(PermissionError):
            client.raw_read(method,endpoint)
    client.close()


def test_request_mutations_require_writes_and_validated_ids():
    """Every new mutation is gated and validated BEFORE the network is touched."""
    dead = lambda request: pytest.fail('network call')
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local'),
                              httpx.MockTransport(dead))
    for function, args in [(client.remove_request, (5,)), (client.remove_media, (9,)),
                           (client.retry_request, (5,)),
                           (client.set_request_status, (5, 'approve'))]:
        with pytest.raises(PermissionError):
            function(*args)
    client.close()

    writable = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                                httpx.MockTransport(dead))
    for bad in (0, -1, 'abc', True):
        for function in (writable.remove_request, writable.remove_media, writable.retry_request):
            with pytest.raises(ValueError):
                function(bad)
        with pytest.raises(ValueError):
            writable.set_request_status(bad, 'approve')
    with pytest.raises(ValueError):
        writable.set_request_status(5, 'denied')
    writable.close()


def test_remove_request_clears_only_the_log_entry():
    """Deleting a request must not delete the title it asked for.

    The media record is what makes a later re-request of the same title fail as
    a duplicate, so the two deletes are deliberately separate operations.
    """
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(204)
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                              httpx.MockTransport(handler))
    assert client.remove_request(74) == {'request_id': 74, 'removed': True}
    assert seen == [('DELETE', '/api/v1/request/74')]
    result = client.remove_media(550)
    assert result['media_id'] == 550 and result['removed'] is True
    # Seerr accepts any media id, so the tool must not claim a verified delete
    # or imply it deleted a file.
    assert result['verified'] is False
    assert 'file' in result['note'] and 'untouched' in result['note']
    assert seen[-1] == ('DELETE', '/api/v1/media/550')
    client.close()


def test_remove_request_reports_an_already_absent_request():
    """A 404 is 'already gone', not a failure: the state the caller wanted holds.

    Classified from a follow-up read rather than by parsing the error text.
    """
    def handler(request):
        if request.method == 'DELETE':
            return httpx.Response(404, json={'message': 'Request not found'})
        assert request.method == 'GET'
        return httpx.Response(404, json={'message': 'Request not found'})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                              httpx.MockTransport(handler))
    assert client.remove_request(999999) == {'request_id': 999999, 'removed': False,
                                            'reason': 'not_found'}
    client.close()


def test_remove_request_still_raises_when_the_delete_really_failed():
    """A delete that failed for any reason other than absence is a real error."""
    def handler(request):
        if request.method == 'DELETE':
            return httpx.Response(500, json={'message': 'boom'})
        return httpx.Response(200, json={'id': 74})   # request still there
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                              httpx.MockTransport(handler))
    with pytest.raises(ServiceError):
        client.remove_request(74)
    client.close()


def test_retry_request_reports_whether_the_arr_push_succeeded():
    """The whole point of a retry is the externalServiceId it leaves behind."""
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={'id': 74, 'status': 2, 'type': 'movie',
                                         'media': {'id': 550, 'tmdbId': 334541,
                                                   'externalServiceId': None,
                                                   'externalServiceId4k': None}})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                              httpx.MockTransport(handler))
    result = client.retry_request(74)
    assert seen == [('POST', '/api/v1/request/74/retry')]
    assert result['retried'] is True and result['pushed_to_arr'] is False
    assert result['external_service_id'] is None
    assert result['request']['media']['id'] == 550
    client.close()


def test_set_request_status_maps_to_the_approval_endpoint():
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={'id': 74, 'status': 2, 'type': 'movie'})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                              httpx.MockTransport(handler))
    assert client.set_request_status(74, 'approve')['status'] == 'approve'
    assert client.set_request_status(74, 'decline')['status'] == 'decline'
    assert seen == [('POST', '/api/v1/request/74/approve'),
                    ('POST', '/api/v1/request/74/decline')]
    client.close()


def test_orphaned_requests_are_visible_as_never_pushed():
    """A request Seerr never pushed has a null externalServiceId.

    That is the only signal distinguishing a stuck request from one merely
    waiting on a release, so the projection has to carry it through.
    """
    page = {'pageInfo': {'results': 1}, 'results': [
        {'id': 74, 'status': 2, 'type': 'movie', 'is4k': False,
         'createdAt': '2026-02-06T01:22:20.000Z',
         'media': {'id': 550, 'tmdbId': 334541, 'externalServiceId': None,
                   'externalServiceId4k': None},
         'requestedBy': {'id': 11, 'displayName': 'dado779'}}]}
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local'),
                              httpx.MockTransport(lambda request: httpx.Response(200, json=page)))
    item = client.list_requests(limit=1)['items'][0]
    assert item['media']['externalServiceId'] is None
    assert item['requested_by']['displayName'] == 'dado779'
    client.close()


def test_server_id_zero_is_accepted():
    """Seerr numbers its configured servers from 0; the default server is id 0.

    Rejecting 0 as "not a positive integer" broke every request against a
    default server, which is how this was found.
    """
    sent = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json=[{'id':0,'is4k':False,'isDefault':True,
                'activeProfileId':7,'activeDirectory':'/mnt/media/tv'}])
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={'id':9,'status':'pending'})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                              httpx.MockTransport(handler))
    assert client.request_media(254528,'tv',seasons=[1])['id'] == 9
    assert sent[0]['serverId'] == 0
    assert sent[0]['mediaId'] == 254528 and sent[0]['mediaType'] == 'tv'
    assert sent[0]['seasons'] == [1]
    client.close()


def test_seerr_media_and_server_ids_are_validated_independently():
    """media_id stays strictly positive; only the Seerr server id may be 0."""
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json=[{'id':0,'is4k':False,'isDefault':True,
                'activeProfileId':7,'activeDirectory':'/mnt/media/tv'}])
        return httpx.Response(201, json={'id':1})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                              httpx.MockTransport(handler))
    for bad in (0, -1, 'abc', True):
        with pytest.raises(ValueError):
            client.request_media(bad, 'tv', seasons=[1])
    client.close()


def test_a_zero_profile_id_is_still_usable():
    """Seerr profiles are 0-based too, so 0 is a valid profile, not "unset"."""
    sent = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json=[{'id':0,'is4k':False,'isDefault':True,
                'activeProfileId':0,'activeDirectory':'/mnt/media/tv'}])
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={'id':1})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                              httpx.MockTransport(handler))
    client.request_media(1, 'tv', seasons=[1])
    assert sent[0]['profileId'] == 0
    client.close()
