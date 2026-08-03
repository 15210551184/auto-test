import csv
import io
import unittest

from autotest.engine.abnormal_api import (
    abnormal_description,
    collect_abnormal_apis,
    collect_failed_case_apis,
    render_abnormal_api_csv,
    render_failed_case_api_csv,
)


class AbnormalApiTests(unittest.TestCase):
    def test_detects_http_and_business_errors(self):
        self.assertIn("HTTP 500", abnormal_description({
            "status": 500,
            "response_body": '{"message":"系统异常"}',
        }))
        self.assertEqual(
            "业务返回 code=401；登录已失效",
            abnormal_description({
                "status": 200,
                "response_body": '{"code":401,"msg":"登录已失效"}',
            }),
        )
        self.assertEqual("", abnormal_description({
            "status": 200,
            "response_body": '{"code":200,"msg":"ok"}',
        }))

    def test_collects_only_abnormal_calls_with_case_description(self):
        results = [{
            "name": "国家管理",
            "cases": [{
                "name": "列表默认加载",
                "status": "fail",
                "error": "",
                "steps": [{"status": "fail", "message": "发现失败请求"}],
                "api_calls": [
                    {"method": "GET", "url": "/api/good", "status": 200,
                     "response_body": '{"code":200}'},
                    {"method": "GET", "url": "/api/bad", "status": 503,
                     "response_body": '{"message":"服务不可用"}'},
                ],
            }],
        }]
        rows = collect_abnormal_apis(results)
        self.assertEqual(1, len(rows))
        self.assertEqual("/api/bad", rows[0]["接口地址"])
        self.assertEqual("发现失败请求", rows[0]["用例异常描述"])
        self.assertIn("服务不可用", rows[0]["接口异常描述"])

    def test_csv_has_utf8_bom_and_description_columns(self):
        content = render_abnormal_api_csv([])
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertIn("接口异常描述", rows[0])
        self.assertIn("用例异常描述", rows[0])

    def test_failed_export_includes_related_successful_api(self):
        results = [{
            "name": "充电加盟商管理",
            "cases": [{
                "name": "导出数据验证",
                "status": "fail",
                "steps": [{
                    "action": "export_and_verify",
                    "status": "fail",
                    "message": "导出缺少页面上的列: ['国家名称']",
                }],
                "api_calls": [{
                    "method": "POST",
                    "url": "/api/vaWeb/business/franchisee/list",
                    "status": 200,
                    "response_body": '{"code":200}',
                }],
            }],
        }]
        rows = collect_failed_case_apis(results)
        self.assertEqual([{
            "接口地址": "/api/vaWeb/business/franchisee/list",
            "错误描述": "导出缺少页面上的列: ['国家名称']",
        }], rows)

        csv_rows = list(csv.DictReader(io.StringIO(
            render_failed_case_api_csv(results).decode("utf-8-sig"))))
        self.assertEqual(rows, csv_rows)
        self.assertEqual(["接口地址", "错误描述"], list(csv_rows[0]))


if __name__ == "__main__":
    unittest.main()
