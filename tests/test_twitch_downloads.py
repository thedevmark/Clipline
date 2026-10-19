"""Selected official download resolution; no network, transfer or grant changes."""
from dataclasses import replace
import traceback
import unittest
from unittest.mock import patch
import urllib.error

from native.core.twitch_inbox import TwitchClip
from native.services import twitch_api as api


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.clip = TwitchClip('Selected-Clip', '42', '43', '', 'Authored metadata',
            '2026-10-18T12:00:00Z', 2_500_000, 1,
            'https://clips.twitch.tv/Selected-Clip', None, None)
        self.url = 'https://production.assets.clips.twitchcdn.net/owned.mp4?sig=fixture-private-signed'

    def resolve(self, response):
        with patch.object(api, 'current_user', return_value={'id': '42'}), \
                patch.object(api, '_get', return_value=response) as request:
            result = api.own_clip_download(self.clip)
        return result, request

    def test_selected_clip_uses_broadcaster_identity_and_preserves_temporary_signature(self):
        result, request = self.resolve({'data': [{'clip_id': self.clip.id,
            'landscape_download_url': self.url, 'portrait_download_url': None}]})
        request.assert_called_once_with('clips/downloads', {
            'broadcaster_id': '42', 'editor_id': '42', 'clip_id': self.clip.id})
        self.assertEqual(result.clip_id, self.clip.id)
        self.assertEqual(result.landscape_url, self.url)
        self.assertNotIn(self.url, repr(result))
        self.assertNotIn('fixture-private-signed', repr(result))

    def test_another_broadcaster_refuses_before_requesting_signed_media(self):
        with patch.object(api, 'current_user', return_value={'id': '99'}), \
                patch.object(api, '_get') as request, self.assertRaises(api.TwitchAuthError):
            api.own_clip_download(self.clip)
        request.assert_not_called()

    def test_unvalidated_or_invalidated_clip_refuses_before_account_read(self):
        bad = replace(self.clip)
        object.__setattr__(bad, 'id', '../invalid')
        with patch.object(api, 'current_user') as account:
            for candidate in ({'id': self.clip.id}, bad):
                with self.subTest(candidate=type(candidate)), self.assertRaises(ValueError):
                    api.own_clip_download(candidate)
        account.assert_not_called()

    def test_missing_landscape_never_substitutes_portrait(self):
        with self.assertRaisesRegex(api.ClipDownloadUnavailable, 'landscape'):
            self.resolve({'data': [{'clip_id': self.clip.id,
                'landscape_download_url': None, 'portrait_download_url': self.url}]})

    def test_response_must_identify_exactly_one_selected_clip(self):
        for response in (None, [], {}, {'data': []}, {'data': [None]},
                {'data': [{'clip_id': 'Other', 'landscape_download_url': self.url}]},
                {'data': [{'clip_id': self.clip.id}] * 2}):
            with self.subTest(response=response), self.assertRaises(api.ClipDownloadUnavailable):
                self.resolve(response)

    def test_invalid_transport_urls_refuse_without_echoing_signed_values(self):
        for url in ('http://example.test/clip?secret=fixture',
                    'https://user:fixture@example.test/clip',
                    'https://example.test:invalid/clip', 'https://example.test:444/clip',
                    'https://example.test/clip#fixture', 'https://example.test/clip\nfixture',
                    'https://example.test', '', 123, 'https://[invalid/fixture'):
            with self.subTest(url=url), self.assertRaises(api.ClipDownloadUnavailable) as caught:
                self.resolve({'data': [{'clip_id': self.clip.id, 'landscape_download_url': url}]})
            self.assertNotIn('fixture', str(caught.exception))

    def test_auth_rejection_is_distinct_and_provider_text_is_not_forwarded(self):
        errors = [api.TwitchAuthError('fixture-private-signed'), RuntimeError(self.url)]
        denied = RuntimeError(self.url)
        denied.__cause__ = urllib.error.HTTPError('https://api.twitch.tv/helix/clips/downloads', 403,
                                                'fixture-private-signed', {}, None)
        errors.append(denied)
        for error in errors:
            expected = api.ClipDownloadUnavailable if type(error) is RuntimeError and error.__cause__ is None else api.TwitchAuthError
            with patch.object(api, 'current_user', return_value={'id': '42'}), \
                    patch.object(api, '_get', side_effect=error):
                with self.subTest(kind=expected), self.assertRaises(expected) as caught:
                    api.own_clip_download(self.clip)
            rendered = ''.join(traceback.format_exception(caught.exception))
            self.assertNotIn('fixture-private-signed', rendered)
        for error in (api.TwitchAuthError(self.url), RuntimeError(self.url)):
            expected = api.TwitchAuthError if isinstance(error, api.TwitchAuthError) else api.ClipDownloadUnavailable
            with patch.object(api, 'current_user', side_effect=error), patch.object(api, '_get') as request:
                with self.subTest(account_error=expected), self.assertRaises(expected) as caught:
                    api.own_clip_download(self.clip)
            request.assert_not_called()
            self.assertNotIn('fixture-private-signed', ''.join(traceback.format_exception(caught.exception)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
