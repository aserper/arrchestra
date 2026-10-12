"""Real stdio MCP handshake and tools, with no external service requests."""
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jellyseerr_mcp.config import SERVICES


@pytest.mark.asyncio
@pytest.mark.parametrize('module', ['jellyseerr_mcp', 'arrchestra_mcp'])
@pytest.mark.parametrize('isolated', [True, False])
async def test_stdio_protocol_only_stdout(module, isolated, caplog, tmp_path):
    env = dict(os.environ)
    for service in SERVICES:
        for suffix in ('URL','API_KEY','USERNAME','PASSWORD','ALLOW_WRITES'):
            env.pop(service.upper() + '_' + suffix, None)
    env.update({'JELLYSEERR_URL':'https://unused.test', 'JELLYSEERR_API_KEY':'wire-secret-key',
                'MCP_READ_ONLY':'true','MCP_ALLOW_RAW_READ':'false','LOG_LEVEL':'DEBUG'})
    # The server loads .env from its own cwd. The isolated child gets a directory
    # with no .env, so it must see only the environment it was handed; the second
    # child keeps the checkout as cwd and is the regression for the .env refill.
    cwd = tmp_path if isolated else None
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
    with tempfile.TemporaryFile(mode='w+') as errlog:
        params = StdioServerParameters(command=sys.executable,args=['-m',module],env=env,cwd=cwd)
        async with stdio_client(params,errlog=errlog) as (read,write):
            async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=10)) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                seerr_reads = {'search_media', 'get_request', 'list_requests',
                               'get_media_requests', 'request_media'}
                if isolated:
                    assert names == {'ping', 'get_services'} | seerr_reads
                else:
                    assert {'ping', 'get_services'} | seerr_reads <= names
                ping = await session.call_tool('ping',{})
                assert not ping.isError
                denied = await session.call_tool('request_media',{'media_id':1,'media_type':'movie'})
                assert denied.isError
                assert 'wire-secret-key' not in str(ping) + str(denied)
        errlog.seek(0)
        assert 'wire-secret-key' not in errlog.read()
    assert 'Failed to parse' not in caplog.text


def test_network_operator_mode_refused():
    env = dict(os.environ, SONARR_URL='https://unused.test', SONARR_API_KEY='wire-secret-key',
               MCP_READ_ONLY='false', SONARR_ALLOW_WRITES='true')
    process = subprocess.run([sys.executable,'-m','jellyseerr_mcp','--transport','sse'],
                             text=True,capture_output=True,env=env,timeout=20)
    assert process.returncode == 2
    assert 'Network transports are disabled' in process.stderr
    assert 'wire-secret-key' not in process.stderr
