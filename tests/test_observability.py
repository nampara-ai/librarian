import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest

from librarian.observability import (
    JsonFormatter,
    MetricsRecorder,
    RedactingFormatter,
    configure_tracing,
    parse_otel_headers,
    redact_secrets,
    sanitize_error_message,
)


def test_metrics_recorder_is_thread_safe_for_concurrent_updates() -> None:
    metrics = MetricsRecorder()

    def record_batch() -> None:
        for _ in range(100):
            metrics.record(status_code=200, duration_ms=2.0)
            metrics.record_run_stage(stage="clean", duration_ms=3.0)
            metrics.record_run_finished(status="succeeded")
            metrics.record_queue_claim(wait_ms=4.0)
            metrics.record_queue_failure()
            metrics.record_conversion_failure(
                failure_type="extraction_failed",
                source_extension=".pdf",
            )
            metrics.record_ocr_page(
                source="pdf",
                status="succeeded",
                duration_ms=10.0,
                corrected=True,
            )
            metrics.record_llm_usage(
                provider="provider",
                model="model",
                prompt_tokens=5,
                completion_tokens=7,
                total_tokens=12,
                estimated_cost_usd=0.003,
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(record_batch) for _ in range(8)]
        for future in futures:
            future.result()

    snapshot = metrics.snapshot()
    assert snapshot["requests_total"] == 800
    assert snapshot["status_counts"] == {"200": 800}
    assert snapshot["run_stage_counts"] == {"clean": 800}
    assert snapshot["runs_completed_total"] == 800
    assert snapshot["queue_claims_total"] == 800
    assert snapshot["queue_failures_total"] == 800
    assert snapshot["conversion_failures_total"] == 800
    assert snapshot["conversion_failures_by_type"] == {"extraction_failed:.pdf": 800}
    assert snapshot["ocr_pages_total"] == 800
    assert snapshot["ocr_corrected_pages_total"] == 800
    assert snapshot["ocr_page_duration_ms_total"] == 8_000
    assert snapshot["ocr_pages_per_second"] == 100
    assert snapshot["ocr_pages_by_status"] == {"pdf:succeeded": 800}
    assert snapshot["llm_prompt_tokens_total"] == 4_000
    assert snapshot["llm_completion_tokens_total"] == 5_600
    assert snapshot["llm_tokens_total"] == 9_600
    total_cost = snapshot["llm_estimated_cost_usd_total"]
    assert isinstance(total_cost, int | float)
    assert abs(total_cost - 2.4) < 1e-9
    assert snapshot["llm_tokens_by_model"] == {"provider:model": 9_600}
    costs = snapshot["llm_estimated_cost_usd_by_model"]
    assert isinstance(costs, dict)
    costs = cast(dict[str, object], costs)
    model_cost = costs["provider:model"]
    assert isinstance(model_cost, int | float)
    assert abs(model_cost - 2.4) < 1e-9
    assert "librarian_requests_total 800" in metrics.prometheus_text()
    assert 'librarian_llm_tokens_by_model_total{model="model",provider="provider"} 9600' in (
        metrics.prometheus_text()
    )
    assert (
        'librarian_llm_estimated_cost_by_model_usd_total{model="model",provider="provider"}'
        in metrics.prometheus_text()
    )
    assert (
        'librarian_conversion_failures_by_type_total{failure_type="extraction_failed",'
        'source_extension=".pdf"} 800'
    ) in metrics.prometheus_text()
    assert (
        'librarian_ocr_pages_by_status_total{source="pdf",status="succeeded"} 800'
        in metrics.prometheus_text()
    )


def test_json_formatter_redacts_common_secret_patterns() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="api_key=abc123 Authorization: Bearer token123 sk-testSECRET123",
        args=(),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["message"] == (
        "api_key=[REDACTED] Authorization: Bearer [REDACTED] [REDACTED]"
    )
    assert "abc123" not in payload["message"]
    assert "token123" not in payload["message"]
    assert "sk-testSECRET123" not in payload["message"]


def test_plain_formatter_redacts_common_secret_patterns() -> None:
    formatter = RedactingFormatter("%(levelname)s %(name)s %(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token=abc123 Authorization: Bearer token123 sk-testSECRET123",
        args=(),
        exc_info=None,
    )

    message = formatter.format(record)

    assert "token=[REDACTED]" in message
    assert "Authorization: Bearer [REDACTED]" in message
    assert "abc123" not in message
    assert "token123" not in message
    assert "sk-testSECRET123" not in message


def test_redact_secrets_handles_multiple_assignment_names() -> None:
    assert redact_secrets("token=one secret=two password=three") == (
        "token=[REDACTED] secret=[REDACTED] password=[REDACTED]"
    )


def test_redact_secrets_handles_colon_separated_secret_names() -> None:
    assert redact_secrets(
        "provider failed api_key: abc123 token:tok secret : hidden password : pass"
    ) == (
        "provider failed api_key: [REDACTED] token:[REDACTED] "
        "secret : [REDACTED] password : [REDACTED]"
    )


def test_redact_secrets_handles_api_key_header_name() -> None:
    message = redact_secrets("request failed x-api-key: abc123")

    assert message == "request failed x-api-key: [REDACTED]"
    assert "abc123" not in message


def test_redact_secrets_handles_json_secret_fields() -> None:
    message = redact_secrets(
        """provider returned {"api_key":"abc123","token": "tok","safe":"ok"}"""
    )

    assert message == (
        """provider returned {"api_key":"[REDACTED]","token": "[REDACTED]","safe":"ok"}"""
    )
    assert "abc123" not in message
    assert '"tok"' not in message


def test_redact_secrets_handles_single_quoted_secret_fields() -> None:
    message = redact_secrets("provider returned {'secret': 'hidden', 'password':'pass'}")

    assert message == "provider returned {'secret': '[REDACTED]', 'password':'[REDACTED]'}"
    assert "hidden" not in message
    assert "'pass'" not in message


def test_redact_secrets_masks_url_credentials() -> None:
    message = redact_secrets("connect postgres://admin:hunter2@db.internal:5432/lib failed")

    assert "hunter2" not in message
    assert "postgres://admin:[REDACTED]@db.internal:5432/lib" in message


def test_redact_secrets_masks_url_password_without_username() -> None:
    # DSNs commonly omit the username: redis://:password@host.
    message = redact_secrets("connect redis://:s3cr3tpass@localhost:6379/0")

    assert "s3cr3tpass" not in message
    assert "redis://:[REDACTED]@localhost:6379/0" in message


def test_redact_secrets_leaves_credential_free_urls_untouched() -> None:
    assert redact_secrets("GET https://example.com/api/v1") == "GET https://example.com/api/v1"
    # A colon in the path (not credentials) must not trigger redaction.
    assert redact_secrets("see http://example.com/a:b") == "see http://example.com/a:b"


def test_redact_secrets_masks_bearer_tokens_without_header_name() -> None:
    message = redact_secrets("retry with Bearer abcDEF1234567890tokenvalue")

    assert "abcDEF1234567890tokenvalue" not in message
    assert "Bearer [REDACTED]" in message


def test_redact_secrets_masks_provider_specific_tokens() -> None:
    secrets = [
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "xoxb-1234567890-ABCDEFghij",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA1234567890abcdefghijklmnopqrstuvw",
    ]
    for secret in secrets:
        message = redact_secrets(f"leaked {secret} in logs")
        assert secret not in message, secret
        assert "[REDACTED]" in message


def test_sanitize_error_message_redacts_and_truncates() -> None:
    message = sanitize_error_message(
        "provider failed api_key=abc123 " + ("private transcript text " * 20),
        max_chars=80,
    )

    assert "api_key=[REDACTED]" in message
    assert "abc123" not in message
    assert message.endswith("...[truncated]")
    assert len(message) <= 94


def test_parse_otel_headers() -> None:
    assert parse_otel_headers("authorization=Bearer token, x-scope = docs") == {
        "authorization": "Bearer token",
        "x-scope": "docs",
    }
    with pytest.raises(ValueError, match="key=value"):
        parse_otel_headers("authorization")


def test_configure_tracing_disabled_without_optional_dependency() -> None:
    assert (
        configure_tracing(
            enabled=False,
            service_name="librarian-test",
            endpoint="http://collector.test/v1/traces",
        )
        is None
    )
