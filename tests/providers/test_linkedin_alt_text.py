"""A LinkedIn image post carries the image's alt text."""

from unittest.mock import MagicMock

import pytest

from providers.linkedin_company import LinkedInCompanyProvider
from providers.types import PostType, PublishContent


def _publish(alt_texts):
    provider = LinkedInCompanyProvider({"client_id": "id", "client_secret": "secret"})
    init = MagicMock(json=MagicMock(return_value={"value": {"uploadUrl": "https://up", "image": "urn:li:image:1"}}))
    post = MagicMock(headers={"x-restli-id": "urn:li:share:1"})
    provider._request = MagicMock(side_effect=[init, post])
    provider._upload_binary = MagicMock()
    provider.publish_post(
        "token",
        PublishContent(
            text="Hallo",
            media_urls=["https://cdn.example/pic.png"],
            media_alt_texts=alt_texts,
            post_type=PostType.IMAGE,
            extra={"author": "urn:li:organization:1"},
        ),
    )
    return provider._request.call_args_list[1].kwargs["json"]["content"]["media"]


@pytest.mark.parametrize(
    ("alt_texts", "expected"),
    [
        (
            ["Eine Checkliste mit fünf Unterlagen"],
            {"id": "urn:li:image:1", "altText": "Eine Checkliste mit fünf Unterlagen"},
        ),
        ([""], {"id": "urn:li:image:1"}),
        ([], {"id": "urn:li:image:1"}),
    ],
)
def test_image_post_sends_alt_text_only_when_there_is_one(alt_texts, expected):
    assert _publish(alt_texts) == expected
