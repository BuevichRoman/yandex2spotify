import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from spotipy.exceptions import SpotifyException
from yandex_music import Album, Artist, Track

from importer import Importer, NotFoundException, Progress, build_parser, pick_match

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeSpotify:
    """Отдаёт заранее заданные результаты поиска по очереди, запоминает запросы."""

    def __init__(self, search_results=(), search_error=None, save_errors=(), cover_errors=()):
        self.search_results = search_results if isinstance(search_results, dict) else list(search_results)
        self.search_error = search_error
        self.save_errors = list(save_errors)
        self.cover_errors = list(cover_errors)
        self.queries = []
        self.saved = []
        self.created_playlists = []
        self.cover_uploads = []

    def me(self):
        return {'id': 'user'}

    def current_user_saved_tracks_add(self, tracks):
        if self.save_errors:
            raise self.save_errors.pop(0)
        self.saved.append(list(tracks))

    def user_playlist_create(self, user, name):
        self.created_playlists.append(name)
        return {'id': f'pl-{len(self.created_playlists)}'}

    def playlist_upload_cover_image(self, playlist_id, image_b64):
        self.cover_uploads.append(playlist_id)
        if self.cover_errors:
            raise self.cover_errors.pop(0)

    def user_playlist_add_tracks(self, user, playlist_id, tracks):
        self.saved.append(list(tracks))

    def current_user_saved_albums_add(self, albums):
        self.saved.append(list(albums))

    def search(self, q, type='track', **kwargs):
        self.queries.append(q)
        if self.search_error:
            raise self.search_error
        if isinstance(self.search_results, dict):
            items = self.search_results.get(q, [])
        else:
            items = self.search_results.pop(0) if self.search_results else []
        return {f'{type}s': {'items': items}}


class FakeYandex:
    def __init__(self, tracks=(), albums=(), playlists=()):
        self._tracks = list(tracks)
        self._albums = list(albums)
        self._playlists = list(playlists)

    def users_likes_tracks(self):
        return SimpleNamespace(tracks=[SimpleNamespace(id=t.id, album_id=1) for t in self._tracks])

    def tracks(self, ids):
        return list(self._tracks)

    def users_likes_albums(self):
        return [SimpleNamespace(album=a) for a in self._albums]

    def users_playlists_list(self):
        return list(self._playlists)


class FakeCover:
    type = 'pic'

    def download(self, filename, size):
        Image.new('RGB', (4, 4)).save(filename, 'JPEG')


def ya_playlist(kind, title, tracks, cover=None):
    return SimpleNamespace(kind=kind, title=title, collective=False, cover=cover,
                           fetch_tracks=lambda: [SimpleNamespace(track=t) for t in tracks])


def make_importer(spotify, tmpdir, strict=False, chunk=40, yandex=None):
    progress = Progress(os.path.join(tmpdir, 'progress.jsonl'))
    return Importer(spotify, yandex, [], strict, chunk, progress)


def ya_artists(names):
    return [Artist(id=i, name=n) for i, n in enumerate(names, 1)]


def ya_track(title, artists, duration_ms=200_000, id=1):
    return Track(id=id, title=title, duration_ms=duration_ms, artists=ya_artists(artists))


def ya_album(title, artists, track_count, id=1):
    return Album(id=id, title=title, track_count=track_count, artists=ya_artists(artists))


def sp_album(name, artists, total_tracks, id):
    return {'id': id, 'name': name, 'total_tracks': total_tracks,
            'artists': [{'name': a} for a in artists]}


def sp_track(name, artists, duration_ms, id):
    return {'id': id, 'name': name, 'duration_ms': duration_ms,
            'artists': [{'name': a} for a in artists]}


class PickMatchTrackTest(unittest.TestCase):
    def test_returns_none_when_no_candidates(self):
        self.assertIsNone(pick_match(ya_track('Искала', ['Земфира']), []))

    def test_accepts_first_when_artist_and_duration_match(self):
        item = ya_track('Искала', ['Земфира'], 210_000)
        found = [sp_track('Искала', ['Земфира'], 211_500, 'ok')]
        self.assertEqual(pick_match(item, found), 'ok')

    def test_rejects_karaoke_with_other_artist_and_duration(self):
        item = ya_track('Искала', ['Земфира'], 210_000)
        found = [sp_track('Искала (Karaoke Version)', ['Karaoke Hits'], 245_000, 'bad')]
        self.assertIsNone(pick_match(item, found))

    def test_prefers_candidate_with_both_signals_over_first_result(self):
        item = ya_track('Искала', ['Земфира'], 210_000)
        found = [
            sp_track('Искала - Live', ['Земфира'], 380_000, 'live'),
            sp_track('Искала', ['Земфира'], 210_000, 'studio'),
        ]
        self.assertEqual(pick_match(item, found), 'studio')

    def test_accepts_by_duration_when_artist_is_transliterated(self):
        item = ya_track('Искала', ['Земфира'], 210_000)
        found = [sp_track('Искала', ['Zemfira'], 209_000, 'translit')]
        self.assertEqual(pick_match(item, found), 'translit')

    def test_accepts_by_artist_when_yandex_duration_unknown(self):
        item = ya_track('Искала', ['Земфира'], duration_ms=None)
        found = [sp_track('Искала', ['Земфира'], 210_000, 'ok')]
        self.assertEqual(pick_match(item, found), 'ok')

    def test_artist_match_is_case_insensitive_and_partial(self):
        item = ya_track('Song', ['Kino'], 100_000)
        found = [sp_track('Song', ['KINO (Кино)'], 300_000, 'ok')]
        self.assertEqual(pick_match(item, found), 'ok')

    def test_very_short_artist_name_does_not_match_by_substring(self):
        item = ya_track('Song', ['Ю'], 100_000)
        found = [sp_track('Song', ['Юлия Савичева'], 300_000, 'bad')]
        self.assertIsNone(pick_match(item, found))

    def test_very_short_artist_name_still_matches_exactly(self):
        item = ya_track('Song', ['Ю'], 100_000)
        found = [sp_track('Song', ['Ю'], 300_000, 'ok')]
        self.assertEqual(pick_match(item, found), 'ok')


class PickMatchAlbumTest(unittest.TestCase):
    def test_accepts_when_artist_and_track_count_match(self):
        item = ya_album('Прости меня моя любовь', ['Земфира'], 13)
        found = [sp_album('Прости меня моя любовь', ['Земфира'], 13, 'ok')]
        self.assertEqual(pick_match(item, found), 'ok')

    def test_rejects_tribute_album_with_same_title(self):
        item = ya_album('Прости меня моя любовь', ['Земфира'], 13)
        found = [sp_album('Прости меня моя любовь', ['Tribute Band'], 8, 'bad')]
        self.assertIsNone(pick_match(item, found))

    def test_prefers_edition_with_matching_track_count(self):
        item = ya_album('Album', ['Band'], 10)
        found = [
            sp_album('Album', ['Band'], 16, 'deluxe'),
            sp_album('Album', ['Band'], 10, 'standard'),
        ]
        self.assertEqual(pick_match(item, found), 'standard')

    def test_accepts_by_track_count_when_artist_is_transliterated(self):
        item = ya_album('Прости меня моя любовь', ['Земфира'], 13)
        found = [sp_album('Прости меня моя любовь', ['Zemfira'], 13, 'translit')]
        self.assertEqual(pick_match(item, found), 'translit')

    def test_album_duration_from_yandex_does_not_break_matching(self):
        item = ya_album('Album', ['Band'], 10)
        item.duration_ms = 2_400_000
        found = [sp_album('Album', ['Band'], 10, 'ok')]
        self.assertEqual(pick_match(item, found), 'ok')


class ImportItemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_skips_karaoke_first_result(self):
        spotify = FakeSpotify([[
            sp_track('Искала (Karaoke)', ['Karaoke Hits'], 245_000, 'karaoke'),
            sp_track('Искала', ['Земфира'], 210_000, 'studio'),
        ]])
        importer = make_importer(spotify, self.tmp.name)
        self.assertEqual(importer._import_item(ya_track('Искала', ['Земфира'], 210_000)), 'studio')

    def test_raises_not_found_when_nothing_verifies(self):
        spotify = FakeSpotify([[sp_track('Искала (Karaoke)', ['Karaoke Hits'], 245_000, 'karaoke')]])
        importer = make_importer(spotify, self.tmp.name)
        with self.assertRaises(NotFoundException):
            importer._import_item(ya_track('Искала', ['Земфира'], 210_000))

    def test_retries_with_first_artist_when_multi_artist_result_unverified(self):
        spotify = FakeSpotify([
            [sp_track('Song', ['Someone Else'], 999_000, 'wrong')],
            [sp_track('Song', ['A'], 180_000, 'right')],
        ])
        importer = make_importer(spotify, self.tmp.name)
        self.assertEqual(importer._import_item(ya_track('Song', ['A', 'B'], 180_000)), 'right')
        self.assertEqual(spotify.queries[1], 'A Song')


class AddItemsErrorHandlingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_section(self, spotify, items, key_prefix='like'):
        importer = make_importer(spotify, self.tmp.name)
        not_imported = []
        importer._add_items_to_spotify(items, not_imported, lambda imp, ids: spotify.saved.extend(ids), key_prefix=key_prefix)
        return importer, not_imported

    def test_server_error_does_not_mark_item_done(self):
        spotify = FakeSpotify(search_error=SpotifyException(502, -1, 'bad gateway'))
        importer, not_imported = self.run_section(spotify, [ya_track('Искала', ['Земфира'], id=7)])
        self.assertFalse(importer.progress.is_done('like:7'))
        self.assertEqual(not_imported, ['Земфира - Искала'])

    def test_server_error_on_artist_does_not_crash(self):
        spotify = FakeSpotify(search_error=SpotifyException(500, -1, 'boom'))
        importer, not_imported = self.run_section(spotify, [Artist(id=3, name='Земфира')], key_prefix='artist')
        self.assertEqual(not_imported, ['Земфира'])
        self.assertFalse(importer.progress.is_done('artist:3'))

    def test_auth_error_aborts_run(self):
        spotify = FakeSpotify(search_error=SpotifyException(403, -1, 'forbidden'))
        with self.assertRaises(SpotifyException):
            self.run_section(spotify, [ya_track('Искала', ['Земфира'], id=7)])

    def test_not_found_is_still_marked_done(self):
        spotify = FakeSpotify([[]])
        importer, not_imported = self.run_section(spotify, [ya_track('Искала', ['Земфира'], id=7)])
        self.assertTrue(importer.progress.is_done('like:7'))
        self.assertEqual(not_imported, ['Земфира - Искала'])


class SaveFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, save_errors):
        tracks = [ya_track(f'Song {i}', ['Band'], 100_000, id=i) for i in range(4)]
        results = {f'Band Song {i}': [sp_track(f'Song {i}', ['Band'], 100_000, f'sp{i}')] for i in range(4)}
        spotify = FakeSpotify(results, save_errors=save_errors)
        importer = make_importer(spotify, self.tmp.name, chunk=2, yandex=FakeYandex(tracks=tracks))
        return spotify, importer

    def test_failed_batch_is_dropped_and_later_batches_still_save(self):
        spotify, importer = self.make([SpotifyException(400, -1, 'invalid id')])
        importer.import_likes()
        # items reversed: first batch = Song 3, Song 2 (failed), second = Song 1, Song 0 (saved)
        self.assertEqual(spotify.saved, [['sp1', 'sp0']])
        self.assertEqual(sorted(importer.not_imported['Likes']), ['Band - Song 2', 'Band - Song 3'])
        self.assertFalse(importer.progress.is_done('like:3'))
        self.assertTrue(importer.progress.is_done('like:0'))

    def test_auth_error_on_save_still_aborts(self):
        spotify, importer = self.make([SpotifyException(403, -1, 'forbidden')])
        with self.assertRaises(SpotifyException):
            importer.import_likes()


class PlaylistCoverResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)  # cover file is written to cwd
        self.addCleanup(os.chdir, self.cwd)

    def test_cover_is_retried_on_next_run_without_duplicating_playlist(self):
        playlist = ya_playlist(7, 'Mix', [], cover=FakeCover())
        spotify = FakeSpotify(cover_errors=[SpotifyException(500, -1, 'boom')])
        yandex = FakeYandex(playlists=[playlist])

        with self.assertRaises(SpotifyException):
            make_importer(spotify, self.tmp.name, yandex=yandex).import_playlists()
        make_importer(spotify, self.tmp.name, yandex=yandex).import_playlists()

        self.assertEqual(spotify.created_playlists, ['Mix'])
        self.assertEqual(spotify.cover_uploads, ['pl-1', 'pl-1'])

    def test_playlist_without_cover_does_not_crash(self):
        playlist = ya_playlist(8, 'No cover', [], cover=None)
        spotify = FakeSpotify()
        make_importer(spotify, self.tmp.name, yandex=FakeYandex(playlists=[playlist])).import_playlists()
        self.assertEqual(spotify.created_playlists, ['No cover'])
        self.assertEqual(spotify.cover_uploads, [])


class EndpointBatchLimitTest(unittest.TestCase):
    """Spotify: /me/albums принимает максимум 20 id, /me/tracks — 50."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_albums_are_saved_in_batches_of_at_most_20(self):
        albums = [ya_album(f'Album {i}', ['Band'], 10, id=i) for i in range(25)]
        results = [[sp_album(f'Album {i}', ['Band'], 10, f'sp{i}')] for i in range(25)]
        spotify = FakeSpotify(results)
        importer = make_importer(spotify, self.tmp.name, chunk=40, yandex=FakeYandex(albums=albums))
        importer.import_albums()
        self.assertEqual([len(b) for b in spotify.saved], [20, 5])

    def test_tracks_are_saved_in_batches_of_at_most_50(self):
        tracks = [ya_track(f'Song {i}', ['Band'], 100_000, id=i) for i in range(60)]
        results = [[sp_track(f'Song {i}', ['Band'], 100_000, f'sp{i}')] for i in range(60)]
        spotify = FakeSpotify(results)
        importer = make_importer(spotify, self.tmp.name, chunk=60, yandex=FakeYandex(tracks=tracks))
        importer.import_likes()
        self.assertEqual([len(b) for b in spotify.saved], [50, 10])


class CliTest(unittest.TestCase):
    BASE = ['-u', 'user', '--id', 'cid', '--secret', 'sec', '-t', 'tok']

    def test_strict_flag_is_boolean_switch(self):
        self.assertTrue(build_parser().parse_args(self.BASE + ['-S']).strict_artists_search)
        self.assertFalse(build_parser().parse_args(self.BASE).strict_artists_search)

    def test_chunk_accepts_playlist_maximum(self):
        self.assertEqual(build_parser().parse_args(self.BASE + ['-c', '100']).chunk, 100)

    ENV = {'SPOTIFY_CLIENT_ID': 'env-id', 'SPOTIFY_CLIENT_SECRET': 'env-secret', 'YANDEX_TOKEN': 'env-token'}

    def test_secrets_fall_back_to_env(self):
        with patch.dict(os.environ, self.ENV, clear=True):
            args = build_parser().parse_args(['-u', 'user'])
        self.assertEqual((args.id, args.secret, args.token), ('env-id', 'env-secret', 'env-token'))

    def test_cli_secret_overrides_env(self):
        with patch.dict(os.environ, self.ENV, clear=True):
            args = build_parser().parse_args(['-u', 'user', '--secret', 'cli-secret'])
        self.assertEqual(args.secret, 'cli-secret')

    def test_spotify_creds_still_required_without_env(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            build_parser().parse_args(['-u', 'user'])

    def test_unexpected_error_prints_traceback_and_exits_nonzero(self):
        # без -t и -j скрипт бросает ValueError уже внутри try — сеть не нужна
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, os.path.join(PROJECT_ROOT, 'importer.py'), '-u', 'user', '--id', 'cid', '--secret', 'sec'],
                cwd=tmp, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Traceback', result.stderr)
        self.assertIn('ValueError', result.stderr)


class NotFoundAcrossRunsTest(unittest.TestCase):
    """Отчёт «не найдено» должен включать треки из предыдущих запусков, а не только текущего."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_report_includes_not_found_from_previous_run(self):
        tracks = [ya_track('Lost', ['Band'], 100_000, id=1), ya_track('Found', ['Band'], 100_000, id=2)]
        results = {'Band Found': [sp_track('Found', ['Band'], 100_000, 'sp2')]}
        yandex = FakeYandex(tracks=tracks)

        make_importer(FakeSpotify(results), self.tmp.name, yandex=yandex).import_likes()
        second = make_importer(FakeSpotify(results), self.tmp.name, yandex=yandex)
        second.import_likes()

        self.assertEqual(second.not_imported['Likes'], ['Band - Lost'])
        self.assertEqual(len(second.progress.done), 2)


class PickMatchPassthroughTest(unittest.TestCase):
    def test_string_query_takes_first_result(self):
        found = [{'id': 'first'}, {'id': 'second'}]
        self.assertEqual(pick_match('Artist Title', found), 'first')

    def test_artist_takes_first_result(self):
        found = [{'id': 'first', 'name': 'Whatever'}]
        self.assertEqual(pick_match(Artist(id=1, name='Земфира'), found), 'first')


if __name__ == '__main__':
    unittest.main()
