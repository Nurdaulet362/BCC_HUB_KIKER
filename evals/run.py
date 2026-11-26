"""Run labelled requirement cases against KIKER and an optional single-call baseline."""

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

from kiker.__main__ import _version, _write_report
from kiker.audit import Audit, SYSTEM, _ask_records, _findings, _pack, _schema, review
from kiker.config import ModelError
from kiker.gemini import Gemini
from kiker.report import Run, render
from kiker.source import read_document
from kiker.snapshot import snapshot


def evaluate(case, document, audit):
    expected = []
    for pair in case['conflicts']:
        ids = set()
        for quote in pair:
            matches = [block.id for block in document.blocks if quote in block.text]
            if len(matches) != 1:
                raise ValueError(f'Expected one source for {quote!r}, found {matches}')
            ids.add(matches[0])
        expected.append(ids)
    found = sum(any(item.kind == 'conflict' and pair <= set(item.sources)
                    for item in audit.findings) for pair in expected)
    unexpected = sum(not any(item.kind == 'conflict' and pair <= set(item.sources)
                             for pair in expected) for item in audit.findings)
    return {
        'expected_conflicts': len(expected), 'found_conflicts': found,
        'unexpected_findings': unexpected,
        'passed': found == len(expected) and unexpected == 0 and audit.rejected == 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3, choices=range(1, 6))
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--suite', choices=('regression', 'holdout'), default='regression')
    parser.add_argument('--trace', action='store_true', help='Save model responses locally for diagnosis')
    parser.add_argument('--case', action='append', help='Run only the selected case ID')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).parent
    suite_root = root / 'holdout' if args.suite == 'holdout' else root
    cases = json.loads((suite_root / 'cases.json').read_text(encoding='utf-8'))
    if args.case:
        missing = set(args.case) - {case['id'] for case in cases}
        if missing:
            parser.error(f'Unknown case IDs: {sorted(missing)}')
        cases = [case for case in cases if case['id'] in args.case]
    results = []
    version = _version()
    modes = ['kiker', 'baseline'] if args.baseline else ['kiker']
    for mode in modes:
        for repeat in range(1, args.repeats + 1):
            for case in cases:
                filename = (suite_root if args.suite == 'holdout' else root / 'cases') / f'{case["id"]}.md'
                document = read_document(filename)
                client = Gemini()
                if args.trace:
                    original_chat = client.chat
                    trace = []
                    trace_path = args.output / f'trace-{mode}-{case["id"]}-{repeat}.json'
                    def traced_chat(system, content, schema, output_limit):
                        response = original_chat(system, content, schema, output_limit)
                        trace.append({'stage': 'verification' if 'checks' in schema.get('properties', {}) else 'analysis',
                                      'response': response})
                        _write_report(trace_path, json.dumps(trace, ensure_ascii=False, indent=2) + '\n')
                        return response
                    client.chat = traced_chat
                start = time.monotonic()
                record = {
                    'case': case['id'], 'mode': mode, 'repeat': repeat,
                    'source_sha256': hashlib.sha256(filename.read_bytes()).hexdigest(),
                    'version': version, 'model': client.model,
                    'suite': args.suite,
                    'thinking_level': client.thinking_level, 'timeout': client.timeout,
                    'request_interval': client.request_interval,
                }
                try:
                    if mode == 'kiker':
                        audit = review(document, client, lambda _: None)
                    else:
                        answer, rejected = _ask_records(
                            client, SYSTEM,
                            'Проверь требования по отдельности и весь набор. Найди существенные '
                            'дефекты и сверь критерии приёмки с правилами.\n\nИсходные фрагменты\n'
                            + _pack(document.blocks), _schema(list(document.blocks), False), 2200,
                        )
                        audit = Audit(document.blocks, tuple(_findings(answer['issues'], 'Один запрос')), rejected)
                    record.update(evaluate(case, document, audit))
                    record['findings'] = [asdict(item) for item in audit.findings]
                    record['rejected'] = audit.rejected
                    run = Run(datetime.now().astimezone(), version, client.model, None,
                              time.monotonic() - start, client.calls, client.input_tokens,
                              client.output_tokens, 'Gemini API', client.version,
                              {'thinking_level': client.thinking_level, 'timeout': client.timeout,
                               'request_interval': client.request_interval}, client.attempts)
                    _write_report(args.output / f'{mode}-{case["id"]}-{repeat}.md', render(document, audit, run))
                    _write_report(args.output / f'{mode}-{case["id"]}-{repeat}.json',
                                  json.dumps(snapshot(document, audit, run), ensure_ascii=False, indent=2) + '\n')
                except (ModelError, ValueError, OSError) as error:
                    record.update(passed=False, error=str(error))
                record.update(seconds=round(time.monotonic() - start, 2), requests=client.calls,
                              input_tokens=client.input_tokens, output_tokens=client.output_tokens,
                              model_version=client.version)
                record.update(attempts=client.attempts, request_seconds=client.request_times)
                results.append(record)
                _write_report(args.output / 'results.json', json.dumps(results, ensure_ascii=False, indent=2) + '\n')
                print(f'{mode} {repeat} {case["id"]}: {"PASS" if record["passed"] else "FAIL"} '
                      f'{record["seconds"]}s', flush=True)
                if 'дневной лимит' in record.get('error', ''):
                    return 1
    return int(any(not result['passed'] for result in results if result['mode'] == 'kiker'))


if __name__ == '__main__':
    raise SystemExit(main())
