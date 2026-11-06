"""Run with python -m unittest discover -s tests -v (Pillow required)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

TOOLS = (Path(__file__).resolve().parents[1] / 'docs/EBK/projects/'
         'prj_275fcf6a380e53889135988a06e61846/tools')
sys.path.insert(0, str(TOOLS))
import build_media_root as builder


class CommonCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'common'
        self.base = 'https://example.com/media'

    def inputs(self, count, alpha='dual-matte', layout='tile', enabled=True,
               duplicate=False):
        folder = self.root / 'images/posters/main'
        folder.mkdir(parents=True)
        for i in range(count):
            color = (0, 0, 0, 128) if duplicate else (i % 256, i // 256, 7, 128)
            Image.new('RGBA', (8, 8), color).save(folder / f'slot{i}.png')
        builder.write_json(self.root / 'data/roster_manifest.json', {
            'members': [], 'posters': [
                {'category': 'EventPoster', 'slot': i} for i in range(count)]})
        builder.write_json(self.root / 'data/video_delivery_settings.json', {
            'schemaVersion': 1, 'enabled': enabled, 'alphaMode': alpha,
            'layout': layout})

    @staticmethod
    def fake_encode(images, output, *args):
        output.write_bytes(b'test video')

    def build(self):
        with patch.object(builder.slides, 'encode', side_effect=self.fake_encode) as encode:
            result = builder.build_common(self.root, self.output, self.base)
        return result, encode

    def test_138_tile_images_keep_every_key_and_live_capture_map(self):
        self.inputs(138)
        _, encode = self.build()
        manifest = builder.read_json(self.output / 'manifest.json')
        video = manifest['videoRoster']
        self.assertEqual(video['sheetCount'], 14)
        self.assertEqual(video['frameCount'], 45)
        self.assertEqual(video['encodedWidth'], 3840)
        report = builder.read_json(self.output / 'build_report.json')
        self.assertEqual(report['captured'], 138)
        old = (self.output / 'frame_map.txt').read_text().splitlines()
        new = (self.output / video['frameMapUrl'].split('/')[-1]).read_text().splitlines()
        self.assertEqual(len(old), 138)
        for i, (legacy, immutable) in enumerate(zip(old, new)):
            self.assertEqual(legacy.split('|')[1:4], ['poster', 'EventPoster', str(i)])
            self.assertEqual(int(legacy.split('|')[0]), (i // 10 + 1) * 3 + 1)
        self.assertEqual(encode.call_count, 1)
        self.assertEqual(video['liveMapVersion'], 1)
        self.assertEqual(video['captureMap'].splitlines(), old)

    def test_opaque_128_still_supported(self):
        self.inputs(128, alpha='opaque')
        self.build()
        self.assertEqual(builder.read_json(self.output / 'build_report.json')['captured'], 128)

    def test_opaque_129_rejected_before_encoding(self):
        self.inputs(129, alpha='opaque')
        with patch.object(builder.slides, 'encode') as encode:
            with self.assertRaisesRegex(ValueError, '128枠'):
                builder.build_common(self.root, self.output, self.base)
            encode.assert_not_called()

    def test_dual_matte_2048_duplicate_keys_share_one_sheet(self):
        self.inputs(2048, duplicate=True)
        self.build()
        self.assertEqual(builder.read_json(self.output / 'build_report.json')['captured'], 2048)
        self.assertEqual(builder.read_json(self.output / 'manifest.json')['videoRoster']['sheetCount'], 1)

    def test_dual_matte_2049_rejected_before_sheets(self):
        self.inputs(2049, duplicate=True)
        with patch.object(builder.slides, 'contact_sheets') as sheets:
            with self.assertRaisesRegex(ValueError, '2048枠'):
                builder.build_common(self.root, self.output, self.base)
            sheets.assert_not_called()

    def test_single_129_unique_sheets_rejected_before_encoding(self):
        self.inputs(129, layout='single')
        # Exercise the capacity boundary without writing 129 full-resolution PNGs.
        with patch.object(builder, 'category_sheets', return_value=([None]*129, [])):
            with patch.object(builder.slides, 'encode') as encode:
                with self.assertRaisesRegex(ValueError, '128枚'):
                    builder.build_common(self.root, self.output, self.base)
                encode.assert_not_called()
        self.assertFalse((self.output / 'manifest.json').exists())

    def test_128_sheets_accepted(self):
        self.inputs(128, layout='single')
        def sheets(records, *args):
            return [path for key, path in records], [{'sourceWidth': 8, 'sourceHeight': 8,
                'sheet': i, 'tile': 0, 'contentCategory': 'poster', 'uv': [0, 0, 1, 1]} for i in range(128)]
        with patch.object(builder, 'category_sheets', side_effect=sheets):
            self.build()
        self.assertEqual(builder.read_json(self.output / 'manifest.json')['videoRoster']['sheetCount'], 128)

    def test_disabled_delivery_bypasses_video_limits(self):
        self.inputs(129, alpha='opaque', enabled=False)
        _, encode = self.build()
        encode.assert_not_called()
        self.assertFalse(builder.read_json(self.output / 'manifest.json')['videoRoster']['enabled'])

    def test_missing_images_keep_fallback(self):
        self.inputs(139)
        (self.root / 'images/posters/main/slot138.png').unlink()
        self.build()
        report = builder.read_json(self.output / 'build_report.json')
        self.assertEqual(report['captured'], 138)
        self.assertEqual(report['fallbackKeys'], ['poster|EventPoster|138'])

if __name__ == '__main__':
    unittest.main()
