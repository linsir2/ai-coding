from ai_coding.infra import file_utils as fu


def test_write_read(tmp_path):
    p = tmp_path / "a.txt"
    fu.write_file(p, "hello")
    assert fu.read_file(p) == "hello"


def test_append(tmp_path):
    p = tmp_path / "a.log"
    fu.append_to_file(p, "one")
    fu.append_to_file(p, "two")
    assert fu.read_file(p) == "one\ntwo\n"


def test_create_directories_and_exists(tmp_path):
    d = tmp_path / "x" / "y"
    fu.create_directories(d)
    assert fu.exists(d)


def test_list_files_and_recursive(tmp_path):
    fu.write_file(tmp_path / "top.txt", "t")
    sub = tmp_path / "s"
    sub.mkdir()
    fu.write_file(sub / "sub.txt", "s")
    flat = fu.list_files(tmp_path)
    assert len(flat) == 1  # top.txt only (sub.txt is nested)
    rec = fu.list_files_recursive(tmp_path)
    assert len(rec) == 2


def test_delete_recursive(tmp_path):
    fu.write_file(tmp_path / "keep.txt", "k")
    d = tmp_path / "drop"
    fu.write_file(d / "inner.txt", "i")
    fu.delete_recursive(d)
    assert fu.exists(d) is False
    assert fu.exists(tmp_path / "keep.txt")  # untouched


def test_extension_helpers():
    assert fu.get_file_extension("a.txt") == "txt"
    # A leading-dot hidden file has no suffix in pathlib (consistent with Java defaults).
    assert fu.get_file_extension(".gitignore") == ""
    assert fu.file_name_without_extension("a.txt") == "a"


def test_copy_file(tmp_path):
    src = tmp_path / "src.txt"
    fu.write_file(src, "data")
    dst = tmp_path / "dst.txt"
    fu.copy(src, dst)
    assert fu.read_file(dst) == "data"


def test_get_file_size(tmp_path):
    p = tmp_path / "s.txt"
    fu.write_file(p, "12345")
    assert fu.get_file_size(p) == 5
