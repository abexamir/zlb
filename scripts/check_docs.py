#!/usr/bin/env python3
"""Validate local documentation links, examples and source release versions offline."""
from pathlib import Path
import json
import re
import subprocess
import tomllib
import yaml

ROOT = Path(__file__).resolve().parents[1]
FENCE = re.compile(r'^```.*?^```\s*$', re.M | re.S)


def anchors(text):
    """GitHub heading anchors: lowercased, punctuation dropped, spaces to hyphens, and
    -1, -2... appended to repeated slugs. Headings inside code fences do not count."""
    seen, result = {}, set()
    for heading in re.findall(r'^#{1,6}\s+(.+?)\s*#*\s*$', FENCE.sub('', text), re.M):
        heading = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', heading)  # link text only
        slug = re.sub(r'[^\w\- ]', '', heading.replace('`', '').lower()).replace(' ', '-')
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else f'{slug}-{count}')
    return result


def rendered_example():
    """The internals guide shows real renderer output for its JSON example; keep them equal."""
    import sys
    sys.path.insert(0, str(ROOT))
    from zlb.natd import render
    text = (ROOT / 'docs/internals.md').read_text()
    state = json.loads(re.search(r'^```json\n(\{\s*"version": 3.*?)^```', text, re.M | re.S).group(1))
    shown = re.search(r'^```nft\n(table ip zlb \{.*?)^```', text, re.M | re.S).group(1)
    ips = sorted({x['ip'] for x in state['listeners']})
    if render(state, '10.0.0.0/24', allowed_ips=ips, public_interface='eth0') != shown:
        raise ValueError('docs/internals.md: the generated rules differ from render() for the JSON example')


def main():
    count = 0
    for path in ROOT.rglob('*.md'):
        if '.git' in path.parts or '.venv' in path.parts:
            continue
        text = path.read_text()
        for target in re.findall(r'\]\(([^)\s]+)\)', FENCE.sub('', text)):
            if target.startswith(('http:', 'https:', 'mailto:')):
                continue
            name, _, fragment = target.partition('#')
            destination = (path.parent / name).resolve() if name else path
            if not destination.exists():
                raise ValueError(f'{path.relative_to(ROOT)}: missing link {target}')
            if fragment and destination.suffix == '.md' and fragment not in anchors(destination.read_text()):
                raise ValueError(f'{path.relative_to(ROOT)}: missing anchor {target}')
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
    rendered_example()
    app = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    chart = yaml.safe_load((ROOT / 'charts/zlb/Chart.yaml').read_text())
    values = yaml.safe_load((ROOT / 'charts/zlb/values.yaml').read_text())
    assert chart['appVersion'] == app == values['image']['tag'], 'source versions disagree'
    assert 'lbNode' in values and 'gateway' not in values
    print(f'Validated local links and anchors, {count} fenced examples, and release versions')


if __name__ == '__main__':
    main()
