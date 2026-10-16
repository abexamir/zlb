import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zlb.bootstrap import SSHBootstrap,main,unit
from zlb.model import digest


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        for name,data in [('key','PRIVATE KEY FOR TEST'),('hosts','10.0.0.3 ssh-ed25519 TEST'),('token','a'*32)]:
            (self.root/name).write_text(data)
        self.bootstrap=SSHBootstrap('10.0.0.3','root',self.root/'key',self.root/'hosts',self.root/'token',
            '10.0.0.3',9808,['198.51.100.10/32'],'nic1','10.0.0.0/24')

    def test_verified_host_key_and_private_stdin_payload(self):
        def command(args,**kwargs):
            self.assertIn('StrictHostKeyChecking=yes',args)
            self.assertIn('IdentitiesOnly=yes',args)
            self.assertNotIn('a'*32,' '.join(args))
            key=Path(args[args.index('-i')+1]);self.assertEqual(key.stat().st_mode&0o777,0o600)
            payload=json.loads(kwargs['input'])
            self.assertEqual(payload['token'],'a'*32)
            self.assertIn('natd.py',payload['files'])
            return subprocess.CompletedProcess(args,0,json.dumps({'revision':digest(payload),'changed':True}).encode(),b'')
        with patch('zlb.bootstrap.subprocess.run',side_effect=command) as run:
            self.assertTrue(self.bootstrap.ensure()['changed'])
            self.assertEqual(run.call_count,1)

    def test_bad_receipt_does_not_acknowledge_bootstrap(self):
        with patch('zlb.bootstrap.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'{"revision":"wrong"}',b'')):
            with self.assertRaisesRegex(RuntimeError,'not acknowledged'):self.bootstrap.ensure()

    def test_remote_failure_does_not_disclose_output(self):
        result=subprocess.CompletedProcess([],1,b'private stdout',b'private stderr')
        with patch('zlb.bootstrap.subprocess.run',return_value=result):
            with self.assertRaisesRegex(RuntimeError,'exit 1') as error:self.bootstrap.ensure()
        self.assertNotIn('private',str(error.exception))

    def test_reject_invalid_network_or_shell_input(self):
        for interface in ('eth0; reboot','bad%h'):
            with self.assertRaises(ValueError):unit('10.0.0.3',9808,['198.51.100.10'],interface,'10.0.0.0/24')
        for address in ('198.51.100.10; reboot','not-an-ip'):
            with self.assertRaises(ValueError):unit('10.0.0.3',9808,[address],'nic1','10.0.0.0/24')

    def test_unit_pins_backend_port_range(self):
        self.assertIn('--backend-port-range 30000-32767',self.bootstrap.unit)
        text=unit('10.0.0.3',9808,['198.51.100.10'],'nic1','10.0.0.0/24',(20000,22767))
        self.assertIn('--backend-port-range 20000-22767',text)
        for invalid in ((0,10),(32767,30000),(1,65536)):
            with self.assertRaises(ValueError):unit('10.0.0.3',9808,['198.51.100.10'],'nic1','10.0.0.0/24',invalid)

    def test_cleanup_and_daemon_use_same_allowlist(self):
        text=self.bootstrap.unit
        self.assertIn('--ip 198.51.100.10/32',text)
        self.assertIn('--public-interface nic1',text)
        self.assertIn('--cleanup',text)
        self.assertIn('StateDirectory=zlb',text)


class BootstrapCommandTests(unittest.TestCase):
    def args(self,url='http://10.0.0.3:9808'):
        return ['--lb-node',url,'--token-file','/t','--key-file','/k','--known-hosts-file','/h',
                '--public-interface','eth1','--ip','198.51.100.10/32','--node-cidr','10.0.0.0/24']

    def test_one_shot_run_reports_success_and_failure(self):
        with patch('zlb.bootstrap.SSHBootstrap') as bootstrap:
            self.assertEqual(main(self.args()),0)
            host,user,key,hosts,token,listen,port=bootstrap.call_args.args[:7]
            self.assertEqual((host,user,listen,port),('10.0.0.3','root','10.0.0.3',9808))
            bootstrap.return_value.ensure.assert_called_once_with()
            bootstrap.return_value.ensure.side_effect=RuntimeError('SSH failed')
            with self.assertLogs('zlb.bootstrap',level='ERROR'):
                self.assertEqual(main(self.args()),1)

    def test_requires_native_http_endpoint(self):
        with self.assertRaises(SystemExit),patch('sys.stderr'):
            main(self.args('https://10.0.0.3:9808'))

    def test_controller_no_longer_accepts_ssh_material(self):
        from zlb import controller
        source=Path(controller.__file__).read_text()
        self.assertNotIn('bootstrap',source)
