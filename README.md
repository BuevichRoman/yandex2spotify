# yandex2spotify

> Русский гайд со всеми шагами: [GUIDE.md](GUIDE.md)

Moves your whole Yandex Music library to Spotify: liked tracks (in the original order), playlists with covers, liked albums and followed artists.

A reworked fork of [MarshalX/yandex2spotify](https://github.com/MarshalX/yandex2spotify). What's different:

- **Resumable.** Progress is journaled to `progress.jsonl` after every confirmed write. Spotify's daily quota for development-mode apps (~700 tracks/day) stops the run with the exact time to come back; run the same command again and it continues where it left off. Playlists are not duplicated, covers are retried if the upload failed.
- **Verified matching.** Instead of taking the first search result, tracks are checked by duration (±3 s) and artist name, albums by track count — karaoke versions, covers and live recordings don't sneak in. Anything that doesn't verify goes to the not-found report (which is cumulative across runs).
- **No silent data loss.** Transient Spotify errors don't mark items as done; auth errors (401/403) abort the run instead of failing 3 000 times.
- **Spotify request limits per endpoint** (albums 20, tracks/artists 50, playlist 100) are respected regardless of `--chunk`.
- **Secrets from the environment**, so tokens stay out of shell history.
- **Tests** (`unittest`, no extra dependencies).

Requires Python 3.9+ and a Spotify account. Spotify requires the *owner* of a development-mode app to have Premium.

## Setup

**1. Spotify app.** Go to the [Developer Dashboard](https://developer.spotify.com/dashboard) → *Create app*. Add `https://open.spotify.com` under *Redirect URIs*, tick *Web API*, save. Copy *Client ID* and *Client Secret* from *Settings*.

**2. Yandex Music token.** Open

```
https://oauth.yandex.ru/authorize?response_type=token&client_id=23cabbbdc6cd418abb4b39c32c41195d
```

log in, and copy the `access_token=…` value from the address bar of the page you land on. This is the OAuth client of Yandex Music itself — there is no way to register a third-party app with a music scope, see [this discussion](https://github.com/MarshalX/yandex-music-api/discussions/513).

**3. Install.**

```bash
pip install -r requirements.txt
```

## Usage

```bash
export SPOTIFY_CLIENT_ID=...
export SPOTIFY_CLIENT_SECRET=...
export YANDEX_TOKEN=...
python3 importer.py -u <any-name>
```

`-u` is just the name of the Spotify token cache file. The same values can be passed as `--id`, `--secret` and `-t` instead of environment variables.

On the first run a browser opens with Spotify's consent screen. After *Agree* you are redirected to `open.spotify.com/?code=…` — copy that URL right away (the web player may strip it after loading) and paste it into the terminal prompt.

When the daily quota is hit you'll see:

```
Spotify выдал лимит: ждать 86400 с (~24.0 ч)
Возвращаться после 2026-09-13 14:07:41
Прогресс сохранён в progress.jsonl — запусти ту же команду снова
```

Run the same command after that time. The not-found report is printed at the end of every run and includes items from previous runs.

### Options

| Flag | Meaning |
|---|---|
| `-i likes playlists albums artists` | Skip sections (any subset) |
| `-p FILE` | Progress journal path (default `progress.jsonl`) — use one per account |
| `--start-after "Artist - Title"` | Skip everything up to and including this item |
| `-c N` | Batch size for save requests, 1–100 (capped per endpoint) |
| `-S` | Don't retry a multi-artist track with only the first artist |
| `-T SEC` | Spotify request timeout |
| `-j FILE` | Import from a JSON list `[{"artist": "...", "track": "..."}]` into a new playlist |

### Tests

```bash
python -m unittest discover -s tests -t .
```

## License

MIT. Original work © 2020 Pavel Lamonov ([MarshalX](https://github.com/MarshalX)); modifications © 2026 Roman Buevich. See [LICENSE.md](LICENSE.md).
