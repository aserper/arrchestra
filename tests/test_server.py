import json

import httpx
import pytest

from jellyseerr_mcp.client import JellyseerrClient
from jellyseerr_mcp.clients.arr import RadarrClient, SonarrClient
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
                     'get_media_requests','request_media'}
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
