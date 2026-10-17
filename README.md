# ArrChestra

![ArrChestra retro-CRT banner](header.png)

ArrChestra connects your MCP client to **Seerr/Jellyseerr, Sonarr, Radarr, NZBGet, SABnzbd, NZBHydra2 and Tautulli**. You can request movies and shows, check upcoming episodes, change monitoring, pause or retry downloads, and ask what your Plex users actually watch.

Each service is optional. Configure the ones you use; ArrChestra starts in read-only mode.

## What you can do

| Service | Supported operations |
| --- | --- |
| Seerr / Jellyseerr | Media search, requests and request status |
| Radarr | Movie lookup and additions, monitoring, release calendars, release searches, queue/history and import diagnostics |
| Sonarr | Series and episode lookup, season monitoring, missing episodes, upcoming airings, searches, queue/history and individual queue removal |
| NZBGet / SABnzbd | Download status, queue/history, categories, pause/resume and targeted retries |
| NZBHydra2 | Release searches and submission of a selected result to either downloader |
| Tautulli | Plex watch history and library questions; read-only |

There are no tools for deleting library titles, purging whole queues, restarting services, changing service configuration or running scripts.

## Quick start

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). From a checkout:

```bash
cp .env.example .env
# Uncomment and fill in the services you want to connect.
chmod 600 .env
uv run arrchestra-mcp
```

A minimal `.env` for Sonarr:

```dotenv
SONARR_URL=https://sonarr.example.com
SONARR_API_KEY=replace-me
MCP_READ_ONLY=true
```

The server uses **stdio**. Your MCP client launches the process and owns stdin/stdout; logs go to stderr. If you use an existing Python environment, `pip install .` installs the same `arrchestra-mcp` command.

Both module entrypoints work:

```bash
python -m arrchestra_mcp
python -m jellyseerr_mcp  # compatibility with earlier installations
```

## Configuration

Environment variables override `.env` in the working directory. Restart after changing settings. There is no YAML loader.

| Service | Required settings |
| --- | --- |
| Seerr / Jellyseerr | `JELLYSEERR_URL`, `JELLYSEERR_API_KEY` |
| Radarr | `RADARR_URL`, `RADARR_API_KEY` |
| Sonarr | `SONARR_URL`, `SONARR_API_KEY` |
| NZBGet | `NZBGET_URL`, `NZBGET_USERNAME`, `NZBGET_PASSWORD` |
| SABnzbd | `SABNZBD_URL`, `SABNZBD_API_KEY` |
| NZBHydra2 | `NZBHYDRA_URL`, `NZBHYDRA_API_KEY` |
| Tautulli | `TAUTULLI_URL`, `TAUTULLI_API_KEY` |

Only configured services appear in your client's tool list. Configure at least one service. If a service is missing required settings, startup fails and lists what's missing. You can connect one instance of each service, including both NZBGet and SABnzbd.

Use each service's base URL, without an API suffix, credentials or query string. Paths such as `https://media.example.com/sonarr` are supported. Use HTTPS where possible; certificate verification is always enabled. SABnzbd requires its full API key, not the add-only NZB key.

| Setting | Default | Purpose |
| --- | --- | --- |
| `MCP_REQUEST_TIMEOUT` | `15` | Per-request timeout in seconds, positive and at most 300 |
| `<SERVICE>_TIMEOUT` | Shared timeout | Override for one service, including `JELLYSEERR_TIMEOUT` |
| `MCP_SEARCH_TIMEOUT` | `120` | Timeout for release/target search, which waits on every enabled indexer |
| `<SERVICE>_SEARCH_TIMEOUT` | Search timeout | Override for Radarr or Sonarr; `0` uses the ordinary request timeout |
| `LOG_LEVEL` | `INFO` | Diagnostic log level |
| `MCP_READ_ONLY` | `true` | Block mutations |
| `<SERVICE>_ALLOW_WRITES` | `false` | Permit that service's operations when read-only mode is off |
| `MCP_ALLOW_RAW_READ` | `false` | Optional legacy `GET status` tool only |
| `NZBHYDRA_ALLOWED_REDIRECT_ORIGINS` | Empty | Trusted indexer origins for NZB retrieval redirects |

Store API keys and passwords in environment variables or a protected `.env` file. Don't put them in prompts or commit them. `.env` files are ignored by Git and excluded from Docker builds.

### Allowing changes

To allow changes, turn off read-only mode and enable writes for each service you want to control:

```dotenv
MCP_READ_ONLY=false
SONARR_ALLOW_WRITES=true
RADARR_ALLOW_WRITES=true
NZBGET_ALLOW_WRITES=true
```

For Seerr, use `JELLYSEERR_ALLOW_WRITES=true`; for SABnzbd, use `SABNZBD_ALLOW_WRITES=true`. Downloading a Hydra result requires write permission for the chosen downloader.

If a write times out, it may still have succeeded. ArrChestra won't retry it automatically. Check the queue, history or command status before trying again. If a submission was accepted but its status check failed, the response still includes the job ID.

## Connecting an MCP client

For clients that use `mcpServers`:

```json
{
  "mcpServers": {
    "arrchestra": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/arrchestra", "arrchestra-mcp"],
      "env": {
        "SONARR_URL": "https://sonarr.example.com",
        "SONARR_API_KEY": "replace-me",
        "MCP_READ_ONLY": "true"
      }
    }
  }
}
```

### Hermes

[Hermes uses `mcp_servers` in `~/.hermes/config.yaml`](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference):

```yaml
mcp_servers:
  arrchestra:
    command: uv
    args: [run, --directory, /path/to/arrchestra, arrchestra-mcp]
    supports_parallel_tool_calls: true
```

Keep the service settings in ArrChestra's `.env`. Hermes also supports per-server `tools.include` and `tools.exclude` filters.

### OpenClaw

[OpenClaw's MCP registry](https://docs.openclaw.ai/cli/mcp/registry) uses `mcp.servers`. Set the same stdio command and arguments in that entry. `openclaw mcp probe` checks a saved server's connection and tool discovery. Consult the documentation for your installed version: runtime adapters differ, and this registry is separate from mcporter.

Hermes and OpenClaw configuration follows their documentation; end-to-end sessions with those clients aren't covered by this project's tests.

## Using ArrChestra

| Ask your client | What it uses |
| --- | --- |
| "Find Arrival in Seerr and request it." | Seerr search and request tools |
| "Which episodes in my Sonarr library air this week?" | Sonarr calendar |
| "When is the next monitored episode of Severance?" | Sonarr library lookup and next-up episodes |
| "Stop monitoring season 1 of Severance. Leave the other seasons alone." | Sonarr season monitoring |
| "Why hasn't Arrival imported? Check Radarr's queue and history." | Radarr queue, history and import diagnostics |
| "Show the SABnzbd queue, then pause the download I choose." | Downloader queue and per-job pause |
| "Who requested Mad Men, and when?" | Seerr request history |
| "That request is stuck. Approve it and make Seerr push it again." | Seerr request status and retry |
| "Remove those bogus requests, but keep the titles." | Seerr request removal |

Calendars, lookups and status checks work in read-only mode. Requests, monitoring changes, searches that trigger downloads, and download controls need [write permission](#allowing-changes). `get_services` shows which services are connected and whether writes are enabled.

<details>
<summary>Tool behavior and arguments</summary>

### Requesting and adding media

`request_media` creates a request in Seerr and uses its approval workflow. TV requests default to season 1; specify `seasons` to request others.

You can also add media directly with `radarr_add_movie` or `sonarr_add_series`. These bypass Seerr. They require a quality profile and root folder from `radarr_get_options` or `sonarr_get_options`, and don't start a search automatically.

IDs depend on the operation: Seerr requests and Radarr additions use TMDB IDs; Sonarr additions use TVDB IDs. Once a title is in your library, monitoring and search tools use its Sonarr or Radarr ID. Episode tools use Sonarr episode IDs and check that they belong to the selected series.

Sonarr monitoring changes only the selected seasons or episodes. Season and episode changes require separate calls. All of this works in read-only mode except `request_media` and the direct additions.

### Seerr request history

"Who asked for this?" is a request-log question, not a library one:

- `list_requests` pages the request log newest-first (`page_number`, `limit` up to 100) and carries the requester on every entry. `requested_by` is who asked; `modified_by` is whoever last acted, usually the approving admin, and the two routinely differ. Narrow with `requested_by` (Seerr user id) and `media_type`, or `filter`.
- `get_media_requests` answers it for one title by TMDB id, oldest first, so the first entry is the original requester. Use it instead of scanning the log when the title is known.

Both are read-only and need no writes. Two upstream quirks shape them: `/api/v1/media` deliberately omits `requests`, and `request?mediaId=` is silently ignored (it returns the whole log), so per-title history only comes from the title's own detail endpoint. Seerr also answers 500, not 404, for a TMDB id it has never seen, which these tools report as `in_seerr: false` rather than an error. Filters, sorts and media types are validated locally because Seerr rejects a bad value with 400 but silently ignores an unknown `requestedBy`.

Paging is reported as `native` from Seerr's `pageInfo`; a title the requester list does not match returns an empty page rather than an error.

### Fixing and removing requests

A request that never reached Sonarr or Radarr sits in the log forever without ever downloading. The signal is `media.externalServiceId` being empty on a request that is otherwise approved. `retry_request` is Seerr's own repair for it: it re-runs the push and reports `pushed_to_arr` from the result, instead of creating a second request. `set_request_status` approves or declines one request (`approve` or `decline`); approval is the decision that lets Seerr push to the arr, not a bookkeeping flag.

Removal comes in two deliberately separate operations, because they delete different things:

- `remove_request` clears ONE log entry. The title's media record survives, so it can be requested again later. A request Seerr has already forgotten reports `removed: false` instead of failing.
- `remove_media` deletes ONE Seerr media item, discarding its availability status, request history and availability state. It does **not** delete the file on disk and does **not** remove the title from Sonarr or Radarr.

A media ID is neither a TMDB ID nor a request ID. Seerr also accepts any media ID with a success response, even one it never had, so `remove_media` reports `removed` (the delete was accepted) separately from `verified` (a record is known to have existed). Neither tool can be used to delete files.

### NZBHydra downloads

### Upcoming episodes and movie releases

`sonarr_get_calendar` and `radarr_get_calendar` take `start` and `end` as ISO dates or timestamps, with a maximum window of 90 days. The Sonarr calendar includes series titles. Both calendars cover the library, not a single title; results include `hasFile` to show whether the episode or movie is already downloaded.

For one show's upcoming episodes, use `sonarr_get_next_up` with its Sonarr `series_id`. It lists monitored episodes in air-date order, starting from now. Set `since` to use a different date, or `include_unmonitored=true` to include episodes you aren't monitoring.

### Searches and downloads

`radarr_search_movie` and `sonarr_search_episodes` start a search in Radarr or Sonarr, which may download a matching release. Sonarr accepts up to 100 episode IDs from one series per call. The response includes a command ID; check it with `radarr_get_command` or `sonarr_get_command` to see whether the search finished. A finished search doesn't mean the download or import is complete.

To choose a release yourself, use `radarr_get_releases` or `sonarr_get_releases`, then the matching `grab_release` tool. Sonarr requires an episode or season. These searches use indexer quota, and grabs refuse releases that are stale or rejected by the service.

Downloader tools require `backend="nzbget"` or `backend="sabnzbd"`. Pause and resume take either a `job_id` from that downloader's queue or `whole_queue=true`. Omitting both is an error. Sonarr/Radarr queue and history entries include `downloadId` for matching a download to its job. NZBGet IDs are positive integer strings; SABnzbd IDs are opaque strings.

`downloads_retry` retries one failed history item. NZBGet downloads it again and may replace the failed partial files.

`sonarr_remove_queue_item` clears one Sonarr queue entry by its queue `item_id`. By default it leaves the downloader's files alone and doesn't blocklist the release. `remove_from_client=true` also requests removal from the downloader and may delete its files; `blocklist=true` marks the release failed. Queue removal requires Sonarr write permission.

### Plex watch history (Tautulli)

Tautulli tools answer what has actually been watched, rather than what is available. They are read-only: there is no tool to delete history, terminate a stream, edit a library or change Tautulli settings, and none needs write permission.

Free text from Tautulli (titles, user names, status messages, summaries) is truncated to 512 characters; ids, timestamps and other numeric values are returned intact.

| Ask your client | Tool |
| --- | --- |
| "Is anyone watching anything right now?" | `tautulli_get_activity` |
| "Has anyone watched Severance, and who?" | `tautulli_get_item_watch_stats` |
| "When was Severance last played?" | `tautulli_get_last_played` |
| "When was Game of Thrones last played by anyone?" | `tautulli_get_last_played` (as a show) |
| "What was the last thing watched on this server?" | `tautulli_get_home_stats` (`stat_id="last_watched"`) |
| "What has the family watched this month?" | `tautulli_get_history` |
| "Which movies in my library has nobody ever played?" | `tautulli_get_library_media_info` |
| "What are my most-watched shows and top users?" | `tautulli_get_home_stats` |
| "What libraries do I have?" | `tautulli_get_libraries` |

IDs come from Plex, not Sonarr/Radarr: history and stats use a Plex `rating_key`, where a show is the grandparent key and an episode or movie is its own. Use `tautulli_search` to turn a title into a rating key, or `tautulli_get_metadata` once you have one. `tautulli_get_library_media_info` needs a `section_id` from `tautulli_get_libraries`.

`tautulli_get_activity` shows only streams playing right now, so an empty result means nobody is streaming at this moment, not that an item was never watched; use `tautulli_get_item_watch_stats` for that. Tautulli's own users (as it tracks viewing) feed `tautulli_get_user_watch_stats`, while `tautulli_get_users` lists the Plex accounts that can reach the server.

Older-watched questions split two ways. `tautulli_get_last_played` returns the single most recent play with its timestamp and who played it, and needs `media_type` because Tautulli records each play against the actual file: a movie or episode matches on its own rating key, a show matches through its episodes' grandparent key, and a season through the parent key. The tool picks that column for you, so pass `media_type="show"` for a series and `media_type="movie"` for a film. `played: false` in the reply means no plays are recorded, which is the correct answer for an untouched title. For a list of past plays instead of just the latest one, use `tautulli_get_history`.

`tautulli_get_item_watch_stats` and `tautulli_get_user_watch_stats` return per-window rows (`watch_time`) plus the users or players involved. Their `total_plays` is all-time only, and is `null` when you ask for windows that exclude all time (`query_days` without `0`), because Tautulli's windows nest (1 day sits inside 7, which sits inside 30) and cannot be added up.

### Downloading from NZBHydra2

`nzbhydra_search` searches your indexers through Hydra. `nzbhydra_get_capabilities` lists the supported filters and categories.

To send a result to a downloader, call `nzbhydra_download_result` with its `result_id`, a `backend` and a configured downloader `category`. ArrChestra fetches the NZB and submits it to that downloader. The category's existing post-processing settings still apply.

This bypasses Sonarr/Radarr release selection. It doesn't fulfill a Seerr request or guarantee that the download will be imported into your library.

If NZB retrieval redirects to an indexer, add that indexer's origin to `NZBHYDRA_ALLOWED_REDIRECT_ORIGINS`. ArrChestra doesn't forward Hydra credentials or cookies to a different origin, follow HTTPS-to-HTTP redirects, or follow same-origin redirects outside the configured service path.

</details>

<details>
<summary>Full tool list</summary>

| Group | Tools |
| --- | --- |
| Common | `ping`, `get_services` |
| Seerr | `search_media`, `request_media`, `get_request`, `list_requests`, `get_media_requests`, `set_request_status`, `retry_request`, `remove_request`, `remove_media` |
| Radarr | `radarr_find_movies`, `radarr_get_movie`, `radarr_get_options`, `radarr_get_queue`, `radarr_get_history`, `radarr_get_health`, `radarr_get_calendar`, `radarr_get_releases`, `radarr_get_command`, `radarr_add_movie`, `radarr_set_monitored`, `radarr_search_movie`, `radarr_grab_release` |
| Sonarr | `sonarr_find_series`, `sonarr_get_series`, `sonarr_get_episodes`, `sonarr_get_next_up`, `sonarr_get_missing`, `sonarr_get_calendar`, `sonarr_get_options`, `sonarr_get_queue`, `sonarr_get_history`, `sonarr_get_health`, `sonarr_get_releases`, `sonarr_get_command`, `sonarr_add_series`, `sonarr_set_monitoring`, `sonarr_search_episodes`, `sonarr_grab_release`, `sonarr_remove_queue_item` |
| Downloaders | `downloads_get_status`, `downloads_get_queue`, `downloads_get_history`, `downloads_get_categories`, `downloads_pause`, `downloads_resume`, `downloads_retry` |
| NZBHydra | `nzbhydra_get_capabilities`, `nzbhydra_search`, `nzbhydra_download_result` |
| Tautulli | `tautulli_get_server_status`, `tautulli_get_activity`, `tautulli_get_history`, `tautulli_get_last_played`, `tautulli_get_metadata`, `tautulli_get_libraries`, `tautulli_get_library_media_info`, `tautulli_get_item_watch_stats`, `tautulli_get_users`, `tautulli_get_user_names`, `tautulli_get_user_watch_stats`, `tautulli_get_home_stats`, `tautulli_search` |

The Hydra submission tool appears only when a downloader is configured. Downloader calls always require an explicit backend.

Your MCP client's `tools/list` response includes the argument schemas. Most lists default to 25 results, up to 100 per page. Sonarr/Radarr queue, history and Sonarr missing episodes use `page_number`; other paged lists use `offset`. APIs that return a whole list are paged locally.

</details>

## Docker

```bash
docker build -t arrchestra-mcp .
docker run --rm -i --env-file .env arrchestra-mcp
```

The image runs as a non-root user. Use `-i`, not `-t`, for stdio clients. Pass credentials when the container starts; no media-service credentials are needed to build the image.

GitHub Actions builds pull requests without publishing an image. Pushes to `main`, release tags matching `v*.*.*`, and manual workflow runs publish to `ghcr.io/aserper/arrchestra` using GitHub's job token.

## Limits and compatibility

- **Stdio only.** SSE and HTTP are disabled until server authentication is implemented. Don't expose the process through an unauthenticated network bridge.
- Responses are limited to 8 MiB. NZBs/XML must be UTF-8 and stay within 4 MiB, 50,000 elements and depth 32. DTD/entities and compressed responses are refused. Large replies fail with a limit error.
- Writes to the same service run one at a time within this process. Reads can continue while a write is running. Other processes and clients can still make concurrent changes.
- Treat release titles and backend messages as data, not instructions to the agent.

Earlier Jellyseerr installations can keep using `python -m jellyseerr_mcp`, `search_media`, `request_media` and `get_request`. Requests now require write permission. The optional legacy `raw_request` tool only accepts `GET status`, without parameters or a body. `ping` returns `arrchestra-mcp` as the service name.

## Development

```bash
uv run --extra test pytest -q
```

Tests use `httpx.MockTransport` for service API calls, MCP sessions for tool behavior, and subprocesses for stdio. They cover permissions, IDs, both Hydra downloaders, redirects, response limits, timeouts and concurrent calls.

API tests target Sonarr/Radarr `/api/v3`, NZBGet v18+, SABnzbd 5.x and NZBHydra's public Newznab API. They don't check compatibility with every server release. Use disposable media and queues when testing writes against a live service.

[Implementation notes](IMPLEMENTATION_PLAN.md) cover the code structure and follow-up work. Changes to API operations should include a mock contract test.