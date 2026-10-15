# klog → Tempo exporter

Export [klog](https://klog.jotaen.net/) time entries to [Tempo Server](https://tempo.io/wiki/server).

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```sh
uv sync
uv run klog-tempo --help
```

## Usage

The file argument is optional. If omitted, the exporter reads the `@default` bookmark.

```sh
uv run klog-tempo          # Preview the default bookmark against Tempo
uv run klog-tempo --sync   # Synchronize immediately, without confirmation
```

An explicit file overrides the bookmark. Missing or invalid default bookmarks
produce an error asking you to pass a file explicitly.

By default, only the current calendar month is considered. Use `--from
YYYY-MM-DD` and `--to YYYY-MM-DD` to select another range. Each option
overrides its respective boundary: an omitted `--from` defaults to the first
day of the current month, and an omitted `--to` defaults to its last day. The
selected range is printed before the plan.

In Jira, open your profile → **Personal Access Tokens** → **Create token**.
Then copy the token (it is only shown once). See
[Atlassian’s PAT documentation](https://confluence.atlassian.com/display/ENTERPRISE/Using+Personal+Access+Tokens).

Configure the Jira base URL and personal access token:

```sh
export TEMPO_URL='https://jira.example.com'
read -rs -p 'Jira personal access token: ' TEMPO_TOKEN; echo
export TEMPO_TOKEN

uv run klog-tempo
```

## Matching and synchronization

- Only entry descriptions starting with a Jira key, e.g. `FOO-2070`, `ABC2-123: description`, are exported. Record summaries do not supply keys.
- Durations and closed ranges are summed **per record date and ticket**. Actual start/end times are discarded; Tempo receives the date and total seconds corresponding to whole minutes.
- Supports standard `1h30m`, `30m`, signed durations, multiline descriptions, 12/24-hour ranges, and explicit previous/next-day markers. Also accepts the sample's nonstandard `1h 30 FOO-2070` as 90 minutes (standard klog would treat `30 FOO-2070` as the description of a one-hour entry). Prefer `1h30m FOO-2070` for compatibility with klog itself.
- Entries without a leading key and open ranges are skipped and counted. Unknown entry syntax, invalid times, and negative daily ticket totals abort the export. This is a focused parser, not a complete [klog format](https://klog.jotaen.net/#file-format) validator; should-total targets are ignored.
- Existing worklogs for the authenticated user, date, and ticket count toward the desired total, including manually created worklogs. Only a **positive difference** is added. Repeating an export with unchanged data therefore adds nothing once Tempo reflects the writes.
- Excess remote time is reported but **never reduced or deleted**. Tickets/dates absent from the input are untouched. Use a complete daily record for each ticket: a partial file is not interpreted as additional time on top of existing logs.
- Dry-run is the default. Only `--sync` applies changes.
