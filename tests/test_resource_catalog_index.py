import json

from tests.test_discovery import FixtureCase
from wuwaru.resource_catalog_index import ResourceCatalogIndex


class ResourceCatalogIndexTests(FixtureCase):
    def test_streams_jsonl_into_indexed_sqlite_and_searches_without_loading_catalog(self):
        source = self.home / 'catalog.jsonl'
        rows = [
            {'type': 'catalog', 'schema_version': 1, 'mode': 'READ_ONLY'},
            {'type': 'package', 'archive': 'base.pak', 'kind': 'BASE_RESOURCE'},
            {'type': 'entry', 'archive': 'base.pak', 'path': 'Config/EN/Text.db',
             'offset': 10, 'size': 22, 'compression': 'None'},
            {'type': 'package', 'archive': 'patch.pak', 'kind': 'PATCH_RESOURCE'},
            {'type': 'entry', 'archive': 'patch.pak', 'path': 'Config/en/Text.db',
             'offset': 20, 'size': 23, 'compression': 'None'},
            {'type': 'pruned_entry', 'archive': 'patch.pak', 'path': 'Config/en/Old.db'},
            {'type': 'complete', 'package_count': 2, 'available_entries': 2},
        ]
        source.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

        result = ResourceCatalogIndex.build(source, self.home / 'catalog.sqlite')
        with ResourceCatalogIndex(result) as index:
            matches = index.search(path_prefix='config/en/', extensions={'.db'},
                                   include_pruned=False)
            no_extensions = index.search(extensions=set())
            active = index.search(exact_path='CONFIG/EN/OLD.DB', include_pruned=False)
            pruned = index.search(exact_path='CONFIG/EN/OLD.DB')

        self.assertEqual(result.stat().st_size > 0, True)
        self.assertEqual(len(matches), 2)
        self.assertEqual(no_extensions, ())
        self.assertEqual({row['archive'] for row in matches}, {'base.pak', 'patch.pak'})
        self.assertEqual(active, ())
        self.assertEqual(pruned[0]['type'], 'pruned_entry')

    def test_rejects_incomplete_jsonl_without_publishing_database(self):
        source = self.home / 'incomplete.jsonl'
        source.write_text('{"type":"catalog","schema_version":1,"mode":"READ_ONLY"}\n',
                          encoding='utf-8')
        target = self.home / 'incomplete.sqlite'

        with self.assertRaisesRegex(ValueError, 'complete'):
            ResourceCatalogIndex.build(source, target)

        self.assertFalse(target.exists())
        self.assertEqual([item.name for item in self.home.iterdir()], ['incomplete.jsonl'])
