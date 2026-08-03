import unittest
from pathlib import Path


INDEX_HTML = (
    Path(__file__).resolve().parents[1] / "autotest" / "web" / "index.html"
).read_text(encoding="utf-8")


class RightColumnLayoutTests(unittest.TestCase):
    def test_operation_and_report_cards_have_layout_roles(self):
        self.assertIn('<div class="card ops-card">', INDEX_HTML)
        self.assertIn('<div class="card reports-card">', INDEX_HTML)

    def test_operation_card_cannot_squeeze_reports_out_of_view(self):
        self.assertIn(".col-right>.ops-card{", INDEX_HTML)
        self.assertIn("max-height:52%", INDEX_HTML)
        self.assertIn(".col-right>.reports-card{", INDEX_HTML)
        self.assertIn("min-height:220px", INDEX_HTML)


class DefaultExecutionTagsTests(unittest.TestCase):
    def test_safe_read_only_categories_are_checked_by_default(self):
        for tag in ("smoke", "health", "search", "list", "export"):
            self.assertIn(
                f'data-tagf value="{tag}" checked',
                INDEX_HTML,
            )

    def test_mutating_and_language_categories_are_opt_in(self):
        self.assertIn('data-tagf value="crud">', INDEX_HTML)
        self.assertIn('data-tagf value="i18n">', INDEX_HTML)
        self.assertNotIn('data-tagf value="crud" checked', INDEX_HTML)
        self.assertNotIn('data-tagf value="i18n" checked', INDEX_HTML)

    def test_execution_presets_are_explicit_and_clear_is_removed(self):
        for button_id in ("btnTagNormal", "btnTagAll", "btnTagSmoke",
                          "btnTagExport", "btnTagExportI18n"):
            self.assertIn(f'id="{button_id}"', INDEX_HTML)
        self.assertNotIn('id="btnTagNone"', INDEX_HTML)
        self.assertNotIn("都不选 = 全部", INDEX_HTML)

    def test_zero_categories_are_rejected_before_execution(self):
        self.assertIn("!selectedTags().length", INDEX_HTML)
        self.assertIn("请至少选择一个用例类别", INDEX_HTML)
        self.assertIn("至少保留一个用例类别", INDEX_HTML)

    def test_categories_have_short_explanations_and_risk_marker(self):
        self.assertIn("加载、表头、基础错误", INDEX_HTML)
        self.assertIn("文件、表头及数据比对", INDEX_HTML)
        self.assertIn('class="exec-tag risk"', INDEX_HTML)
        self.assertIn("会创建并清理测试数据", INDEX_HTML)

    def test_all_languages_and_export_presets_have_distinct_payloads(self):
        self.assertIn('value="__all__">全部配置语言（逐一执行）', INDEX_HTML)
        self.assertIn("$('#btnTagExport').onclick=()=>setSelectedTags(['export'])", INDEX_HTML)
        self.assertIn("$('#btnTagExportI18n').onclick=()=>{", INDEX_HTML)
        self.assertIn("body.all_languages=true", INDEX_HTML)


class RuntimeTaskStatusTests(unittest.TestCase):
    def test_runtime_log_has_status_summary_and_task_list(self):
        self.assertIn('id="taskStatus"', INDEX_HTML)
        self.assertIn('id="taskCounts"', INDEX_HTML)
        self.assertIn('id="taskList"', INDEX_HTML)

    def test_status_filters_cover_waiting_running_and_completed(self):
        for key in ("completed", "running", "waiting", "passed", "failed"):
            self.assertIn(f"['{key}'", INDEX_HTML)

    def test_clicking_task_switches_to_its_log(self):
        self.assertIn("btn.onclick=()=>switchLogTab(btn.dataset.taskName)", INDEX_HTML)

    def test_timestamped_logs_still_map_to_task(self):
        self.assertIn("function logPayload(line)", INDEX_HTML)
        self.assertIn("logPayload(line)", INDEX_HTML)

    def test_sse_backlog_is_not_rendered_twice(self):
        self.assertIn("let replay=logLines.__all__.slice(-300)", INDEX_HTML)
        self.assertIn("if(l===replay[0]){replay.shift();return;}", INDEX_HTML)

    def test_stopped_tasks_have_filter_and_force_stop_action(self):
        self.assertIn("['stopped','已停止']", INDEX_HTML)
        self.assertIn("$('#btnStop').textContent='强制终止'", INDEX_HTML)
        self.assertIn("停止中 · 正在生成部分报告", INDEX_HTML)


class GlobalListApiRedetectTests(unittest.TestCase):
    def test_operation_panel_has_global_redetect_button(self):
        self.assertIn('id="btnRedetectAll"', INDEX_HTML)
        self.assertIn('>全局重探接口</button>', INDEX_HTML)

    def test_global_button_starts_project_wide_job(self):
        self.assertIn("trigger('redetect-all-list-apis')", INDEX_HTML)
        self.assertIn("redetect:'重探接口'", INDEX_HTML)

    def test_legacy_log_tab_row_is_removed(self):
        self.assertNotIn('id="termTabs"', INDEX_HTML)
        self.assertNotIn('class="term-tabs"', INDEX_HTML)

    def test_total_tasks_returns_to_complete_log(self):
        self.assertIn("if(activeTaskFilter==='all')switchLogTab('__all__')", INDEX_HTML)


class ReportDownloadTests(unittest.TestCase):
    def test_history_report_has_download_link(self):
        self.assertIn("/api/reports/'+encodeURIComponent(r.dir)+'/download", INDEX_HTML)
        self.assertIn('>完整报告</a>', INDEX_HTML)

    def test_history_report_has_abnormal_api_download_link(self):
        self.assertIn("/api/reports/'+encodeURIComponent(r.dir)+'/download-errors", INDEX_HTML)
        self.assertIn('>异常接口</a>', INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
