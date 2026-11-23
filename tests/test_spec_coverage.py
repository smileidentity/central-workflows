"""Tests for scripts/spec_sync/spec_coverage.py. Run: python3 -m unittest discover -s tests"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "spec_sync"))

import spec_coverage as c

SYNC_HEADERS = {
    "SmileID-Job-ID": {"schema": {"type": "string"}},
    "Response-Signature": {"schema": {"type": "string"}},
    "X-Internal-Trace": {"schema": {"type": "string"}, "x-smile-sdk-exclude": True},
}
SPEC = {
    "paths": {
        "/v3/basic_kyc/sync": {"post": {
            "operationId": "submitBasicKycSync",
            "responses": {"200": {"headers": SYNC_HEADERS},
                          "408": {"headers": {"smileid-job-id": {"schema": {"type": "string"}}}},
                          "400": {"description": "Bad request"}}}},
        "/v3/phone": {"post": {"operationId": "v3PhoneVerification", "responses": {"200": {}}}},
        "/v3/internal": {"post": {"operationId": "internal", "x-internal": True}},
        "/v3/hidden": {"x-smile-sdk-exclude": True, "get": {"operationId": "hiddenPath"}},
        "/v3/status/{jobId}": {"parameters": [], "get": {"operationId": "getVerificationStatus"}},
    },
}


def manifest(**changes):
    m = {
        "spec_tag": "v3.1215.0",
        "operations": {
            "submitBasicKycSync": {
                "method": "basic_kyc.verify_sync",
                "path": "POST /v3/basic_kyc/sync",
                "response_headers": {
                    "SmileID-Job-ID": "BasicKycResult.job_id, SmileIDError.job_id",
                    "Response-Signature": {"unsupported": "Not exposed yet."},
                },
            },
            "v3PhoneVerification": {"unsupported": "Planned for 13.3.0."},
            "getVerificationStatus": {"method": "verifications.retrieve", "path": "GET /v3/status/{jobId}"},
        },
    }
    m.update(changes)
    return m


class SpecOperationsTest(unittest.TestCase):
    def test_lists_public_operations_and_their_headers(self):
        ops = c.spec_operations(SPEC)
        self.assertEqual(sorted(ops), ["getVerificationStatus", "submitBasicKycSync", "v3PhoneVerification"])
        # Header names are matched without case and excluded headers are left out.
        self.assertEqual(ops["submitBasicKycSync"],
                         {"path": "POST /v3/basic_kyc/sync", "headers": ["Response-Signature", "SmileID-Job-ID"]})
        self.assertEqual(ops["getVerificationStatus"]["headers"], [])

    def test_an_operation_without_an_id_is_keyed_by_its_path(self):
        ops = c.spec_operations({"paths": {"/v3/x": {"get": {}}}})
        self.assertEqual(ops, {"GET /v3/x": {"path": "GET /v3/x", "headers": []}})

    def test_empty_spec(self):
        self.assertEqual(c.spec_operations(None), {})
        self.assertEqual(c.spec_operations({"paths": {"/v3/x": "nonsense"}}), {})


class CheckTest(unittest.TestCase):
    def test_a_matching_manifest_passes(self):
        self.assertEqual(c.check(manifest(), SPEC), [])
        self.assertEqual(c.check(manifest(), SPEC, "v3.1215.0"), [])
        self.assertEqual(c.summary(manifest()), "2 operations supported, 1 unsupported: v3PhoneVerification")

    def test_an_operation_missing_from_the_manifest(self):
        m = manifest()
        del m["operations"]["v3PhoneVerification"]
        self.assertEqual(c.check(m, SPEC), [
            "`v3PhoneVerification` (POST /v3/phone) is in the spec but not in spec-coverage.json. Add the SDK "
            "method that calls it, or mark it unsupported with a reason."])

    def test_an_operation_the_spec_does_not_have(self):
        m = manifest()
        m["operations"]["internal"] = {"unsupported": "Internal."}
        self.assertEqual(c.check(m, SPEC),
                         ["`internal` is in spec-coverage.json but is not a public operation in the spec. Remove it."])

    def test_a_missing_header(self):
        m = manifest()
        del m["operations"]["submitBasicKycSync"]["response_headers"]["Response-Signature"]
        (problem,) = c.check(m, SPEC)
        self.assertIn("the spec returns the `Response-Signature` header, but spec-coverage.json doesn't say where",
                      problem)

    def test_headers_match_without_case(self):
        m = manifest()
        headers = m["operations"]["submitBasicKycSync"]["response_headers"]
        headers["smileid-job-id"] = headers.pop("SmileID-Job-ID")
        self.assertEqual(c.check(m, SPEC), [])

    def test_a_header_the_spec_does_not_return(self):
        m = manifest()
        m["operations"]["submitBasicKycSync"]["response_headers"]["X-Internal-Trace"] = "result.trace"
        self.assertEqual(c.check(m, SPEC),
                         ["`submitBasicKycSync`: the spec doesn't return a `X-Internal-Trace` header. Remove it."])

    def test_a_header_needs_a_field_or_a_reason(self):
        for value in ("", " ", {"unsupported": ""}, {"unsupported": "x", "extra": 1}, None):
            m = manifest()
            m["operations"]["submitBasicKycSync"]["response_headers"]["Response-Signature"] = value
            (problem,) = c.check(m, SPEC)
            self.assertIn("the `Response-Signature` header needs the field that exposes it", problem)

    def test_a_supported_operation_needs_a_method_and_the_spec_path(self):
        m = manifest()
        m["operations"]["getVerificationStatus"] = {"method": " ", "path": "GET /v3/status/{job_id}"}
        self.assertEqual(c.check(m, SPEC), [
            "`getVerificationStatus`: `method` must name the SDK method that calls it.",
            "`getVerificationStatus`: `path` must be `GET /v3/status/{jobId}`, as in the spec."])

    def test_an_entry_is_either_supported_or_unsupported(self):
        for entry in ({"unsupported": ""}, {"unsupported": "x", "method": "m"}, "phone.verify", None):
            m = manifest()
            m["operations"]["v3PhoneVerification"] = entry
            (problem,) = c.check(m, SPEC)
            self.assertIn("give either `method` with `path` and `response_headers`, or only `unsupported`", problem)

    def test_response_headers_must_be_an_object(self):
        m = manifest()
        m["operations"]["submitBasicKycSync"]["response_headers"] = ["SmileID-Job-ID"]
        self.assertEqual(c.check(m, SPEC),
                         ["`submitBasicKycSync`: `response_headers` must be an object keyed by header name."])

    def test_spec_tag(self):
        self.assertIn("is for the spec at `v3.1215.0`, not `v3.1300.0`", c.check(manifest(), SPEC, "v3.1300.0")[0])
        for tag in (None, "main", "3.1.0"):
            self.assertIn("needs a `spec_tag`", c.check(manifest(spec_tag=tag), SPEC)[0])

    def test_shape(self):
        self.assertEqual(c.check([], SPEC), ["spec-coverage.json must be a JSON object."])
        self.assertEqual(c.check(manifest(operations=[]), SPEC),
                         ["spec-coverage.json needs an `operations` object keyed by operationId."])
        self.assertEqual(c.summary([]), "")


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.spec = self.tmp / "openapi.json"
        self.spec.write_text(json.dumps(SPEC))
        self.manifest = self.tmp / "spec-coverage.json"

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = c.main(["--manifest", str(self.manifest), "--spec", str(self.spec), *args])
        return code, out.getvalue()

    def test_passes(self):
        self.manifest.write_text(json.dumps(manifest()))
        code, out = self.run_main("--spec-tag", "v3.1215.0")
        self.assertEqual(code, 0)
        self.assertIn("matches the spec at v3.1215.0: 2 operations supported, 1 unsupported", out)

    def test_fails_with_the_problems(self):
        m = manifest()
        del m["operations"]["v3PhoneVerification"]
        self.manifest.write_text(json.dumps(m))
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("doesn't match the spec (1 problem(s)):\n- `v3PhoneVerification`", out)

    def test_bad_files(self):
        with self.assertRaisesRegex(SystemExit, "The coverage manifest not found"):
            self.run_main()
        self.manifest.write_text("{")
        with self.assertRaisesRegex(SystemExit, "The coverage manifest is not valid JSON"):
            self.run_main()


if __name__ == "__main__":
    unittest.main()
