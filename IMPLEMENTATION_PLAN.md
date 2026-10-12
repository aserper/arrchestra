# ArrChestra implementation notes

ArrChestra provides MCP tools for Seerr/Jellyseerr, Radarr, Sonarr, NZBGet, SABnzbd and NZBHydra2. Each service is optional. The server supplies operations and status; the agent decides which calls to make.

## Structure

| File | Responsibility |
| --- | --- |
| `config.py` | Environment settings, optional integrations and effective write permissions |
| `http.py` | Bounded HTTP transport, redaction, URL checks, safe XML parsing |
| `client.py` | Seerr/Jellyseerr client and legacy tool contracts |
| `clients/arr.py` | Radarr and Sonarr operations with shared API mechanics |
| `clients/nzbget.py` | Positional JSON-RPC and NZBGet state conversion |
| `clients/sabnzbd.py` | SABnzbd's mode-based API and multipart submission |
| `clients/nzbhydra.py` | Public Newznab capabilities, searches and NZB retrieval |
| `server.py` | Fixed tool registration, dispatch, concurrency and cleanup |
| `arrchestra_mcp/` | Branded module entrypoint; `jellyseerr_mcp` remains available |

The clients use synchronous httpx. MCP handlers run them in worker threads because FastMCP v1 otherwise blocks its event loop. Writes to the same service are serialized. Reads don't take that lock.

There is no plugin discovery, background scheduler, job database or separate workflow engine. One instance of each service is supported. NZBGet and SABnzbd share tool names but keep separate clients because their APIs and states differ.

## Configuration

Environment variables override `.env`. A service with no settings is omitted; partial settings fail validation. Unavailable backends fail their own calls rather than preventing startup.

Keep `JELLYSEERR_URL`, `JELLYSEERR_API_KEY` and `JELLYSEERR_TIMEOUT` for compatibility. New services use their own prefixes. URLs can contain reverse-proxy paths but not embedded credentials, query strings or fragments. TLS checks remain on.

Writes require both `MCP_READ_ONLY=false` and the target service's `ALLOW_WRITES` flag. Hydra submissions use the selected downloader's permission. There are no deletion, purge, restart, configuration-write or script-control tools. The optional legacy raw tool permits only `GET status`.

## API contracts

### Seerr

Use `/api/v1` with `X-Api-Key`. Preserve `%20` search encoding, existing tool names and the default TV season `[1]`. Service defaults come from Seerr; language-profile IDs aren't guessed. A direct Sonarr/Radarr addition is separate from a Seerr request and its approval policy.

Request history reads two endpoints because neither alone is sufficient. `GET /api/v1/request` is the paged log (`take`, `skip`, `filter`, `sort`, `requestedBy`, `mediaType`) and carries `requestedBy` on every record; `GET /api/v1/{tv|movie}/{tmdbId}` carries `mediaInfo.requests` for one title. `GET /api/v1/media` omits `requests` entirely, and `request` silently ignores a `mediaId` filter, so a title-scoped read must not be built on either. Two upstream behaviours are load-bearing: an unknown `requestedBy` returns 200 with an empty page instead of an error, so the response cannot confirm the filter was honored and the values are validated locally; and an unknown TMDB id answers 500 (not 404), which is "never requested", not a fault.

### Radarr and Sonarr

Use `/api/v3` with `X-Api-Key`. External catalog IDs and native library IDs must stay distinct. Additions check existing library membership and validate profile/root choices before posting. They don't search automatically.

Radarr uses `MoviesSearch`; Sonarr uses `EpisodeSearch` with explicit episode IDs. Episode selections must belong to the stated series. Season and episode monitoring are separate operations, avoiding partial updates across two scopes.

Release grabs requery the GUID/indexer pair and check approval before posting. Sonarr requires an episode or season scope. A release rejected by current checks can't be forced through a tool argument.

Queue/history include `downloadId` so agents can correlate records with downloader jobs. Whole-list endpoints use bounded fetches and local paging; native queue/history pagination stays native.

### NZBGet

Use `/jsonrpc` with Basic authentication and positional parameters. Reads can use POST, so HTTP method alone doesn't determine permissions. RPC errors may arrive with HTTP 200.

Operator actions require a cached version probe confirming v18 or newer. Sizes combine the native Hi/Lo fields. Submissions use base64 NZB content and the verified positional `append` form, without script parameters or forced duplicate handling. Current source includes an `AutoCategory` argument that older documentation examples omit.

Pause/resume distinguish one group from the whole queue. Retry accepts one failed history item with retry data and uses `HistoryRedownload`. This can replace failed partial output and consume bandwidth.

### SABnzbd

Use `/api` with `apikey` and JSON output. Mutations expressed as GET are still permission-checked. Native error/status fields matter even when HTTP status is 200.

Keep opaque `nzo_id` strings intact. Validate returned IDs before reusing them in query parameters. Queue sizes are reported as MiB strings; history can provide exact bytes. Submit NZBs through multipart `addfile`, and check failed-history eligibility before retrying.

### NZBHydra2

Use the public `/api` Newznab interface for `caps`, `search`, `tvsearch`, `movie` and `get`. Neither the UI's internal API nor the separate stats/history API is needed.

Capabilities can be fetched and cached before searches. Responses may be JSON or XML, including error envelopes under HTTP 200. Search results omit download links and raw descriptions.

A download call accepts a numeric Hydra result ID, not an arbitrary URL. Fetch the NZB server-side, validate it, then upload bytes to the explicitly chosen downloader/category. Redirects require trusted origins and must not carry Hydra credentials across origins or escape a configured same-origin service path.

Direct Hydra submissions don't claim a managed library import or completion of a Seerr request.

## Safety rules

- Check write permissions before mutation preflight traffic.
- Validate wire JSON before the SDK can coerce booleans into IDs or strings into flags.
- Don't retry mutations automatically. Return an uncertain outcome when a response can't establish success.
- Preserve an accepted submission's ID when a verification probe fails; report `verified=false`.
- Bound responses, XML depth/element counts and NZB size. Reject DTD/entities, alternate XML encodings and compressed replies.
- Redact credentials in results and errors. Don't echo upstream error bodies or descriptions.
- Reserve stdout for MCP; send logs to stderr.
- Keep network transports disabled until authentication and authorization are implemented and tested.

Per-service locks prevent races within one process. They don't provide exactly-once execution or protect against changes from other clients.

## Implementation checklist

- [x] Optional configuration, safe transport, permissions and consistent entrypoints.
- [x] Radarr inspection, additions, monitoring, calendar reads, searches and accepted-release grabs.
- [x] Sonarr seasons, episodes, missing content, calendar/next-up reads and ownership checks.
- [x] NZBGet queue/history, submission, pause/resume and targeted retry.
- [x] SABnzbd equivalents with its native API contracts.
- [x] NZBHydra search and both downloader handoffs.
- [x] Packaging, public documentation and wheel installation checks.

## Testing

Use pytest with httpx MockTransport for client contracts and MCP client/server sessions for tool behavior. Test each integration alone as well as both downloaders together.

Cover permission checks before traffic, native IDs, pagination, malformed responses, secret redaction, rejected releases, unsafe redirects, XML limits, uncertain outcomes and concurrency. Include actual stdio subprocess initialization, tool discovery and calls.

Mock tests don't certify compatibility with every server release. Live mutation tests should use disposable targets and queues. Client-specific setup examples should follow the current Hermes/OpenClaw documentation rather than assume every MCP host uses the same file format.

## Follow-up work

Authenticated network transport, named instances of the same service, and broader API coverage can be added when needed. Publishing packages, changing repository URLs and deploying the server are separate from building it locally.

## API references

- [Sonarr](https://sonarr.tv/docs/api/) and [Radarr](https://radarr.video/docs/api/).
- [NZBGet](https://nzbget.com/documentation/api/), including [append](https://nzbget.com/documentation/api/append/) and [editqueue](https://nzbget.com/documentation/api/editqueue/).
- [SABnzbd](https://sabnzbd.org/wiki/configuration/5.1/api).
- [NZBHydra public API](https://github.com/theotherp/nzbhydra2/wiki/External-API,-RSS-and-cached-queries) and [Newznab specification](https://newznab.readthedocs.io/en/latest/misc/api.html).
- [MCP Python SDK v1](https://py.sdk.modelcontextprotocol.io/v1/server/).
