"""Tests for src/common_lib/azure/blob_tags.py"""
from unittest.mock import MagicMock, patch

import pytest

from common_lib.azure.blob_tags import get_blob_tag, get_blob_tags, set_blob_tags


def make_blob_client(tags: dict | None = None):
    client = MagicMock()
    if tags is not None:
        client.get_blob_tags.return_value = tags
    return client


class TestSetBlobTags:
    def test_returns_true_on_success(self):
        blob = make_blob_client()
        assert set_blob_tags(blob, {"env": "prod"}) is True

    def test_converts_values_to_str(self):
        blob = make_blob_client()
        set_blob_tags(blob, {"count": 42, "flag": True})
        called_tags = blob.set_blob_tags.call_args[0][0]
        assert called_tags == {"count": "42", "flag": "True"}

    def test_returns_false_on_exception(self):
        blob = MagicMock()
        blob.set_blob_tags.side_effect = Exception("SDK error")
        assert set_blob_tags(blob, {"k": "v"}) is False

    def test_empty_tags(self):
        blob = make_blob_client()
        assert set_blob_tags(blob, {}) is True
        blob.set_blob_tags.assert_called_once_with({})


class TestGetBlobTags:
    def test_returns_tags(self):
        blob = make_blob_client(tags={"env": "dev", "ver": "1"})
        assert get_blob_tags(blob) == {"env": "dev", "ver": "1"}

    def test_returns_empty_dict_on_exception(self):
        blob = MagicMock()
        blob.get_blob_tags.side_effect = Exception("SDK error")
        assert get_blob_tags(blob) == {}

    def test_returns_empty_dict_when_no_tags(self):
        blob = make_blob_client(tags={})
        assert get_blob_tags(blob) == {}


class TestGetBlobTag:
    def test_returns_tag_value(self):
        blob = make_blob_client(tags={"env": "prod"})
        assert get_blob_tag(blob, "env") == "prod"

    def test_returns_none_for_missing_key(self):
        blob = make_blob_client(tags={"env": "prod"})
        assert get_blob_tag(blob, "missing") is None

    def test_returns_none_on_exception(self):
        blob = MagicMock()
        blob.get_blob_tags.side_effect = Exception("SDK error")
        assert get_blob_tag(blob, "env") is None
