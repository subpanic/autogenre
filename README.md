# Release Genre Sync

`release_genre_sync.py` recursively scans a music library, groups tracks into releases, and harmonizes genre tags across each release.

It can also enrich genres from:

- MusicBrainz
- Bandcamp (HTML scraping)

## What a "release" means

A release is a set of 1+ audio files that:

- are in the same folder
- share the same `album` tag

Supported extensions:

- `.flac`
- `.m4a`
- `.mp3`
- `.ogg`
- `.oga`
- `.opus`
- `.wav`
- `.aiff`
- `.aif`

## Install

Python 3.10+ is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python3 release_genre_sync.py /path/to/music
```

Useful options:

- `--dry-run`  
  Compute and log changes, but do not write tags.
- `--interactive`  
  If multiple MusicBrainz/Bandcamp candidates are found, prompt you to choose one.
- `--no-musicbrainz`  
  Disable MusicBrainz lookups.
- `--no-bandcamp`  
  Disable Bandcamp lookups.
- `--musicbrainz-contact "you@example.com"`  
  Appends contact info to the MusicBrainz User-Agent.
- `--db-path /custom/path.sqlite3`  
  Store the state DB at a custom path.

Example:

```bash
python3 release_genre_sync.py /path/to/music --interactive --musicbrainz-contact "you@example.com"
```

## Genre merge behavior

For each release:

1. Build union of existing genre tags across all tracks in the release.
2. Add missing genres from MusicBrainz (if enabled).
3. Add missing tags from Bandcamp (if enabled).
4. Write final merged genre list to every track in that release.

Semantics:

- It only writes the `genre` field in code.
- It does not intentionally remove existing genre values from the merged set.
- It normalizes and de-duplicates genre values (case/whitespace-insensitive), and may split values that contain `,` or `;`.

## Rate limiting and pacing

- MusicBrainz: at most 1 request per second.
- Bandcamp: random 1-5 second delay between requests.

## Interactive mode

With `--interactive`, when multiple matches are found:

- press `Enter` to accept the top-ranked candidate
- enter `1..N` to pick a specific candidate
- enter `0` to skip that source for the current release

## Resume database

By default, state is stored at:

`<root>/.release_genre_sync.sqlite3`

Each processed release is recorded with:

- release id
- fingerprint
- status (`ok`, `dry_run`, `error`, `interrupted`)
- timestamp and notes

On rerun, releases are skipped only when status is `ok` and fingerprint matches.

## Ctrl-C behavior

The script is interrupt-safe:

- `Ctrl-C` during processing marks the current release as `interrupted`.
- DB connection is closed in `finally`.
- Exit code is `130`.
- On next run, completed `ok` releases are skipped and interrupted releases are retried.

## Notes

- On startup we create a current state of releases in memory, resume database is only written to on modification of that release
- Files missing an `album` tag are ignored.
- Bandcamp lookups depend on current site HTML structure and may occasionally fail.
- If no genres are found anywhere, no genre write is performed.
