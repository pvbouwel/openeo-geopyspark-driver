import collections
import datetime
import getpass
import logging
from pathlib import Path

import pytest

from openeogeotrellis.utils import (
    dict_merge_recursive,
    describe_path,
    lonlat_to_mercator_tile_indices,
    nullcontext,
    utcnow,
    UtcNowClock,
    single_value,
    StatsReporter,
)


@pytest.mark.parametrize(["a", "b", "expected"], [
    ({}, {}, {}),
    ({1: 2}, {}, {1: 2}),
    ({}, {1: 2}, {1: 2}),
    ({1: 2}, {3: 4}, {1: 2, 3: 4}),
    ({1: {2: 3}}, {1: {4: 5}}, {1: {2: 3, 4: 5}}),
    ({1: {2: 3, 4: 5}, 6: 7}, {1: {8: 9}, 10: 11}, {1: {2: 3, 4: 5, 8: 9}, 6: 7, 10: 11}),
    ({1: {2: {3: {4: 5, 6: 7}}}}, {1: {2: {3: {8: 9}}}}, {1: {2: {3: {4: 5, 6: 7, 8: 9}}}}),
    ({1: {2: 3}}, {1: {2: 3}}, {1: {2: 3}})
])
def test_merge_recursive_default(a, b, expected):
    assert dict_merge_recursive(a, b) == expected


@pytest.mark.parametrize(["a", "b", "expected"], [
    ({1: 2}, {1: 3}, {1: 3}),
    ({1: 2, 3: 4}, {1: 5}, {1: 5, 3: 4}),
    ({1: {2: {3: {4: 5}}, 6: 7}}, {1: {2: "foo"}}, {1: {2: "foo", 6: 7}}),
    ({1: {2: {3: {4: 5}}, 6: 7}}, {1: {2: {8: 9}}}, {1: {2: {3: {4: 5}, 8: 9}, 6: 7}}),
])
def test_merge_recursive_overwrite(a, b, expected):
    result = dict_merge_recursive(a, b, overwrite=True)
    assert result == expected


@pytest.mark.parametrize(["a", "b", "expected"], [
    ({1: 2}, {1: 3}, {1: 3}),
    ({1: "foo"}, {1: {2: 3}}, {1: {2: 3}}),
    ({1: {2: 3}}, {1: "bar"}, {1: "bar"}),
    ({1: "foo"}, {1: "bar"}, {1: "bar"}),
])
def test_merge_recursive_overwrite_conflict(a, b, expected):
    with pytest.raises(ValueError) as e:
        dict_merge_recursive(a, b)
    assert "key 1" in str(e)

    result = dict_merge_recursive(a, b, overwrite=True)
    assert result == expected


def test_merge_recursive_preserve_input():
    a = {1: {2: 3}}
    b = {1: {4: 5}}
    result = dict_merge_recursive(a, b)
    assert result == {1: {2: 3, 4: 5}}
    assert a == {1: {2: 3}}
    assert b == {1: {4: 5}}


def test_dict_merge_recursive_accepts_arbitrary_mapping():
    class EmptyMapping(collections.Mapping):
        def __getitem__(self, key):
            raise KeyError(key)

        def __len__(self) -> int:
            return 0

        def __iter__(self):
            return iter(())

    a = EmptyMapping()
    b = {1: 2}
    assert dict_merge_recursive(a, b) == {1: 2}
    assert dict_merge_recursive(b, a) == {1: 2}
    assert dict_merge_recursive(a, a) == {}


def test_describe_path(tmp_path):
    tmp_path = Path(tmp_path)
    a_dir = tmp_path / "dir"
    a_dir.mkdir()
    a_file = tmp_path / "file.txt"
    a_file.touch()
    a_symlink = tmp_path / "symlink.txt"
    a_symlink.symlink_to(a_file)
    paths = [a_dir, a_file, a_symlink]
    paths.extend([str(p) for p in paths])
    for path in paths:
        d = describe_path(path)
        assert "rw" in d["mode"]
        assert d["user"] == getpass.getuser()

    assert describe_path(tmp_path / "invalid")["status"] == "does not exist"


@pytest.mark.parametrize(["lon", "lat", "zoom", "flip_y", "expected"], [
    (0, 0, 0, False, (0, 0)),
    (0, 0, 1, False, (0, 0)),
    (0, 0, 2, False, (1, 1)),
    (0, 0, 5, False, (15, 15)),
    (0, 0, 5, True, (15, 16)),
    (179, 85, 0, False, (0, 0)),
    (179, 85, 1, False, (1, 1)),
    (179, 85, 2, False, (3, 3)),
    (179, 85, 3, False, (7, 7)),
    (179, 85, 5, False, (31, 31)),
    (-179, 85, 5, False, (0, 31)),
    (179, -85, 5, False, (31, 0)),
    (-179, -85, 5, False, (0, 0)),
    (179, -85, 0, True, (0, 0)),
    (179, -85, 1, True, (1, 1)),
    (179, -85, 2, True, (3, 3)),
    (179, -85, 3, True, (7, 7)),
    (179, -85, 5, True, (31, 31)),
    (179, 85, 5, True, (31, 0)),
    (-179, -85, 5, True, (0, 31)),
    (-179, 85, 5, True, (0, 0)),
    (3.2, 51.3, 0, True, (0, 0)),
    (3.2, 51.3, 1, True, (1, 0)),
    (3.2, 51.3, 2, True, (2, 1)),
    (3.2, 51.3, 3, True, (4, 2)),
    (3.2, 51.3, 4, True, (8, 5)),
    (3.2, 51.3, 6, True, (32, 21)),
    (3.2, 51.3, 8, True, (130, 85)),
    (3.2, 51.3, 10, True, (521, 341)),
])
def test_lonlat_to_mercator_tile_indices(lon, lat, zoom, flip_y, expected):
    assert lonlat_to_mercator_tile_indices(longitude=lon, latitude=lat, zoom=zoom, flip_y=flip_y) == expected


def test_nullcontext():
    with nullcontext() as n:
        assert n is None


class TestUtcNowClock:

    def test_default(self):
        now = utcnow()
        real_now = datetime.datetime.utcnow()
        assert isinstance(now, datetime.datetime)
        assert (real_now - now).total_seconds() < 1

    def test_mock(self):
        with UtcNowClock.mock(now=datetime.datetime(2012, 3, 4, 5, 6)):
            assert utcnow() == datetime.datetime(2012, 3, 4, 5, 6)

    def test_mock_str_date(self):
        with UtcNowClock.mock(now="2021-10-22"):
            assert utcnow() == datetime.datetime(2021, 10, 22)

    def test_mock_str_datetime(self):
        with UtcNowClock.mock(now="2021-10-22 12:34:56"):
            assert utcnow() == datetime.datetime(2021, 10, 22, 12, 34, 56)


def test_single_value():
    try:
        single_value([])
        pytest.fail("an empty list doesn't have a single value")
    except ValueError:
        pass

    assert single_value([1]) == 1
    assert single_value([1, 1]) == 1

    try:
        xs = [1, 2]
        single_value(xs)
        pytest.fail(f"{xs} doesn't have a single value")
    except ValueError:
        pass

    assert single_value({'a': ['VH'], 'b': ['VH']}.values()) == ['VH']


class TestStatsReporter:
    def test_basic(self, caplog):
        caplog.set_level(logging.INFO)
        with StatsReporter() as stats:
            stats["apple"] += 1
            stats["banana"] += 2
            for i in range(3):
                stats["banana"] += 5
            stats["coconut"] = 8

        assert caplog.messages == ["stats: {'apple': 1, 'banana': 17, 'coconut': 8}"]

    def test_exception(self, caplog):
        caplog.set_level(logging.INFO)
        with pytest.raises(ValueError):
            with StatsReporter() as stats:
                stats["apple"] += 1
                stats["banana"] += 2
                for i in range(3):
                    if i > 1:
                        raise ValueError
                    stats["banana"] += 5
                stats["coconut"] = 8

        assert caplog.messages == ["stats: {'apple': 1, 'banana': 12}"]
