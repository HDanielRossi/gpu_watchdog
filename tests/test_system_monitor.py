"""Tests de SystemMonitor, en particular el soporte de multiples
`disk_paths` (varios filesystems vigilados a la vez, p.ej. / y almacenamiento
adicional para modelos)."""
from __future__ import annotations

from collections import namedtuple

from guardian.monitors.system import SystemMonitor

_GB = 1024 ** 3

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free", "percent"])


def test_single_disk_path_is_the_default(monkeypatch):
    monkeypatch.setattr("psutil.disk_usage", lambda path: _DiskUsage(100 * _GB, 50 * _GB, 50 * _GB, 50.0))
    monitor = SystemMonitor()

    sample = monitor.read()

    assert [d.mountpoint for d in sample.disks] == ["/"]


def test_multiple_disk_paths_are_all_reported(monkeypatch):
    usage_by_path = {
        "/": _DiskUsage(100 * _GB, 90 * _GB, 10 * _GB, 90.0),
        "/mnt/ai-storage": _DiskUsage(2000 * _GB, 500 * _GB, 1500 * _GB, 25.0),
    }
    monkeypatch.setattr("psutil.disk_usage", lambda path: usage_by_path[path])
    monitor = SystemMonitor(disk_paths=["/", "/mnt/ai-storage"])

    sample = monitor.read()

    assert [d.mountpoint for d in sample.disks] == ["/", "/mnt/ai-storage"]
    root, storage = sample.disks
    assert round(root.free_gb) == 10
    assert round(storage.free_gb) == 1500
    assert round(storage.percent) == 25


def test_disk_path_failure_is_skipped_without_raising_others_still_reported(monkeypatch):
    def _fake_disk_usage(path):
        if path == "/mnt/broken":
            raise OSError("no such mountpoint")
        return _DiskUsage(100 * _GB, 10 * _GB, 90 * _GB, 10.0)

    monkeypatch.setattr("psutil.disk_usage", _fake_disk_usage)
    monitor = SystemMonitor(disk_paths=["/", "/mnt/broken", "/mnt/ai-storage"])

    sample = monitor.read()

    assert [d.mountpoint for d in sample.disks] == ["/", "/mnt/ai-storage"]
