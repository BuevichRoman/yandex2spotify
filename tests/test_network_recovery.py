import json
import os
import tempfile
import unittest
from collections import Counter
from contextlib import ExitStack
from unittest.mock import patch

import requests
from yandex_music.exceptions import BadRequestError, NotFoundError, TimedOutError, UnauthorizedError

import importer
from tests.test_importer import FakeSpotify, FakeYandex, make_importer, sp_track, ya_playlist, ya_track

ACCOUNT = {'result': {'account': {'uid': 123, 'login': 'test',
                                'now': '2026-11-05T13:00:00+03:00', 'serviceAvailable': True}}}


def response(body, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    return result


class SpotifyPayloadTest(unittest.TestCase):
    PLAYLIST_ID = '3cEYpjA9oz9GiPac4AsH4n'
    TRACK_ID = '4iV5W9uYEdYUVa79Axb7Rh'

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

    def make_client(self):
        # Keep Spotipy's real request serialization; replace only HTTP transport.
        client = importer.SpotifyClient(auth='test-token')
        self.addCleanup(client._session.close)
        self.transport = self.stack.enter_context(patch.object(client._session, 'request'))
        self.transport.return_value = response({'snapshot_id': 'saved'})
        return client

    def assert_payload(self):
        args, kwargs = self.transport.call_args
        self.assertEqual(args, ('POST', f'https://api.spotify.com/v1/playlists/{self.PLAYLIST_ID}/items'))
        self.assertEqual(json.loads(kwargs['data']), {'uris': [f'spotify:track:{self.TRACK_ID}']})

    def test_playlist_request_has_object_body_and_normalized_uris(self):
        client = self.make_client()
        result = client.playlist_add_items(self.PLAYLIST_ID, [self.TRACK_ID], position=0)
        args, kwargs = self.transport.call_args
        self.assertEqual(json.loads(kwargs['data']), {
            'uris': [f'spotify:track:{self.TRACK_ID}'], 'position': 0,
        })
        self.assertEqual(args[1], f'https://api.spotify.com/v1/playlists/{self.PLAYLIST_ID}/items')
        self.assertEqual(result, {'snapshot_id': 'saved'})

    def test_yandex_playlist_import_uses_correct_http_payload(self):
        client = self.make_client()
        client.me = lambda: {'id': 'user'}
        client.search = lambda *a, **kw: {'tracks': {'items': [
            sp_track('Song', ['Band'], 100000, self.TRACK_ID),
        ]}}
        playlist = ya_playlist(7, 'Mix', [ya_track('Song', ['Band'], 100000)])
        with tempfile.TemporaryDirectory() as tmp:
            progress = importer.Progress(os.path.join(tmp, 'progress.jsonl'))
            progress.set_playlist(7, self.PLAYLIST_ID)
            imp = importer.Importer(client, FakeYandex(playlists=[playlist]), [], False, 40, progress)
            imp.import_playlists()
            self.assert_payload()
            self.assertTrue(importer.Progress(progress.path).is_done('pl:7:1'))

    def test_json_import_uses_correct_http_payload(self):
        client = self.make_client()
        client.me = lambda: {'id': 'user'}
        client.search = lambda *a, **kw: {'tracks': {'items': [{'id': self.TRACK_ID}]}}
        client.current_user_playlist_create = lambda name: {'id': self.PLAYLIST_ID}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'tracks.json')
            with open(path, 'w') as f:
                json.dump([{'artist': 'Band', 'track': 'Song'}], f)
            make_importer(client, tmp).import_from_json(path)
            self.assert_payload()


class YandexRetryTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.transport = self.stack.enter_context(patch('requests.request'))
        self.sleep = self.stack.enter_context(patch('importer.sleep'))

    def make_client(self):
        return importer.create_yandex_client('test-token')

    def test_init_retries_timeout_and_connection_failure(self):
        self.transport.side_effect = [
            requests.ReadTimeout('slow'), requests.ConnectionError('offline'),
            response(ACCOUNT),
        ]
        client = self.make_client()
        self.assertEqual(client.account_uid, 123)
        self.assertEqual(self.transport.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])
        self.assertEqual(self.transport.call_args.kwargs['timeout'], 15)

    def test_reads_after_init_also_retry(self):
        self.transport.side_effect = [
            response(ACCOUNT),
            requests.ReadTimeout('slow'),
            response({'result': {'library': {'uid': 123, 'revision': 1, 'tracks': []}}}),
        ]
        self.assertEqual(self.make_client().users_likes_tracks().tracks, [])
        self.assertEqual(self.transport.call_count, 3)

    def test_persistent_timeout_stops_after_five_retries(self):
        self.transport.side_effect = requests.ReadTimeout('slow')
        with self.assertRaises(TimedOutError):
            self.make_client()
        self.assertEqual(self.transport.call_count, 6)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2, 4, 8, 16])

    def test_permanent_errors_are_not_retried(self):
        for status, error in ((400, BadRequestError), (401, UnauthorizedError),
                              (403, UnauthorizedError), (404, NotFoundError)):
            with self.subTest(status=status):
                self.transport.reset_mock()
                self.sleep.reset_mock()
                self.transport.side_effect = None
                self.transport.return_value = response({'error': {'message': 'no'}}, status)
                with self.assertRaises(error):
                    self.make_client()
                self.assertEqual(self.transport.call_count, 1)
                self.sleep.assert_not_called()


class ProgressRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tracks = [ya_track(f'Song {i}', ['Band'], 100000, id=i) for i in range(5)]
        self.results = {f'Band Song {i}': [sp_track(f'Song {i}', ['Band'], 100000, f'sp{i}')]
                        for i in range(5)}

    def test_failed_playlist_batch_is_retried_without_losing_or_repeating_tracks(self):
        from spotipy.exceptions import SpotifyException
        sp = FakeSpotify(self.results)
        ya = FakeYandex(playlists=[ya_playlist(7, 'Mix', self.tracks)])
        original_save = sp.playlist_add_items
        attempts = 0

        def fail_second_batch(playlist_id, tracks):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise SpotifyException(404, -1, 'Resource not found')
            original_save(playlist_id, tracks)

        sp.playlist_add_items = fail_second_batch
        first = make_importer(sp, self.tmp.name, chunk=2, yandex=ya)
        first.import_playlists()
        progress = importer.Progress(first.progress.path)
        self.assertEqual(progress.done, {'pl:7:4', 'pl:7:3', 'pl:7:0'})
        self.assertEqual(first.not_imported['Mix'], ['Band - Song 2', 'Band - Song 1'])

        make_importer(sp, self.tmp.name, chunk=2, yandex=ya).import_playlists()
        self.assertEqual(Counter(t for batch in sp.saved for t in batch),
                         Counter(['sp0', 'sp1', 'sp2', 'sp3', 'sp4']))
        self.assertEqual(sp.created_playlists, ['Mix'])
        self.assertEqual(len(importer.Progress(first.progress.path).done), 5)

        saved_before = list(sp.saved)
        make_importer(sp, self.tmp.name, chunk=2, yandex=ya).import_playlists()
        self.assertEqual(sp.saved, saved_before)
        self.assertEqual(sp.created_playlists, ['Mix'])

    def test_likes_resume_after_save_timeout_keeps_confirmed_batches(self):
        sp = FakeSpotify(self.results)
        ya = FakeYandex(tracks=self.tracks)
        original_save = sp.current_user_saved_tracks_add
        attempts = 0

        def fail_after_first_batch(tracks):
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                raise requests.ReadTimeout('not saved')
            original_save(tracks)

        sp.current_user_saved_tracks_add = fail_after_first_batch
        first = make_importer(sp, self.tmp.name, chunk=2, yandex=ya)
        with self.assertRaises(requests.ReadTimeout):
            first.import_likes()
        self.assertEqual(importer.Progress(first.progress.path).done, {'like:4', 'like:3'})

        sp.current_user_saved_tracks_add = original_save
        make_importer(sp, self.tmp.name, chunk=2, yandex=ya).import_likes()
        self.assertEqual(Counter(t for batch in sp.saved for t in batch),
                         Counter(['sp0', 'sp1', 'sp2', 'sp3', 'sp4']))
        self.assertEqual(importer.Progress(first.progress.path).done, {f'like:{i}' for i in range(5)})

    def test_exhausted_yandex_retries_keep_likes_and_playlist_id_for_resume(self):
        sp = FakeSpotify(self.results)
        with patch('requests.request') as transport, patch('importer.sleep'):
            transport.return_value = response(ACCOUNT)
            ya = importer.create_yandex_client('test-token')
            playlist = ya_playlist(7, 'Mix', self.tracks)
            playlist.fetch_tracks = lambda: ya.tracks(['1:1'])
            source = FakeYandex(tracks=self.tracks, playlists=[playlist])
            first = make_importer(sp, self.tmp.name, chunk=2, yandex=source)
            first.import_likes()
            transport.side_effect = requests.ReadTimeout('offline')
            with self.assertRaises(TimedOutError):
                first.import_playlists()
            progress = importer.Progress(first.progress.path)
            self.assertEqual(progress.done, {f'like:{i}' for i in range(5)})
            self.assertEqual(progress.playlist_id(7), 'pl-1')

        source = FakeYandex(tracks=self.tracks, playlists=[ya_playlist(7, 'Mix', self.tracks)])
        second = make_importer(sp, self.tmp.name, chunk=2, yandex=source)
        second.import_likes()
        self.assertEqual(sum(map(len, sp.saved)), 5)
        second.import_playlists()
        self.assertEqual(Counter(t for batch in sp.saved for t in batch),
                         Counter({'sp0': 2, 'sp1': 2, 'sp2': 2, 'sp3': 2, 'sp4': 2}))
        self.assertEqual(sp.created_playlists, ['Mix'])
        self.assertEqual(len(importer.Progress(second.progress.path).done), 10)
