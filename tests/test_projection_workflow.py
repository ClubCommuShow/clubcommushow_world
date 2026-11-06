"""Run the real workflow snippets in fixtures and local Git remotes, without GitHub."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = Path(__file__).resolve().parents[1]
PROJECT = 'docs/EBK/projects/prj_275fcf6a380e53889135988a06e61846'
WORKFLOW_PATH = '.github/workflows/ebk_project_275fcf6a380e5388.yml'
VENUE = 'docs/EBK/venues/venue'
SNAPSHOT_STEP = 'Refresh main and record the build snapshot'
SAVE_STEP = 'Commit generated files with input freshness check'
DEPLOY_STEP = 'Check the publication snapshot after acquiring the Pages lock'


def step_script(workflow, name):
    step = workflow.split('- name: ' + name + '\n', 1)[1]
    step = re.split(r'\n      - |\n  deploy:', step, maxsplit=1)[0]
    run = step.split('        run: ', 1)[1]
    if run.startswith('|\n'):
        return textwrap.dedent(run[2:])
    return run.splitlines()[0] + '\n'


class ProjectionWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (REPO / WORKFLOW_PATH).read_text()
        cls.bytecode_setting = re.search(
            r'^  PYTHONDONTWRITEBYTECODE: "(\d+)"$', cls.workflow, re.M).group(1)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.root = self.temp / 'build'
        self.root.mkdir()
        self.output = self.temp / 'output'
        self.env_file = self.temp / 'env'
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE=self.bytecode_setting,
                        EBK_MEDIA_ROOT=PROJECT, EBK_BRANCH='main',
                        GITHUB_OUTPUT=str(self.output), GITHUB_ENV=str(self.env_file))
        self.write(WORKFLOW_PATH, self.workflow)
        self.write('.gitignore', (REPO / '.gitignore').read_text())
        self.write(PROJECT + '/tools/build_media_root.py',
                   'import fixture_helper\nfrom pathlib import Path\n'
                   'Path("build-imported.txt").write_text(str(fixture_helper.VALUE))\n')
        self.write(PROJECT + '/tools/fixture_helper.py', 'VALUE = 42\n')
        self.write(PROJECT + '/data/roster_manifest.json', '{"fallback":true}')
        self.write(PROJECT + '/images/source.png', 'image')
        self.write(PROJECT + '/ebk_media_root.json', '{}')
        self.write(PROJECT + '/themes/theme_catalog.json', '{}')
        self.write(PROJECT + '/common/manifest.json', '{"new":true}')
        self.write(PROJECT + '/common/roster.mp4', 'old generated video')
        self.write(VENUE + '/data/active_project.json', json.dumps({'projectRoot': PROJECT}))
        self.write(VENUE + '/venue.json', '{"venue":"keep"}')
        self.write(VENUE + '/common/roster.mp4', 'old projected video')

    def write(self, relative, content, root=None):
        path = (root or self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def git(self, *args, root=None):
        result = subprocess.run(['git', *args], cwd=root or self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def run_step(self, name, **env):
        self.output.write_text('')
        self.env_file.write_text('')
        return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c',
                               step_script(self.workflow, name)], cwd=self.root,
                              env=dict(self.env, **env), capture_output=True, text=True)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def initialize_remote(self):
        self.origin = self.temp / 'origin.git'
        self.peer = self.temp / 'peer'
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('add', '.')
        self.git('commit', '-m', 'initial')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('init', '--bare', '-b', 'main', str(self.origin))
        self.git('remote', 'add', 'origin', str(self.origin))
        self.git('push', '-u', 'origin', 'main')
        self.git('clone', str(self.origin), str(self.peer))
        self.git('config', 'user.name', 'Concurrent writer', root=self.peer)
        self.git('config', 'user.email', 'peer@example.invalid', root=self.peer)
        self.env.update(EBK_BUILD_BASE_SHA=self.base, EBK_WORKFLOW_SHA=self.base)

    def peer_change(self, path, content):
        self.write(path, content, root=self.peer)
        self.git('add', '--', path, root=self.peer)
        self.git('commit', '-m', 'concurrent edit', root=self.peer)
        self.git('push', 'origin', 'main', root=self.peer)
        return self.git('rev-parse', 'HEAD', root=self.peer)

    def generate(self):
        self.write(PROJECT + '/common/roster.mp4', 'fresh generated video')
        self.assert_success(self.run_step('Refresh fixed venue projections using this project'))

    def test_build_imports_do_not_generate_bytecode(self):
        self.assert_success(self.run_step('Build only the selected folder'))
        self.assertEqual((self.root / 'build-imported.txt').read_text(), '42')
        self.assertFalse(list(self.root.rglob('__pycache__')))
        self.assertFalse(list(self.root.rglob('*.pyc')))

    def test_projection_copies_full_source_but_not_bytecode_or_venue_metadata(self):
        content = {'index.html': 'full page', 'themes/site.css': 'css',
                   'media/clip.mp4': 'source video', 'tools/new.py': 'new tool',
                   'images/products/item.png': 'product', 'extra/nested.txt': 'other source'}
        for path, value in content.items():
            self.write(PROJECT + '/' + path, value)
        caches = ['tools/__pycache__/fixture.cpython-311.pyc', 'tools/legacy.pyc',
                  'tools/optimized.pyo', '__pycache__/do-not-copy.txt',
                  'images/__pycache__/nested/cache.pyc']
        for path in caches:
            self.write(PROJECT + '/' + path, 'cache')
        self.write(PROJECT + '/venue.json', '{"venue":"wrong"}')
        self.write(PROJECT + '/data/active_project.json', '{"projectRoot":"wrong"}')
        self.assert_success(self.run_step('Refresh fixed venue projections using this project'))
        for path, value in content.items():
            self.assertEqual((self.root / VENUE / path).read_text(), value)
        for path in caches:
            self.assertFalse((self.root / VENUE / path).exists(), path)
        self.assertEqual((self.root / VENUE / 'venue.json').read_text(), '{"venue":"keep"}')
        self.assertEqual(json.loads((self.root / VENUE / 'data/active_project.json').read_text()),
                         {'projectRoot': PROJECT})
        self.assertEqual((self.root / VENUE / 'data/roster_manifest.json').read_text(), '{"new":true}')

    def test_projection_removes_only_obsolete_opaque_media(self):
        old = ['common/roster_' + 'a' * 32 + '.mp4',
               'common/frame_map_' + 'a' * 32 + '.txt',
               'ebk-room-media/videos/album_' + 'b' * 32 + '.mp4',
               'images/products/' + 'c' * 32 + '.png',
               'images/products/' + 'c' * 32 + '-icon.png']
        keep = ['common/roster_named.mp4', 'images/products/product.png',
                'custom/' + 'd' * 32 + '.png']
        current = 'common/roster_' + 'e' * 32 + '.mp4'
        for path in old + keep + [current]:
            self.write(VENUE + '/' + path, 'existing')
        self.write(PROJECT + '/' + current, 'current')
        self.assert_success(self.run_step('Refresh fixed venue projections using this project'))
        for path in old:
            self.assertFalse((self.root / VENUE / path).exists(), path)
        for path in keep:
            self.assertEqual((self.root / VENUE / path).read_text(), 'existing')
        self.assertEqual((self.root / VENUE / current).read_text(), 'current')

    def test_projection_ignores_other_projects_and_invalid_metadata(self):
        self.write(VENUE + '/data/active_project.json', '{"projectRoot":"other-project"}')
        self.write('docs/EBK/venues/invalid/data/active_project.json', 'not json')
        self.assert_success(self.run_step('Refresh fixed venue projections using this project'))
        self.assertEqual((self.root / VENUE / 'common/roster.mp4').read_text(), 'old projected video')
        self.assertFalse((self.root / 'docs/EBK/venues/invalid/common').exists())

    def test_projection_uses_fallback_manifest(self):
        (self.root / PROJECT / 'common/manifest.json').unlink()
        self.assert_success(self.run_step('Refresh fixed venue projections using this project'))
        self.assertEqual((self.root / VENUE / 'data/roster_manifest.json').read_text(), '{"fallback":true}')

    def test_snapshot_refreshes_main_before_recording_base(self):
        self.initialize_remote()
        latest = self.peer_change(PROJECT + '/data/new.json', '{"latest":true}')
        self.assert_success(self.run_step(SNAPSHOT_STEP))
        self.assertEqual(self.git('rev-parse', 'HEAD'), latest)
        self.assertIn('EBK_BUILD_BASE_SHA=' + latest, self.env_file.read_text())
        self.assertEqual(self.output.read_text(), 'build=true\n')

    def test_snapshot_skips_when_loaded_workflow_has_changed(self):
        self.initialize_remote()
        self.peer_change(WORKFLOW_PATH, self.workflow + '\n# newer implementation\n')
        self.assert_success(self.run_step(SNAPSHOT_STEP))
        self.assertEqual(self.output.read_text(), 'build=false\n')
        self.assertEqual(self.env_file.read_text(), '')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)

    def test_gitignore_ignores_nested_and_legacy_caches(self):
        self.initialize_remote()
        for path in [PROJECT + '/tools/__pycache__/x.pyc', VENUE + '/tools/y.pyo',
                     VENUE + '/images/z.pyc', '__pycache__/nested.txt']:
            self.write(path, 'cache')
            self.assertEqual(self.git('check-ignore', path), path)

    def test_save_excludes_caches_even_without_ignore_rules(self):
        # Pathspec exclusions are independent of .gitignore.
        self.write('.gitignore', '')
        self.initialize_remote()
        self.generate()
        caches = [VENUE + '/tools/__pycache__/x.pyc', VENUE + '/images/y.pyo',
                  VENUE + '/tools/z.pyc', PROJECT + '/common/__pycache__/ignored.txt',
                  PROJECT + '/data/legacy.pyc', PROJECT + '/images/picture.pyo']
        for path in caches:
            self.write(path, 'cache')
        result = self.run_step(SAVE_STEP)
        self.assert_success(result)
        self.assertIn('publish=true\n', self.output.read_text())
        published = self.git('rev-parse', 'HEAD')
        self.assertIn('published_sha=' + published, self.output.read_text())
        self.assertEqual(self.git('rev-parse', 'main', root=self.origin), published)
        tracked = self.git('ls-files').splitlines()
        for path in caches:
            self.assertNotIn(path, tracked)
        self.assertEqual(self.git('show', 'main:' + VENUE + '/common/roster.mp4', root=self.origin),
                         'fresh generated video')

    def test_save_rejects_already_tracked_cache_without_cleanup(self):
        self.initialize_remote()
        cache = VENUE + '/tools/__pycache__/tracked.pyc'
        self.write(cache, 'tracked cache')
        self.git('add', '-f', cache)
        self.git('commit', '-m', 'bad historical cache')
        result = self.run_step(SAVE_STEP)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Python cache files must not be tracked', result.stderr)
        self.assertEqual((self.root / cache).read_text(), 'tracked cache')
        self.assertEqual(self.git('rev-parse', 'main', root=self.origin), self.base)
        self.assertNotIn('publish=true', self.output.read_text())

    def test_save_rejects_concurrent_venue_activation_before_rebase(self):
        self.initialize_remote()
        self.generate()
        latest = self.peer_change(VENUE + '/data/active_project.json', '{"projectRoot":"new-project"}')
        self.assert_success(self.run_step(SAVE_STEP))
        self.assertEqual(self.output.read_text(), 'publish=false\n')
        self.assertEqual(self.git('rev-parse', 'main', root=self.origin), latest)
        self.assertEqual(self.git('show', 'main:' + VENUE + '/common/roster.mp4', root=self.origin),
                         'old projected video')
        self.assertFalse((self.root / '.git/rebase-merge').exists())

    def test_save_rejects_concurrent_project_root_file_change(self):
        self.initialize_remote()
        self.generate()
        latest = self.peer_change(PROJECT + '/index.html', 'newer UI not included in build')
        self.assert_success(self.run_step(SAVE_STEP))
        self.assertEqual(self.output.read_text(), 'publish=false\n')
        self.assertEqual(self.git('rev-parse', 'main', root=self.origin), latest)

    def test_save_preserves_unrelated_concurrent_changes(self):
        self.initialize_remote()
        self.generate()
        self.peer_change('docs/unrelated.html', 'latest unrelated page')
        self.assert_success(self.run_step(SAVE_STEP))
        self.assertIn('publish=true\n', self.output.read_text())
        self.assertEqual(self.git('show', 'main:docs/unrelated.html', root=self.origin),
                         'latest unrelated page')
        self.assertEqual(self.git('show', 'main:' + VENUE + '/common/roster.mp4', root=self.origin),
                         'fresh generated video')

    def test_save_rechecks_after_activation_races_with_push(self):
        self.initialize_remote()
        self.generate()
        activation = VENUE + '/data/active_project.json'
        self.write(activation, '{"projectRoot":"racing-project"}', root=self.peer)
        self.git('add', activation, root=self.peer)
        self.git('commit', '-m', 'racing activation', root=self.peer)
        peer_sha = self.git('rev-parse', 'HEAD', root=self.peer)
        hook = self.write('.git/hooks/pre-push', '#!/bin/sh\nset -eu\n'
                          'if [ ! -f "' + str(self.temp / 'raced') + '" ]; then\n'
                          '  touch "' + str(self.temp / 'raced') + '"\n'
                          '  git -C "' + str(self.peer) + '" push origin main\nfi\n')
        hook.chmod(0o755)
        result = self.run_step(SAVE_STEP)
        self.assert_success(result)
        self.assertIn('failed to push', result.stderr)
        self.assertEqual(self.output.read_text(), 'publish=false\n')
        self.assertEqual(self.git('rev-parse', 'main', root=self.origin), peer_sha)
        self.assertEqual(json.loads(self.git('show', 'main:' + activation, root=self.origin)),
                         {'projectRoot': 'racing-project'})

    def test_save_requires_explicit_prebuild_snapshot(self):
        self.initialize_remote()
        del self.env['EBK_BUILD_BASE_SHA']
        result = self.run_step(SAVE_STEP)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing pre-build snapshot', result.stderr)

    def test_deploy_skips_venue_changes_while_waiting_for_pages(self):
        self.initialize_remote()
        self.env['EBK_PUBLISHED_SHA'] = self.base
        self.peer_change(VENUE + '/venue.json', '{"venue":"newer"}')
        self.git('pull', '--ff-only', 'origin', 'main')
        self.assert_success(self.run_step(DEPLOY_STEP))
        self.assertEqual(self.output.read_text(), 'publish=false\n')

    def test_deploy_allows_unrelated_changes_after_build(self):
        self.initialize_remote()
        self.env['EBK_PUBLISHED_SHA'] = self.base
        self.peer_change('docs/unrelated.html', 'latest unrelated page')
        self.git('pull', '--ff-only', 'origin', 'main')
        self.assert_success(self.run_step(DEPLOY_STEP))
        self.assertEqual(self.output.read_text(), 'publish=true\n')

    def test_regression_tests_and_all_publication_actions_are_gated(self):
        self.assertIn('run: python -m unittest discover -s tests -v', self.workflow)
        self.assertLess(self.workflow.index('- name: ' + SNAPSHOT_STEP),
                        self.workflow.index('- name: Run publication regression tests'))
        self.assertLess(self.workflow.index('- name: Run publication regression tests'),
                        self.workflow.index('- name: Build only the selected folder'))
        self.assertEqual(self.workflow.count("if: steps.snapshot.outputs.build == 'true'"), 4)
        self.assertIn("if: needs.build.outputs.publish == 'true'", self.workflow)
        self.assertEqual(self.workflow.count("if: steps.freshness.outputs.publish == 'true'"), 2)
        self.assertIn('"' + PROJECT + '/**"', self.workflow)
        self.assertIn('"docs/EBK/venues/**"', self.workflow)


if __name__ == '__main__':
    unittest.main()
