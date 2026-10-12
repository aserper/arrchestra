import json

import httpx
import pytest

from jellyseerr_mcp.client import JellyseerrClient
from jellyseerr_mcp.config import AppConfig, ServiceConfig


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
