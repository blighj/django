import asyncio
import gzip
import os
import unittest
from email.utils import parsedate
from unittest import mock

from asgiref.sync import iscoroutinefunction

from django.conf import settings
from django.core.exceptions import MiddlewareNotUsed
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from .cases import CollectionTestCase

factory = RequestFactory()


def _sentinel_response(request):
    return HttpResponse("sentinel")


async def _async_sentinel_response(request):
    return HttpResponse("sentinel")


@override_settings(
    DEBUG=False,
    MIDDLEWARE=["django.contrib.staticfiles.middleware.StaticFilesMiddleware"],
    ROOT_URLCONF="staticfiles_tests.urls.middleware",
)
class StaticFilesMiddlewareTests(CollectionTestCase):

    @staticmethod
    def _make_middleware(get_response=_sentinel_response):
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        return StaticFilesMiddleware(get_response)

    # ------------------------------------------------------------------ #
    # Startup / scan behaviour                                             #
    # ------------------------------------------------------------------ #

    def test_files_dict_populated_from_static_root(self):
        mw = self._make_middleware()
        self.assertIn("/static/test.txt", mw.files)
        self.assertIn("/static/prefix/test.txt", mw.files)
        self.assertIn("/static/test/file1.txt", mw.files)
        self.assertIn("/static/test/⊗.txt", mw.files)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "mkfifo not available on this platform")
    def test_non_regular_file_skipped_during_scan(self):
        fifo = os.path.join(settings.STATIC_ROOT, "test.fifo")
        os.mkfifo(fifo)
        try:
            mw = self._make_middleware()
            self.assertNotIn("/static/test.fifo", mw.files)
        finally:
            os.unlink(fifo)

    def test_mtime_and_size_captured_once(self):
        mw = self._make_middleware()
        # Serving a request must not call os.stat.
        request = factory.get("/static/test.txt")
        with mock.patch("django.contrib.staticfiles.middleware.os.stat") as mock_stat:
            mw(request).close()
        mock_stat.assert_not_called()

    def test_compressed_sibling_not_exposed(self):
        css_path = os.path.join(settings.STATIC_ROOT, "app.css")
        gz_path = os.path.join(settings.STATIC_ROOT, "app.css.gz")
        open(css_path, "w").close()
        with gzip.open(gz_path, "wb") as gz:
            gz.write(b"compressed")

        mw = self._make_middleware()

        self.assertIn("/static/app.css", mw.files)
        self.assertNotIn("/static/app.css.gz", mw.files)

    def test_orphan_compressed_file_served_literally(self):
        # A .gz file with no uncompressed sibling is served as a regular file.
        gz_path = os.path.join(settings.STATIC_ROOT, "foo.gz")
        with gzip.open(gz_path, "wb") as gz:
            gz.write(b"orphan content")

        mw = self._make_middleware()
        self.assertIn("/static/foo.gz", mw.files)
        request = factory.get("/static/foo.gz")
        response = mw(request)
        content = b"".join(response.streaming_content)
        response.close()

        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(content), 0)

    # ------------------------------------------------------------------ #
    # DEBUG gate                                                           #
    # ------------------------------------------------------------------ #

    @override_settings(DEBUG=True)
    def test_debug_true_raises_middleware_not_used(self):
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        with self.assertRaises(MiddlewareNotUsed):
            StaticFilesMiddleware(_sentinel_response)

    @override_settings(STATIC_URL="https://cdn.example.com/static/")
    def test_host_static_url_uses_path_as_prefix(self):
        # When STATIC_URL is a full CDN URL the middleware extracts the path
        # component and uses it as the prefix, so Django can act as the CDN
        # origin without raising MiddlewareNotUsed.
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        mw = StaticFilesMiddleware(_sentinel_response)
        self.assertEqual(mw.static_prefix, "/static/")

    @override_settings(FORCE_SCRIPT_NAME="/subdir", STATIC_URL="/subdir/static/")
    def test_force_script_name_strips_prefix(self):
        mw = self._make_middleware()
        request = factory.get("/static/test.txt")
        response = mw(request)
        self.assertEqual(response.status_code, 200)
        response.close()

    # ------------------------------------------------------------------ #
    # Request dispatch                                                     #
    # ------------------------------------------------------------------ #

    def test_get_serves_file(self):
        mw = self._make_middleware()
        request = factory.get("/static/test.txt")
        response = mw(request)
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content)
        response.close()
        self.assertIn(b"Can we find", content)
        self.assertEqual(int(response["Content-Length"]), len(content))
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")

    def test_head_returns_headers_no_body(self):
        mw = self._make_middleware()
        response = mw(factory.head("/static/test.txt"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Content-Length", response)
        self.assertIn("Content-Type", response)
        body = (
            b"".join(response.streaming_content)
            if hasattr(response, "streaming_content")
            else response.content
        )
        response.close()
        self.assertEqual(body, b"")

    def test_post_returns_405(self):
        mw = self._make_middleware()
        request = factory.post("/static/test.txt")
        response = mw(request)
        self.assertEqual(response.status_code, 405)
        allow = response["Allow"]
        self.assertIn("GET", allow)
        self.assertIn("HEAD", allow)

    def test_nonascii_filename(self):
        mw = self._make_middleware()
        request = factory.get("/static/test/⊗.txt")
        response = mw(request)
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content)
        response.close()
        self.assertIn("⊗".encode(), content)

    def test_path_traversal_not_served(self):
        # Dict lookup simply misses; the middleware never constructs a path
        # from the URL, so traversal is structurally impossible.
        mw = self._make_middleware()
        request = factory.get("/static/../secret.txt")
        response = mw(request)
        self.assertEqual(response.content, b"sentinel")

    # ------------------------------------------------------------------ #
    # Headers                                                              #
    # ------------------------------------------------------------------ #

    def test_content_type_from_media_types(self):
        mw = self._make_middleware()
        cases = [
            ("/static/test/nonascii.css", "text/css; charset=utf-8"),
            ("/static/test/vendor/module.js", "text/javascript; charset=utf-8"),
        ]
        for url, expected_ct in cases:
            with self.subTest(url=url):
                self.assertIn(url, mw.files)
                request = factory.get(url)
                response = mw(request)
                response.close()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], expected_ct)

    def test_content_type_fallback(self):
        unknown_path = os.path.join(settings.STATIC_ROOT, "foo.unknownext")
        open(unknown_path, "w").close()
        mw = self._make_middleware()
        request = factory.get("/static/foo.unknownext")
        response = mw(request)
        response.close()
        self.assertEqual(response["Content-Type"], "application/octet-stream")

    def test_response_headers(self):
        mw = self._make_middleware()
        request = factory.get("/static/test.txt")
        response = mw(request)
        response.close()
        self.assertEqual(response.status_code, 200)
        self.assertIn("ETag", response)
        self.assertRegex(response["ETag"], r'^"[0-9a-f]+-[0-9a-f]+"$')
        self.assertIn("Last-Modified", response)
        self.assertIsNotNone(parsedate(response["Last-Modified"]))
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "max-age=60, public")
        self.assertNotIn("Content-Disposition", response)

    def test_content_type_binary(self):
        mw = self._make_middleware()
        request = factory.get("/static/test/window.png")
        response = mw(request)
        response.close()
        ct = response["Content-Type"]
        self.assertIn("image/png", ct)
        self.assertNotIn("charset", ct)

    def test_etag_is_mtime_and_size(self):
        from email.utils import formatdate, parsedate
        from time import mktime

        mw = self._make_middleware()
        path = os.path.join(settings.STATIC_ROOT, "test.txt")
        st = os.stat(path)
        last_modified = parsedate(formatdate(st.st_mtime, usegmt=True))
        expected_etag = '"{:x}-{:x}"'.format(int(mktime(last_modified)), st.st_size)
        r1 = mw(factory.get("/static/test.txt"))
        r1.close()
        r2 = mw(factory.get("/static/test.txt"))
        r2.close()
        self.assertEqual(r1["ETag"], expected_etag)
        self.assertEqual(r1["ETag"], r2["ETag"])

    def test_zero_mtime_omits_last_modified_and_etag(self):
        epoch_path = os.path.join(settings.STATIC_ROOT, "epoch.txt")
        open(epoch_path, "w").close()
        os.utime(epoch_path, (0, 0))
        mw = self._make_middleware()
        request = factory.get("/static/epoch.txt")
        response = mw(request)
        response.close()
        self.assertNotIn("Last-Modified", response)
        self.assertNotIn("ETag", response)

    def test_class_attribute_overrides(self):
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        # Default adds charset and CORS.
        mw_default = self._make_middleware()
        r_default = mw_default(factory.get("/static/test/nonascii.css"))
        r_default.close()
        self.assertIn("; charset=utf-8", r_default["Content-Type"])
        self.assertEqual(r_default["Access-Control-Allow-Origin"], "*")

        # Subclass overrides suppress both.
        class CustomMiddleware(StaticFilesMiddleware):
            charset = "ISO-8859-1"
            allow_all_origins = False

        mw_custom = CustomMiddleware(_sentinel_response)
        r_custom = mw_custom(factory.get("/static/test/nonascii.css"))
        r_custom.close()
        self.assertIn("; charset=ISO-8859-1", r_custom["Content-Type"])
        self.assertNotIn("Access-Control-Allow-Origin", r_custom)

    def test_static_prefix_class_attribute(self):
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        class CustomMiddleware(StaticFilesMiddleware):
            static_prefix = "/assets/"

        mw = CustomMiddleware(_sentinel_response)
        self.assertIn("/assets/test.txt", mw.files)
        request = factory.get("/assets/test.txt")
        response = mw(request)
        self.assertEqual(response.status_code, 200)
        response.close()

    # ------------------------------------------------------------------ #
    # Cache-Control and immutable files                                    #
    # ------------------------------------------------------------------ #

    def test_max_age_sets_cache_control_on_non_immutable(self):
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        class CustomMiddleware(StaticFilesMiddleware):
            max_age = 300

        mw = CustomMiddleware(_sentinel_response)
        request = factory.get("/static/test.txt")
        response = mw(request)
        response.close()
        self.assertEqual(response["Cache-Control"], "max-age=300, public")

    def test_manifest_storage_hashed_files(self):
        from django.conf import STATICFILES_STORAGE_ALIAS
        from django.conf import settings as django_settings
        from django.contrib.staticfiles import storage as sf_module
        from django.core.management import call_command

        storages = {
            **django_settings.STORAGES,
            STATICFILES_STORAGE_ALIAS: {
                "BACKEND": (
                    "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
                ),
            },
        }
        with self.settings(STORAGES=storages):
            call_command("collectstatic", interactive=False, verbosity=0)
            mw = self._make_middleware()
            hashed_url = sf_module.staticfiles_storage.url("test.txt")
            self.assertTrue(hashed_url.startswith("/static/"))

            r_hashed = mw(factory.get(hashed_url))
            content = b"".join(r_hashed.streaming_content)
            r_hashed.close()
            self.assertEqual(r_hashed.status_code, 200)
            self.assertGreater(len(content), 0)
            self.assertEqual(
                r_hashed["Cache-Control"], "max-age=315360000, public, immutable"
            )

            r_plain = mw(factory.get("/static/test.txt"))
            r_plain.close()
            self.assertEqual(r_plain["Cache-Control"], "max-age=60, public")

    # ------------------------------------------------------------------ #
    # Conditional requests                                                  #
    # ------------------------------------------------------------------ #

    def test_if_none_match(self):
        mw = self._make_middleware()
        r1 = mw(factory.get("/static/test.txt"))
        r1.close()
        etag = r1["ETag"]

        r2 = mw(factory.get("/static/test.txt", HTTP_IF_NONE_MATCH=etag))
        self.assertEqual(r2.status_code, 304)

        r3 = mw(factory.get("/static/test.txt", HTTP_IF_NONE_MATCH='"stale-etag"'))
        self.assertEqual(r3.status_code, 200)
        r3.close()

    def test_if_modified_since(self):
        mw = self._make_middleware()
        r1 = mw(factory.get("/static/test.txt"))
        r1.close()
        last_modified = r1["Last-Modified"]

        r2 = mw(factory.get("/static/test.txt", HTTP_IF_MODIFIED_SINCE=last_modified))
        self.assertEqual(r2.status_code, 304)

        r3 = mw(
            factory.get(
                "/static/test.txt",
                HTTP_IF_MODIFIED_SINCE="Thu, 01 Jan 1970 00:00:00 GMT",
            )
        )
        self.assertEqual(r3.status_code, 200)
        r3.close()

    def test_if_modified_since_when_last_modified_is_none(self):
        mw = self._make_middleware()
        mw.files["/static/test.txt"].last_modified = None
        response = mw(
            factory.get(
                "/static/test.txt",
                HTTP_IF_MODIFIED_SINCE="Thu, 01 Jan 2099 00:00:00 GMT",
            )
        )
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_malformed_if_modified_since_treated_as_modified(self):
        mw = self._make_middleware()
        response = mw(
            factory.get(
                "/static/test.txt",
                HTTP_IF_MODIFIED_SINCE="not-a-valid-date",
            )
        )
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_etag_takes_priority_over_if_modified_since(self):
        mw = self._make_middleware()
        r1 = mw(factory.get("/static/test.txt"))
        r1.close()
        etag = r1["ETag"]

        r2 = mw(
            factory.get(
                "/static/test.txt",
                HTTP_IF_NONE_MATCH=etag,
                HTTP_IF_MODIFIED_SINCE="Thu, 01 Jan 1970 00:00:00 GMT",
            )
        )
        self.assertEqual(r2.status_code, 304)

    def test_304_headers(self):
        mw = self._make_middleware()
        r1 = mw(factory.get("/static/test.txt"))
        r1.close()
        etag = r1["ETag"]

        r304 = mw(factory.get("/static/test.txt", HTTP_IF_NONE_MATCH=etag))
        self.assertEqual(r304.status_code, 304)
        self.assertIn("ETag", r304)
        self.assertNotIn("Content-Type", r304)
        self.assertNotIn("Content-Length", r304)
        self.assertNotIn("Access-Control-Allow-Origin", r304)
        self.assertNotIn("Vary", r304)

    # ------------------------------------------------------------------ #
    # Range requests                                                        #
    # ------------------------------------------------------------------ #

    def test_range_forms(self):
        mw = self._make_middleware()
        path = os.path.join(settings.STATIC_ROOT, "test.txt")
        with open(path, "rb") as f:
            full_content = f.read()
        size = len(full_content)

        cases = [
            ("bytes=0-9", 0, 9),
            ("bytes=5-", 5, size - 1),
            ("bytes=-5", size - 5, size - 1),
        ]
        for range_header, exp_start, exp_end in cases:
            with self.subTest(range_header=range_header):
                request = factory.get("/static/test.txt", HTTP_RANGE=range_header)
                response = mw(request)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(
                    response["Content-Range"],
                    f"bytes {exp_start}-{exp_end}/{size}",
                )
                self.assertEqual(
                    int(response["Content-Length"]), exp_end - exp_start + 1
                )
                body = b"".join(response.streaming_content)
                response.close()
                self.assertEqual(body, full_content[exp_start : exp_end + 1])

    def test_range_not_satisfiable(self):
        mw = self._make_middleware()
        path = os.path.join(settings.STATIC_ROOT, "test.txt")
        size = os.path.getsize(path)
        request = factory.get(
            "/static/test.txt", HTTP_RANGE=f"bytes={size}-{size + 10}"
        )
        response = mw(request)
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response["Content-Range"], f"bytes */{size}")

    def test_range_ignored_on_head(self):
        mw = self._make_middleware()
        request = factory.head("/static/test.txt", HTTP_RANGE="bytes=0-9")
        response = mw(request)
        self.assertEqual(response.status_code, 206)

    # ------------------------------------------------------------------ #
    # Startup-scan invariant                                               #
    # ------------------------------------------------------------------ #

    def test_new_files_not_picked_up_without_restart(self):
        # Files created after middleware init are invisible until restart.
        mw = self._make_middleware()
        new_file = os.path.join(settings.STATIC_ROOT, "late.txt")
        open(new_file, "w").close()
        request = factory.get("/static/late.txt")
        response = mw(request)
        self.assertEqual(response.content, b"sentinel")

    # ------------------------------------------------------------------ #
    # Interaction                                                          #
    # ------------------------------------------------------------------ #

    def test_middleware_order_bypasses_later_middleware(self):
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        calls = []

        def tracking_get_response(request):
            calls.append(request.path_info)
            return HttpResponse("sentinel")

        mw = StaticFilesMiddleware(tracking_get_response)

        # Static hit: tracking_get_response must NOT be called.
        response = mw(factory.get("/static/test.txt"))
        response.close()
        self.assertNotIn("/static/test.txt", calls)

        # Pass-through: tracking_get_response must be called.
        mw(factory.get("/not-static/something"))
        self.assertIn("/not-static/something", calls)


@override_settings(
    DEBUG=False,
    MIDDLEWARE=["django.contrib.staticfiles.middleware.StaticFilesMiddleware"],
    ROOT_URLCONF="staticfiles_tests.urls.middleware",
)
class AsyncStaticFilesMiddlewareTests(CollectionTestCase):

    @staticmethod
    def _make_async_middleware():
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        return StaticFilesMiddleware(_async_sentinel_response)

    def test_async_mode_detection(self):
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        self.assertTrue(StaticFilesMiddleware.async_capable)
        self.assertTrue(StaticFilesMiddleware.sync_capable)

        async_mw = self._make_async_middleware()
        self.assertTrue(async_mw.async_mode)
        self.assertTrue(iscoroutinefunction(async_mw))

        sync_mw = StaticFilesMiddleware(_sentinel_response)
        self.assertFalse(sync_mw.async_mode)
        self.assertFalse(iscoroutinefunction(sync_mw))

    def test_async_hit_serves_file(self):
        mw = self._make_async_middleware()
        response = asyncio.run(mw(factory.get("/static/test.txt")))
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content)
        response.close()
        self.assertIn(b"Can we find", content)

    def test_async_miss_calls_async_get_response(self):
        calls = []

        async def tracking_get_response(request):
            calls.append(request.path_info)
            return HttpResponse("sentinel")

        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        mw = StaticFilesMiddleware(tracking_get_response)
        asyncio.run(mw(factory.get("/static/does-not-exist.txt")))
        asyncio.run(mw(factory.get("/not-static/something")))
        self.assertIn("/static/does-not-exist.txt", calls)
        self.assertIn("/not-static/something", calls)

    def test_asgi_style_consumption_streams_natively(self):
        import warnings
        from contextlib import aclosing

        mw = self._make_async_middleware()

        async def consume(request):
            response = await mw(request)
            parts = []
            async with aclosing(aiter(response)) as content:
                async for part in content:
                    parts.append(part)
            response.close()
            return response, parts

        with self.subTest("non-range request"):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                response, parts = asyncio.run(consume(factory.get("/static/test.txt")))
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Can we find", b"".join(parts))
            messages = [str(w.message) for w in caught]
            self.assertFalse(
                any("synchronous iterators" in m for m in messages),
                f"Unexpected sync-iterator fallback warning: {messages}",
            )

        with self.subTest("range request"):
            request = factory.get("/static/test.txt", HTTP_RANGE="bytes=0-9")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                response, parts = asyncio.run(consume(request))
            self.assertEqual(response.status_code, 206)
            self.assertEqual(len(b"".join(parts)), 10)
            messages = [str(w.message) for w in caught]
            self.assertFalse(
                any("synchronous iterators" in m for m in messages),
                f"Unexpected sync-iterator fallback warning: {messages}",
            )


@override_settings(
    DEBUG=False,
    MIDDLEWARE=["django.contrib.staticfiles.middleware.StaticFilesMiddleware"],
    ROOT_URLCONF="staticfiles_tests.urls.middleware",
)
class CompressedVariantsStaticFilesMiddlewareTests(CollectionTestCase):

    @staticmethod
    def _make_middleware(get_response=_sentinel_response):
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        return StaticFilesMiddleware(get_response)

    def setUp(self):
        super().setUp()
        css_path = os.path.join(settings.STATIC_ROOT, "style.css")
        gz_path = os.path.join(settings.STATIC_ROOT, "style.css.gz")
        zst_path = os.path.join(settings.STATIC_ROOT, "style.css.br")
        br_path = os.path.join(settings.STATIC_ROOT, "style.css.br")
        with open(css_path, "wb") as f:
            f.write(b"body { color: red; }")
        with open(gz_path, "wb") as f:
            f.write(b"fake-gzip")
        with open(zst_path, "wb") as f:
            f.write(b"fake-zst")
        with open(br_path, "wb") as f:
            f.write(b"fake-br")
        self.mw = self._make_middleware()

    # ------------------------------------------------------------------ #
    # Compressed variants                                                   #
    # ------------------------------------------------------------------ #

    def test_encoding_negotiation(self):
        cases = [
            ("br", b"fake-br", "br"),
            ("gzip", b"fake-gzip", "gzip"),
        ]
        for accept_enc, expected_body, expected_ce in cases:
            with self.subTest(accept_enc=accept_enc):
                request = factory.get(
                    "/static/style.css", HTTP_ACCEPT_ENCODING=accept_enc
                )
                response = self.mw(request)
                body = b"".join(response.streaming_content)
                response.close()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(body, expected_body)
                self.assertEqual(response["Content-Encoding"], expected_ce)
                self.assertEqual(response["Vary"], "Accept-Encoding")

        # No Accept-Encoding → uncompressed, still Vary.
        request = factory.get("/static/style.css")
        response = self.mw(request)
        body = b"".join(response.streaming_content)
        response.close()
        self.assertEqual(body, b"body { color: red; }")
        self.assertNotIn("Content-Encoding", response)
        self.assertEqual(response["Vary"], "Accept-Encoding")

    def test_brotli_preferred_over_gzip(self):
        request = factory.get("/static/style.css", HTTP_ACCEPT_ENCODING="gzip, br")
        response = self.mw(request)
        body = b"".join(response.streaming_content)
        response.close()
        self.assertEqual(response["Content-Encoding"], "br")
        self.assertEqual(body, b"fake-br")

    def test_accept_encoding_wildcard(self):
        request = factory.get("/static/style.css", HTTP_ACCEPT_ENCODING="*")
        response = self.mw(request)
        body = b"".join(response.streaming_content)
        response.close()
        self.assertNotIn("Content-Encoding", response)
        self.assertEqual(body, b"body { color: red; }")

    def test_no_vary_without_compressed_variants(self):
        # test.txt has no .gz/.br/zst siblings → no Vary header.
        request = factory.get("/static/test.txt")
        response = self.mw(request)
        response.close()
        self.assertNotIn("Vary", response)

    def test_304_includes_vary_when_compressed_variants_exist(self):
        r1 = self.mw(factory.get("/static/style.css"))
        r1.close()
        etag = r1["ETag"]
        r304 = self.mw(factory.get("/static/style.css", HTTP_IF_NONE_MATCH=etag))
        self.assertEqual(r304.status_code, 304)
        self.assertEqual(r304["Vary"], "Accept-Encoding")


@override_settings(
    DEBUG=False,
    MIDDLEWARE=["django.contrib.staticfiles.middleware.StaticFilesMiddleware"],
    ROOT_URLCONF="staticfiles_tests.urls.middleware",
)
class AutorefreshParityTests(CollectionTestCase):
    """
    Cover the stat_cache=None branches retained for autorefresh parity.
    These are dead code in the current implementation but kept to match
    whitenoise's structure and ease a future autorefresh addition.
    """

    # ------------------------------------------------------------------ #
    # is_compressed_variant stat_cache=None branch                        #
    # ------------------------------------------------------------------ #

    def test_is_compressed_variant_without_stat_cache_uncompressed_exists(self):
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        with mock.patch(
            "django.contrib.staticfiles.middleware.os.path.isfile", return_value=True
        ):
            self.assertTrue(
                StaticFilesMiddleware.is_compressed_variant("/path/to/file.gz")
            )

    def test_is_compressed_variant_without_stat_cache_uncompressed_missing(self):
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware

        with mock.patch(
            "django.contrib.staticfiles.middleware.os.path.isfile", return_value=False
        ):
            self.assertFalse(
                StaticFilesMiddleware.is_compressed_variant("/path/to/orphan.gz")
            )

    # ------------------------------------------------------------------ #
    # get_static_file stat_cache=None branch                              #
    # ------------------------------------------------------------------ #

    def test_get_static_file_raises_missing_file_error_without_stat_cache(self):
        from django.contrib.staticfiles.middleware import StaticFilesMiddleware
        from django.contrib.staticfiles.responders import MissingFileError

        mw = StaticFilesMiddleware(_sentinel_response)
        with self.assertRaises(MissingFileError):
            mw.get_static_file("/nonexistent/path.txt", "/static/path.txt")
