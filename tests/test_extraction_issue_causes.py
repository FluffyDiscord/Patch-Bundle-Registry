import pytest

from scripts.refresh_issues import extraction_cause


def test_missing_bundle_class_is_reported_once_across_runtime_retries():
    attempt = {
        "status": "runtime_missing_dependency",
        "cause": (
            "IllegalStateException <- NoClassDefFoundError <- ClassNotFoundException "
            "app.template.patches.shared.Constants"
        ),
    }
    assert extraction_cause([attempt, attempt]) == (
        "Missing class app.template.patches.shared.Constants"
    )


def test_unsupported_archives_explain_why_runtime_retries_cannot_help():
    assert extraction_cause([{"cause": "No JVM patch classes"}]) == (
        "No JVM patch classes; archive contains DEX or metadata only"
    )


@pytest.mark.parametrize("cause", [
    "https://user:secret@test/path?token=secret",
    "Authorization: Bearer secret",
    "ClassNotFoundException app.test.Missing https://test/?token=secret",
    None,
    {},
])
def test_worker_text_and_credentials_are_not_exposed(cause):
    assert extraction_cause([{"status": "runtime_missing_dependency", "cause": cause}]) == ""


def test_native_protocol_diagnoses_are_reported():
    assert extraction_cause([{"cause": "engine_too_old"}]) == "engine_too_old"
