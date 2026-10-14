import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zlb.bootstrap import SSHBootstrap,main,sandbox,unit
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

    def test_tls_material_and_api_allowlist_are_installed(self):
        for name in ('tls.crt','tls.key','ca.crt'):(self.root/name).write_text(name+' PEM')
        files={'tls.crt':self.root/'tls.crt','tls.key':self.root/'tls.key','client-ca.crt':self.root/'ca.crt'}
        bootstrap=SSHBootstrap('10.0.0.3','root',self.root/'key',self.root/'hosts',self.root/'token','10.0.0.3',
            9808,['198.51.100.10/32'],'nic1','10.0.0.0/24',tls_files=files,allow_from=['10.0.0.0/24'])
        for flag in ('--tls-cert /etc/zlb/tls.crt --tls-key /etc/zlb/tls.key',
                     '--tls-client-ca /etc/zlb/client-ca.crt','--allow-from 10.0.0.0/24'):
            self.assertIn(flag,bootstrap.unit)
        self.assertEqual(bootstrap.payload()['tls'],{'tls.crt':'tls.crt PEM','tls.key':'tls.key PEM','client-ca.crt':'ca.crt PEM'})
        self.assertEqual(self.bootstrap.payload()['tls'],{})
        self.assertNotIn('--tls',self.bootstrap.unit)
        with self.assertRaises(ValueError):
            SSHBootstrap('10.0.0.3','root','k','h','t','10.0.0.3',9808,['198.51.100.10'],'nic1','10.0.0.0/24',
                         tls_files={'tls.crt':'c'})
        with self.assertRaises(ValueError):
            unit('10.0.0.3',9808,['198.51.100.10'],'nic1','10.0.0.0/24',client_ca=True)
        with self.assertRaises(ValueError):
            unit('10.0.0.3',9808,['198.51.100.10'],'nic1','10.0.0.0/24',allow_from=['not-a-cidr'])

    def test_daemon_runs_unprivileged_and_sandboxed(self):
        text=self.bootstrap.unit
        for line in ('User=zlb-natd','AmbientCapabilities=CAP_NET_ADMIN\n','CapabilityBoundingSet=CAP_NET_ADMIN\n',
                     'NoNewPrivileges=true','ProtectSystem=strict','SystemCallFilter=@system-service',
                     'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK'):
            self.assertIn(line,text)
        self.assertNotIn('CAP_NET_RAW',text)
        privileged=unit('10.0.0.3',443,['198.51.100.10'],'nic1','10.0.0.0/24')
        self.assertIn('AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE',privileged)
        example=(Path(__file__).parents[1]/'examples/systemd/zlb-natd.service').read_text()
        self.assertIn(sandbox(['CAP_NET_ADMIN']),example)

    def test_opt_in_routes_are_validated_and_passed_to_both_unit_commands(self):
        wanted={'destination':'0.0.0.0/0','interface':'eth1','gateway':'198.51.100.1'}
        text=unit('10.0.0.3',9808,['198.51.100.10'],'eth1','10.0.0.0/24',routes=[wanted])
        self.assertEqual(text.count('--route'),2)
        self.assertIn('198.51.100.1',text)
        with self.assertRaises(ValueError):
            unit('10.0.0.3',9808,['198.51.100.10'],'eth1','10.0.0.0/24',routes=[dict(wanted,interface='bad%h')])

    def test_host_guard_flags(self):
        self.assertNotIn('--allow-foreign-forwarding',self.bootstrap.unit)
        text=unit('10.0.0.3',9808,['198.51.100.10'],'nic1','10.0.0.0/24',allow_foreign_forwarding=True,isolate_public=True)
        self.assertIn('--allow-foreign-forwarding',text)
        self.assertIn('--isolate-public-ips',text)
        self.assertNotIn('--hairpin-snat-from',text)
        text=unit('10.0.0.3',9808,['198.51.100.10'],'nic1','10.0.0.0/24',hairpin=['10.0.0.0/24'])
        self.assertIn('--hairpin-snat-from 10.0.0.0/24',text)
        with self.assertRaises(ValueError):
            unit('10.0.0.3',9808,['198.51.100.10'],'nic1','10.0.0.0/24',hairpin=['10.0.0.1/24'])

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

    def test_https_endpoint_requires_matching_tls_files(self):
        for args in (self.args('https://10.0.0.3:9808'),
                     self.args()+['--tls-cert-file','/c','--tls-key-file','/k'],
                     self.args('ftp://10.0.0.3'),
                     self.args('https://10.0.0.3')+['--tls-cert-file','/c','--tls-key-file','/k']):
            with self.subTest(args=args),self.assertRaises(SystemExit),patch('sys.stderr'):
                main(args)
        with patch('zlb.bootstrap.SSHBootstrap') as bootstrap:
            self.assertEqual(main(self.args('https://10.0.0.3:9443')+['--tls-cert-file','/c','--tls-key-file','/k',
                '--tls-client-ca-file','/ca','--allow-from','10.20.0.0/24']),0)
            self.assertEqual(bootstrap.call_args.args[6],9443)
            self.assertEqual(bootstrap.call_args.args[12],{'tls.crt':'/c','tls.key':'/k','client-ca.crt':'/ca'})
            self.assertEqual([str(x) for x in bootstrap.call_args.args[13]],['10.20.0.0/24'])

    def test_controller_no_longer_accepts_ssh_material(self):
        from zlb import controller
        source=Path(controller.__file__).read_text()
        self.assertNotIn('bootstrap',source)
