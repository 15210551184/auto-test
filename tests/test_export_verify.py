import sys
import tempfile
import types
import unittest
from pathlib import Path

if "playwright.sync_api" not in sys.modules:
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Page = object
    sync_api.Locator = object
    sync_api.TimeoutError = type('TimeoutError', (Exception,), {})
    sync_api.sync_playwright = object()
    playwright.sync_api = sync_api
    sys.modules["playwright"] = playwright
    sys.modules["playwright.sync_api"] = sync_api

from autotest.engine import export_verify as EV
from autotest.engine.actions import AssertionFailed


class FakeUI:
    def __init__(self, headers, total=None):
        self._headers = headers
        self._total = total

    def headers(self, page):
        return self._headers

    def total_count(self, page):
        return self._total


class FakeCtx:
    def __init__(self, report_root, ui_headers, total=None, export_mode="direct",
                 header_variants=None):
        self.ui = FakeUI(ui_headers, total)
        self.page = None
        self.report_root = report_root
        self.config = types.SimpleNamespace(
            export_mode=export_mode,
            header_variants=header_variants or {},
            cases=[],
        )
        self.data = {}
        self.shot_calls = []
        self.preview_calls = []
        self.api_log = []
        self.phases = []

    def set_phase(self, phase):
        self.phases.append(phase)

    def shot(self, tag):
        self.shot_calls.append(tag)
        return f"screenshots/{tag}.png"

    def table_preview_shot(self, rows, tag, title, source_name="", max_rows=20):
        self.preview_calls.append({
            "rows": rows, "tag": tag, "title": title,
            "source_name": source_name, "max_rows": max_rows,
        })
        return f"screenshots/{tag}.png"


class ExportDetailAttachmentTests(unittest.TestCase):
    """
    导出验证失败时，报告里光一句"缺少列 X/Y"判断不了是导出真漏了还是
    工具自己表头识别多了——这里验证 verify_export() 把导出文件本身（可
    下载链接）和当时的页面截图都挂到 AssertionFailed.detail 上，
    run_step() 才能把它们搬进 StepResult 供 report.py 渲染。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake_path = Path(self.tmp.name) / "city_1785312571891.xlsx"
        self.fake_path.write_bytes(b"x" * 4096)
        self._orig_download_direct = EV._download_direct
        self._orig_read_table = EV._read_table
        EV._download_direct = lambda ctx, timeout: str(self.fake_path)

    def tearDown(self):
        EV._download_direct = self._orig_download_direct
        EV._read_table = self._orig_read_table
        self.tmp.cleanup()

    def test_failure_attaches_download_link(self):
        EV._read_table = lambda path: [{"国家": "中国"}]   # 缺"状态"这一列
        ctx = FakeCtx(self.tmp.name, ui_headers=["国家", "状态"])

        with self.assertRaises(AssertionFailed) as cm:
            EV.verify_export(ctx)

        detail = cm.exception.detail
        self.assertEqual("导出文件 city_1785312571891.xlsx", detail["download"]["label"])
        self.assertEqual("city_1785312571891.xlsx", detail["download"]["path"])
        self.assertEqual(
            ["列表页（完整列）", "导出文件内容"],
            [image["label"] for image in detail["images"]],
        )
        self.assertEqual(["export_list_page"], ctx.shot_calls)
        self.assertEqual("export_file_content", ctx.preview_calls[0]["tag"])

    def test_download_path_relative_to_report_root(self):
        # 报告 html 跟下载文件不在同一目录时（批量执行常见），链接要相对
        # report_root 算，不是相对下载文件自己所在目录。
        report_root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(report_root, ignore_errors=True))
        EV._read_table = lambda path: [{"国家": "中国"}]
        ctx = FakeCtx(report_root, ui_headers=["国家", "状态"])

        with self.assertRaises(AssertionFailed) as cm:
            EV.verify_export(ctx)

        rel = cm.exception.detail["download"]["path"]
        self.assertFalse(Path(rel).is_absolute())
        self.assertTrue((Path(report_root) / rel).resolve() == self.fake_path.resolve())

    def test_success_also_attaches_download_link(self):
        # 通过的导出也该能下载下来看一眼，不是只有失败才留证据。
        EV._read_table = lambda path: [{"国家": "中国", "状态": "启用"}]
        ctx = FakeCtx(self.tmp.name, ui_headers=["国家", "状态"], total=1)

        msg, detail = EV.verify_export(ctx)

        self.assertIn("导出验证通过", msg)
        self.assertEqual("city_1785312571891.xlsx", detail["download"]["path"])
        self.assertEqual(2, len(detail["images"]))
        self.assertEqual("列表页（完整列）", detail["images"][0]["label"])
        self.assertEqual("导出文件内容", detail["images"][1]["label"])
        self.assertEqual(["export_list_page"], ctx.shot_calls)

    def test_success_attaches_the_synchronous_export_api(self):
        EV._read_table = lambda path: [{"国家": "中国"}]
        ctx = FakeCtx(self.tmp.name, ui_headers=["国家"], total=1)

        def downloaded(current_ctx, timeout):
            current_ctx._last_export_api = {
                "method": "POST",
                "url": "http://example.test/api/country/export",
                "status": 200,
                "duration_ms": 321,
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
            return str(self.fake_path)

        EV._download_direct = downloaded
        _, detail = EV.verify_export(ctx)

        self.assertEqual("POST", detail["export_api"]["method"])
        self.assertEqual("http://example.test/api/country/export",
                         detail["export_api"]["url"])
        self.assertEqual(200, detail["export_api"]["status"])

    def test_translated_headers_are_compared_by_canonical_name(self):
        columns = ["国家编码", "国家名称", "状态"]
        EV._read_table = lambda path: [{
            "Code pays": "SN",
            "Nom du pays": "Le Sénégal",
            "Statut": "Actif",
        }]
        ctx = FakeCtx(
            self.tmp.name,
            ui_headers=["Code pays", "Nom du pays", "Statut"],
            total=1,
            header_variants={
                "国家编码": {"fr": "Code pays"},
                "国家名称": {"fr": "Nom du pays"},
                "状态": {"fr": "Statut"},
            },
        )
        ctx.data["page_data"] = [{
            "国家编码": "SN",
            "国家名称": "Le Sénégal",
            "状态": "Actif",
        }]

        msg, _ = EV.verify_export(
            ctx,
            compare_with="page_data",
            columns=columns,
        )

        self.assertIn("抽样比对 3 个字段一致", msg)

    def test_old_config_without_variants_uses_runtime_headers_safely(self):
        columns = ["国家编码", "国家名称", "状态"]
        EV._read_table = lambda path: [{
            "Code du pays": "SN",
            "Nom du pays": "Le Sénégal",
            "Statut": "Actif",
        }]
        ctx = FakeCtx(
            self.tmp.name,
            ui_headers=["Code pays", "Nom pays", "Statut"],
            total=1,
        )
        ctx.config.cases = [types.SimpleNamespace(steps=[
            types.SimpleNamespace(
                action="assert_headers",
                params={"contains": columns},
            ),
        ])]
        ctx.data["page_data"] = [{
            "国家编码": "SN",
            "国家名称": "Le Sénégal",
            "状态": "Actif",
        }]

        msg, _ = EV.verify_export(
            ctx,
            compare_with="page_data",
            columns=columns,
        )

        self.assertIn("抽样比对 3 个字段一致", msg)

    def test_different_translated_headers_are_inferred_from_column_values(self):
        columns = ["区号", "是否包车"]
        EV._read_table = lambda path: [
            {"Calling Code": "+221", "Charter Service": "开启"},
            {"Calling Code": "+86", "Charter Service": "开启"},
        ]
        ctx = FakeCtx(
            self.tmp.name,
            ui_headers=["Area Code", "是否包车"],
            total=2,
        )
        ctx.config.cases = [types.SimpleNamespace(steps=[
            types.SimpleNamespace(
                action="assert_headers",
                params={"contains": columns},
            ),
        ])]
        ctx.data["page_data"] = [
            {"区号": "+221", "是否包车": "开启"},
            {"区号": "+86", "是否包车": "开启"},
        ]

        msg, _ = EV.verify_export(
            ctx, compare_with="page_data", columns=columns)

        self.assertIn("抽样比对 4 个字段一致", msg)

    def test_value_mapping_refuses_ambiguous_columns(self):
        mapping = EV._augment_header_map_by_values(
            [{"Unknown": "相同"}],
            [{"字段A": "相同", "字段B": "相同"}],
            ["字段A", "字段B"],
            {},
        )
        self.assertEqual({}, mapping)

    def test_runtime_mapping_refuses_different_column_counts(self):
        ctx = FakeCtx(self.tmp.name, ui_headers=["Code pays", "Nom pays"])
        ctx.config.cases = [types.SimpleNamespace(steps=[
            types.SimpleNamespace(
                action="assert_headers",
                params={"contains": ["国家编码", "国家名称", "状态"]},
            ),
        ])]
        self.assertEqual({}, EV._runtime_header_map(ctx, {}))

    def test_configured_columns_compare_values_beyond_first_five(self):
        # 页面第 7 个可比字段“加盟商”显示名称，而导出错误地写了内部 ID。
        columns = ["国家", "城市", "司机姓名", "司机手机号", "车牌号", "VIN码", "加盟商"]
        ui_row = {col: f"页面-{col}" for col in columns}
        xl_row = dict(ui_row)
        xl_row["加盟商"] = "869508055602171904"
        EV._read_table = lambda path: [xl_row]
        ctx = FakeCtx(
            self.tmp.name,
            ui_headers=["序号", "司机头像", *columns, "操作"],
            total=1,
        )
        ctx.data["page_data"] = [ui_row]

        with self.assertRaises(AssertionFailed) as cm:
            EV.verify_export(
                ctx,
                compare_with="page_data",
                columns=columns,
            )

        message = str(cm.exception)
        self.assertIn("字段不一致 1/7", message)
        self.assertIn("加盟商", message)
        self.assertIn("869508055602171904", message)
        # 头像是纯展示列，不该遮住真正的字段值错误。
        self.assertNotIn("司机头像", message)

    def test_missing_configured_column_cannot_be_silently_skipped(self):
        EV._read_table = lambda path: [{"国家": "中国"}]
        ctx = FakeCtx(self.tmp.name, ui_headers=["国家", "加盟商"], total=1)
        ctx.data["page_data"] = [{"国家": "中国", "加盟商": "北京加盟商"}]

        with self.assertRaises(AssertionFailed) as cm:
            EV.verify_export(
                ctx,
                compare_with="page_data",
                columns=["国家", "加盟商"],
            )

        message = str(cm.exception)
        self.assertIn("导出缺少页面上的列: ['加盟商']", message)
        self.assertIn("以下配置列未实际参与数据比对: ['加盟商']", message)

    def test_auto_failure_reports_real_wait_and_export_api(self):
        ctx = FakeCtx(self.tmp.name, ui_headers=["国家"], export_mode="auto")

        def fail_after_export_request(current_ctx, timeout):
            current_ctx.api_log.append({
                "url": "http://example.test/api/export/country",
                "status": 200,
            })
            return None

        EV._download_direct = fail_after_export_request

        with self.assertRaises(AssertionFailed) as cm:
            EV.verify_export(ctx)

        message = str(cm.exception)
        self.assertIn("实际等待约", message)
        self.assertNotIn("90000ms 内没拿到文件", message)
        self.assertIn("/api/export/country", message)
        self.assertIn("确认弹窗和文件响应", message)

    def test_auto_export_shares_one_total_timeout_budget(self):
        ctx = FakeCtx(self.tmp.name, ui_headers=["国家"], export_mode="auto")
        ctx.config.export_task_api = "/api/export/tasks"
        calls = []
        clock = {"now": 100.0}
        original_monotonic = EV.time.monotonic

        def direct(current_ctx, timeout):
            calls.append(("direct", timeout))
            clock["now"] += 20
            return None

        def async_download(current_ctx, timeout):
            calls.append(("async", timeout))
            return None

        EV._download_direct = direct
        original_async = EV._download_async
        EV._download_async = async_download
        EV.time.monotonic = lambda: clock["now"]
        try:
            with self.assertRaises(AssertionFailed):
                EV.verify_export(ctx, timeout=90000)
        finally:
            EV._download_async = original_async
            EV.time.monotonic = original_monotonic

        self.assertEqual(("direct", 20000), calls[0])
        self.assertEqual("async", calls[1][0])
        self.assertLessEqual(calls[1][1], 70000)
        self.assertGreater(calls[1][1], 69000)

    def test_default_export_timeout_is_forty_five_seconds(self):
        ctx = FakeCtx(self.tmp.name, ui_headers=["国家"], export_mode="direct")
        calls = []
        EV._download_direct = lambda current_ctx, timeout: calls.append(timeout)

        with self.assertRaises(AssertionFailed):
            EV.verify_export(ctx)

        self.assertEqual([45000], calls)


class ExportResponseDetectionTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, url, headers):
            self.url = url
            self.headers = headers

    def test_export_button_fallback_covers_supported_languages(self):
        from autotest.engine.i18n_terms import button_selector
        selector = button_selector("export")
        for label in ("导出", "Export", "Exporter", "تصدير"):
            self.assertIn(f":has-text('{label}')", selector)

    def test_content_disposition_utf8_filename(self):
        response = self.FakeResponse(
            "http://example.test/api/export",
            {
                "content-type": "application/octet-stream",
                "content-disposition":
                    "attachment; filename*=UTF-8''%E5%9B%BD%E5%AE%B6%E7%AE%A1%E7%90%86.xlsx",
            },
        )
        self.assertEqual("国家管理.xlsx", EV._export_response_name(response))

    def test_blob_response_without_filename_gets_spreadsheet_extension(self):
        response = self.FakeResponse(
            "http://example.test/api/export",
            {
                "content-type":
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
        )
        self.assertEqual("export.xlsx", EV._export_response_name(response))

    def test_json_response_is_not_mistaken_for_export_file(self):
        response = self.FakeResponse(
            "http://example.test/api/export",
            {"content-type": "application/json"},
        )
        self.assertIsNone(EV._export_response_name(response))


if __name__ == "__main__":
    unittest.main()
