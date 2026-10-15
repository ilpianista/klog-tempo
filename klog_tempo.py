#!/usr/bin/env python3
"""Conservative klog → Tempo Server exporter (Python 3.10+, standard library)."""
import argparse
import json
import os
import re
import sys
from calendar import monthrange
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ISSUE = re.compile(r'^([A-Z][A-Z0-9]*-[0-9]+)(?![\w-])')
TIME = r'<?\d{1,2}:\d{2}(?:am|pm)?>?'
RANGE = re.compile(rf'^({TIME}) *- *({TIME}|\?+)(?: +(.*))?$')
DURATION = re.compile(r'^([+-]?)(?:(\d+)h(?: *(\d+)m?)?|(\d+)m)(?: +(.*))?$')
HEADER = re.compile(r'^(\d{4}[-/]\d{2}[-/]\d{2})(?: +\([^()]+!\))? *$')


def clock(value):
    match = re.fullmatch(r'(<)?(\d{1,2}):(\d{2})(am|pm)?(>)?', value)
    before, h, m, meridiem, after = match.groups()
    h, m = int(h), int(m)
    if m > 59 or (before and after):
        raise ValueError('invalid clock time')
    if meridiem:
        if not 1 <= h <= 12:
            raise ValueError('invalid 12-hour time')
        h = h % 12 + (12 if meridiem == 'pm' else 0)
    elif h > 24 or (h == 24 and (m or after)):
        raise ValueError('invalid 24-hour time')
    return h * 60 + m + (1440 if after else -1440 if before else 0)


def parse_klog(text, first=None, last=None):
    """Return daily totals and skipped count within inclusive ISO date bounds."""
    entries = []
    day = indent = current = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            day = indent = current = None
            continue
        try:
            header = HEADER.fullmatch(line)
            if header:
                day = date.fromisoformat(header[1].replace('/', '-')).isoformat()
                indent = current = None
                continue
            if day is None:
                raise ValueError('expected a date header')
            if not line[0].isspace():
                if current is not None:
                    raise ValueError('record summary after entries')
                continue  # Record summaries do not supply issue keys.
            if indent is None:
                indent = re.match(r'^(\t| {2,4})(?=\S)', line)
                if not indent:
                    raise ValueError('expected 2–4 spaces or one tab')
                indent = indent[1]
            if line.startswith(indent * 2):
                if current is None:
                    raise ValueError('summary without entry')
                current[2] += '\n' + line[len(indent) * 2:].strip()
                continue
            if not line.startswith(indent) or line[len(indent):][:1].isspace():
                raise ValueError('inconsistent indentation')
            value = line[len(indent):]
            match = RANGE.fullmatch(value)
            if match:
                start, end, summary = match.groups()
                start = clock(start)
                minutes = None if end.startswith('?') else clock(end) - start
                if minutes is not None and minutes < 0:
                    raise ValueError('range ends before it starts; use > for next day')
            else:
                match = DURATION.fullmatch(value)
                if not match:
                    raise ValueError('unsupported entry syntax')
                sign, hours, mins, only_mins, summary = match.groups()
                if hours is not None and int(mins or 0) > 59:
                    raise ValueError('minute component must be below 60')
                minutes = (int(hours or 0) * 60 + int(mins or only_mins or 0))
                if sign == '-':
                    minutes = -minutes
            current = [day, minutes, summary or '']
            entries.append(current)
        except ValueError as exc:
            raise ValueError(f'line {number}: {exc}') from exc
    totals = defaultdict(int)
    skipped = 0
    for day, minutes, summary in entries:
        if (first and day < first) or (last and day > last):
            continue
        match = ISSUE.match(summary.strip())
        if not match or minutes is None:
            skipped += 1
            continue
        totals[day, match[1]] += minutes
    if any(value < 0 for value in totals.values()):
        raise ValueError('negative daily ticket total')
    return dict(totals), skipped


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to a login page/another host.


class Tempo:
    def __init__(self, base_url, token=None):
        parsed = urlsplit(base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('base URL must be HTTPS, without credentials/query/fragment')
        self.base_url = base_url.rstrip('/')
        self.headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if not token or not token.strip():
            raise ValueError('set TEMPO_TOKEN to a Jira personal access token')
        self.headers['Authorization'] = 'Bearer ' + token.strip()
        self.opener = build_opener(NoRedirect())
        identity = self.request('/rest/api/2/myself')
        if not isinstance(identity, dict) or not isinstance(identity.get('key'), str) or not identity['key'].strip():
            raise ValueError('Jira /myself response has no user key; cannot determine worklog owner')
        self.worker = identity['key']

    def request(self, path, body=None):
        req = Request(self.base_url + path, headers=self.headers,
                      data=None if body is None else json.dumps(body).encode())
        try:
            with self.opener.open(req, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            raise ValueError(f'Tempo HTTP {exc.code} for {path}; check authentication/permissions') from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ValueError(f'Tempo request failed for {path}. No automatic retries; check remote state before rerunning.') from exc

    def totals(self, first, last):
        rows = self.request('/rest/tempo-timesheets/4/worklogs/search',
                            {'from': first, 'to': last, 'worker': [self.worker]})
        if not isinstance(rows, list):
            raise ValueError('unexpected worklog search response (expected complete array)')
        totals = defaultdict(int)
        seen = set()
        for row in rows:
            try:
                if row['worker'] != self.worker:
                    continue
                identifier = row['tempoWorklogId']
                if identifier in seen:
                    raise ValueError('duplicate worklog in response')
                seen.add(identifier)
                day = date.fromisoformat(row['started'][:10]).isoformat()
                seconds = row['timeSpentSeconds']
                if type(seconds) is not int or seconds < 0:
                    raise ValueError('invalid timeSpentSeconds')
                if first <= day <= last:
                    totals[day, row['issue']['key']] += seconds
            except (KeyError, TypeError) as exc:
                raise ValueError('malformed worklog response') from exc
        return dict(totals)

    def issue_id(self, key):
        result = self.request('/rest/api/latest/issue/' + quote(key, safe=''))
        if not isinstance(result, dict) or not str(result.get('id', '')).isdigit():
            raise ValueError(f'invalid issue lookup for {key}')
        return str(result['id'])

    def add(self, day, issue_id, seconds):
        return self.request('/rest/tempo-timesheets/4/worklogs/', {
            'attributes': {}, 'billableSeconds': '', 'worker': self.worker,
            'comment': None, 'started': day, 'timeSpentSeconds': seconds,
            'originTaskId': issue_id, 'remainingEstimate': None,
            'endDate': None, 'includeNonWorkingDays': False,
        })


def plan(desired, existing):
    return [(day, key, minutes * 60, existing.get((day, key), 0))
            for (day, key), minutes in sorted(desired.items())]


def sync(client, desired, dry_run=False):
    if not desired:
        print('No matching Jira entries to export.')
        return
    days = [day for day, key in desired]
    first, last = min(days), max(days)
    existing = client.totals(first, last)
    rows = plan(desired, existing)
    print('Date        Ticket                 Klog min  Tempo min  Action')
    additions = []
    for day, key, wanted, actual in rows:
        delta = wanted - actual
        action = f'ADD {delta / 60:g} min' if delta > 0 else 'OK' if delta == 0 else 'EXCESS — left unchanged'
        print(f'{day}  {key:22} {wanted / 60:8g}  {actual / 60:9g}  {action}')
        if delta > 0:
            if delta % 60:
                raise ValueError('existing worklogs contain partial minutes; reconcile manually')
            additions.append((day, key, delta))
    if dry_run or not additions:
        print('No changes written.')
        return
    # Resolve every ticket before the first write, so invalid keys cannot cause partial sync.
    ids = {key: client.issue_id(key) for key in sorted({key for _, key, _ in additions})}
    # Detect changes while preparing the plan and resolving issue IDs.
    refreshed = client.totals(first, last)
    if any(existing.get(pair, 0) != refreshed.get(pair, 0) for pair in desired):
        raise ValueError('Tempo changed while preparing sync; rerun to create a new plan')
    for day, key, seconds in additions:
        client.add(day, ids[key], seconds)
        print(f'Added {seconds // 60} min: {day} {key}', flush=True)


def default_bookmark():
    bookmarks_path = Path.home() / '.config/klog/bookmarks.json'
    try:
        bookmarks = json.loads(bookmarks_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'Cannot read klog bookmarks from {bookmarks_path}; pass a .klg file explicitly') from exc
    if not isinstance(bookmarks, list):
        raise ValueError(f'Expected an array in {bookmarks_path}')
    matches = [item for item in bookmarks
               if isinstance(item, dict) and item.get('name') == 'default']
    if len(matches) != 1 or not isinstance(matches[0].get('path'), str) or not matches[0]['path'].strip():
        raise ValueError(f'Expected one default bookmark with a path in {bookmarks_path}; pass a .klg file explicitly')
    return Path(matches[0]['path']).expanduser()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     epilog='By default, compare with Tempo without writing. Use --sync to apply changes.')
    parser.add_argument('file', type=Path, nargs='?',
                        help='klog file (default: default bookmark in ~/.config/klog/bookmarks.json)')
    parser.add_argument('--url', default=os.getenv('TEMPO_URL'))
    today = date.today()
    parser.add_argument('--from', dest='first', type=date.fromisoformat,
                        default=today.replace(day=1),
                        help='inclusive start date, YYYY-MM-DD (default: start of current month)')
    parser.add_argument('--to', dest='last', type=date.fromisoformat,
                        default=today.replace(day=monthrange(today.year, today.month)[1]),
                        help='inclusive end date, YYYY-MM-DD (default: end of current month)')
    parser.add_argument('--sync', action='store_true', help='apply changes without a confirmation prompt')
    args = parser.parse_args()
    try:
        if args.first > args.last:
            raise ValueError('--from must not be later than --to')
        source = args.file if args.file is not None else default_bookmark()
        totals, skipped = parse_klog(source.read_text(encoding='utf-8'),
                                     args.first.isoformat(), args.last.isoformat())
        print(f'Range: {args.first} to {args.last}')
        print(f'Skipped {skipped} entries in this range without a leading Jira key or with an open range.')
        if not args.url:
            raise ValueError('supply --url (or TEMPO_URL)')
        sync(Tempo(args.url, token=os.getenv('TEMPO_TOKEN')),
             totals, dry_run=not args.sync)
        return 0
    except (ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nCancelled. If writes had started, check Tempo before rerunning.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
