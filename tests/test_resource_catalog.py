import json

from tests.pak_fixture import pak
from tests.test_discovery import FixtureCase
from wuwaru.resource_catalog import LocalResourceCatalog


class ResourceCatalogTests(FixtureCase):
    def test_discovers_base_and_patch_and_writes_verified_entry_manifest(self):
        root = self.home / 'Wuthering Waves'
        (root / 'Client/Content/Paks').mkdir(parents=True)
        (root / 'Wuthering Waves.exe').write_bytes(b'fixture')
        base = pak(root / 'Client/Content/Paks/pakchunk1-WindowsNoEditor.pak',
                   {'same.bytes': b'base', 'deleted.bytes': b'base-old'})
        patch_dir = root / 'Client/Saved/Resources/3.7.0/Resource/3.7.16'
        patch_dir.mkdir(parents=True)
        patch = pak(patch_dir / 'pakchunk1-WindowsNoEditor_P.pak',
                    {'same.bytes': b'patch'}, pruned_entries=('deleted.bytes',))
        catalog = LocalResourceCatalog(root)

        resources = catalog.discover_resources()
        manifest = catalog.build_manifest()
        search = catalog.search_entries(path_contains='same', extensions={'.bytes'}, limit=1)
        output = catalog.write_entry_manifest(self.home / 'available-entries.jsonl')
        repeated = catalog.write_entry_manifest(self.home / 'available-entries-copy.jsonl')
        rows = [json.loads(line) for line in output['path'].read_text(encoding='utf-8').splitlines()]

        self.assertEqual(set(resources), {base, patch})
        self.assertEqual([p.parent.name for p in catalog.resolve_resource_priority(resources)],
                         ['3.7.16', 'Paks'])
        self.assertEqual(manifest['resource_version'], '3.7.0')
        self.assertTrue(manifest['inventory_complete'])
        self.assertEqual(next(p for p in manifest['packages']
                              if p['kind'] == 'PATCH_RESOURCE')['mount_point'], '../../../')
        self.assertEqual(len(search), 1)
        self.assertEqual(search[0]['kind'], 'PATCH_RESOURCE')
        self.assertEqual(search[0]['path'].rsplit('/', 1)[-1], 'same.bytes')
        entries = [row for row in rows if row['type'] == 'entry']
        same_entries = [row for row in entries if row['path'].endswith('same.bytes')]
        self.assertEqual(len(same_entries), 2)
        self.assertTrue(same_entries[1]['overlap_candidate'])
        deleted_base = next(row for row in entries if row['path'].endswith('deleted.bytes'))
        self.assertTrue(deleted_base['overlap_candidate'])
        self.assertEqual(deleted_base['overlaps_with'], patch.relative_to(root).as_posix())
        self.assertEqual([row['path'].rsplit('/', 1)[-1] for row in rows
                          if row['type'] == 'pruned_entry'], ['deleted.bytes'])
        self.assertEqual(output['available_entries'], 3)
        self.assertEqual(output['path'].read_bytes(), repeated['path'].read_bytes())
        self.assertIn('actual client load order UNKNOWN', manifest['priority_basis'])
