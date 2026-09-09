"""Migrate legacy scraping preferences while preserving the selected storage backend."""
from configparser import ConfigParser
from copy import deepcopy
from pathlib import Path
import re


def read_ini(path):
    raw = Path(path).read_bytes()
    for encoding in ('utf-8-sig', 'gbk'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('INI 必须使用 UTF-8 或 GBK 编码')
    config = ConfigParser(interpolation=None)
    config.read_string(text)
    return config


def migrate_ini(ini, base):
    """Return a new config; never mutate the base or discard AList credentials/state paths."""
    result = deepcopy(base)

    def value(section, key, fallback=''):
        return ini.get(section, key, fallback=fallback).strip()

    def boolean(section, key, fallback=False):
        return ini.getboolean(section, key, fallback=fallback)

    def items(section, key, separator=','):
        return [part.strip() for part in value(section, key).split(separator) if part.strip()]

    def template(pattern):
        return re.sub(r'\$([a-z_]+)', r'{\1}', pattern)

    scanner = result['scanner']
    remote = scanner.get('source', 'local') == 'alist'
    scanner['input_directory'] = None if remote else value('File', 'scan_dir') or None
    scanner['manual'] = False if remote else scanner.get('manual', False)
    scanner['filename_extensions'] = ['.' + ext.lstrip('.').lower() for ext in items('File', 'media_ext', ';')]
    scanner['minimum_size'] = value('File', 'ignore_video_file_less_than', '232') + 'MiB'
    scanner['skip_nfo_dir'] = boolean('File', 'skip_nfo_dir')
    scanner['ignored_folder_name_pattern'] = [r'^\.'] + [
        '^' + re.escape(name) + '$' for name in items('File', 'ignore_folder', ';')]
    # Legacy normalization used re.I | re.A and treated '_' as a word boundary.
    patterns = ['(?ai:' + pattern + ')' for pattern in items('MovieID', 'ignore_regex', ';')]
    words = items('MovieID', 'ignore_whole_word', ';')
    if words:
        patterns.append(r'(?ai:(?:\b|_)(?:' + '|'.join(re.escape(word) for word in words) + r')(?=\b|_))')
    scanner['ignored_id_pattern'] = list(dict.fromkeys(patterns))

    network = result['network']
    network['proxy_server'] = (value('Network', 'proxy') or None) if boolean('Network', 'use_proxy') else None
    network['retry'] = ini.getint('Network', 'retry', fallback=3)
    network['timeout'] = 'PT' + value('Network', 'timeout', '10') + 'S'
    network['proxy_free'] = dict(ini.items('ProxyFree')) if ini.has_section('ProxyFree') else {}

    crawler = result['crawler']
    crawler['selection'] = {kind: items('CrawlerSelect', kind) for kind in crawler['selection']}
    crawler['required_keys'] = items('Crawler', 'required_keys')
    for old, new in [('hardworking_mode', 'hardworking'), ('respect_site_avid', 'respect_site_avid'),
                     ('unify_actress_name', 'normalize_actress_name')]:
        crawler[new] = boolean('Crawler', old)
    crawler['fc2fan_local_path'] = value('Crawler', 'fc2fan_local_path') or None
    crawler['sleep_after_scraping'] = 'PT' + value('Crawler', 'sleep_after_scraping', '0') + 'S'
    crawler['use_javdb_cover'] = {'auto': 'fallback', 'yes': 'no', 'no': 'yes'}[
        value('Crawler', 'ignore_javdb_cover', 'auto').lower()]

    summarizer = result['summarizer']
    summarizer['move_files'] = True if remote else boolean('File', 'enable_file_move', True)
    path = summarizer['path']
    output = value('NamingRule', 'output_folder').replace('\\', '/').rstrip('/')
    folder = value('NamingRule', 'save_dir').replace('\\', '/').lstrip('/')
    path['output_folder_pattern'] = template(output + '/' + folder if output else folder)
    path['basename_pattern'] = template(value('NamingRule', 'filename', '$num'))
    path['length_maximum'] = ini.getint('NamingRule', 'max_path_len', fallback=250)
    by_byte = value('NamingRule', 'calc_path_len_by_byte', 'auto').lower()
    path['length_by_byte'] = True if by_byte == 'auto' else boolean('NamingRule', 'calc_path_len_by_byte')
    path['max_actress_count'] = ini.getint('NamingRule', 'max_actress_count', fallback=10)
    path['hard_link'] = False if remote else boolean('File', 'use_hardlink')
    summarizer['title'] = dict(remove_trailing_actor_name=boolean('Crawler', 'title__remove_actor'),
                               prefer_chinese=boolean('Crawler', 'title__chinese_first'))
    for old, new in [('title', 'title'), ('actress', 'actress'), ('serial', 'series'),
                     ('director', 'director'), ('producer', 'producer'), ('publisher', 'publisher')]:
        summarizer['default'][new] = value('NamingRule', 'null_for_' + old)
    summarizer['censor_options_representation'] = [value('NamingRule', 'text_for_' + key)
                                                  for key in ('uncensored', 'censored', 'unknown_censorship')]
    if value('NamingRule', 'media_servers', 'universal') != 'universal':
        raise ValueError('该迁移目前支持 INI 的 universal 媒体命名；请先确认其他媒体服务器的文件名映射')
    summarizer['nfo']['basename_pattern'] = 'movie'
    summarizer['nfo']['title_pattern'] = template(value('NamingRule', 'nfo_title', '$num $title'))
    for kind in ('genres', 'tags'):
        summarizer['nfo']['custom_' + kind + '_fields'] = (
            [template(field) for field in items('NFO', 'add_custom_' + kind + '_fields')]
            if boolean('NFO', 'add_custom_' + kind) else [])
    cover = summarizer['cover']
    cover['basename_pattern'] = 'poster'
    cover['highres'] = boolean('Picture', 'use_big_cover')
    cover['add_label'] = boolean('Picture', 'add_label_to_cover')
    if boolean('Picture', 'use_ai_crop'):
        if value('Picture', 'ai_engine').lower() != 'slimeface':
            raise ValueError('旧版 AI 引擎不能直接迁移到 Slimeface；请明确选择新引擎')
        cover['crop']['engine'] = {'name': 'slimeface'}
    else:
        cover['crop']['engine'] = None
    cover['crop']['on_id_pattern'] = [r'^\d{6}[-_]\d{3}$' if label == r'\d' else '^' + label
                                      for label in items('Picture', 'use_ai_crop_labels')]
    summarizer['fanart']['basename_pattern'] = 'fanart'
    summarizer['extra_fanarts'] = dict(enabled=boolean('Picture', 'use_extra_fanarts'),
                                      scrap_interval='PT' + value('Picture', 'extra_fanarts_scrap_interval', '1.5') + 'S')

    name = value('Translate', 'engine').lower()
    engine = None
    if name == 'baidu':
        engine = dict(name=name, app_id=value('Translate', 'baidu_appid'), api_key=value('Translate', 'baidu_key'))
    elif name in ('bing', 'claude'):
        engine = dict(name=name, api_key=value('Translate', name + '_key'))
    elif name == 'google':
        engine = dict(name=name)
    elif name not in ('', 'none', 'no'):
        raise ValueError('请明确配置无法直接迁移的翻译引擎')
    result['translator'] = dict(engine=engine, fields=dict(title=boolean('Translate', 'translate_title'),
                                                          plot=boolean('Translate', 'translate_plot')))
    result['other']['check_update'] = boolean('Other', 'check_update')
    result['other']['auto_update'] = boolean('Other', 'auto_update')
    # AList is deliberately a one-shot noninteractive pipeline even if old EXEs waited before exit.
    if remote:
        result['other']['interactive'] = False
    return result
