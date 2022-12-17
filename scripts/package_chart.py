#!/usr/bin/env python3
"""Package a chart whose immutable image matches the build; never edit source files."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def replace_one(text, pattern, replacement):
    result, count = re.subn(pattern, lambda _: replacement, text, flags=re.M)
    if count != 1:
        raise ValueError(f'expected one field matching {pattern}')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--image-repository', default='ghcr.io/abexamir/zlb')
    parser.add_argument('--image-tag', required=True)
    parser.add_argument('--image-digest', default='')
    args = parser.parse_args()
    if args.image_digest and not re.fullmatch(r'sha256:[a-f0-9]{64}', args.image_digest):
        parser.error('invalid image digest')
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='zlb-chart-') as temporary:
        chart = Path(temporary) / 'zlb'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'charts/zlb', chart)
        values = (chart / 'values.yaml').read_text()
        import json
        for field, value in [('repository', args.image_repository), ('tag', args.image_tag), ('digest', args.image_digest)]:
            values = replace_one(values, rf'^  {field}:.*$', f'  {field}: {json.dumps(value)}')
        (chart / 'values.yaml').write_text(values)
        subprocess.run(['helm', 'lint', str(chart), '--set', 'lbNode.url=https://lb.example.invalid',
                        '--set', 'lbNode.existingSecret=zlb-api-token', '--set', 'nodeCIDR=10.20.0.0/24'], check=True)
        subprocess.run(['helm', 'package', str(chart), '--version', args.version,
                        '--app-version', args.image_tag, '--destination', str(output)], check=True)


if __name__ == '__main__':
    main()
