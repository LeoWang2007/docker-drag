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

urllib3.disable_warnings()

if len(sys.argv) != 2:
    print('Usage:\n\tdocker_pull.py [registry/][repository/]image[:tag|@digest]\n')
    exit(1)

# Look for the Docker image to download
repo = 'library'
tag = 'latest'
imgparts = sys.argv[1].split('/')
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

# Get Docker authentication endpoint when it is required
auth_url = 'https://auth.docker.io/token'
reg_service = 'registry.docker.io'
resp = requests.get('https://{}/v2/'.format(registry), verify=False)
if resp.status_code == 401:
    auth_url = resp.headers['WWW-Authenticate'].split('"')[1]
    try:
        reg_service = resp.headers['WWW-Authenticate'].split('"')[3]
    except IndexError:
        reg_service = ""


# Get Docker token (this function is useless for unauthenticated registries like Microsoft)
def get_auth_head(type):
    resp = requests.get('{}?service={}&scope=repository:{}:pull'.format(auth_url, reg_service, repository),
                        verify=False)
    access_token = resp.json()['token']
    auth_head = {'Authorization': 'Bearer ' + access_token, 'Accept': type}
    return auth_head


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
auth_head = get_auth_head(accept_all)
resp = requests.get('https://{}/v2/{}/manifests/{}'.format(registry, repository, tag), headers=auth_head, verify=False)
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
    resp = requests.get('https://{}/v2/{}/manifests/{}'.format(registry, repository, selected_digest),
                        headers=auth_head, verify=False)
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

confresp = requests.get('https://{}/v2/{}/blobs/{}'.format(registry, repository, config), headers=auth_head,
                        verify=False)
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

    auth_head = get_auth_head('application/vnd.docker.distribution.manifest.v2+json')
    bresp = requests.get('https://{}/v2/{}/blobs/{}'.format(registry, repository, ublob), headers=auth_head,
                         stream=True, verify=False)
    if bresp.status_code != 200:  # When the layer is located at a custom URL
        bresp = requests.get(layer['urls'][0], headers=auth_head, stream=True, verify=False)
        if bresp.status_code != 200:
            print('\rERROR: Cannot download layer {} [HTTP {}]'.format(ublob[7:19], bresp.status_code))
            print(bresp.content)
            exit(1)

    bresp.raise_for_status()
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
