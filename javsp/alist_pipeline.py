"""One-shot remote scraping, with a durable journal and no video transfers."""
from contextlib import contextmanager, ExitStack
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import posixpath as pp
import re
import shutil
import sqlite3
import uuid

from javsp.alist_client import AlistClient, AlistError, AuthenticationError, ConflictError, TaskFailedError
from javsp.file import recognize_movies

logger = logging.getLogger(__name__)


@contextmanager
def run_logging(work):
    """Show progress even without root logging configured; isolate each run's log."""
    from javsp.print import TqdmOut

    directory = work / 'logs'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.log')
    file_handler = logging.FileHandler(path, encoding='utf-8')
    console = logging.StreamHandler(TqdmOut)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
    previous = logger.level, logger.propagate
    for handler in (file_handler, console):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info('本次 AList 日志: %s', path)
        yield path
    finally:
        logger.info('本次 AList 日志已保存: %s', path)
        for handler in (file_handler, console):
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(previous[0])
        logger.propagate = previous[1]


def beneath(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def remote_root(value):
    if not value.startswith('/') or '\\' in value or '..' in value.split('/'):
        raise ValueError('AList 根目录必须是无 .. 的绝对 / 路径')
    result = pp.normpath(value)
    if result == '/' or result.startswith('//'):
        raise ValueError('不能使用 AList 根目录 /')
    return result


def relative_path(value):
    value = str(value).replace('\\', '/')
    if (not value or value.startswith('/') or '..' in value.split('/')
            or ':' in value or '\x00' in value):
        raise ValueError(f'命名模板必须生成安全的相对路径: {value!r}')
    value = pp.normpath(value)
    if value in ('.', '..'):
        raise ValueError('命名模板不能生成空路径')
    return value


def hashes(entry):
    value = entry.get('hash_info') or entry.get('hashinfo') or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = {}
    if not isinstance(value, dict):
        return {}
    return {str(k).lower().replace('-', ''): str(v).lower()
            for k, v in value.items() if v}


def same_file(actual, expected, *, strong=False, unchanged=False):
    if actual is None or actual.get('is_dir') or actual.get('size') != expected.get('size'):
        return False
    left, right = hashes(actual), hashes(expected)
    common = left.keys() & right.keys()
    if any(left[k] != right[k] for k in common):
        return False
    aid, eid = actual.get('id'), expected.get('id')
    if aid and eid and aid != eid:
        return False
    if unchanged and actual.get('modified') != expected.get('modified'):
        return False
    return not strong or bool(common) or bool(aid and eid and aid == eid)


def snapshot_entry(entry):
    # Never persist sign, raw URLs, readme or storage credentials.
    return {key: entry.get(key) for key in
            ('size', 'is_dir', 'modified', 'created', 'id', 'hash_info', 'hashinfo') if key in entry}


def same_directory(actual, expected):
    if not actual or not actual.get('is_dir') or not expected.get('is_dir'):
        return False
    evidence = False
    for key in ('id', 'created'):
        if actual.get(key) and expected.get(key) and actual[key] != expected[key]:
            return False
        if actual.get(key) and expected.get(key):
            if key != 'created' or not str(expected[key]).startswith(('0001-', '1970-01-01T00:00:00')):
                evidence = True
    return evidence


@contextmanager
def process_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'run.lock').open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlistError('另一个 JavSP 实例正在使用此状态目录') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Journal:
    def __init__(self, directory, scope):
        self.db = sqlite3.connect(directory / 'state.sqlite3')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id))')
        old = self.get('scope', 'scope')
        if old is not None and old != scope:
            self.db.close()
            raise AlistError('状态目录属于其他服务、挂载或 A/B 路径；请使用独立 work_dir')
        self.put('scope', 'scope', scope)

    def put(self, kind, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)',
                            (kind, key, json.dumps(value, ensure_ascii=False)))

    def get(self, kind, key):
        row = self.db.execute('SELECT value FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT value FROM records WHERE kind=? ORDER BY rowid', (kind,))]

    def close(self):
        self.db.close()


class RemotePipeline:
    def __init__(self, cfg, client, journal, scrape):
        self.cfg, self.remote = cfg, cfg.alist
        self.client, self.journal, self.scrape = client, journal, scrape
        self.source = remote_root(self.remote.source_dir)
        self.a = remote_root(self.remote.metadata_dir)
        self.b = remote_root(self.remote.video_dir)
        self.work = self.remote.work_dir.resolve()
        self.counts = dict(success=0, failed=0, conflict=0, cleaned=0, skipped=0)
        self.results = []
        self.retained = {}
        self.phase = ''
        self.current_scan = None

    @staticmethod
    def source_label(job):
        return '；'.join(sorted({pp.dirname(item['source']) for item in job['files']}))

    def result(self, kind, path, reason):
        record = (kind, path, str(reason))
        if record not in self.results:
            self.results.append(record)

    def retain(self, directory, reason):
        if self.retained.get(directory) != reason:
            logger.info('源目录保留: %s | 原因: %s', directory, reason)
        self.retained[directory] = reason

    def save(self, job):
        self.journal.put('job', job['id'], job)

    def tree(self, root):
        result = {}
        pending = [root]
        while pending:
            directory = pending.pop()
            for entry in self.client.list_dir(directory, refresh=True):
                name = entry['name']
                if name in ('', '.', '..') or '/' in name or '\\' in name:
                    raise AlistError('远程目录包含非法名称，停止扫描')
                path = pp.join(directory, name)
                result[path] = snapshot_entry(entry)
                if entry['is_dir']:
                    pending.append(path)
        return result

    def scan(self, excluded):
        logger.info('开始递归扫描来源: %s', self.source)
        tree = self.tree(self.source)
        ignored = re.compile('|'.join(self.cfg.scanner.ignored_folder_name_pattern))
        protected = set()
        for path, entry in tree.items():
            if entry['is_dir'] and ignored.match(pp.basename(path)):
                protected.add(path)
            if not entry['is_dir'] and path.lower().endswith('.nfo') and self.cfg.scanner.skip_nfo_dir:
                protected.add(pp.dirname(path))
        records = [(path, entry['size']) for path, entry in tree.items()
                   if not entry['is_dir'] and path not in excluded
                   and not any(beneath(path, p) for p in protected)]
        movies = recognize_movies(records, self.source, path_module=pp)
        scan = dict(id=uuid.uuid4().hex, tree=tree, protected=sorted(protected), cleanup={},
                    videos=[p for p, e in tree.items() if self.is_video(p, e)])
        self.current_scan = scan['id']
        self.journal.put('scan', scan['id'], scan)
        recognized = {p for movie in movies for p in movie.files}
        skipped = [p for p, e in tree.items()
                   if self.is_video(p, e) and p not in recognized and p not in excluded]
        self.counts['skipped'] += len(skipped)
        for path in skipped:
            if any(beneath(path, p) for p in protected):
                reason = '命中忽略目录或已有 NFO 目录规则'
            elif tree[path]['size'] < self.cfg.scanner.minimum_size:
                reason = '视频小于最小大小，且未被识别为有效分片'
            else:
                reason = '未识别番号、重复番号或分片不符合规则'
            logger.warning('跳过视频: %s | 原因: %s', path, reason)
            self.result('跳过视频', path, reason)
        logger.info('扫描完成: %s | 视频 %d 个，待刮削 %d 部，跳过 %d 个，恢复记录占用 %d 个',
                    self.source, len(scan['videos']), len(movies), len(skipped),
                    sum(p in excluded for p in scan['videos']))
        return scan, movies

    def is_video(self, path, entry):
        return not entry['is_dir'] and pp.splitext(path)[1].lower() in self.cfg.scanner.filename_extensions

    def prepare(self, movie, scan):
        job_id = uuid.uuid4().hex
        local_root = self.work / 'artifacts' / job_id
        job = dict(id=job_id, scan=scan['id'], status='prepared', admitted=False, metadata=[], files=[])

        def output(item):
            folder = relative_path(item.save_dir)
            basename = relative_path(item.basename)
            if '/' in basename:
                raise ValueError('影片 basename 模板不得包含目录')
            job['folder'] = folder
            for index, source in enumerate(item.files, 1):
                suffix = f'-CD{index}' if len(item.files) > 1 else ''
                name = basename + suffix + pp.splitext(source)[1]
                target = pp.join(self.b, folder, name)
                job['files'].append(dict(source=source, renamed=pp.join(pp.dirname(source), name),
                                         target=target, expected=scan['tree'][source], state='new'))
            for attr in ('nfo_file', 'poster_file', 'fanart_file'):
                relative = relative_path(getattr(item, attr))
                if not beneath(relative, folder):
                    raise ValueError('元数据路径超出影片目录')
                setattr(item, attr, str(local_root.joinpath(*relative.split('/'))))
            item.save_dir = str(local_root.joinpath(*folder.split('/')))

        self.scrape([movie], remote_roots=(self.a, self.b), prepare_output=output, move_files=False)
        for file in movie.metadata_files:
            local = Path(file).resolve()
            relative = local.relative_to(local_root).as_posix()
            content = local.read_bytes()
            expected = dict(size=len(content), is_dir=False,
                            hash_info={'sha1': hashlib.sha1(content).hexdigest(),
                                       'sha256': hashlib.sha256(content).hexdigest()})
            job['metadata'].append(dict(local=str(local), target=pp.join(self.a, relative),
                                        stage=pp.join(self.a, '.javsp-staging', job_id, relative),
                                        expected=expected, state='new', task=None))
        if not job['metadata'] or not job['files']:
            raise AlistError('刮削没有生成完整文件清单')
        # Validate all generated names after actual image extensions are known.
        for item in job['metadata'] + job['files']:
            target = item['target']
            length = len(target.encode('utf-8')) if self.cfg.summarizer.path.length_by_byte else len(target)
            if length > self.cfg.summarizer.path.length_maximum:
                raise ValueError('生成的远程路径超过配置的长度上限')
        self.save(job)
        return job

    def preflight(self, job, claims):
        targets = [i['target'] for i in job['metadata'] + job['files']]
        if len(set(targets)) != len(targets):
            raise ConflictError('同一影片的输出文件名冲突')
        for target in self.job_paths(job):
            if target in claims and claims[target] != job['id']:
                raise ConflictError(f'本批次命名冲突: {target}')
        for item in job['metadata'] + job['files']:
            if item['state'] == 'new' and self.client.stat(item['target']) is not None:
                raise ConflictError(f'目标已存在: {item["target"]}')
        for item in job['files']:
            if item['state'] != 'new':
                continue
            if not same_file(self.client.stat(item['source']), item['expected'], unchanged=True):
                raise ConflictError(f'源视频已变化或不存在: {item["source"]}')
            if item['renamed'] != item['source'] and self.client.stat(item['renamed']) is not None:
                raise ConflictError(f'源目录改名位置已存在: {item["renamed"]}')
        for target in self.job_paths(job):
            claims[target] = job['id']
        job['admitted'] = True
        self.save(job)

    @staticmethod
    def job_paths(job):
        paths = {item['target'] for item in job['metadata'] + job['files']}
        paths.update(item[key] for item in job['files'] for key in ('source', 'renamed'))
        return paths

    def transfer(self, job, item, source, target, pending, returned, done):
        """Reconcile a rename/move; uncertain outcomes require stable identity/hash."""
        action = ('视频改名' if pending == 'rename_pending' else '视频移动') if 'source' in item else '元数据发布到 A'
        self.phase = f'{action}: {source} -> {target}'
        logger.info('%s，开始执行／核对: %s -> %s', action, source, target)
        state = item['state']
        if state == pending and item.get('move_task'):
            logger.info('等待已有移动任务: %s | %s', item['move_task'], target)
            try:
                self.client.wait_move(item['move_task'])
                item['state'] = state = returned
            except TaskFailedError:
                if (self.client.stat(target) is not None or not same_file(
                        self.client.stat(source), item.get('location_expected', item['expected']),
                        unchanged=True)):
                    raise ConflictError('失败任务的源或目标发生变化，不能重新提交')
                item['state'] = state = 'retry_ready'
                item['move_task'] = None
            self.save(job)
        src, dst = self.client.stat(source), self.client.stat(target)
        if state == done:
            if not same_file(dst, item.get('location_expected', item['expected']),
                             unchanged='location_expected' in item) or src is not None:
                raise ConflictError(f'已完成操作的文件位置变化: {target}')
            logger.info('%s已完成，恢复核对通过: %s', action, target)
            return
        if dst is not None:
            if state not in (pending, returned) or src is not None:
                raise ConflictError(f'目标冲突: {target}')
            if not same_file(dst, item['expected'], strong=(state == pending)):
                raise ConflictError(f'无法证明目标属于上次操作，保留待核对: {target}')
        else:
            if state == pending:
                raise AlistError(f'上次请求仍可能执行中，保留待核对，不重复提交: {source}')
            if state == returned:
                raise AlistError(f'已确认操作的目标暂不可见: {target}')
            if not same_file(src, item.get('location_expected', item['expected']), unchanged='source' in item):
                raise ConflictError(f'待移动文件已变化或不存在: {source}')
            item['state'] = pending
            self.save(job)
            try:
                if pp.dirname(source) == pp.dirname(target):
                    self.client.rename(source, pp.basename(target))
                else:
                    if pp.basename(source) != pp.basename(target):
                        raise AlistError('移动接口不能同时改名')
                    def remember_task(task_id):
                        item['move_task'] = task_id
                        self.save(job)
                        logger.info('移动任务已提交，等待完成: %s | %s', task_id, target)

                    self.client.move(source, pp.dirname(target), on_task=remember_task)
            except (ConflictError, AuthenticationError):
                # A refusal before submission is retryable; task-query auth failures
                # after acceptance must retain the submitted task and pending state.
                if not item.get('move_task'):
                    item['state'] = state
                    self.save(job)
                raise
            item['state'] = returned
            self.save(job)
            dst, src = self.client.stat(target), self.client.stat(source)
            if src is not None or not same_file(dst, item['expected']):
                raise AlistError(f'远程操作尚未核对成功: {target}')
        item['state'] = done
        item['location_expected'] = snapshot_entry(dst)
        self.save(job)
        logger.info('%s成功，目标文件及原路径消失均已核对: %s -> %s', action, source, target)

    def upload_metadata(self, job, item):
        self.phase = f'元数据上传到 A: {item["target"]}'
        logger.info('元数据上传／恢复核对: %s (%d 字节)', item['target'], item['expected']['size'])
        if item['state'] == 'done':
            if not same_file(self.client.stat(item['target']), item.get('location_expected', item['expected']),
                             unchanged='location_expected' in item):
                raise ConflictError(f'已上传元数据发生变化: {item["target"]}')
            logger.info('元数据上传已完成，恢复核对通过: %s', item['target'])
            return
        logger.info('准备 A 上传暂存目录及目标目录: %s', pp.dirname(item['target']))
        self.client.mkdirs(pp.dirname(item['stage']))
        self.client.mkdirs(pp.dirname(item['target']))
        if item['state'] == 'new':
            if self.client.stat(item['stage']) is not None or self.client.stat(item['target']) is not None:
                raise ConflictError('上传位置已存在')
            local = Path(item['local'])
            if not local.is_file() or hashlib.sha256(local.read_bytes()).hexdigest() != hashes(item['expected'])['sha256']:
                raise AlistError('本地元数据缺失或已被修改；保留恢复记录')
            item['state'] = 'upload_pending'
            self.save(job)
            item['task'] = self.client.upload(local, item['stage'])
            item['state'] = 'upload_returned'
            self.save(job)
            logger.info('元数据上传请求已返回，等待核对: %s | 任务: %s',
                        item['target'], item['task'] or '同步请求')
        if item['state'] in ('upload_pending', 'upload_returned'):
            if item['task']:
                logger.info('等待上传任务: %s | %s', item['task'], item['target'])
                try:
                    self.client.wait_upload(item['task'])
                except TaskFailedError:
                    item.setdefault('failed_uploads', []).append(dict(stage=item['stage'], task=item['task']))
                    item['stage'] = pp.join(pp.dirname(item['stage']), 'retry-' + uuid.uuid4().hex,
                                            pp.basename(item['stage']))
                    item['task'], item['state'] = None, 'new'
                    self.save(job)
                    raise
            actual = self.client.stat(item['stage'])
            if not same_file(actual, item['expected'], strong=(item['state'] == 'upload_pending')):
                raise AlistError('上传尚未确认完成；保留任务和临时文件，稍后重试')
            item['state'] = 'uploaded'
            item['location_expected'] = snapshot_entry(actual)
            self.save(job)
            logger.info('元数据暂存上传成功，大小及可用哈希已核对: %s', item['stage'])
        self.transfer(job, item, item['stage'], item['target'], 'publish_pending', 'publish_returned', 'done')
        logger.info('元数据上传到 A 成功: %s', item['target'])

    def execute(self, job, claims):
        self.phase = '源文件及目标冲突检查'
        self.preflight(job, claims)
        for item in job['metadata']:
            self.upload_metadata(job, item)
        # All metadata must be verified again before touching any video.
        self.phase = 'A 元数据最终核对'
        for item in job['metadata']:
            if not same_file(self.client.stat(item['target']), item['expected']):
                raise AlistError('A 的元数据验证失败，停止移动视频')
        logger.info('A 元数据全部核对通过 (%d 个)，开始整理视频到 B: %s',
                    len(job['metadata']), pp.join(self.b, job['folder']))
        for item in job['files']:
            self.phase = f'准备 B 目录及核对源视频: {item["source"]}'
            logger.info('准备 B 目标目录: %s', pp.dirname(item['target']))
            self.client.mkdirs(pp.dirname(item['target']))
            if item['state'] == 'new':
                if not same_file(self.client.stat(item['source']), item['expected'], unchanged=True):
                    raise ConflictError('源视频在刮削期间发生变化')
            if item['state'] in ('new', 'rename_pending', 'rename_returned'):
                if item['source'] == item['renamed']:
                    item['state'] = 'renamed'
                    item['location_expected'] = item['expected']
                    self.save(job)
                    logger.info('视频无需改名: %s', item['source'])
                else:
                    self.transfer(job, item, item['source'], item['renamed'],
                                  'rename_pending', 'rename_returned', 'renamed')
            self.transfer(job, item, item['renamed'], item['target'], 'move_pending', 'move_returned', 'done')
        job['status'] = 'done'
        self.save(job)

    def try_job(self, job, claims):
        source = self.source_label(job)
        logger.info('开始整理／恢复: %s | 源目录: %s | 任务: %s', job['folder'], source, job['id'])
        try:
            self.execute(job, claims)
            self.counts['success'] += 1
            logger.info('AList 整理完成: %s | A: %s | B: %s', source,
                        pp.join(self.a, job['folder']), pp.join(self.b, job['folder']))
            self.result('整理成功', source, f'A: {pp.join(self.a, job["folder"])} | B: {pp.join(self.b, job["folder"])}')
        except AuthenticationError:
            raise
        except Exception as exc:
            kind = 'conflict' if isinstance(exc, ConflictError) else 'failed'
            self.counts[kind] += 1
            reason = f'{self.phase} | {exc}'
            logger.error('AList 整理未完成 | 源目录: %s | 任务: %s | %s', source, job['id'], reason)
            self.result('冲突' if kind == 'conflict' else '整理失败', source, reason)

    def remaining_tree_matches(self, root, original, moved):
        if not same_directory(self.client.stat(root), original['tree'][original['root']]):
            return False
        current = self.tree(root)
        expected = {pp.relpath(p, original['root']): e for p, e in original['tree'].items()
                    if beneath(p, original['root']) and p != original['root'] and p not in moved}
        actual = {pp.relpath(p, root): e for p, e in current.items()}
        if actual.keys() != expected.keys():
            return False
        for path, entry in expected.items():
            if entry['is_dir']:
                if not same_directory(actual[path], entry):
                    return False
            elif not same_file(actual[path], entry, unchanged=True):
                return False
        return True

    def cleanup(self, scan):
        jobs = [job for job in self.journal.all('job') if job['scan'] == scan['id']]
        done = [job for job in jobs if job['status'] == 'done']
        moved = {item['source'] for job in done for item in job['files']}
        if 'videos' not in scan:
            logger.warning('旧扫描记录缺少视频分类，保留源目录: %s', scan['id'])
            return
        videos = set(scan['videos'])
        dirs = sorted((p for p, e in scan['tree'].items() if e['is_dir']), key=lambda p: (p.count('/'), p))
        covered = []
        for directory in dirs:
            if any(beneath(directory, p) for p in covered):
                continue
            subset = {p for p in videos if beneath(p, directory)}
            if not subset:
                if scan['id'] == self.current_scan:
                    self.retain(directory, '没有本次扫描中成功整理的视频，不自动删除')
                continue
            if (not subset <= moved
                    or any(beneath(p, directory) or beneath(directory, p) for p in scan['protected'])):
                if scan['id'] == self.current_scan:
                    if not subset <= moved:
                        self.retain(directory, '仍有失败、冲突或跳过的视频: ' + '；'.join(sorted(subset - moved)))
                    else:
                        self.retain(directory, '包含受忽略目录或已有 NFO 规则保护的内容')
                continue
            record = scan['cleanup'].get(directory)
            if record and record['state'] == 'done':
                covered.append(directory)
                continue
            original = dict(root=directory, tree=scan['tree'])
            try:
                # Recheck B and A even for jobs completed during an earlier invocation.
                for job in done:
                    if any(beneath(i['source'], directory) for i in job['files']):
                        for item in job['files'] + job['metadata']:
                            if not same_file(self.client.stat(item['target']),
                                             item.get('location_expected', item['expected']),
                                             unchanged='location_expected' in item):
                                raise AlistError('目标文件已变化，保留来源目录')
                if record is None:
                    if self.client.stat(directory) is None:
                        continue
                    if not self.remaining_tree_matches(directory, original, moved):
                        logger.warning('源目录内容变化，保留: %s', directory)
                        self.retain(directory, '扫描后内容或目录身份发生变化，未通过删除前核对')
                        continue
                    quarantine = pp.join(pp.dirname(directory), '.javsp-cleanup-' + uuid.uuid4().hex)
                    if self.client.stat(quarantine) is not None:
                        raise ConflictError('清理暂存路径已存在')
                    record = dict(path=quarantine, state='rename_pending')
                    scan['cleanup'][directory] = record
                    self.journal.put('scan', scan['id'], scan)
                    logger.info('源目录清理，改名到暂存位置: %s -> %s', directory, quarantine)
                    self.client.rename(directory, pp.basename(quarantine))
                    record['state'] = 'quarantined'
                    self.journal.put('scan', scan['id'], scan)
                quarantine = record['path']
                if record['state'] == 'delete_pending':
                    if self.client.stat(quarantine) is not None:
                        raise AlistError('上次删除结果不确定，保留清理暂存目录待人工核对')
                else:
                    if self.client.stat(quarantine) is None:
                        raise AlistError('清理改名结果不确定；不会重新操作原路径')
                    if not self.remaining_tree_matches(quarantine, original, moved):
                        raise AlistError(f'清理暂存目录有新增或变化内容，已保留: {quarantine}')
                    record['state'] = 'delete_pending'
                    self.journal.put('scan', scan['id'], scan)
                    logger.info('源目录清理，开始删除已核对的暂存目录: %s', quarantine)
                    self.client.remove(quarantine)
                    if self.client.stat(quarantine) is not None:
                        raise AlistError('删除暂存目录尚未确认')
                record['state'] = 'done'
                self.journal.put('scan', scan['id'], scan)
                covered.append(directory)
                self.counts['cleaned'] += 1
                logger.info('源目录清理成功，已确认删除: %s | 暂存路径: %s', directory, quarantine)
                self.result('清理成功', directory, '目录及残留文件已删除')
                for path in list(self.retained):
                    if beneath(path, directory):
                        del self.retained[path]
            except AuthenticationError:
                raise
            except Exception as exc:
                self.counts['failed'] += 1
                covered.append(directory)
                logger.error('源目录清理未完成 %s: %s', directory, exc)
                self.result('清理失败', directory, exc)
                self.retain(directory, str(exc))
        # Local artifacts are disposable only after their job has finished.
        for job in done:
            local = self.work / 'artifacts' / job['id']
            if local.exists() and local.resolve().parent == (self.work / 'artifacts').resolve():
                shutil.rmtree(local)
                logger.info('本地临时元数据已清理: %s', local)

    def run(self):
        logger.info('AList 本轮开始 | 来源: %s | 元数据 A: %s | 视频 B: %s', self.source, self.a, self.b)
        claims = {}
        previous = self.journal.all('job')
        logger.info('待恢复影片: %d 部', sum(job['status'] != 'done' for job in previous))
        # Admitted jobs own their paths ahead of rejected, never-started candidates.
        previous.sort(key=lambda job: not job.get('admitted', False))
        previous_owners = {}
        for job in previous:
            if job['status'] != 'done':
                for path in self.job_paths(job):
                    previous_owners.setdefault(path, []).append(job)
                    claims.setdefault(path, job['id'])
        previous_collisions = set()
        for owners in previous_owners.values():
            if len(owners) > 1:
                admitted = [job for job in owners if job.get('admitted', False)]
                losers = owners if len(admitted) != 1 else [job for job in owners if job != admitted[0]]
                previous_collisions.update(job['id'] for job in losers)
        for job in previous:
            if job['status'] != 'done':
                if job['id'] in previous_collisions:
                    self.counts['conflict'] += 1
                    logger.error('恢复记录之间存在输出冲突，保留来源: %s', job['id'])
                    self.result('恢复冲突', self.source_label(job), f'恢复记录输出冲突，任务: {job["id"]}')
                else:
                    self.try_job(job, claims)
        for scan in self.journal.all('scan'):
            self.cleanup(scan)
        excluded = {item[key] for job in self.journal.all('job') if job['status'] != 'done'
                    for item in job['files'] for key in ('source', 'renamed')}
        scan, movies = self.scan(excluded)
        prepared = []
        for index, movie in enumerate(movies, 1):
            source = '；'.join(sorted({pp.dirname(path) for path in movie.files}))
            logger.info('开始刮削 [%d/%d]: %s | 源目录: %s | 视频: %s',
                        index, len(movies), movie, source, '；'.join(movie.files))
            try:
                job = self.prepare(movie, scan)
                prepared.append(job)
                logger.info('刮削完成: %s | 源目录: %s | 元数据 %d 个，视频 %d 个 | A: %s | B: %s',
                            movie, source, len(job['metadata']), len(job['files']),
                            pp.join(self.a, job['folder']), pp.join(self.b, job['folder']))
            except AuthenticationError:
                raise
            except Exception as exc:
                self.counts['failed'] += 1
                logger.error('AList 刮削失败 %s | 源目录: %s | %s', movie, source, exc)
                self.result('刮削失败', source, f'{movie}: {exc}')
        # Detect all in-batch target collisions before publishing any of this batch.
        owners = {}
        for job in prepared:
            for path in self.job_paths(job):
                owners.setdefault(path, set()).add(job['id'])
        collisions = set().union(*(ids for ids in owners.values() if len(ids) > 1)) if owners else set()
        for job in prepared:
            if job['id'] in collisions:
                self.counts['conflict'] += 1
                logger.error('本批次输出路径冲突，保留来源: %s', job['folder'])
                self.result('冲突', self.source_label(job), f'本批次输出路径冲突: {job["folder"]}')
            else:
                self.try_job(job, claims)
        self.cleanup(scan)
        logger.info('来源根目录始终保留: %s', self.source)
        logger.warning('AList 本轮结果: 成功 %(success)s，失败 %(failed)s，冲突 %(conflict)s，'
                       '清理目录 %(cleaned)s，跳过视频 %(skipped)s', self.counts)
        logger.info('本轮结果明细（整理成功与源目录清理分别记录）:')
        for kind, path, reason in self.results:
            logger.info('[%s] %s | %s', kind, path, reason)
        for path, reason in self.retained.items():
            logger.info('[目录保留] %s | %s', path, reason)
        return 1 if self.counts['failed'] or self.counts['conflict'] else 0


def run_alist(cfg, scrape, *, client=None):
    journal = None
    log_context = ExitStack()
    owns_client = client is None
    try:
        if cfg.alist is None:
            raise ValueError('scanner.source=alist 时必须提供 alist 配置')
        roots = [remote_root(p) for p in (cfg.alist.source_dir, cfg.alist.metadata_dir, cfg.alist.video_dir)]
        if any(beneath(a, b) or beneath(b, a) for index, a in enumerate(roots) for b in roots[index + 1:]):
            raise ValueError('来源、A、B 目录不得相同或相互包含')
        if not cfg.summarizer.move_files or cfg.summarizer.path.hard_link:
            raise ValueError('AList 模式要求 move_files=true、hard_link=false')
        # Reject traversal before normpath could conceal it inside generated names.
        for pattern in (cfg.summarizer.path.output_folder_pattern, cfg.summarizer.path.basename_pattern,
                        cfg.summarizer.nfo.basename_pattern, cfg.summarizer.cover.basename_pattern,
                        cfg.summarizer.fanart.basename_pattern):
            relative_path(pattern)
        client = client or AlistClient(cfg.alist)
        with process_lock(cfg.alist.work_dir.resolve()):
            log_context.enter_context(run_logging(cfg.alist.work_dir.resolve()))
            storage = client.ensure_same_storage(roots[0], roots[2])
            source_entry = client.stat(roots[0])
            if source_entry is None or not source_entry['is_dir']:
                raise ValueError('来源目录不存在或不是目录')
            scope = dict(url=cfg.alist.base_url.rstrip('/'), roots=roots, storage=storage,
                         metadata_storage=client.storage_identity(roots[1]))
            journal = Journal(cfg.alist.work_dir.resolve(), scope)
            return RemotePipeline(cfg, client, journal, scrape).run()
    except (AlistError, ValueError, OSError) as exc:
        logger.error('AList 运行终止: %s', exc)
        return 1
    finally:
        if journal:
            journal.close()
        if owns_client and client is not None:
            client.close()
        log_context.close()
