import os
import sys
import sysconfig
from pathlib import Path
from typing import List, Tuple
from cx_Freeze import setup, Executable
import yaml

# https://github.com/marcelotduarte/cx_Freeze/issues/1288
base = None

proj_root = os.path.abspath(os.path.dirname(__file__))

# Build from the credential-free example, never from the user's active config.
release_config_dir = Path(proj_root) / 'build' / 'release-config'
release_config_dir.mkdir(parents=True, exist_ok=True)
release_config = yaml.safe_load((Path(proj_root) / 'config.alist.yml').read_text(encoding='utf-8'))
release_config['network']['proxy_server'] = None
release_config['translator']['engine'] = None
release_config['crawler']['fc2fan_local_path'] = None
release_config['scanner']['input_directory'] = None
release_config['other'].update(check_update=False, auto_update=False)
release_config['alist'].update(
    base_url='http://127.0.0.1:5244', token='', source_dir='/115/incoming',
    metadata_dir='/local-emby/library', video_dir='/115/library', work_dir='.javsp-alist',
)
for config_name in ('config.yml', 'config.alist.yml'):
    (release_config_dir / config_name).write_text(
        '# Configure your AList server and directories before running.\n' +
        yaml.safe_dump(release_config, allow_unicode=True, sort_keys=False), encoding='utf-8')

include_files: List[Tuple[str, str]] = [
    (str(release_config_dir / 'config.yml'), 'config.yml'),
    (str(release_config_dir / 'config.alist.yml'), 'config.alist.yml'),
    (f'{proj_root}/data', 'data'),
    (f'{proj_root}/image', 'image'),
    (f'{proj_root}/LICENSE', 'LICENSE'),
    (f'{proj_root}/README.md', 'README.md'),
    (f'{proj_root}/ALIST.md', 'ALIST.md'),
    (f'{proj_root}/release/使用说明.txt', '使用说明.txt'),
    (f'{proj_root}/release/启动.cmd', '启动.cmd'),
]

if sys.platform == 'win32':
    pywin32_dir = Path(sysconfig.get_path('platlib')) / 'pywin32_system32'
    for dll in pywin32_dir.glob('*.dll'):
        include_files.append((str(dll), f'lib/{dll.name}'))

includes = []

for file in os.listdir('javsp/web'):
    name, ext = os.path.splitext(file)
    if ext == '.py':
        includes.append('javsp.web.' + name)

packages = [ 
    'pendulum', # pydantic_extra_types depends on pendulum
    'slimeface',
]

build_exe = {
    'include_files': include_files,
    'includes': includes,
    'excludes': ['unittest'],
    'packages': packages,
    'include_msvcr': True,
}

javsp = Executable(
    './javsp/launcher.py',
    target_name='JavSP', 
    base=base,
    icon='./image/JavSP.ico',
)

setup(
    name='JavSP',
    options = {'build_exe': build_exe}, 
    executables=[javsp]
)

