from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kiker.audit import Audit, Finding
from kiker.report import Run
from kiker.snapshot import compare, snapshot, validate_snapshot
from kiker.source import read_document
from kiker.web import Conflict, Server, Store


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root / 'requirements.md'
        path.write_text('# Повтор\n\nREQ-1. Вернуть старый номер.\n\nREQ-2. Вернуть новый номер.\n')
        self.document = read_document(path)
        self.finding = Finding('conflict', 'major', 'Два разных номера для повтора.',
                               'Какой номер вернуть?', ('S001', 'S002'), 'Проверка')
        self.run = Run(datetime.now(timezone.utc), 'test', 'test', None, 1.5, 2, 30, 20, 'Test')
        self.bundle = snapshot(self.document, Audit(self.document.blocks, (self.finding,)), self.run)
        self.store = Store(self.root / 'runs', execute=lambda document, progress: (
            Audit(document.blocks, ()), self.run))
        self.addCleanup(self.store.close)

    def test_saved_report_roundtrip_retains_sources_and_provenance(self):
        result = json.loads(json.dumps(self.bundle))
        self.assertEqual(validate_snapshot(result), result)
        imported = self.store.import_result(result)
        self.assertTrue(imported['imported'])
        self.assertEqual(imported['result']['report'], self.bundle['report'])
        self.assertEqual(imported['result']['document']['sha256'], self.document.sha256)

    def test_import_rejects_unknown_sources_duplicate_ids_and_invalid_numbers(self):
        for mutate in (
            lambda b: b['findings'][0].update(sources=['S001', 'S999']),
            lambda b: b['document']['blocks'].append(deepcopy(b['document']['blocks'][0])),
            lambda b: b['run'].update(seconds=float('nan')),
        ):
            value = json.loads(json.dumps(self.bundle)); mutate(value)
            with self.assertRaises(ValueError): self.store.import_result(value)
        self.assertEqual(self.store.list(), [])

    def test_decisions_persist_without_mutating_model_report(self):
        entry = self.store.import_result(json.loads(json.dumps(self.bundle)))
        decision = {'revision': 0, 'id': 'D001', 'decision': 'accepted', 'note': 'Вернуть старый номер.'}
        self.store.decide(entry['id'], decision)
        with self.assertRaises(Conflict): self.store.decide(entry['id'], decision)
        reloaded = Store(self.root / 'runs'); self.addCleanup(reloaded.close)
        saved = reloaded.get(entry['id'])
        self.assertEqual(saved['decisions']['items']['D001']['note'], decision['note'])
        self.assertEqual(saved['result']['report'], self.bundle['report'])

    def test_upload_uses_actual_input_and_rejects_empty_or_unsupported_files(self):
        for name, data in [('bad.md', b''), ('bad.exe', b'data'), ('../outside.md', b'data'),
                           ('empty.md', b'# Only a heading')]:
            with self.assertRaises(ValueError): self.store.submit(name, data)
        self.assertEqual(self.store.list(), [])
        entry = self.store.submit('test.md', b'# Test\n\nREQ-1. Return the first number.\n')
        self.store.close()
        result = self.store.get(entry['id'])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['result']['document']['name'], 'test.md')

    def test_comparison_handles_shifted_source_ids_and_does_not_call_absence_a_fix(self):
        before = json.loads(json.dumps(self.bundle)); after = deepcopy(before)
        for block in after['document']['blocks']:
            block['id'] = {'S001': 'S003', 'S002': 'S004'}[block['id']]
        after['findings'][0]['sources'] = ['S003', 'S004']
        result = compare(before, after)
        self.assertEqual(result['matched'], [{'before': 'D001', 'after': 'D001'}])
        after['findings'] = []
        result = compare(before, after)
        self.assertEqual(result['not_repeated'], ['D001'])
        self.assertNotIn('fixed', result)

    def test_comparison_does_not_match_unrelated_sources_by_position(self):
        before = json.loads(json.dumps(self.bundle)); after = deepcopy(before)
        for b in after['document']['blocks']:
            b.update(original_id=None, text='Порог уведомлений обязан быть меньше миллиона тенге.')
        after['findings'][0].update(description='Не заданы границы порога.', question='Какие границы порога?')
        result = compare(before, after)
        self.assertEqual(result['matched'], [])
        self.assertEqual(result['new'], ['D001'])

    def test_http_rejects_cross_origin_writes_and_untrusted_host(self):
        server = Server(('127.0.0.1', 0), self.store)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_address[1]}'
        for headers in ({}, {'X-Kiker-Token': server.token, 'Origin': 'https://other.example'}):
            request = Request(base + '/api/import', data=b'{}', headers=headers)
            with self.assertRaises(HTTPError) as caught: urlopen(request)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(base + '/api/session', headers={'Host': 'other.example'}))
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        with urlopen(Request(base + '/api/import', data=json.dumps(self.bundle).encode(),
                             headers={'X-Kiker-Token': server.token, 'Content-Type': 'application/json'})) as response:
            self.assertEqual(response.status, 201)
            self.assertEqual(json.load(response)['status'], 'complete')

    def test_interrupted_run_is_visible_as_error_after_restart(self):
        directory, entry = self.store._create('interrupted.md', 'running')
        restarted = Store(self.root / 'runs'); self.addCleanup(restarted.close)
        self.assertEqual(restarted.get(entry['id'])['status'], 'error')


if __name__ == '__main__': unittest.main()
