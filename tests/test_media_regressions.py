"""Regression tests for the live-map contract and non-destructive publication.

Pillow builds real contact sheets. Video encoding and unrelated module builds
are replaced with deterministic fixtures where their behavior is not under test.
"""
from contextlib import ExitStack
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'docs/EBK/projects/prj_275fcf6a380e53889135988a06e61846/tools'
sys.path.insert(0, str(TOOLS))
import build_media_root as builder


class ExistingMediaContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = 'https://example.com/project'
        builder.write_json(self.root / 'data/room_media_slides.json', {
            'schemaVersion': 1, 'pagesBaseUrl': self.base})
        builder.write_json(self.root / 'data/video_delivery_settings.json', {
            'schemaVersion': 1, 'enabled': True, 'layout': 'tile',
            'alphaMode': 'dual-matte'})

    def image(self, relative, color=(20, 50, 80, 128)):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGBA', (12, 16), color).save(path)
        return path

    @staticmethod
    def encode(images, output, *args):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'encoded fixture')

    def roster(self, value=None):
        builder.write_json(self.root / 'data/roster_manifest.json', value or {
            'members': [], 'posters': [{'category': 'EventPoster', 'slot': 0}]})

    def test_live_map_keeps_categories_separate_and_menu_guide_keys(self):
        # Identical pixels must not merge different content categories.
        for relative in ['images/portraits/owner/slot0.png',
                         'images/posters/main/slot0.png',
                         'images/posters/notice/slot0.png',
                         'images/posters/kanpe/slot0.png',
                         'images/products/item.png', 'images/guide/source.png']:
            self.image(relative)
        self.roster({
            'members': [{'category': 'Owner', 'slot': 0}],
            'posters': [{'category': name, 'slot': 0}
                        for name in ['EventPoster', 'Notice', 'Kanpe']],
            'catalogGuide': {
                'products': [{'id': 'drink', 'price': 450,
                              'imageUrl': self.base + '/images/products/item.png'}],
                'guidePages': [{'title': 'Guide',
                                'imageUrl': self.base + '/images/guide/source.png'}]}})
        output = self.root / 'common'
        with patch.object(builder.slides, 'encode', side_effect=self.encode):
            builder.build_common(self.root, output, self.base)
        result = builder.read_json(output / 'manifest.json')
        video = result['videoRoster']
        text = (output / 'frame_map.txt').read_text()
        self.assertEqual(video['captureMap'], text)
        self.assertEqual(video['liveMapVersion'], 1)
        self.assertEqual(video['videoUrl'], self.base + '/common/roster.mp4')
        self.assertEqual(video['frameMapUrl'], self.base + '/common/frame_map.txt')
        self.assertEqual(video['generationMarkerFrame'], 1)
        self.assertEqual(video['sheetCount'], 5)
        self.assertEqual(video['frameCount'], 18)
        rows = {tuple(line.split('|')[1:4]): line.split('|')
                for line in text.splitlines()}
        self.assertEqual(len(rows), 6)
        expected = {('member', 'Owner', '0'): 'portrait',
                    ('poster', 'EventPoster', '0'): 'poster',
                    ('poster', 'Notice', '0'): 'notice',
                    ('poster', 'Kanpe', '0'): 'cue',
                    ('product', 'drink', '0'): 'other',
                    ('guide', 'Pages', '0'): 'other'}
        for key, category in expected.items():
            row = rows[key]
            self.assertIn('contentCategory=' + category, row)
            sheet = int(next(v.split('=')[1] for v in row if v.startswith('sheet=')))
            self.assertEqual(int(row[0]), (sheet + 1) * 3 + 1)
        self.assertEqual(result['catalogGuide']['products'][0]['price'], 450)
        self.assertTrue((self.root / 'images/menu/slot0.png').is_file())
        self.assertTrue((self.root / 'images/guide/slot0.png').is_file())

    def test_cache_revision_is_stable_and_tracks_non_video_changes(self):
        target = self.root / 'common/manifest.json'
        manifest = {'members': [], 'roomMedia': {'albums': []},
                    'clubCards': {'revision': 7}}
        original = copy.deepcopy(manifest)
        builder.write_common_cache_manifest(target, manifest)
        first = builder.read_json(target)
        builder.write_common_cache_manifest(target, first)
        second = builder.read_json(target)
        self.assertEqual(first, second)
        self.assertEqual(manifest, original)
        self.assertEqual((target.parent / 'revision.txt').read_text(),
                         'ebk-media-cache-v1|' + second['cacheRevision'] + '\n')
        second['clubCards']['revision'] = 8
        builder.write_common_cache_manifest(target, second)
        self.assertNotEqual(first['cacheRevision'],
                            builder.read_json(target)['cacheRevision'])


    def module_fixtures(self, fail=False):
        def room(stage, config):
            if fail:
                raise ValueError('deliberate later-module failure')
            builder.write_json(stage / 'docs/ebk-room-media/manifest.json',
                               {'generation': 'room', 'albums': []})
        def themes(folder):
            builder.write_json(folder / 'theme_catalog.json', {'generation': 'theme'})
        stack = ExitStack()
        stack.enter_context(patch.object(builder.slides, 'encode', side_effect=self.encode))
        stack.enter_context(patch.object(builder.slides, 'build', side_effect=room))
        stack.enter_context(patch.object(builder.themes, 'build', side_effect=themes))
        return stack


    def test_later_module_failure_does_not_publish_partial_common(self):
        self.image('images/posters/main/slot0.png')
        self.roster()
        (self.root / 'common').mkdir()
        (self.root / 'common/roster.mp4').write_bytes(b'published video')
        (self.root / 'common/manifest.json').write_text('{"published":true}')
        with self.module_fixtures(fail=True):
            with self.assertRaisesRegex(ValueError, 'deliberate'):
                builder.build(self.root)
        self.assertEqual((self.root / 'common/roster.mp4').read_bytes(), b'published video')
        self.assertEqual(builder.read_json(self.root / 'common/manifest.json'), {'published': True})

    def test_retention_does_not_restore_mutable_aliases_or_replace_new_files(self):
        previous = self.root / 'previous'
        generated = self.root / 'generated'
        previous.mkdir()
        generated.mkdir()
        immutable = 'roster_' + 'b' * 32 + '.mp4'
        for name in [immutable, 'roster.mp4', 'manifest.json', 'frame_map.txt']:
            (previous / name).write_bytes(b'previous')
        (generated / immutable).write_bytes(b'new generation')
        builder.retain_common_generations(previous, generated)
        self.assertEqual((generated / immutable).read_bytes(), b'new generation')
        self.assertEqual({path.name for path in generated.iterdir()}, {immutable})

    def slide_revision(self):
        board_id = 'test-board'
        folder = 'images/slide-posters/' + hashlib.sha256(board_id.encode()).hexdigest()
        alias = self.image(folder + '/slot0.png', (20, 30, 40, 128))
        source = self.image('new.png', (200, 30, 40, 128))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        revision = source.rename(self.root / folder / ('slot0-' + digest + '.png'))
        self.image('images/portraits/owner/slot0.png')
        self.roster({'members': [{'category': 'Owner', 'slot': 0}], 'posters': []})
        builder.write_json(self.root / 'data/slide_poster_manifest.json', {
            'boards': [{'boardId': board_id, 'enabled': True,
                        'selectedIndex': 2048, 'order': [2048],
                        'additionalImages': [{'index': 2048, 'enabled': True,
                            'imagePath': revision.relative_to(self.root).as_posix()}]}],
            'displayCatalog': {'boards': [{'boardId': board_id, 'images': []}]}})
        return alias, revision

    def test_slide_revision_alias_updates_only_after_success(self):
        alias, revision = self.slide_revision()
        manifest = self.root / 'data/slide_poster_manifest.json'
        authoring = manifest.read_bytes()
        old_alias = alias.read_bytes()
        source = revision.read_bytes()
        with self.module_fixtures(fail=True):
            with self.assertRaisesRegex(ValueError, 'deliberate'):
                builder.build(self.root)
        self.assertEqual(alias.read_bytes(), old_alias)
        self.assertEqual(manifest.read_bytes(), authoring)
        with self.module_fixtures():
            builder.build(self.root)
        self.assertEqual(alias.read_bytes(), source)
        self.assertEqual(revision.read_bytes(), source)
        self.assertEqual(manifest.read_bytes(), authoring)
        self.assertIn('slide|test-board|2048',
                      (self.root / 'common/frame_map.txt').read_text())

    def test_slide_hash_mismatch_leaves_alias_unchanged(self):
        alias, revision = self.slide_revision()
        original = alias.read_bytes()
        revision.write_bytes(b'wrong content')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            builder.slides.synchronize_slide_image_aliases(self.root)
        self.assertEqual(alias.read_bytes(), original)

    def test_hidden_portrait_and_explicit_empty_posters_are_respected(self):
        self.image('images/portraits/owner/slot0.png')
        self.image('images/posters/main/slot0.png')
        self.roster({'members': [{'category': 'Owner', 'slot': 0,
                                 'showPortrait': False}], 'posters': []})
        with patch.object(builder.slides, 'encode') as encode:
            common, skipped = builder.build_common(self.root, self.root/'common', self.base)
        self.assertIsNone(common)
        self.assertEqual(skipped, [])
        encode.assert_not_called()

    def test_member_slot_overrides_stale_portrait_url(self):
        expected = self.image('images/portraits/owner/slot0.png')
        self.image('images/portraits/owner/slot1.png', (80, 50, 20, 255))
        self.roster({'members': [{'category': 'Owner', 'slot': 0,
            'portraitUrl': self.base + '/images/portraits/owner/slot1.png'}], 'posters': []})
        with patch.object(builder.slides, 'encode', side_effect=self.encode):
            builder.build_common(self.root, self.root/'common', self.base)
        report = builder.read_json(self.root/'common/build_report.json')
        self.assertEqual(report['sourceHashes'], [hashlib.sha256(expected.read_bytes()).hexdigest()])
        self.assertEqual(len(report['ignoredPortraitUrls']), 1)

    def test_clubcard_content_revision_survives_repeat_builds(self):
        path = 'images/clubcards/cards/00000.png'
        image = self.image(path)
        manifest = {'schemaVersion': 1, 'revision': 2,
            'rarityRules': {'mode': 'cardNumberLastDigit',
                'byLastDigit': dict(enumerate(builder.cards.RARITIES)),
                'defaultWeights': dict(zip(builder.cards.RARITIES, builder.cards.WEIGHTS))},
            'cards': [{'cardNumber': '00000', 'serial': 0, 'cardName': 'Card',
                'enabled': True, 'exchangeEnabled': True, 'exchangePriceOverride': 10,
                'manualRarityWeightOverride': False, 'imagePath': path}]}
        source = self.root/'data/clubcard_manifest.json'
        builder.write_json(source, manifest)
        original = source.read_bytes()
        with patch.object(builder.cards, 'encode', side_effect=self.encode):
            first = builder.cards.build(self.root, self.base)
            same = builder.cards.build(self.root, self.base)
            self.image(path, (200, 30, 40, 128))
            changed = builder.cards.build(self.root, self.base)
        self.assertEqual(first['revision'], 2)
        self.assertEqual(same['revision'], 2)
        self.assertEqual(changed['revision'], 3)
        self.assertEqual(first['contentFingerprint'], same['contentFingerprint'])
        self.assertNotEqual(first['contentFingerprint'], changed['contentFingerprint'])
        self.assertEqual(source.read_bytes(), original)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_138_image_common_video_real_encoder(self):
        for i in range(138):
            self.image(f'images/posters/main/slot{i}.png', (i, 50, 80, 128))
        self.roster({'members': [], 'posters': [
            {'category': 'EventPoster', 'slot': i} for i in range(138)]})
        builder.build_common(self.root, self.root/'common', self.base)
        manifest = builder.read_json(self.root/'common/manifest.json')['videoRoster']
        self.assertEqual(manifest['sheetCount'], 14)
        self.assertEqual(manifest['frameCount'], 45)
        self.assertEqual(len(manifest['captureMap'].splitlines()), 138)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-count_frames',
            '-select_streams', 'v:0', '-show_entries', 'stream=nb_read_frames,width,height',
            '-of', 'json', str(self.root/'common/roster.mp4')],
            check=True, capture_output=True, text=True)
        stream = json.loads(probe.stdout)['streams'][0]
        self.assertEqual((stream['width'], stream['height'], int(stream['nb_read_frames'])),
                         (3840, 1080, 45))


if __name__ == '__main__':
    unittest.main()


