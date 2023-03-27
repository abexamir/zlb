"""Opt-in SSH provisioning of a dedicated Debian/Ubuntu load balancer node VM.

Runs as a one-shot command (a Helm hook Job or CronJob), never inside the long-running
controller, so the SSH root key and Kubernetes API credentials never share a process."""
import argparse
import hashlib
import ipaddress
import json
import logging
from pathlib import Path
import shlex
import subprocess
import tempfile
from urllib.parse import urlparse

from .http_api import read_token
from .model import NODE_PORTS, canonical, port_range

LOG = logging.getLogger(__name__)

# This runs as root on the explicitly configured VM. Input is carried only on SSH stdin.
REMOTE = r'''
import fcntl,hashlib,json,os,pathlib,shutil,subprocess,tempfile
p=json.load(__import__('sys').stdin)
def run(*args): subprocess.run(args,check=True,stdout=subprocess.DEVNULL,timeout=600)
with open('/run/zlb-bootstrap.lock','w') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 revision=hashlib.sha256(json.dumps(p,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 marker=pathlib.Path('/var/lib/zlb-bootstrap/revision')
 unit=pathlib.Path('/etc/systemd/system/zlb-natd.service')
 target=pathlib.Path('/opt/zlb/zlb')
 token=pathlib.Path('/etc/zlb/token')
 def healthy():
  return (marker.exists() and marker.read_text().strip()==revision and unit.exists()
    and unit.read_text()==p['unit'] and token.exists() and token.read_text().strip()==p['token']
    and all((target/name).exists() and (target/name).read_text()==data for name,data in p['files'].items())
    and subprocess.run(['systemctl','is-active','--quiet','zlb-natd.service']).returncode==0
    and all(shutil.which(x) for x in ('nft','ip')))
 if healthy():
  print(json.dumps({'changed':False,'revision':revision})); __import__('sys').exit(0)
 if unit.exists() and '-m zlb.natd' not in unit.read_text():
  raise RuntimeError('refusing to replace an unrelated systemd unit')
 if not all(shutil.which(x) for x in ('nft','ip')):
  if not shutil.which('apt-get'): raise RuntimeError('SSH bootstrap requires Debian/Ubuntu apt-get')
  run('apt-get','update')
  run('env','DEBIAN_FRONTEND=noninteractive','apt-get','install','-y','nftables','iproute2')
 pathlib.Path('/var/lib/zlb-bootstrap').mkdir(mode=0o700,parents=True,exist_ok=True)
 for name in p['files']:
  if pathlib.PurePath(name).name!=name or not name.endswith('.py'): raise ValueError('invalid package filename')
 with tempfile.TemporaryDirectory(prefix='backup-',dir='/var/lib/zlb-bootstrap') as directory:
  backup=pathlib.Path(directory)
  had_package=target.exists()
  if had_package: shutil.copytree(target,backup/'package')
  saved={str(path):path.read_bytes() if path.exists() else None for path in (unit,token,marker)}
  running=subprocess.run(['systemctl','is-active','--quiet','zlb-natd.service']).returncode==0
  def write(path,data,mode):
   path=pathlib.Path(path);path.parent.mkdir(parents=True,exist_ok=True)
   temporary=path.with_name('.'+path.name+'.new')
   with open(temporary,'wb') as stream: stream.write(data);stream.flush();os.fsync(stream.fileno())
   os.chmod(temporary,mode);os.replace(temporary,path)
  try:
   if unit.exists(): run('systemctl','stop','zlb-natd.service')
   if target.exists(): shutil.rmtree(target)
   target.mkdir(parents=True,mode=0o755)
   for name,data in p['files'].items(): write(target/name,data.encode(),0o644)
   write(token,p['token'].encode()+b'\n',0o600)
   write(unit,p['unit'].encode(),0o644)
   write('/etc/sysctl.d/90-zlb-forwarding.conf',b'net.ipv4.ip_forward=1\n',0o644)
   run('sysctl','-w','net.ipv4.ip_forward=1')
   run('systemd-analyze','verify',str(unit))
   run('systemctl','daemon-reload')
   run('systemctl','enable','--now','zlb-natd.service')
   run('systemctl','is-active','--quiet','zlb-natd.service')
   write(marker,(revision+'\n').encode(),0o600)
  except Exception:
   subprocess.run(['systemctl','stop','zlb-natd.service'],stdout=subprocess.DEVNULL)
   if target.exists(): shutil.rmtree(target)
   if had_package: shutil.copytree(backup/'package',target)
   for path,data in saved.items():
    if data is None: pathlib.Path(path).unlink(missing_ok=True)
    else: write(path,data,0o644 if path==str(unit) else 0o600)
   run('systemctl','daemon-reload')
   if running: run('systemctl','start','zlb-natd.service')
   raise
 print(json.dumps({'changed':True,'revision':revision}))
'''


def unit(listen, port, ips, interface, node_cidr, backend_ports=NODE_PORTS):
    first,last=(int(x) for x in backend_ports)
    if not 1<=first<=last<=65535:
        raise ValueError('invalid backend port range')
    args=['--listen',str(ipaddress.IPv4Address(listen)),'--port',str(port),
          '--public-interface',interface,'--node-cidr',str(ipaddress.IPv4Network(node_cidr)),
          '--backend-port-range',f'{first}-{last}']
    # Share the privileged daemon's exact validation before generating any shell/systemd input.
    from .addresses import Addresses
    validated=Addresses(None,interface,ips,'/nonexistent/zlb-bootstrap-journal')
    for address in validated.desired.values(): args.extend(['--ip',address])
    command='/usr/bin/python3 -m zlb.natd '+shlex.join(args)
    return f'''[Unit]
Description=zlb nftables load balancer node (controller managed)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=/opt/zlb
ExecStart={command} --token-file /etc/zlb/token
ExecStopPost={command} --cleanup
Restart=on-failure
RestartSec=2
RuntimeDirectory=zlb
RuntimeDirectoryMode=0700
StateDirectory=zlb
StateDirectoryMode=0700
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
ProtectHome=true
ProtectSystem=strict
PrivateTmp=true
[Install]
WantedBy=multi-user.target
'''


class SSHBootstrap:
    def __init__(self, host, user, key_file, known_hosts, token_file, listen, port, ips, interface,
                 node_cidr, ssh_port=22, backend_ports=NODE_PORTS):
        ipaddress.IPv4Address(host)
        if not user or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in user):
            raise ValueError('invalid SSH user')
        if not 1<=ssh_port<=65535 or not 1<=port<=65535:
            raise ValueError('invalid SSH/API port')
        self.host,self.user,self.key_file,self.known_hosts=host,user,key_file,known_hosts
        self.token_file,self.ssh_port=token_file,ssh_port
        self.unit=unit(listen,port,ips,interface,node_cidr,backend_ports)

    def payload(self):
        files={path.name:path.read_text() for path in Path(__file__).parent.glob('*.py')}
        return dict(files=files,token=read_token(self.token_file),unit=self.unit)

    def ensure(self):
        payload=self.payload()
        # Copy the mounted key to a mode-0600 temporary file for OpenSSH. Never put it in argv.
        with tempfile.TemporaryDirectory(prefix='zlb-ssh-') as directory:
            key=Path(directory)/'identity';key.write_bytes(Path(self.key_file).read_bytes());key.chmod(0o600)
            hosts=Path(directory)/'known_hosts';hosts.write_bytes(Path(self.known_hosts).read_bytes());hosts.chmod(0o600)
            script='''if ! command -v python3 >/dev/null; then
apt-get update >&2 && DEBIAN_FRONTEND=noninteractive apt-get install -y python3 >&2 || exit 1
fi
exec python3 -c '''+shlex.quote(REMOTE)
            command=script if self.user=='root' else 'sudo -n sh -c '+shlex.quote(script)
            args=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                  '-o',f'UserKnownHostsFile={hosts}','-o','IdentitiesOnly=yes',
                  '-o','ConnectTimeout=10','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
                  '-i',str(key),'-p',str(self.ssh_port),self.user+'@'+self.host,command]
            result=subprocess.run(args,input=canonical(payload),capture_output=True,timeout=900)
            if result.returncode:
                # Remote output may contain privileged command data; never forward it to logs.
                raise RuntimeError(f'SSH load balancer node bootstrap failed (exit {result.returncode}); inspect the VM')
            receipt=json.loads(result.stdout)
            expected=hashlib.sha256(canonical(payload)).hexdigest()
            if receipt.get('revision')!=expected:
                raise RuntimeError('SSH bootstrap revision was not acknowledged')
        LOG.info('load balancer node SSH bootstrap verified changed=%s',receipt.get('changed'))
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lb-node", required=True, help="Daemon API URL used by the controller")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--key-file", required=True, help="Dedicated SSH private key for the VM")
    parser.add_argument("--known-hosts-file", required=True, help="Trusted host key; no TOFU")
    parser.add_argument("--ssh-host", help="Defaults to the --lb-node host")
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--public-interface", required=True)
    parser.add_argument("--ip", action="append", required=True,
                        help="Daemon public IPv4 allowlist entry, optionally /prefix")
    parser.add_argument("--node-cidr", required=True)
    parser.add_argument("--backend-port-range", type=port_range, default=NODE_PORTS)
    args = parser.parse_args(argv)
    endpoint = urlparse(args.lb_node)
    if endpoint.scheme != "http" or not endpoint.hostname:
        parser.error("SSH bootstrap configures the native HTTP daemon; use a trusted private HTTP endpoint")
    logging.basicConfig(level=logging.INFO)
    try:
        SSHBootstrap(args.ssh_host or endpoint.hostname, args.ssh_user, args.key_file,
                     args.known_hosts_file, args.token_file, endpoint.hostname, endpoint.port or 9808,
                     args.ip, args.public_interface, args.node_cidr, args.ssh_port,
                     args.backend_port_range).ensure()
    except Exception:
        LOG.exception("load balancer node bootstrap failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
