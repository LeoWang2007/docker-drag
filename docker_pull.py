import os
import sys
import gzip
from io import BytesIO
import json
import hashlib
import shutil
import requests
import tarfile
import urllib3
import time
import argparse
import base64

urllib3.disable_warnings()

_auth_cache = {}
_auth_config = {'username': None, 'password': None, 'token': None}

def get_basic_auth_header():
    if _auth_config['username'] and _auth_config['password'] is not None:
        auth_str = base64.b64encode(f"{_auth_config['username']}:{_auth_config['password']}".encode()).decode()
        return {'Authorization': 'Basic ' + auth_str}
    return {}

def get_token_from_auth_url(auth_url, service, scope):
    if _auth_config['token']:
        return _auth_config['token']
    url = f"{auth_url}?service={service}&scope={scope}"
    auth_headers = get_basic_auth_header()
    try:
        resp = requests.get(url, headers=auth_headers, verify=False, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            token = data.get('token') or data.get('access_token')
            if token:
                return token
        print(f"[Warning] Token request failed: HTTP {resp.status_code}, {resp.text[:200]}")
    except Exception as e:
        print(f"[Warning] Token request exception: {e}")
    return None

def do_request_with_auth(url, method='GET', headers=None, stream=False, timeout=60, auth_retry=True):
    if headers is None:
        headers = {}
    req_headers = headers.copy()
    if _auth_config['token']:
        req_headers['Authorization'] = 'Bearer ' + _auth_config['token']
    resp = requests.request(method, url, headers=req_headers, stream=stream, verify=False, timeout=timeout)
    if resp.status_code == 401 and auth_retry and not _auth_config['token']:
        auth_header = resp.headers.get('WWW-Authenticate', '')
        realm = service = scope = None
        parts = auth_header.split(',')
        for part in parts:
            part = part.strip()
            if 'realm=' in part:
                realm = part.split('"')[1]
            elif 'service=' in part:
                service = part.split('"')[1]
            elif 'scope=' in part:
                scope = part.split('"')[1]
        if realm and service and scope:
            token = get_token_from_auth_url(realm, service, scope)
            if token:
                req_headers['Authorization'] = 'Bearer ' + token
                resp = requests.request(method, url, headers=req_headers, stream=stream, verify=False, timeout=timeout)
                if resp.status_code == 200:
                    print("[+] Authentication succeeded.")
                else:
                    print(f"[-] Authentication succeeded but request still failed: {resp.status_code}")
            else:
                print("[-] Failed to obtain token.")
        else:
            print("[-] Could not parse WWW-Authenticate headers.")
    return resp

def parse_platform(plat_str):
    parts = plat_str.split('/')
    if len(parts) == 2:
        return {'os': parts[0], 'architecture': parts[1]}
    elif len(parts) == 3:
        return {'os': parts[0], 'architecture': parts[1], 'variant': parts[2]}
    else:
        raise ValueError(f'Invalid platform format: {plat_str}')

parser = argparse.ArgumentParser(
    description='Pull Docker image and save as tar file.',
    epilog='If no registry is specified in the image name and --registry is not set, '
           'the default registry "registry-1.docker.io" is used.'
)
parser.add_argument('image', help='Image name, e.g. ubuntu:latest or ghcr.io/owner/repo:tag')
parser.add_argument('--platform', help='Target platform, e.g. linux/amd64, linux/arm64. If omitted, interactive selection.')
args = parser.parse_args()

# Get image name from command line arguments
image_name = args.image

# Look for the Docker image to download
repo = 'library'
tag = 'latest'
imgparts = image_name.split('/')
try:
    img, tag = imgparts[-1].split('@')
except ValueError:
    try:
        img, tag = imgparts[-1].split(':')
    except ValueError:
        img = imgparts[-1]
# Docker client doesn't seem to consider the first element as a potential registry unless there is a '.' or ':'
if len(imgparts) > 1 and ('.' in imgparts[0] or ':' in imgparts[0]):
    registry = imgparts[0]
    repo = '/'.join(imgparts[1:-1])
else:
    registry = 'registry-1.docker.io'
    if len(imgparts[:-1]) != 0:
        repo = '/'.join(imgparts[:-1])
    else:
        repo = 'library'
repository = '{}/{}'.format(repo, img)

def format_size(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def progress_with_speed(ublob, downloaded, total, start_time, bar_width=50):
    if total == 0:
        return
    percent = downloaded / total
    filled = int(bar_width * percent)
    bar = '=' * (filled - 1) + '>' if filled > 0 else ''
    bar = bar.ljust(bar_width, ' ')
    elapsed = time.time() - start_time
    speed = downloaded / elapsed if elapsed > 0 else 0
    speed_str = format_size(speed) + '/s' if speed > 0 else '0 B/s'
    sys.stdout.write('\r{}: [{}] {:.1f}% ({}/{}) {}          '.format(
        ublob[7:19], bar, percent * 100,
        format_size(downloaded), format_size(total), speed_str))
    sys.stdout.flush()

# Support Docker V2, Docker Manifest List, OCI Index, OCI Manifest
accept_all = (
    'application/vnd.docker.distribution.manifest.v2+json, '
    'application/vnd.docker.distribution.manifest.list.v2+json, '
    'application/vnd.oci.image.index.v1+json, '
    'application/vnd.oci.image.manifest.v1+json'
)

# Fetch manifest v2 and get image layer digests
headers = {'Accept': accept_all}
resp = do_request_with_auth(f'https://{registry}/v2/{repository}/manifests/{tag}', headers=headers)
if resp.status_code != 200:
    print('[-] Cannot fetch manifest for {} [HTTP {}]'.format(repository, resp.status_code))
    print(resp.content)
    exit(1)

# Check single or multi-arch manifest
content_type = resp.headers.get('Content-Type', '')
resp_json = resp.json()

if 'index' in content_type or 'manifest.list' in content_type:
    manifests = resp_json.get('manifests', [])
    if not manifests:
        print('[-] No manifest found in index')
        exit(1)

    # Follow command-line argument for platform selection
    if args.platform is not None:
        target_plat = parse_platform(args.platform)
        matched_digest = None
        for manifest in manifests:
            plat = manifest.get('platform', {})
            if plat.get('os') == target_plat.get('os') and plat.get('architecture') == target_plat.get('architecture'):
                if 'variant' in target_plat:
                    if plat.get('variant') == target_plat['variant']:
                        matched_digest = manifest['digest']
                        break
                else:
                    matched_digest = manifest['digest']
                    break
        if not matched_digest:
            print('[-] No manifest found for platform {}'.format(args.platform))
            print('[+] Available platforms:')
            for manifest in manifests:
                plat = manifest.get('platform', {})
                print("  OS: {}, Arch: {}, Variant: {}, digest: {}".format(
                    plat.get('os', 'unknown'), plat.get('architecture', 'unknown'),
                    plat.get('variant', ''), manifest['digest']))
            exit(1)
        selected_digest = matched_digest
        print('[+] Selected platform: {}/{} (digest: {})'.format(
            target_plat.get('os'), target_plat.get('architecture'), selected_digest))
    else:
        if len(manifests) == 1:
            # multi-arch manifest - only one platform available
            selected_digest = manifests[0]['digest']
            plat = manifests[0].get('platform', {})
            print('[+] Only one platform available: {}/{} (digest: {})'.format(
                plat.get('os', 'unknown'), plat.get('architecture', 'unknown'), selected_digest))
        else:
            # multi-arch manifest - multiple platforms available
            print('[+] Multiple platforms available. Please select one:')
            for idx, manifest in enumerate(manifests, start=1):
                plat = manifest.get('platform', {})
                print('  {}. OS: {}, Arch: {}, Variant: {}, digest: {}'.format(
                    idx,
                    plat.get('os', 'unknown'),
                    plat.get('architecture', 'unknown'),
                    plat.get('variant', ''),
                    manifest['digest']
                ))
            while True:
                try:
                    choice = input('Enter the number of your choice: ').strip()
                    if not choice:
                        continue
                    idx = int(choice)
                    if 1 <= idx <= len(manifests):
                        selected_digest = manifests[idx-1]['digest']
                        plat = manifests[idx-1].get('platform', {})
                        print('[+] Selected platform: {}/{} (digest: {})'.format(
                            plat.get('os'), plat.get('architecture'), selected_digest))
                        break
                    else:
                        print('Invalid number. Please enter a number between 1 and {}.'.format(len(manifests)))
                except ValueError:
                    print('Invalid input. Please enter a number.')

    # Fetch the specific manifest by digest
    resp = do_request_with_auth(f'https://{registry}/v2/{repository}/manifests/{selected_digest}',
                                headers={'Accept': accept_all})
    if resp.status_code != 200:
        print('[-] Failed to fetch manifest for digest {}'.format(selected_digest))
        exit(1)
    resp_json = resp.json()

# single-arch manifest
layers = resp_json.get('layers')
if layers is None:
    print('[-] No layers found in manifest (maybe unsupported format)')
    exit(1)
config = resp_json['config']['digest']

# Create tmp folder that will hold the image
imgdir = 'tmp_{}_{}'.format(img, tag.replace(':', '@'))
os.mkdir(imgdir)
print('Creating image structure in: ' + imgdir)

confresp = do_request_with_auth(f'https://{registry}/v2/{repository}/blobs/{config}',
                                headers={'Accept': 'application/vnd.docker.distribution.manifest.v2+json'})
file = open('{}/{}.json'.format(imgdir, config[7:]), 'wb')
file.write(confresp.content)
file.close()

content = [{
    'Config': config[7:] + '.json',
    'RepoTags': [],
    'Layers': []
}]
if len(imgparts[:-1]) != 0:
    content[0]['RepoTags'].append('/'.join(imgparts[:-1]) + '/' + img + ':' + tag)
else:
    content[0]['RepoTags'].append(img + ':' + tag)

empty_json = '{"created":"1970-01-01T00:00:00Z","container_config":{"Hostname":"","Domainname":"","User":"","AttachStdin":false, \
    "AttachStdout":false,"AttachStderr":false,"Tty":false,"OpenStdin":false, "StdinOnce":false,"Env":null,"Cmd":null,"Image":"", \
    "Volumes":null,"WorkingDir":"","Entrypoint":null,"OnBuild":null,"Labels":null}}'

# Build layer folders
parentid = ''
for layer in layers:
    ublob = layer['digest']
    # FIXME: Creating fake layer ID. Don't know how Docker generates it
    fake_layerid = hashlib.sha256((parentid + '\n' + ublob + '\n').encode('utf-8')).hexdigest()
    layerdir = imgdir + '/' + fake_layerid
    os.mkdir(layerdir)

    # Creating VERSION file
    file = open(layerdir + '/VERSION', 'w')
    file.write('1.0')
    file.close()

    bresp = do_request_with_auth(f'https://{registry}/v2/{repository}/blobs/{ublob}',
                                 headers={'Accept': 'application/vnd.docker.distribution.manifest.v2+json'},
                                 stream=True)
    if bresp.status_code != 200 and 'urls' in layer:  # When the layer is located at a custom URL
        bresp = do_request_with_auth(layer['urls'][0], stream=True)
    if bresp.status_code != 200:
        print('\rERROR: Cannot download layer {} [HTTP {}]'.format(ublob[7:19], bresp.status_code))
        print(bresp.content)
        exit(1)

    total_size = int(bresp.headers.get('Content-Length', 0))
    downloaded = 0
    start_time = time.time()

    with open(layerdir + '/layer_gzip.tar', "wb") as file:
        for chunk in bresp.iter_content(chunk_size=8192):
            if chunk:
                file.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    progress_with_speed(ublob, downloaded, total_size, start_time)
    # Prevent the progress bar from overwriting the output
    sys.stdout.write('\n')
    sys.stdout.flush()

    sys.stdout.write("{}: Extracting...{}".format(ublob[7:19], " " * 50))
    sys.stdout.flush()
    with open(layerdir + '/layer.tar', "wb") as file:  # Decompress gzip response
        unzLayer = gzip.open(layerdir + '/layer_gzip.tar', 'rb')
        shutil.copyfileobj(unzLayer, file)
        unzLayer.close()
    os.remove(layerdir + '/layer_gzip.tar')
    print("\r{}: Pull complete [{}]".format(ublob[7:19], bresp.headers.get('Content-Length', '?')))
    content[0]['Layers'].append(fake_layerid + '/layer.tar')

    # Creating json file
    file = open(layerdir + '/json', 'w')
    # last layer = config manifest - history - rootfs
    if layers[-1]['digest'] == layer['digest']:
        # FIXME: json.loads() automatically converts to unicode, thus decoding values whereas Docker doesn't
        json_obj = json.loads(confresp.content)
        del json_obj['history']
        try:
            del json_obj['rootfs']
        except:  # Because Microsoft loves case insensitiveness
            del json_obj['rootfS']
    else:  # other layers json are empty
        json_obj = json.loads(empty_json)
    json_obj['id'] = fake_layerid
    if parentid:
        json_obj['parent'] = parentid
    parentid = json_obj['id']
    file.write(json.dumps(json_obj))
    file.close()

file = open(imgdir + '/manifest.json', 'w')
file.write(json.dumps(content))
file.close()

if len(imgparts[:-1]) != 0:
    content = {'/'.join(imgparts[:-1]) + '/' + img: {tag: fake_layerid}}
else:  # when pulling only an img (without repo and registry)
    content = {img: {tag: fake_layerid}}
file = open(imgdir + '/repositories', 'w')
file.write(json.dumps(content))
file.close()

# Create image tar and clean tmp folder
docker_tar = repo.replace('/', '_') + '_' + img + '.tar'
sys.stdout.write("Creating archive...")
sys.stdout.flush()
tar = tarfile.open(docker_tar, "w")
tar.add(imgdir, arcname=os.path.sep)
tar.close()
shutil.rmtree(imgdir)
print('\rDocker image pulled: ' + docker_tar)
