import os
import re
import sys
import json
import argparse
import logging
from base64 import b64encode
from time import sleep
from datetime import datetime, timedelta

import spotipy
from PIL import Image
from requests.exceptions import ReadTimeout
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth
from yandex_music import Client, Album, Artist

REDIRECT_URI = 'https://open.spotify.com'
MAX_REQUEST_RETRIES = 5
# 429 с Retry-After больше этого значения = суточная блокировка Development Mode,
# ждать её в цикле бессмысленно — выходим, прогресс уже в журнале
MAX_RATE_LIMIT_WAIT = 300
# 429 исключён намеренно: пусть urllib3 его НЕ перехватывает, иначе spotipy
# отдаёт исключение без заголовка Retry-After и понять длительность блокировки
# уже невозможно. 5xx по-прежнему ретраятся штатно.
RETRY_STATUS_FORCELIST = (500, 502, 503, 504)
# Разница длительности, при которой считаем, что это та же запись (не live/karaoke/cover)
MATCH_DURATION_TOLERANCE_MS = 3000
# Максимум id за один запрос по документации Spotify — превышение даёт 400
MAX_IDS_SAVED_TRACKS = 50
MAX_IDS_SAVED_ALBUMS = 20
MAX_IDS_FOLLOW_ARTISTS = 50
MAX_IDS_PLAYLIST_ADD = 100

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Progress:
    """Журнал уже обработанных объектов. Append-only JSONL, переживает падения."""

    def __init__(self, path):
        self.path = path
        self.done = set()
        self.playlists = {}
        # key -> label: чего в Spotify нет; хранится, чтобы итоговый отчёт был полным после resume
        self.not_found = {}

        if os.path.exists(path):
            with open(path, 'r', encoding='UTF-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get('t') == 'item':
                        self.done.add(rec['k'])
                        if rec.get('nf') is not None:
                            self.not_found[rec['k']] = rec['nf']
                    elif rec.get('t') == 'playlist':
                        self.playlists[str(rec['kind'])] = rec['id']
            logger.info(f'Progress loaded: {len(self.done)} items, {len(self.playlists)} playlists')

    def _append(self, rec):
        with open(self.path, 'a', encoding='UTF-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())

    def is_done(self, key):
        return key is not None and key in self.done

    def mark(self, key, not_found=None):
        if key is None or key in self.done:
            return
        self.done.add(key)
        rec = {'t': 'item', 'k': key}
        if not_found is not None:
            self.not_found[key] = not_found
            rec['nf'] = not_found
        self._append(rec)

    def playlist_id(self, kind):
        return self.playlists.get(str(kind))

    def set_playlist(self, kind, spotify_id):
        self.playlists[str(kind)] = spotify_id
        self._append({'t': 'playlist', 'kind': str(kind), 'id': spotify_id})


def label_key(text):
    """Ключ для сравнения строки вида "Артист - Название" из лога."""
    return re.sub(r'\s+', ' ', str(text or '')).strip().casefold()


def _names_overlap(yandex_artists, spotify_artists):
    """Совпадение имён точное, либо подстрокой для случаев вида "KINO (Кино)".
    Подстрока только от 3 символов — иначе "Ю" совпадёт с любой Юлией."""
    ya = [label_key(a.name) for a in yandex_artists]
    sp = [label_key(a['name']) for a in spotify_artists]
    for y in ya:
        for s in sp:
            if not y or not s:
                continue
            if y == s:
                return True
            if min(len(y), len(s)) >= 3 and (y in s or s in y):
                return True
    return False


def pick_match(item, candidates):
    """Выбрать из результатов поиска Spotify тот, что реально соответствует объекту Яндекса.

    Треки: длительность ±3с и/или совпадение артиста, название — только как tie-breaker.
    Альбомы: то же, но вместо длительности — число треков (у Spotify нет duration у альбома).
    Одного названия недостаточно — так отсеиваются караоке/каверы/live-версии/трибьюты.
    Строки (JSON) и артисты — без проверки, берётся первый результат.
    """
    if not candidates:
        return None

    if isinstance(item, (str, Artist)):
        return candidates[0]['id']

    def size_matches(cand):
        if isinstance(item, Album):
            return item.track_count is not None and cand.get('total_tracks') == item.track_count
        return item.duration_ms is not None and abs(cand['duration_ms'] - item.duration_ms) <= MATCH_DURATION_TOLERANCE_MS

    best_id, best_score = None, 0
    for cand in candidates:
        score = 0
        if size_matches(cand):
            score += 2
        if _names_overlap(item.artists, cand['artists']):
            score += 2
        if label_key(item.title) == label_key(cand['name']):
            score += 1
        if score > best_score:
            best_id, best_score = cand['id'], score

    return best_id if best_score >= 2 else None


def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def encode_file_base64_jpeg(filename):
    img = Image.open(filename)
    if img.format != 'JPEG':
        img.convert('RGB').save(filename, 'JPEG')

    with open(filename, 'rb') as f:
        return b64encode(f.read())


def handle_spotify_exception(func):
    def wrapper(*args, **kwargs):
        retry = 1
        while True:
            try:
                return func(*args, **kwargs)
            except SpotifyException as exception:
                if exception.http_status != 429:
                    raise exception

                headers = exception.headers or {}
                raw = headers.get('retry-after') or headers.get('Retry-After')

                if raw is None:
                    # заголовка нет — длительность неизвестна, крутиться вслепую нельзя
                    raise RateLimitExceeded(None)

                retry_after = int(float(raw))

                if retry_after > MAX_RATE_LIMIT_WAIT:
                    raise RateLimitExceeded(retry_after)

                logger.info(f'Rate limited, sleeping {retry_after} s...')
                sleep(retry_after + 1)
            except ReadTimeout as exception:
                logger.info(f'Read timed out. Retrying #{retry}...')

                if retry > MAX_REQUEST_RETRIES:
                    logger.info('Max retries reached.')
                    raise exception

                logger.info('Trying again...')
                retry += 1

    return wrapper


class RateLimitExceeded(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        self.resume_at = datetime.now() + timedelta(seconds=retry_after) if retry_after else None
        super().__init__(f'Rate limit: retry after {retry_after} s')


class NotFoundException(SpotifyException):
    def __init__(self, item_name):
        self.item_name = item_name


class Importer:
    def __init__(self, spotify_client, yandex_client: Client, ignore_list, strict_search, chunk_size, progress, start_after=None):
        self.spotify_client = spotify_client
        self.yandex_client = yandex_client
        self.chunk_size = chunk_size
        self.progress = progress
        self._start_after = start_after

        self._importing_items = {
            'likes': self.import_likes,
            'playlists': self.import_playlists,
            'albums': self.import_albums,
            'artists': self.import_artists
        }

        for item in ignore_list:
            del self._importing_items[item]

        self._strict_search = strict_search

        self.user = handle_spotify_exception(spotify_client.me)()['id']
        logger.info(f'User ID: {self.user}')
        logger.info(f'Chunk size: {self.chunk_size}')

        self.not_imported = {}

    def _item_label(self, item):
        if isinstance(item, str):
            return item
        if isinstance(item, Artist):
            return item.name
        return f'{", ".join([artist.name for artist in item.artists])} - {item.title}'

    def _apply_start_after(self, items):
        """Отрезать всё до маркера включительно. Срабатывает один раз."""
        target = label_key(self._start_after)
        for idx, item in enumerate(items):
            if label_key(self._item_label(item)) == target:
                self._start_after = None
                logger.info(f'Start-after marker found at #{idx + 1}, resuming from #{idx + 2}')
                return items[idx + 1:]

        logger.warning(
            f'Start-after marker "{target}" not found in this section — processing it in full. '
            f'Use -i to skip sections that are already done.')
        return items

    def _import_item(self, item):
        # if the item is a string, it is a query from the JSON file
        if isinstance(item, str):
            query = item
            item_name = item
            type_ = 'track'  # Default type for string items
            artists = []  # Default artists for string items
        # else it is an object from Yandex
        else:
            type_ = item.__class__.__name__.casefold()
            item_name = self._item_label(item)
            artists = item.artists if not isinstance(item, Artist) else []  # Artists for Yandex items

            # A workaround for when track name is too long (100+ characters) there is an exception happening
            # because spotify API can not process it.
            if len(item_name) > 100:
                item_name = item_name[:100]
                logger.info('Name too long... Trimming to 100 characters. May affect search accuracy')

            query = item_name.replace('- ', '')

        logger.info(f'Importing {type_}: {item_name}...')
        logger.info(f'Searching "{query}"...')
        found_items = handle_spotify_exception(self.spotify_client.search)(query, type=type_)[f'{type_}s']['items']
        spotify_id = pick_match(item, found_items)

        # ничего подходящего среди результатов — пробуем без второстепенных артистов
        if spotify_id is None and not self._strict_search and not isinstance(item, Artist) and len(artists) > 1:
            query = f'{artists[0].name} {item.title}'
            logger.info(f'Searching "{query}"...')
            found_items = handle_spotify_exception(self.spotify_client.search)(query, type=type_)[f'{type_}s']['items']
            spotify_id = pick_match(item, found_items)

        if spotify_id is None:
            raise NotFoundException(item_name)

        return spotify_id

    def _add_items_to_spotify(self, items, not_imported_section, save_items_callback, key_prefix=None, max_ids=MAX_IDS_SAVED_TRACKS):
        buffer = []
        skipped = 0
        saved = 0
        chunk_size = min(self.chunk_size, max_ids)

        items.reverse()

        if self._start_after:
            items = self._apply_start_after(items)

        def flush():
            nonlocal saved
            if not buffer:
                return
            try:
                save_items_callback(self, [spotify_id for spotify_id, _, _ in buffer])
            except SpotifyException as exception:
                if exception.http_status in (401, 403):
                    raise
                # батч не записался — сбрасываем его целиком без отметки, иначе он будет
                # расти и падать на каждом следующем треке; повторим при следующем запуске
                logger.warning(f'Batch of {len(buffer)} not saved (Spotify {exception.http_status}: {exception.msg}) — will retry next run')
                not_imported_section.extend(label for _, _, label in buffer)
                buffer.clear()
                return
            # отмечаем только после подтверждённой записи в Spotify
            for _, done_key, _ in buffer:
                self.progress.mark(done_key)
            saved += len(buffer)
            buffer.clear()

        for item in items:
            key = None
            if key_prefix and not isinstance(item, str) and getattr(item, 'id', None) is not None:
                key = f'{key_prefix}:{item.id}'
                if self.progress.is_done(key):
                    skipped += 1
                    if key in self.progress.not_found:
                        not_imported_section.append(self.progress.not_found[key])
                    continue

            try:
                spotify_id = self._import_item(item)
                buffer.append((spotify_id, key, self._item_label(item)))
                logger.info('OK')

                if len(buffer) >= chunk_size:
                    flush()

            except NotFoundException as exception:
                not_imported_section.append(exception.item_name)
                # не искать снова то, чего в Spotify нет; название — для итогового отчёта
                self.progress.mark(key, not_found=exception.item_name)
                logger.warning('NO')
            except SpotifyException as exception:
                # 401/403 — проблема авторизации, а не трека: дальше всё упадёт так же
                if exception.http_status in (401, 403):
                    raise
                # прочее (5xx после ретраев, 400) — НЕ помечаем, повторим при следующем запуске
                not_imported_section.append(self._item_label(item))
                logger.warning(f'NO (Spotify {exception.http_status}: {exception.msg}) — will retry next run')

        flush()

        if skipped:
            logger.info(f'Skipped {skipped} already imported items')
        if not saved:
            logger.info('No valid Spotify items to add.')



    def import_likes(self):
        self.not_imported['Likes'] = []

        likes_tracks = self.yandex_client.users_likes_tracks().tracks
        tracks = self.yandex_client.tracks([f'{track.id}:{track.album_id}' for track in likes_tracks if track.album_id])
        logger.info('Importing liked tracks...')

        def save_tracks_callback(importer, spotify_tracks):
            logger.info(f'Saving {len(spotify_tracks)} tracks...')
            handle_spotify_exception(importer.spotify_client.current_user_saved_tracks_add)(spotify_tracks)
            logger.info('OK')

        self._add_items_to_spotify(tracks, self.not_imported['Likes'], save_tracks_callback, key_prefix='like', max_ids=MAX_IDS_SAVED_TRACKS)

    def import_playlists(self):
        playlists = self.yandex_client.users_playlists_list()
        for playlist in playlists:
            spotify_playlist_id = self.progress.playlist_id(playlist.kind)

            if spotify_playlist_id:
                logger.info(f'Reusing already created playlist {playlist.title}')
            else:
                spotify_playlist = handle_spotify_exception(self.spotify_client.user_playlist_create)(self.user, playlist.title)
                spotify_playlist_id = spotify_playlist['id']
                self.progress.set_playlist(playlist.kind, spotify_playlist_id)

            # обложка журналируется отдельно: если её загрузка упала, плейлист уже есть
            # и при повторном запуске не задублируется, а обложку догрузим
            cover_key = f'cover:{playlist.kind}'
            if playlist.cover and playlist.cover.type == 'pic' and not self.progress.is_done(cover_key):
                filename = f'{playlist.kind}-cover'
                playlist.cover.download(filename, size='400x400')

                handle_spotify_exception(self.spotify_client.playlist_upload_cover_image)(spotify_playlist_id, encode_file_base64_jpeg(filename))
                self.progress.mark(cover_key)

            logger.info(f'Importing playlist {playlist.title}...')

            self.not_imported[playlist.title] = []

            playlist_tracks = playlist.fetch_tracks()
            if not playlist.collective:
                tracks = [track.track for track in playlist_tracks]
            elif playlist.collective and playlist_tracks:
                tracks = self.yandex_client.tracks([track.track_id for track in playlist_tracks])
            else:
                tracks = []

            def save_tracks_callback(importer, spotify_tracks):
                logger.info(f'Saving {len(spotify_tracks)} tracks in playlist {playlist.title}...')
                handle_spotify_exception(importer.spotify_client.user_playlist_add_tracks)(importer.user,
                                                                                           spotify_playlist_id,
                                                                                           spotify_tracks)
                logger.info('OK')

            self._add_items_to_spotify(tracks, self.not_imported[playlist.title], save_tracks_callback, key_prefix=f'pl:{playlist.kind}', max_ids=MAX_IDS_PLAYLIST_ADD)

    def import_albums(self):
        self.not_imported['Albums'] = []

        likes_albums = self.yandex_client.users_likes_albums()
        albums = [album.album for album in likes_albums]
        logger.info('Importing albums...')

        def save_albums_callback(importer, spotify_albums):
            logger.info(f'Saving {len(spotify_albums)} albums...')
            handle_spotify_exception(importer.spotify_client.current_user_saved_albums_add)(spotify_albums)
            logger.info('OK')

        self._add_items_to_spotify(albums, self.not_imported['Albums'], save_albums_callback, key_prefix='album', max_ids=MAX_IDS_SAVED_ALBUMS)

    def import_artists(self):
        self.not_imported['Artists'] = []

        likes_artists = self.yandex_client.users_likes_artists()
        artists = [artist.artist for artist in likes_artists]
        logger.info('Importing artists...')

        def save_artists_callback(importer, spotify_artists):
            logger.info(f'Saving {len(spotify_artists)} artists...')
            handle_spotify_exception(importer.spotify_client.user_follow_artists)(spotify_artists)
            logger.info('OK')

        self._add_items_to_spotify(artists, self.not_imported['Artists'], save_artists_callback, key_prefix='artist', max_ids=MAX_IDS_FOLLOW_ARTISTS)

    def import_all(self):
        for item in self._importing_items.values():
            item()

        self.print_not_imported()

    def print_not_imported(self):
        logger.error('Not imported items:')
        for section, items in self.not_imported.items():
            logger.info(f'{section}:')
            for item in items:
                logger.info(item)

    def import_from_json(self, file_path):
        with open(file_path, 'r', encoding='UTF-8') as file:
            tracks = json.load(file)

        spotify_tracks = []
        not_imported = []

        for track in tracks:
            query = f'{track["artist"]} {track["track"]}'

            try:
                spotify_track_id = self._import_item(query)
                spotify_tracks.append(spotify_track_id)
                logger.info('OK')
            except NotFoundException as exception:
                not_imported.append(exception.item_name)
                logger.warning('NO')
            except SpotifyException:
                not_imported.append(query)
                logger.warning('NO')

        # Create a new playlist
        playlist_name = 'Imported from JSON'
        playlist = handle_spotify_exception(self.spotify_client.user_playlist_create)(self.user, playlist_name)

        # Add tracks to the new playlist
        for chunk in chunks(spotify_tracks, min(self.chunk_size, MAX_IDS_PLAYLIST_ADD)):
            logger.info(f'Saving {len(chunk)} tracks...')
            handle_spotify_exception(self.spotify_client.user_playlist_add_tracks)(self.user, playlist['id'], chunk)
            logger.info('OK')

        logger.error('Not imported tracks:')
        for track in not_imported:
            logger.info(track)


def build_parser():
    parser = argparse.ArgumentParser(description='Creates a playlist for user')
    parser.add_argument('-u', '-s', '--spotify', required=True, help='Username at spotify.com')

    # секреты можно не светить в argv/истории шелла — берутся из env, если флаг не передан
    env_id = os.environ.get('SPOTIFY_CLIENT_ID')
    env_secret = os.environ.get('SPOTIFY_CLIENT_SECRET')
    env_token = os.environ.get('YANDEX_TOKEN')

    spotify_oauth = parser.add_argument_group('spotify_oauth')
    spotify_oauth.add_argument('--id', required=env_id is None, default=env_id,
                               help='Client ID of your Spotify app (or env SPOTIFY_CLIENT_ID)')
    spotify_oauth.add_argument('--secret', required=env_secret is None, default=env_secret,
                               help='Client Secret of your Spotify app (or env SPOTIFY_CLIENT_SECRET)')

    parser.add_argument('-t', '--token', default=env_token,
                        help='Token from music.yandex.com account (or env YANDEX_TOKEN)')

    parser.add_argument('-i', '--ignore', nargs='+', help='Don\'t import some items',
                        choices=['likes', 'playlists', 'albums', 'artists'], default=[])

    parser.add_argument('-T', '--timeout', help='Request timeout for spotify', type=float, default=10)

    parser.add_argument('-S', '--strict-artists-search', action='store_true',
                        help='Don\'t retry with only the first artist when a multi-artist track isn\'t found')

    parser.add_argument('-j', '--json-path', help='JSON file to import tracks from')

    parser.add_argument('--start-after', metavar='"Artist - Title"',
                        help='Skip everything up to and including this item, then resume. '
                             'Takes the exact string from the log line "Importing track: ..."')

    parser.add_argument('-p', '--progress', default='progress.jsonl',
                        help='Path to the progress journal (default: progress.jsonl)')

    parser.add_argument(
        '-c', '--chunk',
        help=f'Batch size for Spotify save/add requests (1..{MAX_IDS_PLAYLIST_ADD}); '
             'capped per endpoint: albums 20, tracks/artists 50, playlists 100',
        type=int,
        default=40
    )
    return parser


if __name__ == '__main__':
    arguments = build_parser().parse_args()

    if not 1 <= arguments.chunk <= MAX_IDS_PLAYLIST_ADD:
        raise ValueError(f'The -c/--chunk argument must be between 1 and {MAX_IDS_PLAYLIST_ADD}.')

    try:
        auth_manager = SpotifyOAuth(
            client_id=arguments.id,
            client_secret=arguments.secret,
            redirect_uri=REDIRECT_URI,
            scope='playlist-modify-public, user-library-modify, user-follow-modify, ugc-image-upload',
            username=arguments.spotify,
        )

        if arguments.token is None and arguments.json_path is None:
            raise ValueError('Either the -t (token) or -j (json_path) argument must be specified.')

        spotify_client_ = spotipy.Spotify(auth_manager=auth_manager, requests_timeout=arguments.timeout,
                                          status_forcelist=RETRY_STATUS_FORCELIST)
        yandex_client_ = None

        if arguments.token:
            yandex_client_ = Client(arguments.token)
            yandex_client_.init()

        progress_ = Progress(arguments.progress)

        importer_instance = Importer(spotify_client_, yandex_client_, arguments.ignore, arguments.strict_artists_search, arguments.chunk, progress_, arguments.start_after)

        if arguments.json_path:
            importer_instance.import_from_json(arguments.json_path)
        else:
            importer_instance.import_all()
    except RateLimitExceeded as e:
        logger.error('=' * 60)
        if e.retry_after:
            logger.error(f'Spotify выдал лимит: ждать {e.retry_after} с (~{e.retry_after / 3600:.1f} ч)')
            logger.error(f'Возвращаться после {e.resume_at.strftime("%Y-%m-%d %H:%M:%S")}')
        else:
            logger.error('Spotify выдал 429 без заголовка Retry-After — длительность неизвестна.')
            logger.error('Скорее всего суточная блокировка Development Mode, вернись через сутки.')
        logger.error(f'Прогресс сохранён в {arguments.progress} — запусти ту же команду снова')
        logger.error('=' * 60)
        sys.exit(2)
    except KeyboardInterrupt:
        logger.warning(f'Прервано. Прогресс сохранён в {arguments.progress}')
        sys.exit(130)
    except Exception:
        logger.exception('An unexpected error occurred')
        sys.exit(1)
