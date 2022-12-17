#!/usr/bin/env python3
"""Validate local documentation links, examples and source release versions offline."""
from pathlib import Path
import json
import re
import subprocess
import tomllib
import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    count = 0
    for path in ROOT.rglob('*.md'):
        if '.git' in path.parts or '.venv' in path.parts:
            continue
        text = path.read_text()
        for target in re.findall(r'\]\(([^)]+)\)', text):
            if target.startswith(('http:', 'https:', '#', 'mailto:')):
                continue
            if not (path.parent / target.split('#')[0]).exists():
                raise ValueError(f'{path.relative_to(ROOT)}: missing link {target}')
        blocks = re.findall(r'^```([^\n]*)\n(.*?)^```\s*$', text, re.M | re.S)
        if text.count('```') != 2 * len(blocks):
            raise ValueError(f'{path}: unclosed code fence')
        for language, body in blocks:
            if language in ('sh', 'bash'):
                subprocess.run(['bash', '-n'], input=body, text=True, check=True)
            elif language == 'yaml':
                list(yaml.safe_load_all(body))
            elif language == 'json':
                json.loads(body)
            count += 1
    app = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    chart = yaml.safe_load((ROOT / 'charts/zlb/Chart.yaml').read_text())
    values = yaml.safe_load((ROOT / 'charts/zlb/values.yaml').read_text())
    assert chart['appVersion'] == app == values['image']['tag'], 'source versions disagree'
    assert 'lbNode' in values and 'gateway' not in values
    print(f'Validated local links, {count} fenced examples, and release versions')


if __name__ == '__main__':
    main()
