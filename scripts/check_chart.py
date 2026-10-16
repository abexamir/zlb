#!/usr/bin/env python3
"""Render the chart with SSH bootstrap enabled and check its upgrade-ordering and credential
invariants: no older bootstrap writer may run after the bootstrap hook, and no pod may hold
both the SSH key and Kubernetes API credentials."""
from pathlib import Path
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]
VALUES = ['--set', 'bootstrap.enabled=true', '--set', 'bootstrap.existingSecret=zlb-ssh',
          '--set', 'bootstrap.sshHost=10.0.0.3', '--set', 'bootstrap.publicInterface=eth1',
          '--set', 'bootstrap.publicIPs={198.51.100.10/32}', '--set', 'bootstrap.schedule=0 * * * *']


def render():
    output = subprocess.run(['helm', 'template', 'zlb', str(ROOT / 'charts/zlb'), '-n', 'zlb-system',
                             '-f', str(ROOT / 'examples/helm-values.yaml'), *VALUES],
                            check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(output) if d]


def template(document):
    spec = document['spec']
    if document['kind'] == 'CronJob':
        spec = spec['jobTemplate']['spec']
    return spec['template']


def pod(document):
    return template(document)['spec']


def hook(document, event):
    annotations = document['metadata'].get('annotations', {})
    events = annotations.get('helm.sh/hook', '').split(',')
    return int(annotations.get('helm.sh/hook-weight', '0')) if event in events else None


def main():
    documents = render()
    by_name = {(d['kind'], d['metadata']['name']): d for d in documents}
    bootstrap = by_name[('Job', 'zlb-controller-bootstrap')]
    quiesce = by_name[('Job', 'zlb-controller-quiesce')]
    for event in ('pre-upgrade', 'pre-rollback'):
        assert hook(quiesce, event) is not None and hook(bootstrap, event) is not None, event
        assert hook(quiesce, event) < hook(bootstrap, event), f'quiesce must precede bootstrap on {event}'
        for kind in ('ServiceAccount', 'Role', 'RoleBinding'):
            assert hook(by_name[(kind, 'zlb-controller-quiesce')], event) < hook(quiesce, event), kind
    assert hook(bootstrap, 'pre-install') is not None
    assert by_name[('CronJob', 'zlb-controller-bootstrap')]['spec']['suspend'] is False, \
        'an upgrade must resume the CronJob that quiesce suspended'
    controller = by_name[('Deployment', 'zlb-controller')]
    assert controller['spec']['replicas'] == 1, 'an upgrade must restore a controller that quiesce scaled down'
    # quiesce waits for every pod matching a legacy controller's selector, which is the
    # controller selector rendered here; no hook or bootstrap pod may ever match it.
    selector = controller['spec']['selector']['matchLabels']
    for document in documents:
        if document['kind'] in ('Job', 'CronJob'):
            labels = template(document)['metadata'].get('labels', {})
            assert not all(labels.get(k) == v for k, v in selector.items()), \
                f"{document['metadata']['name']} pods match the controller selector {selector}"
    env = {e['name']: e for e in pod(quiesce)['containers'][0].get('env', [])}
    assert env['POD_NAME']['valueFrom']['fieldRef']['fieldPath'] == 'metadata.name'
    for document in documents:
        if document['kind'] not in ('Deployment', 'Job', 'CronJob'):
            continue
        spec = pod(document)
        ssh = any(v.get('secret', {}).get('secretName') == 'zlb-ssh' for v in spec.get('volumes', []))
        api = spec.get('automountServiceAccountToken', True)
        assert not (ssh and api), f"{document['metadata']['name']} would hold the SSH key and API credentials"
        args = [a for c in spec['containers'] for a in c.get('args', [])]
        assert document['kind'] != 'Deployment' or not any(a.startswith('--bootstrap') for a in args)
    print('Validated chart hook ordering and credential separation')


if __name__ == '__main__':
    main()
