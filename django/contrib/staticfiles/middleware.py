# Adapted from WhiteNoise (MIT), https://github.com/evansd/whitenoise
import os
import warnings
from urllib.parse import urlparse

from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async

from django.conf import settings
from django.core.exceptions import MiddlewareNotUsed

from .media_types import MediaTypes
from .responders import MissingFileError, NotARegularFileError, StaticFile


class BaseFileServingMiddleware:
    # Ten years is what nginx sets a max age if you use 'expires max;'
    # so we'll follow its lead
    FOREVER = 10 * 365 * 24 * 60 * 60
    sync_capable = True
    async_capable = True

    charset = "utf-8"
    allow_all_origins = False
    max_age = 0
    mimetypes = None

    def __init__(self, get_response):
        self.get_response = get_response

        self.media_types = MediaTypes(extra_types=self.mimetypes)
        self.files = {}

        self.async_mode = iscoroutinefunction(self.get_response)
        if self.async_mode:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self.async_mode:
            return self.__acall__(request)
        static_file = self.files.get(request.path_info)
        if static_file is not None:
            return self.serve(static_file, request)
        return self.get_response(request)

    async def __acall__(self, request):
        static_file = self.files.get(request.path_info)
        if static_file is not None:
            return await sync_to_async(self.serve, thread_sensitive=False)(
                static_file, request
            )
        return await self.get_response(request)

    @staticmethod
    def serve(static_file, request):
        return static_file.get_response(request.method, request.META)

    def add_files(self, root, prefix=None):
        root = os.path.abspath(root)
        root = root.rstrip(os.path.sep) + os.path.sep
        prefix = ensure_leading_trailing_slash(prefix)
        if os.path.isdir(root):
            self.update_files_dictionary(root, prefix)
        else:
            warnings.warn(f"No directory at: {root}", stacklevel=3)

    def update_files_dictionary(self, root, prefix):
        # Build a mapping from paths to the results of `os.stat` calls
        # so we only have to touch the filesystem once
        stat_cache = dict(scantree(root))
        for path in stat_cache:
            relative_path = path[len(root) :]
            relative_url = relative_path.replace("\\", "/")
            url = prefix + relative_url
            self.add_file_to_dictionary(url, path, stat_cache=stat_cache)

    def add_file_to_dictionary(self, url, path, stat_cache=None):
        if self.is_compressed_variant(path, stat_cache=stat_cache):
            return
        try:
            static_file = self.get_static_file(path, url, stat_cache)
        except NotARegularFileError:
            return
        self.files[url] = static_file

    @staticmethod
    def is_compressed_variant(path, stat_cache=None):
        for ext in (".gz", ".br", ".zst"):
            if path.endswith(ext):
                uncompressed_path = path[: -len(ext)]
                if stat_cache is None:
                    return os.path.isfile(uncompressed_path)
                else:
                    return uncompressed_path in stat_cache
        return False

    def get_static_file(self, path, url, stat_cache=None):
        # Optimization: bail early if file does not exist
        if stat_cache is None and not os.path.exists(path):
            raise MissingFileError(path)
        headers = {}
        self.add_mime_headers(headers, path, url)
        self.add_cache_headers(headers, path, url)
        if self.allow_all_origins:
            headers["Access-Control-Allow-Origin"] = "*"
        self.add_headers_function(headers, path, url)
        return StaticFile(
            path=path,
            headers=headers,
            stat_cache=stat_cache,
            encodings={"gzip": path + ".gz", "br": path + ".br", "zstd": path + ".zst"},
        )

    def add_mime_headers(self, headers, path, url):
        media_type = self.media_types.get_type(path)
        if media_type.startswith("text/"):
            media_type = f"{media_type}; charset={self.charset}"
        headers["Content-Type"] = media_type

    def add_cache_headers(self, headers, path, url):
        if self.immutable_file_test(path, url):
            headers["Cache-Control"] = f"max-age={self.FOREVER}, public, immutable"
        elif self.max_age is not None:
            headers["Cache-Control"] = f"max-age={self.max_age}, public"

    def immutable_file_test(self, path, url):
        """
        This should be implemented by sub-classes
        (see e.g. StaticFilesMiddleware)
        """
        return False

    def add_headers_function(self, headers, path, url):
        """
        Subclasses can overwrite this function to further modify headers.
        headers - A dict of the headers for the current file
        path - The absolute path to the local file
        url - The host-relative URL of the file e.g. /static/styles/app.css
        The function should not return anything; changes should be made by
        modifying the headers dictionary directly.
        """
        pass


class StaticFilesMiddleware(BaseFileServingMiddleware):
    static_prefix = None
    allow_all_origins = True
    max_age = 60

    def __init__(self, get_response):
        if settings.DEBUG:
            raise MiddlewareNotUsed
        super().__init__(get_response)

        self._hashed_files = set()
        try:
            from django.contrib.staticfiles.storage import staticfiles_storage

            self._hashed_files = set(staticfiles_storage.hashed_files.values())
        except AttributeError:
            pass

        if self.static_prefix is None:
            self.static_prefix = urlparse(settings.STATIC_URL or "").path
            if settings.FORCE_SCRIPT_NAME:
                script_name = settings.FORCE_SCRIPT_NAME.rstrip("/")
                if self.static_prefix.startswith(script_name):
                    self.static_prefix = self.static_prefix[len(script_name) :]
        self.static_prefix = ensure_leading_trailing_slash(self.static_prefix)

        self.static_root = settings.STATIC_ROOT
        if self.static_root:
            self.add_files(self.static_root, prefix=self.static_prefix)

    def immutable_file_test(self, path, url):
        """
        Determine whether given URL represents an immutable file (i.e. a
        file with a hash of its contents as part of its name) which can
        therefore be cached forever
        """
        rel = os.path.relpath(path, self.static_root).replace("\\", "/")
        return rel in self._hashed_files


def scantree(root):
    """
    Recurse the given directory yielding (pathname, os.stat(pathname)) pairs
    """
    for entry in os.scandir(root):
        if entry.is_dir():
            yield from scantree(entry.path)
        else:
            yield entry.path, entry.stat()


def ensure_leading_trailing_slash(path):
    path = (path or "").strip("/")
    return f"/{path}/" if path else "/"
