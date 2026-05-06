import httpx
import openai

from librarian.llm.openai_compatible import is_retriable_openai_error


def test_openai_retry_classification() -> None:
    request = httpx.Request("POST", "https://api.example.test")
    assert is_retriable_openai_error(openai.APITimeoutError(request))
    assert is_retriable_openai_error(openai.APIConnectionError(request=request))
    assert is_retriable_openai_error(
        openai.RateLimitError(
            "rate limited",
            response=httpx.Response(429, request=request),
            body=None,
        )
    )
    assert is_retriable_openai_error(
        openai.APIStatusError(
            "server failed",
            response=httpx.Response(503, request=request),
            body=None,
        )
    )

    assert not is_retriable_openai_error(
        openai.BadRequestError(
            "bad request",
            response=httpx.Response(400, request=request),
            body=None,
        )
    )
    assert not is_retriable_openai_error(
        openai.AuthenticationError(
            "bad auth",
            response=httpx.Response(401, request=request),
            body=None,
        )
    )
