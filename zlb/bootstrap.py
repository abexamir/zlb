"""Opt-in SSH provisioning of a dedicated Debian/Ubuntu load balancer node VM."""
import hashlib
import ipaddress
import json
import logging
from pathlib import Path
import shlex
import subprocess
import tempfile
import time

from .http_api import read_token
from .model import canonical

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


def unit(listen, port, ips, interface, node_cidr):
    args=['--listen',str(ipaddress.IPv4Address(listen)),'--port',str(port),
          '--public-interface',interface,'--node-cidr',str(ipaddress.IPv4Network(node_cidr))]
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
                 node_cidr, ssh_port=22, interval=60):
        ipaddress.IPv4Address(host)
        if not user or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in user):
            raise ValueError('invalid SSH user')
        if not 1<=ssh_port<=65535 or not 1<=port<=65535:
            raise ValueError('invalid SSH/API port')
        self.host,self.user,self.key_file,self.known_hosts=host,user,key_file,known_hosts
        self.token_file,self.ssh_port,self.interval=token_file,ssh_port,interval
        self.unit=unit(listen,port,ips,interface,node_cidr)
        self.last_success=None

    def payload(self):
        files={path.name:path.read_text() for path in Path(__file__).parent.glob('*.py')}
        return dict(files=files,token=read_token(self.token_file),unit=self.unit)

    def invalidate(self):
        self.last_success=None

    def ensure(self):
        if self.last_success is not None and time.monotonic()-self.last_success<self.interval:
            return
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
        self.last_success=time.monotonic()
        LOG.info('load balancer node SSH bootstrap verified changed=%s',receipt.get('changed'))
