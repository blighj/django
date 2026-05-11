import errno
import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

from django.contrib.staticfiles.responders import (
    FileEntry,
    IsDirectoryError,
    MissingFileError,
    NotARegularFileError,
    SlicedFile,
    StaticFile,
)
from django.test import SimpleTestCase


class SlicedFileTests(SimpleTestCase):
    def test_read_after_exhaustion_returns_empty(self):
        sliced = SlicedFile(BytesIO(b"hello world"), 0, 4)
        sliced.read()
        self.assertEqual(sliced.read(), b"")
        sliced.close()

    def test_iter_breaks_on_premature_eof(self):
        sliced = SlicedFile(BytesIO(b"x" * 100), 0, 99)
        with patch.object(sliced, "fileobj") as mock_fileobj:
            mock_fileobj.read.return_value = b""
            result = b"".join(sliced)
        self.assertEqual(result, b"")

    def test_close_propagates_to_underlying_fileobj(self):
        underlying = BytesIO(b"hello world")
        sliced = SlicedFile(underlying, 0, 4)
        sliced.close()
        self.assertTrue(underlying.closed)


class GetRangeNotSatisfiableResponseTests(SimpleTestCase):
    def test_returns_416_with_content_range(self):
        response = StaticFile.get_range_not_satisfiable_response(None, 42)
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response["Content-Range"], "bytes */42")

    def test_closes_file_handle_when_provided(self):
        fileobj = BytesIO(b"data")
        StaticFile.get_range_not_satisfiable_response(fileobj, 4)
        self.assertTrue(fileobj.closed)


class FileEntryTests(SimpleTestCase):
    def test_enoent_raises_missing_file_error(self):
        with self.assertRaises(MissingFileError):
            FileEntry("/nonexistent/path/file.txt")

    def test_non_enoent_oserror_is_reraised(self):
        error = OSError()
        error.errno = errno.EACCES
        with patch("os.stat", side_effect=error):
            with self.assertRaises(OSError) as cm:
                FileEntry("/some/path")
        self.assertEqual(cm.exception.errno, errno.EACCES)

    def test_directory_raises_is_directory_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(IsDirectoryError):
                FileEntry(tmpdir)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "mkfifo not available on this platform")
    def test_special_file_raises_not_a_regular_file_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fifo = os.path.join(tmpdir, "test.fifo")
            os.mkfifo(fifo)
            with self.assertRaises(NotARegularFileError):
                FileEntry(fifo)
