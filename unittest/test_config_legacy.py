from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest
import yaml

from javsp.config import Cfg
from javsp.config_legacy import read_ini, migrate_ini
from javsp.datatype import Movie, MovieInfo


@pytest.fixture
def migrated():
    root = Path(__file__).parents[1]
    base = yaml.safe_load((root / 'config.alist.yml').read_text(encoding='utf-8'))
    base['alist']['token'] = 'test-alist-token'
    old = deepcopy(base)
    ini = read_ini(Path(__file__).parent / 'data/legacy-config.ini')
    result = migrate_ini(ini, base)
    assert base == old
    assert result['alist'] == old['alist']
    return result


def test_ini_preferences_and_credentials_are_mapped(migrated):
    cfg = Cfg.model_validate(migrated)
    assert cfg.scanner.source == 'alist'
    assert cfg.scanner.input_directory is None
    assert not cfg.scanner.manual and not cfg.scanner.skip_nfo_dir
    assert int(cfg.scanner.minimum_size) == 232 * 1024 ** 2
    assert '^ready$' in cfg.scanner.ignored_folder_name_pattern
    assert '^不要扫描$' in cfg.scanner.ignored_folder_name_pattern
    assert str(cfg.network.proxy_server).rstrip('/') == 'http://127.0.0.1:7897'
    assert cfg.network.retry == 1 and cfg.network.timeout.total_seconds() == 5
    assert cfg.summarizer.path.output_folder_pattern == 'ready/{censor}/[{num}] {title}'
    assert cfg.summarizer.path.length_maximum == 150 and cfg.summarizer.path.length_by_byte
    assert cfg.summarizer.title.prefer_chinese
    assert not cfg.summarizer.extra_fanarts.enabled
    assert cfg.summarizer.cover.crop.engine is None
    assert cfg.translator.engine.name == 'baidu'
    assert cfg.translator.engine.app_id == 'test-baidu_appid'
    assert cfg.translator.engine.api_key == 'test-baidu_key'
    assert cfg.translator.fields.title and cfg.translator.fields.plot
    assert cfg.other.check_update and cfg.other.auto_update and not cfg.other.interactive
    assert cfg.summarizer.nfo.custom_genres_fields == ['{genre}', '{censor}']


@pytest.mark.parametrize('uncensored,label', [(True, '无码'), (False, '有码'), (None, '打码情况未知')])
def test_censor_labels_match_output_folder_and_nfo(migrated, monkeypatch, tmp_path, uncensored, label):
    import javsp.__main__ as main
    import javsp.datatype as datatype
    import javsp.nfo as nfo
    cfg = Cfg.model_validate(migrated)
    for module in (main, datatype, nfo):
        monkeypatch.setattr(module, 'Cfg', lambda: cfg)
    movie = Movie('ABC-123')
    movie.files = ['/115/incoming/ABC-123.mp4']
    movie.info = MovieInfo('ABC-123')
    movie.info.title = '测试标题'
    movie.info.uncensored = uncensored
    assert movie.info.get_info_dic()['censor'] == label
    main.generate_names(movie, remote_roots=('/local-emby/bigsister', '/115/emby/bigsister'))
    assert movie.save_dir == f'ready/{label}/[ABC-123] 测试标题'
    target = tmp_path / 'movie.nfo'
    nfo.write_nfo(movie.info, target)
    parsed = ET.parse(target)
    assert label in [tag.text for tag in parsed.findall('genre')]
    assert label in [tag.text for tag in parsed.findall('tag')]


def website_info(title, cover):
    info = MovieInfo('ABC-123')
    info.title, info.cover = title, cover
    return info


@pytest.mark.parametrize('airav_first', [False, True])
def test_chinese_title_selected_preserving_original_and_other_site_priority(migrated, monkeypatch, airav_first):
    import javsp.__main__ as main
    cfg = Cfg.model_validate(migrated)
    monkeypatch.setattr(main, 'Cfg', lambda: cfg)
    japanese = website_info('日本語の作品名', 'https://example.invalid/japanese.jpg')
    chinese = website_info('中文标题', 'https://example.invalid/chinese.jpg')
    items = [('javbus', japanese), ('airav', chinese)]
    if airav_first:
        items.reverse()
    movie = Movie('ABC-123')
    assert main.info_summary(movie, dict(items))
    assert movie.info.title == '中文标题'
    assert movie.info.ori_title == '日本語の作品名'
    assert movie.info.cover == items[0][1].cover


@pytest.mark.parametrize('prefer,airav_title', [(False, '中文标题'), (True, None)])
def test_chinese_preference_disabled_or_unavailable_uses_site_order(migrated, monkeypatch, prefer, airav_title):
    import javsp.__main__ as main
    migrated['summarizer']['title']['prefer_chinese'] = prefer
    cfg = Cfg.model_validate(migrated)
    monkeypatch.setattr(main, 'Cfg', lambda: cfg)
    movie = Movie('ABC-123')
    assert main.info_summary(movie, {'javbus': website_info('日本語の作品名', 'https://example.invalid/a.jpg'),
                                    'airav': website_info(airav_title, 'https://example.invalid/b.jpg')})
    assert movie.info.title == '日本語の作品名'


def test_existing_chinese_title_skips_title_translation_but_translates_plot(migrated, monkeypatch):
    import javsp.web.translate as translate
    cfg = Cfg.model_validate(migrated)
    monkeypatch.setattr(translate, 'Cfg', lambda: cfg)
    movie = MovieInfo('ABC-123')
    movie.title, movie.plot = '中文标题', 'Japanese plot'
    movie.title_from_chinese_source = True
    service = Mock(return_value={'trans': '中文简介'})
    monkeypatch.setattr(translate, 'translate', service)
    assert translate.translate_movie_info(movie)
    service.assert_called_once()
    assert service.call_args.args[0] == 'Japanese plot'
    assert movie.title == '中文标题' and movie.plot == '中文简介'


def test_baidu_failure_does_not_expose_credentials(migrated, monkeypatch):
    import javsp.web.translate as translate
    cfg = Cfg.model_validate(migrated)
    monkeypatch.setattr(translate, 'baidu_translate', lambda *args: {'error_code': 1, 'error_msg': 'denied'})
    error = translate.translate('text', cfg.translator.engine)['error']
    assert error == 'baidu: 1: denied'
    assert cfg.translator.engine.api_key not in error
    assert cfg.translator.engine.app_id not in error
