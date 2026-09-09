"""Exercise real naming and configuration compatibility without crawling."""
import posixpath

import pytest

from javsp.config import Cfg
from javsp.datatype import Movie, MovieInfo
from javsp.__main__ import generate_names


@pytest.mark.parametrize('title', ['测试标题', '很长的中文标题，' * 100, 'A' * 1000])
def test_remote_name_budget_and_template(title, monkeypatch):
    import javsp.__main__ as main
    cfg = Cfg().model_copy(deep=True)
    object.__setattr__(cfg.summarizer.path, 'output_folder_pattern', '#整理完成\\{actress}\\[{num}] {title}')
    object.__setattr__(cfg.summarizer.path, 'basename_pattern', '{num}')
    monkeypatch.setattr(main, 'Cfg', lambda: cfg)
    movie = Movie('ABC-123')
    movie.files = ['/115/incoming/ABC-123-C.mp4']
    movie.info = MovieInfo('ABC-123')
    movie.info.title, movie.info.actress = title, ['测试演员']
    roots = ('/local-emby/library', '/115/library')
    generate_names(movie, remote_roots=roots)
    assert movie.save_dir.startswith('#整理完成/测试演员/[ABC-123-C]')
    assert movie.basename == 'ABC-123-C'
    assert '\\' not in movie.save_dir
    for root in roots:
        for relative in (movie.nfo_file, movie.fanart_file, movie.poster_file,
                         posixpath.join(movie.save_dir, movie.basename + '.mp4')):
            assert len(posixpath.join(root, relative).encode('utf-8')) <= cfg.summarizer.path.length_maximum


def test_old_configuration_still_defaults_to_local():
    import yaml
    from pathlib import Path
    data = yaml.safe_load((Path(__file__).parents[1] / 'config.alist.yml').read_text(encoding='utf-8'))
    data.pop('alist')
    data['scanner'].pop('source')
    old = Cfg.model_validate(data)
    assert old.scanner.source == 'local'
    assert old.alist is None
