"""Deterministic native AList pipeline tests: no server or video bytes are used.

Only the scraper boundary is replaced; recognition, SQLite journaling, recovery,
metadata publication and cleanup run through the production pipeline.
Pagination/wire protocol are covered by test_alist_client.py.
"""
import struct
import zlib
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import posixpath as pp
import sqlite3
from types import SimpleNamespace as NS
import xml.etree.ElementTree as ET

import pytest

import javsp.avid
import javsp.file
from javsp.alist_client import AlistError, AuthenticationError, ConflictError
from javsp.alist_pipeline import run_alist


SOURCE = "/115/incoming"
A = "/metadata/library"
B = "/115/library"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi")
def _png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data)))


# Valid one-pixel RGB PNG, built without an image library or external fixture.
PNG = (b"\x89PNG\r\n\x1a\n"
       + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
       + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
       + _png_chunk(b"IEND", b""))


class FakeClient:
    """In-memory native filesystem with stable IDs/hashes and one-shot faults."""

    def __init__(self):
        self.nodes = {}
        self.events = []
        self.hooks = []
        self.tasks = {}
        self.upload_errors = {}
        self.move_errors = {}
        self.move_tasks = {}
        self.async_move_paths = set()
        self.serial = 0
        self.metadata_storage = {"id": 200, "mount_path": "/metadata", "driver": "Local"}
        self.guard_video = None
        for path in (SOURCE, A, B):
            self.add_dir(path)

    def _identity(self):
        self.serial += 1
        return f"object-{self.serial}"

    def add_dir(self, path):
        path = pp.normpath(path)
        if path in self.nodes:
            assert self.nodes[path]["is_dir"]
            return
        if path != "/":
            self.add_dir(pp.dirname(path))
        self.nodes[path] = dict(
            is_dir=True, size=0, id=self._identity(), modified="2026-01-01T00:00:00Z"
        )

    def add_file(self, path, *, size=4096, content=None):
        self.add_dir(pp.dirname(path))
        identity = self._identity()
        data = content if content is not None else identity.encode()
        self.nodes[path] = dict(
            is_dir=False, size=len(content) if content is not None else size,
            id=identity, modified="2026-01-01T00:00:00Z",
            hash_info={"sha1": hashlib.sha1(data).hexdigest(),
                       "sha256": hashlib.sha256(data).hexdigest()},
        )
        if content is not None:
            self.nodes[path]["content"] = content
        return self.stat(path)

    def on(self, operation, *, when="before", predicate=None, callback=None, error=None):
        """Consume a hook only when operation, phase and predicate all match."""
        self.hooks.append(dict(operation=operation, when=when,
                               predicate=predicate or (lambda *args: True),
                               callback=callback, error=error))

    def _phase(self, operation, when, args):
        for hook in self.hooks[:]:
            if (hook["operation"] == operation and hook["when"] == when
                    and hook["predicate"](*args)):
                self.hooks.remove(hook)
                if hook["callback"]:
                    hook["callback"](*args)
                if hook["error"]:
                    raise hook["error"]

    def _mutate(self, operation, args, action):
        self.events.append((operation, *args))
        self._phase(operation, "before", args)
        result = action()
        self._phase(operation, "after", args)
        return result

    def stat(self, path):
        self._phase("stat", "before", (path,))
        value = deepcopy(self.nodes.get(path))
        if value is not None:
            value.pop("content", None)
            value["name"] = pp.basename(path)
        self._phase("stat", "after", (path,))
        return value

    def list_dir(self, path, refresh=True):
        assert refresh is True, "Cleanup and recovery must refresh listings"
        self._phase("list_dir", "before", (path,))
        if path not in self.nodes or not self.nodes[path]["is_dir"]:
            raise AlistError(f"Missing directory: {path}")
        entries = [self.stat(p) for p in self.nodes if p != path and pp.dirname(p) == path]
        self._phase("list_dir", "after", (path,))
        return entries

    def mkdirs(self, path):
        return self._mutate("mkdirs", (path,), lambda: self.add_dir(path))

    def _relocate(self, source, target):
        if target in self.nodes:
            raise ConflictError(f"Refusing overwrite: {target}")
        if source not in self.nodes:
            raise AlistError(f"Missing source: {source}")
        assert self.nodes[pp.dirname(target)]["is_dir"]
        if not self.nodes[source]["is_dir"] and pp.splitext(source)[1].lower() in VIDEO_EXTENSIONS:
            if self.guard_video:
                self.guard_video(source, target)
        paths = [p for p in self.nodes if p == source or p.startswith(source + "/")]
        moved = {target + p[len(source):]: self.nodes.pop(p) for p in paths}
        self.nodes.update(moved)

    def rename(self, path, name):
        assert "/" not in name and "\\" not in name
        return self._mutate("rename", (path, name),
                            lambda: self._relocate(path, pp.join(pp.dirname(path), name)))

    def move(self, path, dst_dir, *, on_task=None):
        def submit():
            if path in self.async_move_paths:
                task = f"move-{len(self.move_tasks) + 1}"
                self.move_tasks[task] = (path, pp.join(dst_dir, pp.basename(path)))
                if on_task is not None:
                    on_task(task)
                return self.wait_move(task)
            return self._relocate(path, pp.join(dst_dir, pp.basename(path)))
        return self._mutate("move", (path, dst_dir), submit)

    def wait_move(self, task_id):
        assert task_id in self.move_tasks
        def complete():
            if task_id in self.move_errors:
                raise self.move_errors[task_id]
            source, target = self.move_tasks[task_id]
            if source in self.nodes:
                self._relocate(source, target)
            else:
                assert target in self.nodes
        return self._mutate("wait_move", (task_id,), complete)

    def remove(self, path):
        def remove_tree():
            if path not in self.nodes:
                raise AlistError(f"Missing delete target: {path}")
            for candidate in list(self.nodes):
                if candidate == path or candidate.startswith(path + "/"):
                    del self.nodes[candidate]
        return self._mutate("remove", (path,), remove_tree)

    def upload(self, local_path, remote_path):
        local = Path(local_path)
        assert local.suffix.lower() not in VIDEO_EXTENSIONS, "Video upload is forbidden"
        assert local.is_file()
        def upload_file():
            if remote_path in self.nodes:
                raise ConflictError(f"Refusing upload overwrite: {remote_path}")
            self.add_file(remote_path, content=local.read_bytes())
            task = f"upload-{len(self.tasks) + 1}"
            self.tasks[task] = remote_path
            return task
        return self._mutate("upload", (str(local), remote_path), upload_file)

    def wait_upload(self, task_id):
        assert task_id in self.tasks
        def complete():
            if task_id in self.upload_errors:
                raise self.upload_errors[task_id]
        return self._mutate("wait_upload", (task_id,), complete)

    def storage_identity(self, path):
        assert path == A
        return deepcopy(self.metadata_storage)

    def ensure_same_storage(self, source, video):
        assert (source, video) == (SOURCE, B)
        self._phase("ensure_same_storage", "before", (source, video))
        return {"id": 115, "mount_path": "/115", "driver": "115 Open"}

    def download(self, *args, **kwargs):
        pytest.fail("The pipeline must never download video content")

    def files_under(self, root):
        return {p: deepcopy(e) for p, e in self.nodes.items()
                if not e["is_dir"] and p.startswith(root + "/")}

    def source_mutations(self):
        return [event for event in self.events if event[0] in ("rename", "move", "remove")
                and (event[1] == SOURCE or event[1].startswith(SOURCE + "/"))]


class FakeScraper:
    """Honor prepare_output and create the manifest after image extension changes."""

    def __init__(self):
        self.calls = []
        self.fail_ids = set()
        self.layout = lambda movie: (f"#整理完成/{movie.dvdid}", f"{movie.dvdid}-C")
        self.after_output = None

    def __call__(self, movies, *, remote_roots, prepare_output, move_files):
        assert remote_roots == (A, B)
        assert move_files is False
        for movie in movies:
            self.calls.append((movie.dvdid, tuple(movie.files)))
            if movie.dvdid in self.fail_ids:
                raise AlistError("Synthetic scraper failure")
            movie.save_dir, movie.basename = self.layout(movie)
            movie.nfo_file = pp.join(movie.save_dir, movie.basename + ".nfo")
            movie.poster_file = pp.join(movie.save_dir, "poster.jpg")
            movie.fanart_file = pp.join(movie.save_dir, "fanart.jpg")
            prepare_output(movie)
            folder = Path(movie.save_dir)
            folder.mkdir(parents=True, exist_ok=True)
            movie.poster_file = str(Path(movie.poster_file).with_suffix(".png"))
            movie.fanart_file = str(Path(movie.fanart_file).with_suffix(".png"))
            document = ET.Element("movie")
            ET.SubElement(document, "id").text = movie.dvdid
            ET.SubElement(document, "title").text = "测试影片"
            nfo = Path(movie.nfo_file)
            nfo.write_bytes(ET.tostring(document, encoding="utf-8", xml_declaration=True))
            images = [Path(movie.poster_file), Path(movie.fanart_file),
                      folder / "extrafanart" / "scene.png"]
            for image in images:
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(PNG)
            # A manifest, not a directory walk or a size heuristic, controls upload.
            (folder / "scratch.txt").write_text("must not upload", encoding="utf-8")
            movie.metadata_files = [str(nfo), *(str(image) for image in images)]
            if self.after_output:
                self.after_output(movie)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = NS(
        alist=NS(base_url="https://alist.invalid", token="test-only",
                 source_dir=SOURCE, metadata_dir=A, video_dir=B,
                 work_dir=tmp_path / "state", http_timeout=1, task_timeout=1, poll_interval=1),
        scanner=NS(source="alist", minimum_size=1000, filename_extensions=list(VIDEO_EXTENSIONS),
                   ignored_folder_name_pattern=[r"^ignored$", r"^\.javsp-"],
                   ignored_id_pattern=[r"^$"], skip_nfo_dir=False, manual=False),
        summarizer=NS(move_files=True,
                      path=NS(hard_link=False, output_folder_pattern="#整理完成/{num}",
                              basename_pattern="{num}-C", length_by_byte=False, length_maximum=1000),
                      nfo=NS(basename_pattern="{num}"),
                      cover=NS(basename_pattern="poster"),
                      fanart=NS(basename_pattern="fanart")),
    )
    monkeypatch.setattr(javsp.file, "Cfg", lambda: cfg)
    monkeypatch.setattr(javsp.avid, "Cfg", lambda: cfg)
    client, scraper = FakeClient(), FakeScraper()
    return NS(cfg=cfg, client=client, scraper=scraper,
              run=lambda: run_alist(cfg, scraper, client=client))


def records(env, kind):
    with sqlite3.connect(env.cfg.alist.work_dir / "state.sqlite3") as db:
        rows = db.execute("SELECT value FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall()
    return [json.loads(value) for (value,) in rows]


def metadata_paths(avid="ABC-123", folder=None, basename=None):
    folder = folder or f"#整理完成/{avid}"
    basename = basename or f"{avid}-C"
    return {pp.join(A, folder, name)
            for name in (basename + ".nfo", "poster.png", "fanart.png", "extrafanart/scene.png")}


def video_path(avid="ABC-123", suffix="", extension=".mp4"):
    return f"{B}/#整理完成/{avid}/{avid}-C{suffix}{extension}"


def assert_identity(client, path, original):
    current = client.stat(path)
    assert current is not None, path
    assert current["id"] == original["id"]
    assert current["hash_info"] == original["hash_info"]
    assert current["size"] == original["size"]


def read_run_logs(env):
    return [path.read_text(encoding="utf-8")
            for path in sorted((env.cfg.alist.work_dir / "logs").glob("*.log"))]


def test_run_log_lists_paths_stage_results_and_retained_directories(env):
    env.client.add_file(f"{SOURCE}/good/ABC-123.mp4")
    env.client.add_file(f"{SOURCE}/bad/DEF-456.mp4")
    env.client.add_file(f"{SOURCE}/small/GHI-789.mp4", size=10)
    env.scraper.fail_ids.add("DEF-456")
    assert env.run() == 1
    log, = read_run_logs(env)
    assert "元数据上传到 A 成功:" in log
    assert "视频改名成功" in log
    assert "视频移动成功" in log
    assert "源目录清理成功" in log
    assert f"[整理成功] {SOURCE}/good" in log
    assert f"[刮削失败] {SOURCE}/bad" in log
    assert "Synthetic scraper failure" in log
    assert f"[跳过视频] {SOURCE}/small/GHI-789.mp4" in log
    assert f"[目录保留] {SOURCE}/bad" in log
    assert f"来源根目录始终保留: {SOURCE}" in log
    assert video_path() in log
    assert env.cfg.alist.token not in log


def test_lost_move_response_log_never_claims_success_until_recovery(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    path = f"{SOURCE}/batch/ABC-123-C.mp4"
    env.client.on("move", when="after", predicate=lambda source, dest: source == path,
                  error=AlistError("response lost"))
    assert env.run() == 1
    first, = read_run_logs(env)
    assert "视频改名成功" in first
    assert "视频移动成功" not in first
    assert f"[整理失败] {SOURCE}/batch | 视频移动:" in first
    assert "response lost" in first
    assert env.run() == 0
    logs = read_run_logs(env)
    assert len(logs) == 2
    recovered, = [log for log in logs if log != first]
    assert "待恢复影片: 1 部" in recovered
    assert "视频移动成功" in recovered
    assert f"[整理成功] {SOURCE}/batch" in recovered


def test_run_logging_restores_logger_and_records_global_failure(env):
    import logging
    logger = logging.getLogger("javsp.alist_pipeline")
    original = (logger.level, logger.propagate, list(logger.handlers))
    env.client.on("stat", predicate=lambda path: path == SOURCE,
                  error=AuthenticationError("authentication failed"))
    assert env.run() == 1
    assert (logger.level, logger.propagate, logger.handlers) == original
    log, = read_run_logs(env)
    assert "AList 运行终止: authentication failed" in log
    assert "本次 AList 日志已保存:" in log


@pytest.mark.parametrize("folder", ["batch", "中文目录" * 8])
def test_success_matching_layout_manifest_and_full_folder_cleanup(env, folder):
    directory = f"{SOURCE}/{folder}"
    original = env.client.add_file(f"{directory}/ABC-123.mp4")
    env.client.add_file(f"{directory}/readme.txt", content=b"readme")
    env.client.add_file(f"{directory}/ABC-123.srt", content=b"subtitle")
    env.client.add_file(f"{directory}/extras/notes.txt", content=b"nested residue")

    def metadata_before_video(source, target):
        assert metadata_paths() <= env.client.files_under(A).keys()
        assert all(e[0] != "upload" or Path(e[1]).suffix not in VIDEO_EXTENSIONS
                   for e in env.client.events)
    env.client.guard_video = metadata_before_video

    assert env.run() == 0
    assert_identity(env.client, video_path(), original)
    assert env.client.stat(SOURCE)["is_dir"]
    assert env.client.stat(directory) is None
    assert set(env.client.files_under(A)) == metadata_paths()
    assert pp.relpath(pp.dirname(video_path()), B) == "#整理完成/ABC-123"
    assert not list(env.cfg.alist.work_dir.rglob("*.mp4"))
    assert not list((env.cfg.alist.work_dir / "artifacts").rglob("*.nfo"))
    nfo = env.client.nodes[f"{A}/#整理完成/ABC-123/ABC-123-C.nfo"]["content"]
    assert ET.fromstring(nfo).findtext("id") == "ABC-123"
    assert all(job["status"] == "done" for job in records(env, "job"))
    assert all(e[1] != SOURCE for e in env.client.events if e[0] == "remove")


def test_root_video_is_organized_without_deleting_source_root(env):
    original = env.client.add_file(f"{SOURCE}/ABC-123.mp4")
    env.client.add_file(f"{SOURCE}/readme.txt", content=b"keep root residue")
    assert env.run() == 0
    assert_identity(env.client, video_path(), original)
    assert env.client.stat(SOURCE)["is_dir"]
    assert env.client.stat(f"{SOURCE}/readme.txt") is not None
    assert not [e for e in env.client.events if e[0] == "remove"]


def test_multipart_sorted_and_small_matching_part_retained(env):
    second = env.client.add_file(f"{SOURCE}/batch/ABC-123-2.mp4", size=10)
    first = env.client.add_file(f"{SOURCE}/batch/ABC-123-1.mp4")
    assert env.run() == 0
    assert_identity(env.client, video_path(suffix="-CD1"), first)
    assert_identity(env.client, video_path(suffix="-CD2"), second)
    assert len(env.scraper.calls) == 1
    assert env.scraper.calls[0][1] == (f"{SOURCE}/batch/ABC-123-1.mp4",
                                       f"{SOURCE}/batch/ABC-123-2.mp4")
    assert env.client.stat(f"{SOURCE}/batch") is None


@pytest.mark.parametrize("name,size", [("DEF-456.mp4", 10), ("unknown!.mp4", 4096)])
def test_filtered_or_unrecognized_video_prevents_folder_cleanup(env, name, size):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    untouched = env.client.add_file(f"{SOURCE}/batch/{name}", size=size)
    env.client.add_file(f"{SOURCE}/batch/readme.txt", content=b"retain")
    assert env.run() == 0
    assert_identity(env.client, f"{SOURCE}/batch/{name}", untouched)
    assert env.client.stat(video_path()) is not None
    assert env.client.stat(f"{SOURCE}/batch/readme.txt") is not None
    assert not [e for e in env.client.events if e[0] == "remove"]


def test_nested_ignored_directory_blocks_ancestor_even_without_video(env):
    env.client.add_file(f"{SOURCE}/group/good/ABC-123.mp4")
    untouched = env.client.add_file(f"{SOURCE}/group/ignored/deeper/note.txt", content=b"keep")
    assert env.run() == 0
    assert env.client.stat(f"{SOURCE}/group/good") is None
    assert env.client.stat(f"{SOURCE}/group") is not None
    assert_identity(env.client, f"{SOURCE}/group/ignored/deeper/note.txt", untouched)


def test_skip_nfo_directory_preserves_its_videos(env):
    env.cfg.scanner.skip_nfo_dir = True
    env.client.add_file(f"{SOURCE}/good/ABC-123.mp4")
    untouched = env.client.add_file(f"{SOURCE}/previous/DEF-456.mp4")
    env.client.add_file(f"{SOURCE}/previous/existing.NFO", content=b"<movie/>")
    assert env.run() == 0
    assert_identity(env.client, f"{SOURCE}/previous/DEF-456.mp4", untouched)
    assert [avid for avid, _ in env.scraper.calls] == ["ABC-123"]


def test_duplicate_number_in_separate_directories_is_skipped(env):
    env.client.add_file(f"{SOURCE}/one/ABC-123.mp4")
    env.client.add_file(f"{SOURCE}/two/ABC-123.mp4")
    before = deepcopy(env.client.nodes)
    assert env.run() == 0
    assert env.client.nodes == before
    assert env.scraper.calls == []
    assert env.client.events == []


@pytest.mark.parametrize("same_folder", [True, False])
def test_scrape_failure_isolated_and_partial_directory_kept(env, same_folder):
    good = f"{SOURCE}/group/good"
    bad = good if same_folder else f"{SOURCE}/group/bad"
    env.client.add_file(f"{good}/ABC-123.mp4")
    untouched = env.client.add_file(f"{bad}/DEF-456.mp4")
    env.scraper.fail_ids.add("DEF-456")
    assert env.run() == 1
    assert_identity(env.client, f"{bad}/DEF-456.mp4", untouched)
    assert env.client.stat(video_path()) is not None
    assert env.client.stat(f"{SOURCE}/group") is not None
    assert (env.client.stat(good) is not None) is same_folder


@pytest.mark.parametrize("destination", ["metadata", "video"])
def test_existing_target_never_overwritten_even_with_same_size(env, destination):
    source = env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    target = {"metadata": f"{A}/#整理完成/ABC-123/poster.png",
              "video": video_path()}[destination]
    existing = env.client.add_file(target, size=len(PNG) if destination == "metadata" else source["size"])
    before = deepcopy(env.client.nodes)
    assert env.run() == 1
    assert env.client.nodes == before
    assert_identity(env.client, target, existing)
    assert env.client.source_mutations() == []
    assert not [e for e in env.client.events if e[0] == "upload"]


@pytest.mark.parametrize("operation", ["rename", "move"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_interrupted_multipart_recovery_keeps_fixed_cd_numbering(env, operation, when):
    first = env.client.add_file(f"{SOURCE}/batch/ABC-123-1.mp4")
    second = env.client.add_file(f"{SOURCE}/batch/ABC-123-2.mp4")
    expected_source = (f"{SOURCE}/batch/ABC-123-2.mp4" if operation == "rename"
                       else f"{SOURCE}/batch/ABC-123-C-CD2.mp4")
    env.client.on(operation, when=when, predicate=lambda path, name: path == expected_source,
                  error=KeyboardInterrupt("process interrupted"))
    with pytest.raises(KeyboardInterrupt):
        env.run()
    saved = records(env, "job")[0]
    fixed_targets = [item["target"] for item in saved["files"]]
    assert_identity(env.client, video_path(suffix="-CD1"), first)
    calls = len(env.scraper.calls)
    if when == "before":
        # Once intent was saved, no response cannot distinguish an unsubmitted
        # operation from a queued one. Do not blindly resubmit it.
        events = len([e for e in env.client.events if e[0] == operation and e[1] == expected_source])
        assert env.run() == 1
        assert len([e for e in env.client.events
                    if e[0] == operation and e[1] == expected_source]) == events
        assert env.client.stat(f"{SOURCE}/batch") is not None
        # Simulate the original queued request eventually completing server-side.
        target = (f"{SOURCE}/batch/ABC-123-C-CD2.mp4" if operation == "rename"
                  else video_path(suffix="-CD2"))
        env.client._relocate(expected_source, target)
    assert env.run() == 0
    assert len(env.scraper.calls) == calls, "Recovery must not re-scrape the remaining part"
    assert [item["target"] for item in records(env, "job")[0]["files"]] == fixed_targets
    assert_identity(env.client, video_path(suffix="-CD1"), first)
    assert_identity(env.client, video_path(suffix="-CD2"), second)
    assert env.client.stat(f"{SOURCE}/batch") is None
    completed_events = [e for e in env.client.source_mutations()
                        if e[0] == "move" and e[1].endswith("-CD1.mp4")]
    assert len(completed_events) == 1


@pytest.mark.parametrize("operation", ["rename", "move"])
def test_lost_video_mutation_response_recovers_without_repeating_operation(env, operation):
    original = env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    path = f"{SOURCE}/batch/ABC-123{'-C' if operation == 'move' else ''}.mp4"
    env.client.on(operation, when="after", predicate=lambda source, dest: source == path,
                  error=AlistError("response lost"))
    assert env.run() == 1
    assert env.run() == 0
    assert_identity(env.client, video_path(), original)
    assert len([e for e in env.client.events if e[0] == operation and e[1] == path]) == 1
    assert len(env.scraper.calls) == 1


@pytest.mark.parametrize("message", ["unconfirmed task error", "task timeout"])
def test_upload_failure_or_timeout_never_mutates_source_and_reuses_task(env, message):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    original = env.client.files_under(SOURCE)
    env.client.on("wait_upload", error=AlistError(message))
    assert env.run() == 1
    assert env.client.files_under(SOURCE) == original
    assert env.client.source_mutations() == []
    job = records(env, "job")[0]
    pending = next(i for i in job["metadata"] if i["task"])
    task, stage = pending["task"], pending["stage"]
    assert Path(pending["local"]).is_file()
    if message == "unconfirmed task error":
        # An unclassified error is still uncertain; do not invent a new upload.
        env.client.on("wait_upload", error=AlistError(message))
    assert env.run() == (1 if message == "unconfirmed task error" else 0)
    assert [e for e in env.client.events if e[0] == "wait_upload" and e[1] == task] == [
        ("wait_upload", task), ("wait_upload", task)]
    assert len([e for e in env.client.events if e[0] == "upload" and e[2] == stage]) == 1
    if message == "unconfirmed task error":
        assert env.client.files_under(SOURCE) == original
        assert env.client.source_mutations() == []
        assert env.client.stat(video_path()) is None
    else:
        assert env.client.stat(video_path()) is not None


def test_lost_upload_response_reconciles_hash_without_uploading_again(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.client.on("upload", when="after", error=AlistError("upload response lost"))
    assert env.run() == 1
    assert env.client.source_mutations() == []
    stage = next(e[2] for e in env.client.events if e[0] == "upload")
    assert env.run() == 0
    assert len([e for e in env.client.events if e[0] == "upload" and e[2] == stage]) == 1
    assert env.client.stat(video_path()) is not None


def test_upload_before_submission_failure_stays_unconfirmed_and_keeps_source(env):
    original = env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.client.on("upload", error=AlistError("transport failed before submission"))
    assert env.run() == 1
    assert env.run() == 1
    assert_identity(env.client, f"{SOURCE}/batch/ABC-123.mp4", original)
    assert env.client.source_mutations() == []
    assert len([e for e in env.client.events if e[0] == "upload"]) == 1


def test_metadata_publish_response_loss_recovers(env):
    original = env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.client.on("move", when="after",
                  predicate=lambda path, dest: path.startswith(A + "/.javsp-staging/"),
                  error=AlistError("metadata publish response lost"))
    assert env.run() == 1
    assert env.client.source_mutations() == []
    assert env.run() == 0
    assert_identity(env.client, video_path(), original)
    assert set(env.client.files_under(A)) == metadata_paths()


def test_in_batch_collision_blocks_both_jobs_on_initial_run_and_retry(env):
    env.client.add_file(f"{SOURCE}/one/ABC-123.mp4")
    env.client.add_file(f"{SOURCE}/two/DEF-456.mp4")
    env.scraper.layout = lambda movie: ("#整理完成/shared", "same")
    original = deepcopy(env.client.nodes)
    for _ in range(2):
        assert env.run() == 1
        assert env.client.nodes == original
        assert env.client.source_mutations() == []
        assert not [event for event in env.client.events if event[0] == "upload"]


def test_added_source_content_after_video_move_is_preserved(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    added = f"{SOURCE}/batch/new.txt"
    env.client.on("move", when="after",
                  predicate=lambda path, dest: path.startswith(SOURCE + "/"),
                  callback=lambda *args: env.client.add_file(added, content=b"arrived during run"))
    assert env.run() == 0
    assert env.client.stat(added) is not None
    assert env.client.stat(video_path()) is not None
    assert not [event for event in env.client.events if event[0] == "remove"]


def test_content_added_between_cleanup_check_and_quarantine_is_not_deleted(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.client.on("rename", predicate=lambda path, name: path == f"{SOURCE}/batch",
                  callback=lambda *args: env.client.add_file(f"{SOURCE}/batch/new.txt", content=b"new"))
    assert env.run() == 1
    retained = [p for p in env.client.files_under(SOURCE) if p.endswith("/new.txt")]
    assert len(retained) == 1
    assert "/.javsp-cleanup-" in retained[0]
    assert not [event for event in env.client.events if event[0] == "remove"]
    assert env.run() == 1
    assert retained[0] in env.client.nodes


def test_quarantine_delete_response_lost_recreated_source_is_safe(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.client.add_file(f"{SOURCE}/batch/old.txt", content=b"old")
    replacement = f"{SOURCE}/batch/new.txt"
    env.client.on("remove", when="after",
                  callback=lambda *args: env.client.add_file(replacement, content=b"replacement"),
                  error=AlistError("delete response lost"))
    assert env.run() == 1
    recreated = env.client.stat(replacement)
    assert recreated is not None
    assert env.run() == 0
    assert_identity(env.client, replacement, recreated)
    removes = [event for event in env.client.events if event[0] == "remove"]
    assert len(removes) == 1
    assert "/.javsp-cleanup-" in removes[0][1]
    assert env.client.stat(SOURCE) is not None


def test_recreated_quarantine_after_uncertain_delete_is_never_deleted_again(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    replacement = []
    def recreate(path):
        replacement.append(path + "/new.txt")
        env.client.add_file(replacement[0], content=b"new owner")
    env.client.on("remove", when="after", callback=recreate, error=AlistError("response lost"))
    assert env.run() == 1
    assert env.run() == 1
    assert env.client.stat(replacement[0]) is not None
    assert len([event for event in env.client.events if event[0] == "remove"]) == 1


def test_cleanup_quarantine_rename_response_loss_recovers(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.client.add_file(f"{SOURCE}/batch/residue.txt", content=b"old")
    env.client.on("rename", when="after",
                  predicate=lambda path, name: path == f"{SOURCE}/batch",
                  error=AlistError("quarantine rename response lost"))
    assert env.run() == 1
    assert env.run() == 0
    assert env.client.files_under(SOURCE) == {}
    assert env.client.stat(SOURCE) is not None
    assert len([event for event in env.client.events
                if event[0] == "rename" and event[1] == f"{SOURCE}/batch"]) == 1


def test_same_size_changed_source_during_scrape_is_not_moved(env):
    original_path = f"{SOURCE}/batch/ABC-123.mp4"
    env.client.add_file(original_path)
    env.scraper.after_output = lambda movie: env.client.add_file(original_path)
    assert env.run() == 1
    assert env.client.stat(original_path) is not None
    assert env.client.source_mutations() == []
    assert not env.client.files_under(B)


def test_unprovable_lost_move_response_is_not_accepted_by_size_alone(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    entry = env.client.nodes[f"{SOURCE}/batch/ABC-123.mp4"]
    entry.pop("id")
    entry.pop("hash_info")
    env.client.on("move", when="after", predicate=lambda path, dest: path.startswith(SOURCE + "/"),
                  error=AlistError("response lost with no stable identity"))
    assert env.run() == 1
    assert env.run() == 1
    assert env.client.stat(f"{SOURCE}/batch") is not None
    assert not [event for event in env.client.events if event[0] == "remove"]
    assert records(env, "job")[0]["status"] != "done"


def test_scan_failure_does_not_mutate_any_source(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.client.on("list_dir", predicate=lambda path: path == f"{SOURCE}/batch",
                  error=AlistError("incomplete listing"))
    original = deepcopy(env.client.nodes)
    assert env.run() == 1
    assert env.client.nodes == original
    assert env.client.events == []


def test_empty_or_residue_only_folder_is_never_deleted(env):
    env.client.add_dir(f"{SOURCE}/empty")
    env.client.add_file(f"{SOURCE}/notes/info.txt", content=b"keep")
    original = deepcopy(env.client.nodes)
    assert env.run() == 0
    assert env.client.nodes == original
    assert env.client.events == []


def test_authentication_failure_aborts_before_other_jobs(env):
    env.client.add_file(f"{SOURCE}/one/ABC-123.mp4")
    env.client.add_file(f"{SOURCE}/two/DEF-456.mp4")
    env.client.on("upload", error=AuthenticationError("denied"))
    assert env.run() == 1
    assert env.client.source_mutations() == []
    assert len([event for event in env.client.events if event[0] == "upload"]) == 1


@pytest.mark.parametrize("change", ["overlap", "hardlink", "traversal", "different_storage"])
def test_invalid_configuration_or_storage_prevents_mutations(env, change):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    if change == "overlap":
        env.cfg.alist.metadata_dir = SOURCE + "/metadata"
    elif change == "hardlink":
        env.cfg.summarizer.path.hard_link = True
    elif change == "traversal":
        env.cfg.summarizer.path.output_folder_pattern = "../outside/{num}"
    else:
        env.client.on("ensure_same_storage", error=AlistError("different storage"))
    assert env.run() == 1
    assert env.client.events == []
    assert env.scraper.calls == []


@pytest.mark.parametrize("folder,basename", [("../escape", "video"), ("/absolute", "video"),
                                           ("safe", "../escape"), ("safe", "nested/video")])
def test_unsafe_generated_layout_is_rejected_without_remote_writes(env, folder, basename):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.scraper.layout = lambda movie: (folder, basename)
    assert env.run() == 1
    assert env.client.events == []


def test_changed_service_scope_cannot_reuse_recovery_journal(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.client.on("wait_upload", error=AlistError("timeout"))
    assert env.run() == 1
    events = deepcopy(env.client.events)
    env.cfg.alist.base_url = "https://another-alist.invalid"
    assert env.run() == 1
    assert env.client.events == events


def test_new_rename_collision_after_scan_preserves_both_source_files(env):
    source_path = f"{SOURCE}/batch/ABC-123.mp4"
    original = env.client.add_file(source_path)
    collision = f"{SOURCE}/batch/ABC-123-C.mp4"
    env.scraper.after_output = lambda movie: env.client.add_file(collision)
    assert env.run() == 1
    assert_identity(env.client, source_path, original)
    assert env.client.stat(collision) is not None
    assert env.client.source_mutations() == []
    assert not [event for event in env.client.events if event[0] == "upload"]


def test_previous_batch_conflicts_also_reserve_targets_against_new_jobs(env):
    env.client.add_file(f"{SOURCE}/one/ABC-123.mp4")
    env.client.add_file(f"{SOURCE}/two/DEF-456.mp4")
    env.scraper.layout = lambda movie: ("#整理完成/shared", "same")
    assert env.run() == 1
    env.client.add_file(f"{SOURCE}/three/GHI-789.mp4")
    original = deepcopy(env.client.nodes)
    assert env.run() == 1
    assert env.client.nodes == original
    assert env.client.source_mutations() == []
    assert not [event for event in env.client.events if event[0] == "upload"]


def test_interruption_after_rename_before_move_submission_resumes(env):
    original = env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    renamed = f"{SOURCE}/batch/ABC-123-C.mp4"
    # Crash on reading the renamed file after rename completion was journaled,
    # but before the move intent/request. Restart may safely issue the move.
    def after_rename(path, name):
        # transfer() first reads the original source again to confirm it vanished.
        # The next read of renamed happens in the move transfer.
        def arm(*args):
            env.client.on("stat", predicate=lambda path: path == renamed,
                          error=KeyboardInterrupt("crash before move intent"))
        env.client.on("stat", when="after", predicate=lambda source: source == path, callback=arm)
    env.client.on("rename", when="after",
                  predicate=lambda path, name: path == f"{SOURCE}/batch/ABC-123.mp4",
                  callback=after_rename)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    assert records(env, "job")[0]["files"][0]["state"] == "renamed"
    assert env.run() == 0
    assert_identity(env.client, video_path(), original)
    assert len([event for event in env.client.source_mutations()
                if event[0] == "rename" and event[1].endswith(".mp4")]) == 1
    assert len([event for event in env.client.source_mutations() if event[0] == "move"]) == 1


def test_changed_residual_sidecar_preserved_even_when_size_unchanged(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    sidecar = f"{SOURCE}/batch/readme.txt"
    env.client.add_file(sidecar, content=b"old")
    env.client.on("move", when="after", predicate=lambda path, dest: path.startswith(SOURCE + "/"),
                  callback=lambda *args: env.client.add_file(sidecar, content=b"new"))
    assert env.run() == 0
    assert env.client.nodes[sidecar]["content"] == b"new"
    assert not [event for event in env.client.events if event[0] == "remove"]


def test_long_generated_remote_path_is_rejected_before_upload(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.cfg.summarizer.path.length_maximum = 80
    env.cfg.summarizer.path.length_by_byte = True
    env.scraper.layout = lambda movie: ("#整理完成/" + "中文" * 12, "ABC-123-C")
    assert env.run() == 1
    assert env.client.events == []


def test_failed_second_part_does_not_hide_successful_other_movie(env):
    env.client.add_file(f"{SOURCE}/partial/ABC-123-1.mp4")
    env.client.add_file(f"{SOURCE}/partial/ABC-123-2.mp4")
    other = env.client.add_file(f"{SOURCE}/good/DEF-456.mp4")
    env.client.on("move", predicate=lambda path, dest: path.endswith("ABC-123-C-CD2.mp4"),
                  error=AlistError("second part failed"))
    assert env.run() == 1
    assert_identity(env.client, video_path("DEF-456"), other)
    assert env.client.stat(f"{SOURCE}/good") is None
    assert env.client.stat(f"{SOURCE}/partial") is not None
    assert env.client.stat(f"{SOURCE}/partial/ABC-123-C-CD2.mp4") is not None
    assert env.client.stat(video_path(suffix="-CD1")) is not None


def test_known_async_move_timeout_resumes_task_without_resubmitting(env):
    original = env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    renamed = f"{SOURCE}/batch/ABC-123-C.mp4"
    env.client.async_move_paths.add(renamed)
    env.client.on("wait_move", error=AlistError("queued move timed out"))
    assert env.run() == 1
    assert_identity(env.client, renamed, original)
    assert env.client.stat(video_path()) is None
    pending = records(env, "job")[0]["files"][0]
    task = pending["move_task"]
    assert task in env.client.move_tasks
    assert env.run() == 0
    assert_identity(env.client, video_path(), original)
    assert len([event for event in env.client.events
                if event[0] == "move" and event[1] == renamed]) == 1
    assert [event for event in env.client.events if event == ("wait_move", task)] == [
        ("wait_move", task), ("wait_move", task)]


def test_snapshot_video_classification_survives_extension_config_change(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    skipped = f"{SOURCE}/batch/DEF-456.avi"
    original = env.client.add_file(skipped, size=10)
    assert env.run() == 0
    assert_identity(env.client, skipped, original)
    env.cfg.scanner.filename_extensions.remove(".avi")
    assert env.run() == 0
    assert_identity(env.client, skipped, original)
    assert env.client.stat(f"{SOURCE}/batch") is not None
    assert not [event for event in env.client.events if event[0] == "remove"]


@pytest.mark.parametrize("which_directory", ["root", "nested"])
@pytest.mark.parametrize("identity_key", ["id", "created"])
def test_replaced_directory_identity_blocks_cleanup(env, which_directory, identity_key):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.client.add_dir(f"{SOURCE}/batch/empty")
    changed = f"{SOURCE}/batch" + ("/empty" if which_directory == "nested" else "")
    env.client.nodes[changed]["created"] = "2026-01-01T00:00:00Z"
    def replace_directory(*args):
        env.client.nodes[changed][identity_key] = (
            "new-directory-id" if identity_key == "id" else "2026-02-01T00:00:00Z")
    env.client.on("move", when="after", predicate=lambda path, dest: path.startswith(SOURCE + "/"),
                  callback=replace_directory)
    assert env.run() == 0
    assert env.client.stat(video_path()) is not None
    assert env.client.stat(changed) is not None
    assert env.client.stat(f"{SOURCE}/batch") is not None
    assert not [event for event in env.client.events if event[0] == "remove"]


def test_renamed_video_replaced_with_same_size_different_mtime_is_not_moved(env):
    source = f"{SOURCE}/batch/ABC-123.mp4"
    renamed = f"{SOURCE}/batch/ABC-123-C.mp4"
    env.client.add_file(source)
    # Some drivers expose neither stable IDs nor hashes. Timestamp is then the
    # available evidence of a change since successful rename confirmation.
    env.client.nodes[source].pop("id")
    env.client.nodes[source].pop("hash_info")
    def after_rename(*args):
        def replace_file(*args):
            env.client.nodes[renamed]["modified"] = "2026-02-01T00:00:00Z"
        # The destination snapshot is read before this source-absence check;
        # replace it immediately after that snapshot was observed.
        env.client.on("stat", when="after", predicate=lambda path: path == source,
                      callback=replace_file)
    env.client.on("rename", when="after", predicate=lambda path, name: path == source,
                  callback=after_rename)
    assert env.run() == 1
    assert env.run() == 1
    assert env.client.stat(renamed)["modified"] == "2026-02-01T00:00:00Z"
    assert env.client.stat(video_path()) is None
    assert not [event for event in env.client.source_mutations() if event[0] in ("move", "remove")]


def test_failed_recovery_preflight_still_reserves_target_for_new_job(env):
    source = f"{SOURCE}/one/ABC-123.mp4"
    env.client.add_file(source)
    env.scraper.layout = lambda movie: ("#整理完成/shared", "same")
    env.scraper.after_output = lambda movie: env.client.add_file(source)
    assert env.run() == 1
    env.scraper.after_output = None
    env.client.add_file(f"{SOURCE}/two/DEF-456.mp4")
    original = deepcopy(env.client.nodes)
    assert env.run() == 1
    assert env.client.nodes == original
    assert env.client.events == []


def test_terminal_upload_failure_retries_unique_stage_preserving_old_partial(env):
    from javsp.alist_client import TaskFailedError

    original = env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    def fail_task(task):
        env.client.upload_errors[task] = TaskFailedError("terminal upload failed")
        env.client.add_file(env.client.tasks[task], content=b"partial")
    env.client.on("wait_upload", callback=fail_task)
    assert env.run() == 1
    assert env.client.source_mutations() == []
    old_stage = next(event[2] for event in env.client.events if event[0] == "upload")
    assert env.client.nodes[old_stage]["content"] == b"partial"
    assert env.run() == 0
    assert_identity(env.client, video_path(), original)
    assert env.client.nodes[old_stage]["content"] == b"partial"
    uploads = [event[2] for event in env.client.events if event[0] == "upload"]
    assert uploads.count(old_stage) == 1
    # Four final metadata files plus the preserved failed upload.
    assert len(uploads) == 5
    assert len(set(uploads)) == 5
    assert metadata_paths() <= env.client.files_under(A).keys()
    assert len(env.scraper.calls) == 1


def test_terminal_async_move_failure_retries_when_source_unchanged(env):
    from javsp.alist_client import TaskFailedError

    original = env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    renamed = f"{SOURCE}/batch/ABC-123-C.mp4"
    env.client.async_move_paths.add(renamed)
    env.client.on("wait_move", callback=lambda task: env.client.move_errors.setdefault(
        task, TaskFailedError("terminal move failure")))
    assert env.run() == 1
    assert_identity(env.client, renamed, original)
    assert env.client.stat(video_path()) is None
    assert env.run() == 0
    assert_identity(env.client, video_path(), original)
    submissions = [event for event in env.client.events
                   if event[0] == "move" and event[1] == renamed]
    assert len(submissions) == 2
    assert len(env.client.move_tasks) == 2
    assert len(env.scraper.calls) == 1


@pytest.mark.parametrize("change", ["source_changed", "target_appeared"])
def test_terminal_move_retry_refuses_changed_source_or_new_target(env, change):
    from javsp.alist_client import TaskFailedError

    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    renamed = f"{SOURCE}/batch/ABC-123-C.mp4"
    env.client.async_move_paths.add(renamed)
    env.client.on("wait_move", callback=lambda task: env.client.move_errors.setdefault(
        task, TaskFailedError("terminal move failure")))
    assert env.run() == 1
    if change == "source_changed":
        replacement = env.client.add_file(renamed)
        preserved = renamed
    else:
        replacement = env.client.add_file(video_path())
        preserved = video_path()
    assert env.run() == 1
    assert_identity(env.client, preserved, replacement)
    assert env.client.stat(f"{SOURCE}/batch") is not None
    assert len([event for event in env.client.events
                if event[0] == "move" and event[1] == renamed]) == 1
    assert not [event for event in env.client.events if event[0] == "remove"]


def test_changed_metadata_mount_identity_rejects_same_path_journal(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.client.on("wait_upload", error=AlistError("timeout"))
    assert env.run() == 1
    events = deepcopy(env.client.events)
    old_scope = records(env, "scope")[0]
    env.client.metadata_storage["id"] = 201
    assert env.run() == 1
    assert env.client.events == events
    assert records(env, "scope")[0] == old_scope
    assert env.client.source_mutations() == []


def test_rejected_new_candidate_does_not_block_admitted_job_resume(env):
    original = env.client.add_file(f"{SOURCE}/one/ABC-123.mp4")
    env.scraper.layout = lambda movie: ("#整理完成/shared", "same")
    env.client.on("wait_upload", error=AlistError("timeout"))
    assert env.run() == 1
    waiting = env.client.add_file(f"{SOURCE}/two/DEF-456.mp4")
    env.client.on("wait_upload", error=AlistError("still queued"))
    assert env.run() == 1
    assert not env.client.files_under(B)
    # The rejected candidate is still a conflict, but must not starve the
    # earlier job which already owns the destination and uploaded a stage.
    assert env.run() == 1
    assert_identity(env.client, f"{B}/#整理完成/shared/same.mp4", original)
    assert_identity(env.client, f"{SOURCE}/two/DEF-456.mp4", waiting)
    assert env.client.stat(f"{SOURCE}/one") is None
    assert len(env.scraper.calls) == 2


def test_in_batch_rename_location_collision_blocks_distinct_destinations(env):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    env.client.add_file(f"{SOURCE}/batch/DEF-456.mp4")
    env.scraper.layout = lambda movie: (f"#整理完成/{movie.dvdid}", "same")
    original = deepcopy(env.client.nodes)
    for _ in range(2):
        assert env.run() == 1
        assert env.client.nodes == original
        assert env.client.events == []


@pytest.mark.parametrize("created", [None, "0001-01-01T00:00:00Z", "1970-01-01T00:00:00Z"])
def test_directory_without_stable_identity_is_retained(env, created):
    env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    directory = env.client.nodes[f"{SOURCE}/batch"]
    directory.pop("id")
    if created is not None:
        directory["created"] = created
    assert env.run() == 0
    assert env.client.stat(video_path()) is not None
    assert env.client.stat(f"{SOURCE}/batch") is not None
    assert not [event for event in env.client.events if event[0] == "remove"]


@pytest.mark.parametrize("transfer_kind", ["video", "metadata"])
@pytest.mark.parametrize("query_error", [AuthenticationError, ConflictError])
def test_accepted_move_task_query_refusal_resumes_original_task(env, transfer_kind, query_error):
    original = env.client.add_file(f"{SOURCE}/batch/ABC-123.mp4")
    renamed = f"{SOURCE}/batch/ABC-123-C.mp4"
    is_target_move = (
        (lambda path, destination: path == renamed) if transfer_kind == "video"
        else (lambda path, destination: path.startswith(A + "/.javsp-staging/"))
    )
    # Metadata staging paths contain a generated job ID, so enable async mode
    # when this move is submitted, before FakeClient invokes on_task.
    env.client.on("move", predicate=is_target_move,
                  callback=lambda path, destination: env.client.async_move_paths.add(path))
    env.client.on("wait_move", error=query_error("accepted task status query refused"))

    assert env.run() == 1
    job = records(env, "job")[0]
    items = job["files"] if transfer_kind == "video" else job["metadata"]
    pending = next(item for item in items if item.get("move_task"))
    task = pending["move_task"]
    assert pending["state"] == ("move_pending" if transfer_kind == "video" else "publish_pending")
    source, target = env.client.move_tasks[task]
    assert env.client.stat(source) is not None
    assert env.client.stat(target) is None
    assert env.client.stat(video_path()) is None
    if transfer_kind == "metadata":
        assert env.client.source_mutations() == []

    assert env.run() == 0
    assert_identity(env.client, video_path(), original)
    assert set(env.client.files_under(A)) == metadata_paths()
    assert env.client.stat(f"{SOURCE}/batch") is None
    assert len(env.client.move_tasks) == 1
    assert len([event for event in env.client.events
                if event[0] == "move" and event[1] == source]) == 1
    assert [event for event in env.client.events if event == ("wait_move", task)] == [
        ("wait_move", task), ("wait_move", task)]
    assert len(env.scraper.calls) == 1
