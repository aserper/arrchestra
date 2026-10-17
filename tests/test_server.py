import json

import httpx
import pytest

from jellyseerr_mcp.client import JellyseerrClient
from jellyseerr_mcp.clients.arr import RadarrClient, SonarrClient
from jellyseerr_mcp.clients.tautulli import TautulliClient
from jellyseerr_mcp.config import AppConfig, ServiceConfig
from jellyseerr_mcp.server import build_server


def unpack(result):
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_ping_discovery_and_conditional_tools():
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local'),
                             httpx.MockTransport(lambda request: httpx.Response(200,json={'results':[]})))
    server = build_server(AppConfig(), {'jellyseerr':client})
    names = {tool.name for tool in await server.list_tools()}
    assert names == {'ping','get_services','search_media','get_request','list_requests',
                     'get_media_requests','request_media','remove_request','remove_media',
                     'retry_request','set_request_status'}
    assert unpack(await server.call_tool('ping',{})) == {'ok':True,'service':'arrchestra-mcp'}
    assert not unpack(await server.call_tool('get_services',{}))['services'][0]['writes_enabled']
    client.close()


@pytest.mark.asyncio
async def test_seerr_request_reads_are_registered_read_only():
    """Requester attribution is an inspection tool: it must be advertised
    read-only and must work with writes disabled."""
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/api/v1/request':
            return httpx.Response(200, json={'pageInfo': {'results': 1},
                                             'results': [{'id': 72, 'requestedBy': {'id': 8, 'displayName': 'Chromenom'}}]})
        return httpx.Response(200, json={'name': 'Mad Men', 'mediaInfo': {'id': 541, 'requests': []}})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local'),
                              httpx.MockTransport(handler))
    server = build_server(AppConfig(), {'jellyseerr': client})
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in ('list_requests', 'get_media_requests'):
        assert tools[name].annotations.readOnlyHint is True
    listed = unpack(await server.call_tool('list_requests', {'limit': 1}))
    assert listed['items'][0]['requested_by']['displayName'] == 'Chromenom'
    assert listed['total'] == 1 and listed['pagination'] == 'native'
    per_title = unpack(await server.call_tool('get_media_requests', {'tmdb_id': 1104, 'media_type': 'tv'}))
    assert per_title['title'] == 'Mad Men' and per_title['requests'] == []
    assert calls == [('GET', '/api/v1/request'), ('GET', '/api/v1/tv/1104')]
    client.close()


@pytest.mark.asyncio
async def test_readonly_enforced_over_wire_dispatch():
    # Even injecting a writable client does not bypass the server's global policy.
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                             httpx.MockTransport(lambda request: pytest.fail('network call')))
    server = build_server(AppConfig(read_only=True,allow_raw_read=True), {'jellyseerr':client})
    assert not unpack(await server.call_tool('get_services',{}))['services'][0]['writes_enabled']
    with pytest.raises(Exception, match='read-only'):
        await server.call_tool('request_media',{'media_id':1,'media_type':'movie'})
    with pytest.raises(Exception, match='allowlisted'):
        await server.call_tool('raw_request',{'method':'DELETE','endpoint':'request/1'})
    client.close()


@pytest.mark.asyncio
async def test_mocked_seerr_tool_contract():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'results':[{'id':1,'title':'Movie'}]})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local'),httpx.MockTransport(handler))
    server = build_server(AppConfig(),{'jellyseerr':client})
    result = unpack(await server.call_tool('search_media',{'query':'Test Movie'}))
    assert result['results'][0]['id'] == 1
    assert 'query=Test%20Movie' in str(requests[0].url)
    client.close()


@pytest.mark.asyncio
async def test_seerr_request_mutations_are_write_annotated_and_gated():
    """The new request mutations must be advertised as writes (not read-only)
    and must be refused by global read-only mode over the real dispatch path."""
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                              httpx.MockTransport(lambda request: pytest.fail('network call')))
    server = build_server(AppConfig(read_only=True), {'jellyseerr': client})
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in ('remove_request', 'remove_media', 'retry_request', 'set_request_status'):
        assert tools[name].annotations.readOnlyHint is False
    for name, args in [('remove_request', {'request_id': 74}),
                       ('remove_media', {'media_id': 550}),
                       ('retry_request', {'request_id': 74}),
                       ('set_request_status', {'request_id': 74, 'status': 'approve'})]:
        with pytest.raises(Exception, match='read-only'):
            await server.call_tool(name, args)
    client.close()


@pytest.mark.asyncio
async def test_remove_request_tool_reaches_only_the_request_endpoint():
    """Wire-level contract: removing a request must never touch /media.

    Collapsing the two deletes into one call would silently delete the title
    (and, on the file variant, its content), so the endpoint is asserted.
    """
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(204)
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                              httpx.MockTransport(handler))
    # AppConfig defaults to read-only, so operator mode must be explicit here.
    server = build_server(AppConfig(read_only=False), {'jellyseerr': client})
    result = unpack(await server.call_tool('remove_request', {'request_id': 74}))
    assert result == {'request_id': 74, 'removed': True}
    assert seen == [('DELETE', '/api/v1/request/74')]
    client.close()


@pytest.mark.asyncio
async def test_retry_request_tool_reports_the_arr_push_state():
    def handler(request):
        return httpx.Response(200, json={'id': 74, 'type': 'movie',
                                         'media': {'id': 550, 'externalServiceId': 812}})
    client = JellyseerrClient(ServiceConfig('jellyseerr','https://test.local',allow_writes=True),
                              httpx.MockTransport(handler))
    server = build_server(AppConfig(read_only=False), {'jellyseerr': client})
    result = unpack(await server.call_tool('retry_request', {'request_id': 74}))
    assert result['retried'] is True and result['pushed_to_arr'] is True
    assert result['external_service_id'] == 812
    client.close()


@pytest.mark.asyncio
async def test_arr_calendar_tools_register_only_for_the_configured_service():
    """Sonarr is the only service with a next-up read, and both services expose
    a calendar tool, so registration follows configuration."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=[])

    sonarr = SonarrClient(ServiceConfig('sonarr', 'https://sonarr.test'),
                          httpx.MockTransport(handler))
    radarr = RadarrClient(ServiceConfig('radarr', 'https://radarr.test'),
                          httpx.MockTransport(handler))
    server = build_server(AppConfig(), {'sonarr': sonarr, 'radarr': radarr})
    names = {tool.name for tool in await server.list_tools()}
    assert {'sonarr_get_calendar', 'radarr_get_calendar', 'sonarr_get_next_up'} <= names
    assert 'radarr_get_next_up' not in names
    result = unpack(await server.call_tool('sonarr_get_calendar',
                                           {'start': '2026-10-05', 'end': '2026-10-19'}))
    assert result['items'] == [] and calls[-1] == '/api/v3/calendar'
    # A calendar-only window still routes to the right service.
    assert 'kodi' not in names and 'sonarr_add_movie' not in names
    sonarr.close()
    radarr.close()


@pytest.mark.asyncio
async def test_queue_removal_tool_registers_for_sonarr_only():
    """Registration is a property of the code, not of the environment: assert it
    with injected clients so a machine with no .env still checks it."""
    def handler(request):
        return httpx.Response(200, json={'totalRecords': 0, 'records': []})

    sonarr = SonarrClient(ServiceConfig('sonarr', 'https://sonarr.test', allow_writes=True),
                          httpx.MockTransport(handler))
    server = build_server(AppConfig(), {'sonarr': sonarr})
    names = {tool.name for tool in await server.list_tools()}
    assert 'sonarr_remove_queue_item' in names
    assert 'radarr_remove_queue_item' not in names  # not configured, never registered
    tool = next(t for t in await server.list_tools() if t.name == 'sonarr_remove_queue_item')
    # It mutates state, so it must not be advertised as read-only.
    assert tool.annotations.readOnlyHint is False
    sonarr.close()


@pytest.mark.asyncio
async def test_tautulli_tools_register_read_only_and_only_when_configured():
    """Tautulli is inspection-only: every registered tool must be advertised as
    read-only, and none may exist when the service is not configured."""
    def handler(request):
        return httpx.Response(200, json={'response': {'result': 'success', 'data': []}})
    tautulli = TautulliClient(ServiceConfig('tautulli', 'http://tautulli.test:8181',
                                            api_key='key'),
                              httpx.MockTransport(handler))
    server = build_server(AppConfig(), {'tautulli': tautulli})
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    expected = {'tautulli_get_server_status', 'tautulli_get_activity', 'tautulli_get_history',
                'tautulli_get_last_played', 'tautulli_get_metadata', 'tautulli_get_libraries',
                'tautulli_get_library_media_info', 'tautulli_get_item_watch_stats',
                'tautulli_get_users', 'tautulli_get_user_names',
                'tautulli_get_user_watch_stats', 'tautulli_get_home_stats', 'tautulli_search'}
    assert expected <= names
    # No write tool and no other service's tools leak in with a Tautulli-only config.
    assert not {name for name in names if 'remove' in name or 'add_' in name}
    assert not {name for name in names if name.startswith(('radarr_', 'sonarr_', 'downloads_'))}
    for tool in tools:
        if tool.name.startswith('tautulli_'):
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False
    tautulli.close()


@pytest.mark.asyncio
async def test_tautulli_tools_are_absent_without_tautulli():
    def handler(request):
        return httpx.Response(200, json={'recordsTotal': 0, 'records': []})
    radarr = RadarrClient(ServiceConfig('radarr', 'https://radarr.test', api_key='key'),
                          httpx.MockTransport(handler))
    server = build_server(AppConfig(), {'radarr': radarr})
    names = {tool.name for tool in await server.list_tools()}
    assert not {name for name in names if name.startswith('tautulli_')}
    radarr.close()


@pytest.mark.asyncio
async def test_last_played_through_mcp_dispatch_for_a_movie_and_a_show():
    """The Boss asked for latest-play coverage through MCP dispatch, not only the
    client: the routed column must differ between a movie and a show."""
    seen = []

    def handler(request):
        params = dict(request.url.params)
        seen.append(params)
        if params.get('grandparent_rating_key'):  # a show: episodes carry the play
            rows = [{'row_id': 4, 'date': 1762100000, 'user': 'bob', 'media_type': 'episode',
                     'grandparent_title': 'Severance', 'file': '/media/secret.mkv'}]
        elif params.get('rating_key'):            # a movie: its own key
            rows = [{'row_id': 9, 'date': 1762000000, 'user': 'alice', 'media_type': 'movie',
                     'full_title': 'Arrival', 'file': '/media/arrival.mkv'}]
        else:
            rows = []
        return httpx.Response(200, json={'response': {'result': 'success', 'data': {
            'recordsTotal': len(rows), 'recordsFiltered': len(rows), 'data': rows}}})

    tautulli = TautulliClient(ServiceConfig('tautulli', 'http://tautulli.test:8181',
                                            api_key='key'),
                              httpx.MockTransport(handler))
    server = build_server(AppConfig(), {'tautulli': tautulli})

    movie = unpack(await server.call_tool('tautulli_get_last_played',
                                          {'rating_key': 4348, 'media_type': 'movie'}))
    assert seen[-1]['rating_key'] == '4348' and 'grandparent_rating_key' not in seen[-1]
    assert movie['played'] is True and movie['last_played'] == 1762000000
    assert movie['last_play']['user'] == 'alice'
    assert 'file' not in movie['last_play']  # server paths never reach the caller

    show = unpack(await server.call_tool('tautulli_get_last_played',
                                         {'rating_key': 1219, 'media_type': 'show'}))
    assert seen[-1]['grandparent_rating_key'] == '1219' and 'rating_key' not in seen[-1]
    assert show['played'] is True and show['last_played'] == 1762100000
    tautulli.close()


@pytest.mark.asyncio
async def test_last_played_through_mcp_dispatch_reports_no_history():
    """A never-watched item is a valid answer, not an error."""
    def handler(request):
        return httpx.Response(200, json={'response': {'result': 'success', 'data': {
            'recordsTotal': 0, 'recordsFiltered': 0, 'data': []}}})

    tautulli = TautulliClient(ServiceConfig('tautulli', 'http://tautulli.test:8181',
                                            api_key='key'),
                              httpx.MockTransport(handler))
    server = build_server(AppConfig(), {'tautulli': tautulli})
    result = unpack(await server.call_tool('tautulli_get_last_played',
                                           {'rating_key': 999, 'media_type': 'movie'}))
    assert result['played'] is False and result['last_played'] is None
    tautulli.close()
