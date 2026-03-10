"""Tests for core.file_grouping — RAW+JPG grouping logic."""
from core.file_grouping import FileGroup, build_group_map, expand_paths_for_group


class TestBuildGroupMap:
    def test_basic_pair(self):
        files = ["/photos/IMG_001.jpg", "/photos/IMG_001.cr3"]
        group_map, hidden = build_group_map(files)

        assert len(group_map) == 1
        assert "/photos/IMG_001.jpg" in group_map
        g = group_map["/photos/IMG_001.jpg"]
        assert g.primary == "/photos/IMG_001.jpg"
        assert g.raw_files == ("/photos/IMG_001.cr3",)
        assert hidden == {"/photos/IMG_001.cr3"}

    def test_no_pair_raw_only(self):
        files = ["/photos/IMG_001.cr3"]
        group_map, hidden = build_group_map(files)
        assert len(group_map) == 0
        assert len(hidden) == 0

    def test_no_pair_jpg_only(self):
        files = ["/photos/IMG_001.jpg"]
        group_map, hidden = build_group_map(files)
        assert len(group_map) == 0
        assert len(hidden) == 0

    def test_multiple_raws_same_stem(self):
        files = [
            "/photos/IMG_001.jpg",
            "/photos/IMG_001.cr3",
            "/photos/IMG_001.dng",
        ]
        group_map, hidden = build_group_map(files)

        assert len(group_map) == 1
        g = group_map["/photos/IMG_001.jpg"]
        assert set(g.raw_files) == {"/photos/IMG_001.cr3", "/photos/IMG_001.dng"}
        assert hidden == {"/photos/IMG_001.cr3", "/photos/IMG_001.dng"}

    def test_different_directories_not_grouped(self):
        files = [
            "/photos/a/IMG_001.jpg",
            "/photos/b/IMG_001.cr3",
        ]
        group_map, hidden = build_group_map(files)
        assert len(group_map) == 0
        assert len(hidden) == 0

    def test_mixed_paired_and_unpaired(self):
        files = [
            "/photos/IMG_001.jpg",
            "/photos/IMG_001.nef",
            "/photos/IMG_002.jpg",  # no RAW pair
            "/photos/IMG_003.arw",  # no JPG pair
        ]
        group_map, hidden = build_group_map(files)

        assert len(group_map) == 1
        assert "/photos/IMG_001.jpg" in group_map
        assert hidden == {"/photos/IMG_001.nef"}

    def test_case_insensitive_extension(self):
        files = ["/photos/IMG_001.JPG", "/photos/IMG_001.CR3"]
        # Extensions are lowered internally
        group_map, hidden = build_group_map(files)
        # .JPG won't match because file stem comparison is case-sensitive
        # but extension comparison is lowered.  Both uppercase extensions
        # should still be recognised.
        assert len(group_map) == 1

    def test_jpeg_extension(self):
        files = ["/photos/IMG_001.jpeg", "/photos/IMG_001.cr3"]
        group_map, hidden = build_group_map(files)
        assert len(group_map) == 1
        assert "/photos/IMG_001.jpeg" in group_map


class TestExpandPaths:
    def test_expand_with_group(self):
        group_map = {
            "/photos/IMG_001.jpg": FileGroup(
                primary="/photos/IMG_001.jpg",
                raw_files=("/photos/IMG_001.cr3",),
            )
        }
        result = expand_paths_for_group(["/photos/IMG_001.jpg"], group_map)
        assert set(result) == {"/photos/IMG_001.jpg", "/photos/IMG_001.cr3"}

    def test_expand_without_group(self):
        result = expand_paths_for_group(["/photos/IMG_002.jpg"], {})
        assert result == ["/photos/IMG_002.jpg"]

    def test_expand_no_duplicates(self):
        group_map = {
            "/photos/IMG_001.jpg": FileGroup(
                primary="/photos/IMG_001.jpg",
                raw_files=("/photos/IMG_001.cr3",),
            )
        }
        # If RAW is already in the input, don't duplicate it.
        result = expand_paths_for_group(
            ["/photos/IMG_001.jpg", "/photos/IMG_001.cr3"], group_map
        )
        assert result.count("/photos/IMG_001.cr3") == 1


class TestFileGroup:
    def test_all_files(self):
        g = FileGroup(primary="/a.jpg", raw_files=("/a.cr3", "/a.dng"))
        assert g.all_files == ("/a.jpg", "/a.cr3", "/a.dng")

    def test_all_files_no_raws(self):
        g = FileGroup(primary="/a.jpg", raw_files=())
        assert g.all_files == ("/a.jpg",)
