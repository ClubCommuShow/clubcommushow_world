#!/usr/bin/env python3
"""Build an isolated EBK media root. No writes outside the selected folder.

Inputs are copied to a temporary build directory. Only validated generated
outputs are published; the caller commits them together. Legacy inputs, scripts
and videos at the repository root are not read or changed.
"""
from pathlib import Path
from urllib.parse import urlsplit, unquote, quote
import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageOps
import build_room_media_slides as slides
import build_clubcard_video as cards
import build_theme_catalog as themes


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def write_common_cache_manifest(path, manifest):
    # Bind the flag to every consumer's data, including text-only/card/album edits.
    # Exclude our own revision so a rebuild of identical input keeps the same flag.
    content = copy.deepcopy(manifest)
    content.pop('cacheRevision', None)
    revision = hashlib.sha256(json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')).hexdigest()[:32]
    content['cacheRevision'] = revision
    write_json(path, content)
    (path.parent / 'revision.txt').write_text('ebk-media-cache-v1|' + revision + '\n', encoding='utf-8')


def retain_common_generations(previous, generated):
    # Published worlds keep immutable generation URLs until they are reuploaded.
    # Retain only versioned video/map pairs, never old active manifests or aliases.
    if not previous.is_dir():
        return
    for old in previous.iterdir():
        if (old.is_file() and not old.is_symlink()
                and re.fullmatch(r'(roster_[0-9a-f]{32}\.mp4|frame_map_[0-9a-f]{32}\.txt)', old.name)
                and not (generated / old.name).exists()):
            shutil.copy2(old, generated / old.name)


def synchronize_roster_video_source(root, common_manifest):
    # Keep the authoring roster usable by its existing URL without stale video/map paths.
    # Copy the complete generated descriptor so map, video and generation stay paired.
    roster_path = root / 'data/roster_manifest.json'
    if not roster_path.is_file():
        return
    roster = read_json(roster_path)
    roster['videoRoster'] = copy.deepcopy(common_manifest['videoRoster'])
    write_json(roster_path, roster)


def source_image(root, row, kind, base):
    # Portrait authority is the roster's category + slot, never an unverified URL.
    # A stale portraitUrl must not redirect one member to another member's slot.
    from github_manifest_to_video import find_image
    if kind == 'member':
        return find_image(root / 'images', kind, str(row['category']), int(row['slot']))
    address = row.get('portraitUrl' if kind == 'member' else 'imageUrl', '')
    if address:
        if not address.startswith(base + '/'):
            raise ValueError('画像URLは新しい共通ルート内で指定してください: ' + address)
        relative = unquote(urlsplit(address).path[len(urlsplit(base).path) + 1:])
        candidate = root / relative
        if not candidate.resolve().is_relative_to(root.resolve()):
            raise ValueError('Image path escapes media root')
        return candidate if candidate.is_file() else None
    from github_manifest_to_video import find_image
    return find_image(root / 'images', kind, str(row['category']), int(row['slot']))



def generation_marker(path, generation, width, height):
    value = 'eb4b26c1' + generation
    if len(value) != 40 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid generation marker')
    bits = ''.join(f'{int(c, 16):04b}' for c in value)
    image = Image.new('RGB', (width, height), 'black')
    draw = ImageDraw.Draw(image)
    for i, bit in enumerate(bits):
        x0 = (i % 20) * width // 20
        x1 = (i % 20 + 1) * width // 20 - 1
        y0 = (i // 20) * height // 8
        y1 = (i // 20 + 1) * height // 8 - 1
        draw.rectangle((x0, y0, x1, y1), fill='white' if bit == '1' else 'black')
    image.save(path)

def slide_records(root):
    return slides.slide_records(root)


def content_category(key):
    parts = key.split('|')
    if parts[0] == 'member':
        return 'portrait'
    if parts[0] == 'poster' and parts[1] == 'Notice':
        return 'notice'
    if parts[0] == 'poster' and parts[1] == 'Kanpe':
        return 'cue'
    if parts[0] in ('poster', 'slide'):
        return 'poster'
    return 'other'


def category_sheets(records, folder, width, height, columns, rows):
    # Keep semantic record order while preventing pixel deduplication or packing
    # from joining different content categories onto the same sheet.
    frames, geometry = [], [None] * len(records)
    for category in ('notice', 'poster', 'cue', 'portrait', 'other'):
        indexes = [i for i, (key, _) in enumerate(records)
                   if content_category(key) == category]
        if not indexes:
            continue
        grouped_frames, grouped_geometry = slides.contact_sheets(
            [records[i][1] for i in indexes], Path(folder) / category,
            width, height, columns, rows)
        offset = len(frames)
        for i, item in zip(indexes, grouped_geometry):
            geometry[i] = dict(item, sheet=item['sheet'] + offset,
                               contentCategory=category)
        frames.extend(grouped_frames)
    return frames, geometry


def build_common(root, output, base):
    manifest = read_json(root / 'data/roster_manifest.json')
    delivery = slides.delivery_settings(root)
    columns,rows = (5,2) if delivery['layout']=='tile' else (1,1)
    dual_matte = delivery['alphaMode']=='dual-matte'
    video_format = 'ebk-dual-matte-sbs-srgb-v1' if dual_matte else 'ebk-roster-v1'
    if not delivery['enabled']:
        result=copy.deepcopy(manifest)
        result['videoRoster']={'enabled':False,'status':'individual-images'}
        write_json(output/'manifest.json',result)
        return {'id':'common','manifestPath':'common/manifest.json','videoPath':'','format':'ebk-roster-v1'},[]
    from prepare_video_roster_manifest import discover, POSTER_FOLDERS
    posters = list(manifest.get('posters', []))
    known = {(r.get('category'), r.get('slot')) for r in posters if isinstance(r, dict)}
    # A saved list (including []) is authoritative. Folder discovery is only for
    # legacy inputs that have no poster configuration at all.
    if 'posters' not in manifest:
        for folder, category in POSTER_FOLDERS:
            for slot in discover(root / 'images/posters' / folder):
                if (category, slot) not in known:
                    posters.append({'category': category, 'slot': slot, 'enabled': True})
    manifest['posters'] = posters
    records = []
    skipped = []
    ignored_portrait_urls = []
    keys = set()
    for field, kind in [('members', 'member'), ('posters', 'poster')]:
        for row in manifest.get(field, []):
            if not isinstance(row, dict) or not row.get('enabled', True):
                continue
            if kind == 'member' and not row.get('showPortrait', True):
                continue
            category, slot = row.get('category'), row.get('slot')
            if not isinstance(category, str) or not category or '|' in category or type(slot) is not int or slot < 0:
                raise ValueError('Invalid semantic image key')
            key = f'{kind}|{category}|{slot}'
            if key in keys:
                raise ValueError('Duplicate key: ' + key)
            keys.add(key)
            path = source_image(root, row, kind, base)
            if kind == 'member' and row.get('portraitUrl'):
                expected = '' if path is None else base + '/' + quote(path.relative_to(root).as_posix(), safe='/')
                if unquote(row['portraitUrl']) != unquote(expected):
                    ignored_portrait_urls.append({'key': key, 'configuredUrl': row['portraitUrl'], 'resolvedUrl': expected})
            if path is None:
                skipped.append(key)
                continue  # Missing keys use the Wizard's local image.
            records.append((key, path))
    # Menu images share the same MP4, decode material, geometry and generation.
    catalog = manifest.get('catalogGuide') or {}
    for field, kind in [('products', 'product'), ('guidePages', 'guide')]:
        for index, row in enumerate(catalog.get(field, [])):
            if not isinstance(row, dict) or not row.get('enabled', True) or not row.get('imageUrl'):
                continue
            category = str(row.get('id', '')) if kind == 'product' else 'Pages'
            slot = 0 if kind == 'product' else index
            if not category or '|' in category:
                raise ValueError('Invalid menu image identity')
            key = f'{kind}|{category}|{slot}'
            if key in keys:
                raise ValueError('Duplicate key: ' + key)
            keys.add(key)
            # Venue projections retain project URLs; resolve only their local images
            # suffix. No network fetch or traversal outside this media root.
            address = urlsplit(row['imageUrl'])
            relative = unquote(address.path).split('/images/', 1)
            if address.scheme != 'https' or len(relative) != 2 or address.query or address.fragment:
                raise ValueError('Invalid menu image URL: ' + key)
            path = root / 'images' / relative[1]
            if not path.resolve().is_relative_to((root / 'images').resolve()):
                raise ValueError('Menu image escapes media root')
            if not path.is_file():
                raise ValueError('Missing configured menu image: ' + key)
            fixed = root / 'images' / ('menu' if kind == 'product' else 'guide') / f'slot{index}.png'
            fixed.parent.mkdir(parents=True, exist_ok=True)
            if path.resolve() != fixed.resolve():
                with Image.open(path) as source:
                    ImageOps.exif_transpose(source).convert('RGBA').save(fixed)
            records.append((key, fixed))
            row['imageUrl'] = base + '/' + fixed.relative_to(root).as_posix()
    slide_images,missing_slides=slide_records(root)
    records+=slide_images;skipped+=missing_slides
    record_limit = 2048 if dual_matte else 128
    if len(records) > record_limit:
        raise ValueError(f'共通画像は{record_limit}枠までです')
    output.mkdir(parents=True, exist_ok=True)
    if not records:
        return None, skipped
    with tempfile.TemporaryDirectory(prefix='ebk-common-') as tmp:
        logical_width,logical_height=1920,1080
        frames = []
        legacy_lines = []
        generation_lines = []
        hashes = []
        frames,geometry=category_sheets(records,Path(tmp)/'sheets',logical_width,logical_height,columns,rows)
        if len(frames) > 128:
            raise ValueError('共通動画のタイルシートは128枚までです。タイル方式を使用するか画像数を減らしてください')
        for i, (key, path) in enumerate(records):
            item=geometry[i];width=item['sourceWidth'];height=item['sourceHeight']
            address = base + '/' + quote(path.relative_to(root).as_posix(), safe='/')
            legacy_frame = item['sheet'] * 3 + 1
            generation_frame = (item['sheet'] + 1) * 3 + 1
            x,y,w,h=item['uv']
            metadata=f'{w:.8f}|{h:.8f}|{x:.8f}|{y:.8f}|aspect={width/height:.8f}|width={width}|height={height}|fit=contain|tile={item["tile"]}|sheet={item["sheet"]}|bad=0|contentCategory={item["contentCategory"]}'
            legacy_lines.append(f'{legacy_frame}|{key}|{address}|{metadata}|sample={(legacy_frame+.5)/30:.8f}')
            generation_lines.append(f'{generation_frame}|{key}|{address}|{metadata}|sample={(generation_frame+.5)/30:.8f}')
            hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        generation_payload = {
            'format': video_format,
            'sheetColumns':columns,'sheetRows':rows,
            'layoutVersion':3,'deduplicatePixels':True,
            'sheetGrouping':'content-category-v1',
            'logicalWidth':logical_width,'logicalHeight':logical_height,
            'fps': 30,
            'framesPerEntry': 3,
            'sampleFrame': 1,
            'generationMarker': 'eb4b26c1-v1',
            'records': [{'key': records[i][0], 'sha256': hashes[i]} for i in range(len(records))]
        }
        generation = hashlib.sha256(json.dumps(
            generation_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')
        ).encode('utf-8')).hexdigest()[:32]
        video_name = 'roster.mp4'
        frame_map_name = 'frame_map.txt'
        marker = Path(tmp) / 'generation.png'
        generation_marker(marker, generation, logical_width, logical_height)
        # New immutable URL: 3 marker frames first, then the ordinary 3-frame entries.
        slides.encode([marker] + frames, output / video_name, logical_width, logical_height, dual_matte)
        video_sha256 = hashlib.sha256((output / video_name).read_bytes()).hexdigest()
    result = copy.deepcopy(manifest)
    for row in result.get('members', []):
        if not isinstance(row, dict) or type(row.get('slot')) is not int or not row.get('category'):
            continue
        path = source_image(root, row, 'member', base)
        row['portraitUrl'] = '' if path is None else base + '/' + quote(path.relative_to(root).as_posix(), safe='/')
    result['videoRoster'] = {
        'enabled': True, 'status': 'ready', 'generation': generation, 'format': video_format,
        'fps': 30, 'framesPerEntry': 3, 'sampleFrame': 1,
        'generationMarkerFrame': 1, 'frameCount': (len(frames) + 1) * 3,
        'sheetColumns':columns,'sheetRows':rows,'sheetCount':len(frames),
        'logicalWidth':logical_width,'logicalHeight':logical_height,
        'encodedWidth':logical_width*(2 if dual_matte else 1),'encodedHeight':logical_height,
        'videoUrl': base + '/common/roster.mp4',
        'frameMapUrl': base + '/common/frame_map.txt',
        'captureMap': '\n'.join(generation_lines) + '\n',
        'liveMapVersion': 1,
        'sha256': video_sha256
    }
    write_json(output / 'manifest.json', result)
    generation_map_text = '\n'.join(generation_lines) + '\n'
    legacy_map_text = '\n'.join(legacy_lines) + '\n'
    (output / frame_map_name).write_text(generation_map_text, encoding='utf-8')
    (output / 'frame_map.txt').write_text(generation_map_text, encoding='utf-8')
    write_json(output / 'build_report.json', {
        'captured': len(records), 'fallbackKeys': skipped, 'sourceHashes': hashes,
        'ignoredPortraitUrls': ignored_portrait_urls, 'generation': generation,
        'videoSha256': video_sha256
    })
    return {
        'id': 'common', 'manifestPath': 'common/manifest.json',
        'videoPath': 'common/roster.mp4', 'format': 'ebk-roster-v1',
        'generation': generation
    }, skipped


def normalize_product_sources(root, base):
    # Migrate only known product-image references. IDs, prices, roles and stock
    # remain authoring data; a filename is never used as a product identity.
    files = sorted((root / 'data').glob('*.json'))
    documents = {path: read_json(path) for path in files}
    authority = documents.get(root / 'data/roster_manifest.json', {}).get('catalogGuide', {}).get('products', [])
    slots = {str(row.get('id', '')): i for i, row in enumerate(authority)
             if isinstance(row, dict) and row.get('id')}
    def product_rows(node):
        if isinstance(node, dict):
            for row in node.get('products', []) if isinstance(node.get('products'), list) else []:
                if isinstance(row, dict):
                    yield row
            for child in node.values():
                if isinstance(child, (dict, list)):
                    yield from product_rows(child)
        elif isinstance(node, list):
            for child in node:
                yield from product_rows(child)
    replacements = {}
    candidates = set()
    for document in documents.values():
        for row in product_rows(document):
            identity = str(row.get('id') or row.get('productId') or row.get('sku') or '')
            if not identity:
                continue
            if identity not in slots:
                slots[identity] = len(slots)
            for key in ('imageUrl', 'iconUrl'):
                address = row.get(key, '')
                if not isinstance(address, str) or not address:
                    continue
                parsed = urlsplit(address)
                suffix = unquote(parsed.path).split('/images/', 1)
                if parsed.scheme != 'https' or len(suffix) != 2:
                    continue
                source = root / 'images' / suffix[1]
                if not source.resolve().is_relative_to((root / 'images').resolve()) or not source.is_file():
                    raise ValueError('Missing product image during fixed-slot migration: ' + identity)
                relative = 'images/menu/slot' + str(slots[identity]) + ('-icon' if key == 'iconUrl' else '') + '.png'
                fixed = root / relative
                fixed.parent.mkdir(parents=True, exist_ok=True)
                if fixed.resolve() != source.resolve():
                    with Image.open(source) as image:
                        image = ImageOps.exif_transpose(image).convert('RGBA')
                        image.thumbnail((2048,2048),Image.Resampling.LANCZOS)
                        image.save(fixed)
                canonical = base + '/' + relative
                replacements[address] = canonical
                replacements[source.relative_to(root).as_posix()] = relative
                if re.fullmatch(r'[0-9a-fA-F]{24,64}(-icon)?\.png', source.name) and source.parent == root / 'images/products':
                    candidates.add(source.relative_to(root).as_posix())
    def replace(node):
        if isinstance(node, dict):
            return {key: replace(value) for key, value in node.items()}
        if isinstance(node, list):
            return [replace(value) for value in node]
        return replacements.get(node, node) if isinstance(node, str) else node
    changed = []
    for path, document in documents.items():
        updated = replace(document)
        if updated != document:
            write_json(path, updated)
            changed.append(path.relative_to(root).as_posix())
        documents[path] = updated
    serialized = '\n'.join(json.dumps(value, ensure_ascii=False) for value in documents.values())
    removed = []
    for relative in sorted(candidates):
        if relative in serialized:
            continue
        (root / relative).unlink()
        removed.append(relative)
    return changed, removed


def build(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Media root must be an existing regular directory')
    root = root.resolve()
    slides.checked_tree(root)
    config = read_json(root / 'data/room_media_slides.json')
    base = config['pagesBaseUrl'].rstrip('/')
    url = urlsplit(base)
    if url.scheme != 'https' or not url.hostname or url.username or url.query or url.fragment:
        raise ValueError('公開済みのHTTPSルートURLが必要です')
    # Stage all groups before updating even one published group.
    with tempfile.TemporaryDirectory(prefix='ebk-media-root-') as tmp:
        stage = Path(tmp)
        for directory in ['data', 'images', 'media', 'themes']:
            if (root / directory).is_dir():
                shutil.copytree(root / directory, stage / directory)
        product_data_edits, obsolete_products = normalize_product_sources(stage, base)
        (stage / 'themes').mkdir(exist_ok=True)
        (stage / 'media/room-slides').mkdir(parents=True, exist_ok=True)
        generated = stage / 'generated'
        generated.mkdir()
        modules = []
        if (stage / 'data/roster_manifest.json').is_file():
            common, _ = build_common(stage, generated / 'common', base)
            if common:
                modules.append(common)
        card_manifest = stage / 'data/clubcard_manifest.json'
        if card_manifest.is_file() and read_json(card_manifest).get('cards'):
            previous_cards=root/'ebk-clubcards/manifest.json'
            if previous_cards.is_file():
                shutil.copy2(previous_cards,stage/'.ebk_previous_clubcards.json')
            cards.build(stage, base)
            built_cards = read_json(stage / 'docs/ebk-clubcards/manifest.json')
            card_generation = str(built_cards.get('videoRoster', {}).get('generation', ''))
            card_format = str(built_cards.get('videoRoster', {}).get('format', ''))
            if card_format not in ('ebk-clubcards-v2', 'ebk-clubcards-v1'):
                raise ValueError('ClubCard生成形式を確認できません: ' + card_format)
            modules.append({'id': 'clubcards', 'manifestPath': 'ebk-clubcards/manifest.json',
                            'videoPath': 'ebk-clubcards/cards.mp4', 'format': card_format,
                            'generation': card_generation})
        slides.build(stage, stage / 'data/room_media_slides.json')
        room_manifest = read_json(stage / 'docs/ebk-room-media/manifest.json')
        common_manifest_path=generated/'common/manifest.json'
        if common_manifest_path.is_file():
            common_manifest=read_json(common_manifest_path)
            common_manifest['roomMedia']=room_manifest
            if card_manifest.is_file() and read_json(card_manifest).get('cards'):
                common_manifest['clubCards']=built_cards
            write_common_cache_manifest(common_manifest_path,common_manifest)
        modules.append({'id': 'roomMedia', 'manifestPath': 'ebk-room-media/manifest.json',
                        'format': 'ebk-room-media-v1', 'generation': str(room_manifest.get('generation', ''))})
        themes.build(stage / 'themes')
        theme_catalog = read_json(stage / 'themes/theme_catalog.json')
        modules.append({'id': 'themes', 'manifestPath': 'themes/theme_catalog.json',
                        'format': 'ebk-theme-catalog-v1', 'generation': str(theme_catalog.get('generation', ''))})
        if (stage / 'docs').exists():
            for group in (stage / 'docs').iterdir():
                if group.is_dir():
                    shutil.copytree(group, generated / group.name)
        write_json(generated / 'ebk_media_root.json', {'schemaVersion': 1, 'format': 'ebk-media-root-v1', 'modules': modules})
        # Atomicity at the remote end is provided by the single generated commit.
        for directory in ['common', 'ebk-clubcards', 'ebk-room-media']:
            src = generated / directory
            if not src.exists():
                continue
            target = root / directory
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
        for relative in product_data_edits:
            shutil.copy2(stage / relative, root / relative)
        for relative in obsolete_products:
            target = root / relative
            if target.resolve().is_relative_to((root / 'images/products').resolve()) and target.is_file():
                target.unlink()
        for image_group in ['menu', 'guide']:
            fixed_images = stage / 'images' / image_group
            if fixed_images.is_dir():
                shutil.copytree(fixed_images, root / 'images' / image_group, dirs_exist_ok=True)
        (root / 'themes').mkdir(exist_ok=True)
        shutil.copyfile(stage / 'themes/theme_catalog.json', root / 'themes/theme_catalog.json')
        shutil.copyfile(generated / 'ebk_media_root.json', root / 'ebk_media_root.json')
        if common_manifest_path.is_file():
            synchronize_roster_video_source(root, read_json(common_manifest_path))
    print('Built isolated media root:', root.name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    build(args.root)
